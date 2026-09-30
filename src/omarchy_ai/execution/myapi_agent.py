"""MyApi, run by Jev: one tool for everything in the user's connected services.

The user, 2026-09-30: "we need to let JEV handle all of myapi endpoints and
services, not claude and not codex, or at least the decisions". Emailing
Chris had gone through a background Codex task three times and never sent.

`run({"request": ...})` works in steps, and every decision is Jev's:
- which connected service the request needs (a choice over the services);
- each next step: one of that service's methods, `done` when the results
  answer the request, or `ask` when only the user can supply something
  (a choice over the methods, which sees the results so far);
- whether the arguments carry out that step with nothing invented;
- after an action runs, whether it worked.
The Gateway text model only writes: argument values (an email's body). No
Claude, no Codex, no start_task.

Reads run at once, and their results come back compact (Gmail as a digest;
anything else ranked by Jev in myapi_refinement). A method that sends,
changes or deletes is not run: it becomes a pending action and a confirm
card in the conversation (core/pending_actions.py). The user's Send, or a
yes in that chat, runs it; email is then checked in Sent before it counts.
"""
from __future__ import annotations

import json
import logging
import re
import time

from .actions import ActionResult

log = logging.getLogger(__name__)

MAX_STEPS = 6
MAX_OPTIONS = 250
PICK_P = 0.5
ARGS_P = 0.7
_CACHE_SECONDS = 15 * 60
_cache: dict[str, tuple[float, object]] = {}

# A method is a read only when its name says so and nothing in it writes;
# anything else is confirmed first. Jev also judges each step (_writes).
_WRITE_WORDS = {"SEND", "CREATE", "DELETE", "UPDATE", "PATCH", "MODIFY", "TRASH", "UNTRASH", "INSERT", "IMPORT",
                "FORWARD", "ADD", "REMOVE", "BATCH", "MOVE", "STOP", "REPLY", "POST", "UPLOAD", "SET", "STAR",
                "ARCHIVE", "CANCEL", "ACCEPT", "DECLINE", "INVITE", "SHARE", "COPY", "RENAME", "WRITE", "PUBLISH",
                "EDIT", "MARK", "APPLY", "MERGE", "CLOSE", "OPEN", "COMMENT", "ASSIGN", "TRANSFER", "PAY", "CHARGE",
                "REFUND", "BLOCK", "FOLLOW", "UNFOLLOW", "LIKE", "RUN", "TRIGGER", "EXECUTE", "DISPATCH", "PUT",
                "EMPTY", "CLEAR", "RESET", "ENABLE", "DISABLE", "REVOKE", "GRANT", "SCHEDULE", "RESCHEDULE",
                "QUICK_ADD", "SUBMIT", "APPROVE", "RESTORE", "DUPLICATE", "WATCH"}
_READ_WORDS = {"GET", "LIST", "FETCH", "SEARCH", "FIND", "WHO", "QUERY", "READ", "DESCRIBE", "CHECK", "COUNT",
               "RETRIEVE", "LOOKUP", "VIEW", "FREE", "BUSY", "CURRENT", "SHOW", "STATUS", "INFO", "DETAILS"}


def is_write(method: str) -> bool:
    words = set((method or "").upper().split("_")[1:]) or {"?"}
    return bool(words & _WRITE_WORDS) or not words & _READ_WORDS


def _cached(key, fetch):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_SECONDS:
        return hit[1]
    value = fetch()
    _cache[key] = (time.monotonic(), value)
    return value


def _client():
    from .. import myapi
    return myapi.MyApiClient()


def services() -> list[dict]:
    def fetch():
        data = _client().list_services()
        rows = data.get("data") if isinstance(data.get("data"), list) else data if isinstance(data, list) else []
        return [{"id": r.get("id") or r.get("name"), "name": r.get("name") or r.get("id"),
                 "category": r.get("category") or ""}
                for r in rows if isinstance(r, dict) and (r.get("status") in (None, "connected"))]
    return _cached("services", fetch)


def methods(service: str) -> list[dict]:
    def fetch():
        data = _client().service_methods(service)
        rows = data.get("data") if isinstance(data, dict) else data
        if isinstance(rows, dict):
            rows = rows.get("methods") or []
        return [{"name": m.get("name"), "description": " ".join(str(m.get("description") or "").split()),
                 "parameters": m.get("parameters") or {}}
                for m in rows or [] if isinstance(m, dict) and m.get("name") and "." not in m["name"]]
    return _cached(f"methods:{service}", fetch)


# -- Jev ---------------------------------------------------------------------

def _jev():
    from ..core.jev import Jev
    return Jev()


def _choose(state: dict, instructions: str, options: dict) -> tuple[str, float]:
    from ..core.jev import choice
    answer = _jev().ask(state, {"pick": choice(instructions, options)}, timeout=8, retries=1)["pick"]
    return answer["choice"], float(answer["p"])


def _yes(state: dict, instructions: str) -> float:
    from ..core.jev import boolean
    return float(_jev().ask(state, {"q": boolean(instructions)}, timeout=8, retries=1)["q"]["p"])


def _shortlist(options: list[dict], request: str, limit: int = MAX_OPTIONS) -> list[dict]:
    if len(options) <= limit:
        return options
    words = set(re.findall(r"[a-z]{3,}", request.lower()))

    def score(m):
        hay = (m["name"].replace("_", " ") + " " + m["description"]).lower()
        return sum(w in hay for w in words)
    return sorted(options, key=score, reverse=True)[:limit]


# -- arguments -----------------------------------------------------------------

_FILL_SYSTEM = (
    "Fill the arguments of one call to the user's connected service. Input: the user's request, the conversation "
    "so far, the results of earlier steps, and the method (name, description, parameters). Use values from the "
    "request, the conversation and the results only; ids (message_id, thread_id, event ids...) only from the "
    "results. Never invent an email address, id or fact. For Gmail use user_id 'me'. A Gmail search finds the "
    "person first: from:\"Full Name\" (quoted) or their address, plus newer_than:90d -- no topic words unless "
    "the person alone would match too much; their newest messages are read next. When the method writes a "
    "message, write the complete text the user asked for, in the language they asked for (English unless they "
    "said otherwise), polite and short, signed with the user's name if the results show it. A reply in an "
    "existing thread goes to that thread (thread_id) with no new subject. Answer as JSON: "
    "{\"arguments\": {...}}.")


def _fill(method: dict, request: str, context: str, observations: list) -> dict:
    from ..config import load_config
    from ..voice.omarchy import GatewayClient, GatewayError
    user = {"request": request, "conversation": context[-2500:], "results_so_far": observations[-4:],
            "method": {"name": method["name"], "description": method["description"][:600],
                       "parameters": method["parameters"]}}
    try:
        answer = GatewayClient(load_config()).complete_json(_FILL_SYSTEM, user, timeout=12)
    except GatewayError:
        # Once more: live 2026-09-30, a TLS handshake to the Gateway hung and the
        # whole request failed after 25 s; a second try normally goes through.
        answer = GatewayClient(load_config()).complete_json(_FILL_SYSTEM, user, timeout=12)
    args = answer.get("arguments")
    if not isinstance(args, dict):
        return {}
    params = method["parameters"] or {}
    return {k: v for k, v in args.items() if not params or k in params}


def _missing(method: dict, args: dict) -> list[str]:
    return [k for k, spec in (method["parameters"] or {}).items()
            if isinstance(spec, dict) and not spec.get("optional") and k not in args]


# -- results -------------------------------------------------------------------

def _observe(service: str, method: str, request: str, raw: str) -> object:
    """A result she can read whole: Gmail as a digest, the rest ranked by Jev."""
    try:
        result = json.loads(raw)
    except ValueError:
        return raw[:3000]
    if method in ("GMAIL_FETCH_EMAILS", "GMAIL_LIST_THREADS", "GMAIL_FETCH_MESSAGE_BY_THREAD_ID",
                  "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID"):
        from .actions import _gmail_digest, _gmail_messages
        messages = _gmail_messages(result) or [(result.get("data") or {}).get("response", {}).get("data") or {}]
        return {"messages": _gmail_digest([m for m in messages if isinstance(m, dict)])}
    try:
        from ..config import load_config
        from ..voice.myapi_refinement import refine_myapi_result
        return json.loads(refine_myapi_result(load_config(), request, raw))
    except Exception:  # noqa: BLE001 -- the raw text, bounded, still answers
        return raw[:6000]


def _last_sender(observations: list, thread_id) -> str:
    """The address of the newest message in that thread that the user did not send."""
    rows = [m for o in observations if isinstance(o.get("result"), dict)
            for m in o["result"].get("messages") or [] if m.get("thread_id") == thread_id]
    for message in sorted(rows, key=lambda m: str(m.get("date") or ""), reverse=True):
        found = re.search(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", str(message.get("from") or ""))
        if found and not message.get("sent"):
            return found.group(0)
    return ""


def _writes(name: str, method: dict, args: dict) -> bool:
    """Jev's second look at a step its name calls a read: whether it would
    send, change or delete anything. Unsure or unavailable means ask first."""
    try:
        return _yes({"method": name, "description": method["description"][:400], "arguments": args},
                    "Would calling `method` with `arguments` send, create, change or delete anything (rather than "
                    "only read)?") >= 0.5
    except Exception:  # noqa: BLE001
        return True


def _call(service: str, method: str, args: dict) -> ActionResult:
    from .actions import _myapi_execute
    if service == "gmail" and method in ("GMAIL_FETCH_EMAILS", "GMAIL_LIST_THREADS") and args.get("query"):
        # Finding mail goes through the search that relaxes human words and keeps
        # Gmail's filters (live 2026-09-30: "Chris Watson flooring quote" as a
        # literal thread query found nothing, and the agent gave up).
        from .actions import _gmail_search_with_fallback
        from .. import myapi
        # from:Chris Watson -> from:"Chris Watson" (unquoted, Gmail reads the surname as a loose word).
        query = re.sub(r'\b(from|to|cc):([A-Z][\w.\'-]*) ([A-Z][\w.\'-]*)\b', r'\1:"\2 \3"', str(args["query"]))
        try:
            # Five whole messages: each one comes with its payload, and ten took
            # 28 s through MyApi (live 2026-09-30); the newest few are what she needs.
            maximum = int(args.get("max_results") or 5)
            result, attempted = _gmail_search_with_fallback(query, max(1, min(maximum, 5)))
        except (myapi.MyApiError, ValueError) as exc:
            return ActionResult(False, f"Gmail search failed: {exc}")
        return ActionResult(True, json.dumps({"attempted": attempted, **result}, ensure_ascii=False))
    return _myapi_execute(service, method, args)


# -- the steps -----------------------------------------------------------------

def run(args: dict) -> ActionResult:
    from .. import myapi
    if not myapi.is_connected():
        return ActionResult(False, "MyApi isn't connected -- connect it from the Omarchy AI settings panel first.")
    request = " ".join(str(args.get("request") or "").split())
    if not request:
        return ActionResult(False, "myapi needs the user's request in `request`.")
    from ..core import conversations
    cid = conversations.CURRENT.get()
    context = conversations.context(cid) if cid else ""
    try:
        return _run(request, context, cid)
    except Exception as exc:  # noqa: BLE001 -- say what failed; never hand it to a worker
        log.exception("myapi agent failed")
        return ActionResult(False, f"MyApi could not do it: {str(exc)[:300]}. Tell the user in one sentence; do not "
                                   "start a task for it.")


def _run(request: str, context: str, cid: str) -> ActionResult:
    rows = services()
    if not rows:
        return ActionResult(False, "No MyApi service is connected.")
    state = {"request": request, "conversation": context[-1200:]}
    options = {r["id"]: f"{r['name']} ({r['category']})"[:160] for r in rows[:MAX_OPTIONS - 1]}
    options["none"] = "None of the connected services does this."
    service, p = _choose(state, "Which one of the user's connected services does `request` need?", options)
    log.info("myapi agent: service %s (p=%.2f) for %r", service, p, request[:120])
    if service == "none" or p < PICK_P:
        return ActionResult(False, "None of the user's connected services does that (Jev). Say so in one sentence.")
    catalog = {m["name"]: m for m in _shortlist(methods(service), request)}
    observations: list[dict] = []
    for step in range(MAX_STEPS):
        options = {name: m["description"][:180] or name for name, m in catalog.items()}
        options["done"] = "The results so far answer the request (or it needs nothing more)."
        options["ask"] = "Only the user can supply what is still missing."
        state = {"request": request, "conversation": context[-800:],
                 "results_so_far": json.dumps(observations[-3:], ensure_ascii=False)[-5000:]}
        pick, p = _choose(state, "What is the next step toward `request`? Pick the method to call now (a search or "
                                 "read first when an id, address or thread is still unknown), `done` when "
                                 "`results_so_far` already answer it, or `ask`.", options)
        log.info("myapi agent: step %d %s (p=%.2f)", step + 1, pick, p)
        if pick == "done":
            break
        if pick == "ask" or p < PICK_P:
            return ActionResult(True, json.dumps({"results": observations, "note": (
                "Jev needs more from the user to go on. Ask them one short question, then call myapi again with "
                "the full request.")}, ensure_ascii=False))
        method = catalog[pick]
        args = _fill(method, request, context, observations)
        if _missing(method, args) or _yes({**state, "step": pick, "arguments": args},
                                          "Do `arguments` carry out `step` for `request` exactly, with every value "
                                          "taken from the request, the conversation or results_so_far (no invented "
                                          "address, id or fact)?") < ARGS_P:
            args = _fill(method, request, context, observations)  # once more, then it is the user's call
            if _missing(method, args):
                return ActionResult(True, json.dumps({"results": observations, "note": (
                    f"Could not fill {', '.join(_missing(method, args))} for {pick}. Ask the user for it.")},
                    ensure_ascii=False))
        if any(o.get("method") == pick and o.get("arguments") == args for o in observations):
            # The same step again learns nothing new (live: three identical searches).
            return ActionResult(True, json.dumps({"results": observations, "note": (
                "Jev could not get further with what it found. Tell the user what was found, and ask for what is "
                "missing (e.g. the person's email address).")}, ensure_ascii=False)[:14000])
        if pick == "GMAIL_REPLY_TO_THREAD" and not args.get("recipient_email"):
            # The schema leaves it optional; a reply goes to whoever wrote last in that thread.
            args["recipient_email"] = _last_sender(observations, args.get("thread_id"))
        if is_write(pick) or _writes(pick, method, args):
            return _prepare(service, method, args, request, observations, cid)
        result = _call(service, pick, args)
        if not result.ok:
            observations.append({"method": pick, "error": result.message[:300]})
            continue
        observations.append({"method": pick, "arguments": args, "result": _observe(service, pick, request,
                                                                                   result.message)})
    return ActionResult(True, json.dumps({"results": observations}, ensure_ascii=False)[:14000])


# -- actions that wait for the user ------------------------------------------------

def _fields(method: str, args: dict, observations: list) -> tuple[str, list[dict]]:
    if method == "GMAIL_SEND_EMAIL":
        fields = [("To", args.get("recipient_email")), ("Cc", ", ".join(args.get("cc") or [])),
                  ("Subject", args.get("subject")), ("Body", args.get("body"))]
        title = "Send email"
    elif method == "GMAIL_REPLY_TO_THREAD":
        subject = next((m.get("subject") for o in observations for m in
                        ((o.get("result") or {}).get("messages") or [] if isinstance(o.get("result"), dict) else [])
                        if m.get("thread_id") == args.get("thread_id")), "")
        fields = [("To", args.get("recipient_email")), ("Thread", subject or args.get("thread_id")),
                  ("Body", args.get("message_body"))]
        title = "Reply in thread"
    else:
        title = method.split("_", 1)[-1].replace("_", " ").capitalize()
        fields = [(k.replace("_", " ").capitalize(), v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
                  for k, v in args.items() if k != "user_id"]
    return title, [{"label": label, "value": str(value)[:4000]} for label, value in fields if value]


def _prepare(service: str, method: dict, args: dict, request: str, observations: list, cid: str) -> ActionResult:
    from ..core import conversations, pending_actions
    title, fields = _fields(method["name"], args, observations)
    aid = pending_actions.create({"service": service, "method": method["name"], "arguments": args,
                                  "title": title, "fields": fields, "request": request, "conversation": cid})
    if cid:
        conversations.add_card(cid, {"kind": "confirm", "task_id": f"{ACTION}{aid}", "text": title,
                                     "action": {"id": aid, "title": title, "service": service,
                                                "method": method["name"], "fields": fields}})
    shown = "\n".join(f"{f['label']}: {f['value']}" for f in fields)
    return ActionResult(True, json.dumps({"prepared": title, "fields": fields, "note": (
        f"Prepared, NOT done yet: {title} waits for the user's OK (a card with Send is in the chat). Tell the user "
        "what it will do in a sentence or two -- for an email, who it goes to and the gist of the body -- and ask "
        "whether to send it. A yes answers it with task_respond(approve=true); a change means calling myapi again "
        f"with the corrected request.\n{shown}")}, ensure_ascii=False))


ACTION = "action:"


def decide(aid: str, go: bool, how: str) -> tuple[bool, str]:
    """The user's Send (go) or Cancel for a prepared action. Send runs it and
    checks it worked; email must show up in Sent."""
    from ..core import conversations, pending_actions
    action = pending_actions.claim(aid)
    if action is None:
        current = pending_actions.get(aid)
        return False, f"That was already {current.get('outcome') or 'decided'}." if current else "No such action."
    cid = action.get("conversation") or ""
    if not go:
        pending_actions.finish(aid, "cancelled", how)
        return True, "Cancelled."
    result = _call(action["service"], action["method"], action["arguments"])
    ok = result.ok and _worked(action, result.message)
    message = (_sent_check(action) if ok and action["method"].startswith("GMAIL_") and
               action["method"] in ("GMAIL_SEND_EMAIL", "GMAIL_REPLY_TO_THREAD") else None)
    if message is None:
        message = f"{action['title']}: done ✓" if ok else f"{action['title']} failed: {result.message[:200]}"
    else:
        ok = message.startswith("Sent ✓")
    pending_actions.finish(aid, ("sent" if "Sent" in message else "done") if ok else "failed", message)
    if cid:
        conversations.add_card(cid, {"kind": "result", "task_id": f"{ACTION}{aid}",
                                     "event": "certified" if ok else "failed", "text": message})
    return ok, message


def _worked(action: dict, raw: str) -> bool:
    try:
        return _yes({"method": action["method"], "response": raw[-3000:]},
                    "Does `response` show that `method` succeeded?") >= 0.5
    except Exception:  # noqa: BLE001 -- the provider said ok; the Sent check still follows for email
        return True


def _sent_check(action: dict) -> str | None:
    """Email counts as sent only when it is in Sent (2026-09-30: "done" three
    times, nothing in the Sent folder)."""
    args = action["arguments"]
    to = str(args.get("recipient_email") or "").strip()
    for _ in range(4):
        time.sleep(2)
        try:
            from .actions import _gmail_execute, _gmail_messages
            found = _gmail_messages(_gmail_execute("GMAIL_FETCH_EMAILS", {
                "query": f"in:sent newer_than:1h to:{to}" if to else "in:sent newer_than:1h", "max_results": 5,
                "include_payload": False}))
        except Exception:  # noqa: BLE001
            continue
        thread = args.get("thread_id")
        if any(not thread or m.get("threadId") == thread for m in found if isinstance(m, dict)):
            return f"Sent ✓ to {to}" if to else "Sent ✓"
    return f"Not in Sent: the email to {to or 'the recipient'} did not show up there, so it may not have gone."
