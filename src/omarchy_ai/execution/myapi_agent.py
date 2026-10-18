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

from ..core import retry
from .actions import ActionResult

log = logging.getLogger(__name__)

MAX_STEPS = 6
MAX_OPTIONS = 250
PICK_P = 0.5
ARGS_P = 0.7
_CACHE_SECONDS = 15 * 60
# A read step that fails transiently (MyApi or the service unreachable, 429,
# 5xx, a timeout) is tried again after ~1 s, then ~2 s (core/retry.py).
READ_RETRY = retry.Policy(attempts=3, base=1.0, factor=2.0)
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


def _call(service: str, method: str, args: dict, origin: str = "") -> ActionResult:
    """One step. With origin (a prepared action the user approved) it is a
    write: journaled and sent at most once for that origin (core/effects.py)."""
    if service == "gmail" and method in ("GMAIL_FETCH_EMAILS", "GMAIL_LIST_THREADS"):
        # Finding mail goes through the search that relaxes human words and keeps
        # Gmail's filters (live 2026-09-30: "Chris Watson flooring quote" as a
        # literal thread query found nothing, and the agent gave up). With no
        # query it is the newest inbox mail: GMAIL_LIST_THREADS without one came
        # back as a single empty message (live 2026-10-17).
        from .actions import _gmail_search_with_fallback
        from .. import myapi
        # from:Chris Watson -> from:"Chris Watson" (unquoted, Gmail reads the surname as a loose word).
        query = re.sub(r'\b(from|to|cc):([A-Z][\w.\'-]*) ([A-Z][\w.\'-]*)\b', r'\1:"\2 \3"', str(args.get("query") or "in:inbox"))
        try:
            # Five whole messages: each one comes with its payload, and ten took
            # 28 s through MyApi (live 2026-09-30); the newest few are what she needs.
            maximum = int(args.get("max_results") or 5)
            result, attempted = _gmail_search_with_fallback(query, max(1, min(maximum, 5)))
        except (myapi.MyApiError, ValueError) as exc:
            return ActionResult(False, f"Gmail search failed: {exc}")
        return ActionResult(True, json.dumps({"attempted": attempted, **result}, ensure_ascii=False))
    if origin:
        from ..core import effects
        from .actions.myapi_tools import myapi_write_execute
        with effects.origin(origin):
            return myapi_write_execute(service, method, args)
    return _read(service, method, args)


def _read(service: str, method: str, args: dict) -> ActionResult:
    """A read step, retried by core/retry.py's policy when the failure is
    transient. The myapi tool's cancel token (its deadline, the user's stop)
    ends the retries."""
    from .. import myapi
    from .actions.myapi_tools import myapi_execute_request
    try:
        result = retry.run(lambda: myapi_execute_request(service, method, args), effect_class=retry.READ,
                           policy=READ_RETRY, name=f"myapi {method}")
    except myapi.MyApiError as exc:
        return ActionResult(False, f"MyApi action failed: {exc}")
    return ActionResult(True, json.dumps(result))


# -- the steps -----------------------------------------------------------------

def run(args: dict) -> ActionResult:
    from .. import myapi
    from ..core import identities
    if refusal := identities.myapi_refusal():
        return ActionResult(False, refusal)
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
    from ..core import approvals, conversations, pending_actions
    title, fields = _fields(method["name"], args, observations)
    aid = pending_actions.create({"service": service, "method": method["name"], "arguments": args,
                                  "title": title, "fields": fields, "request": request, "conversation": cid,
                                  "tainted": approvals.current_session().tainted})
    if cid:
        expires = pending_actions.expires_at(pending_actions.get(aid) or {})
        until = time.strftime("%H:%M on %Y-%m-%d", time.localtime(expires))
        conversations.add_card(cid, {"kind": "confirm", "task_id": f"{ACTION}{aid}",
                                     "text": f"{title} (waits for your OK until {until})", "expires_at": expires,
                                     "action": {"id": aid, "title": title, "service": service,
                                                "method": method["name"], "fields": fields}})
    shown = "\n".join(f"{f['label']}: {f['value']}" for f in fields)
    return ActionResult(True, json.dumps({"prepared": title, "fields": fields, "note": (
        f"Prepared, NOT done yet: {title} waits for the user's OK (a card with Send is in the chat). Tell the user "
        "what it will do in a sentence or two -- for an email, who it goes to and the gist of the body -- and ask "
        "whether to send it. A yes answers it with task_respond(approve=true); a change means calling myapi again "
        f"with the corrected request.\n{shown}")}, ensure_ascii=False))


ACTION = "action:"


def hold(tool: str, tool_args: dict, service: str, operation: str, arguments: dict) -> ActionResult:
    """A direct send tool's call (myapi_gmail_send, myapi_gmail_reply,
    myapi_execute, myapi_write) with no approval yet: it becomes a confirm
    card showing exactly what would go out, and nothing is sent
    (core/approvals.py). Gmail and /execute sends run through _send (so email
    is checked in Sent); a REST write runs its tool again with the approval."""
    from ..core import approvals, conversations, pending_actions
    cid = conversations.CURRENT.get()
    session = approvals.current_session()
    if tool == "myapi_write":
        title = str(tool_args.get("description") or f"{operation} on {service}")[:120]
        fields = [{"label": label, "value": value[:4000]} for label, value in (
            ("Service", service), ("Request", operation),
            ("Body", json.dumps(arguments.get("body"), ensure_ascii=False) if arguments.get("body") else ""),
            ("Query", json.dumps(arguments.get("query"), ensure_ascii=False) if arguments.get("query") else ""))
            if value]
        action = {"service": service, "method": "myapi_write", "arguments": dict(tool_args), "tool": "myapi_write"}
    else:
        title, fields = _fields(operation, arguments, [])
        action = {"service": service, "method": operation, "arguments": arguments}
    aid = pending_actions.create({**action, "title": title, "fields": fields, "conversation": cid or "",
                                  "request": str(tool_args.get("description") or ""), "tainted": session.tainted})
    if cid:
        expires = pending_actions.expires_at(pending_actions.get(aid) or {})
        until = time.strftime("%H:%M on %Y-%m-%d", time.localtime(expires))
        conversations.add_card(cid, {"kind": "confirm", "task_id": f"{ACTION}{aid}",
                                     "text": f"{title} (waits for your OK until {until})", "expires_at": expires,
                                     "action": {"id": aid, "title": title, "service": service,
                                                "method": action["method"], "fields": fields}})
    shown = "\n".join(f"{f['label']}: {f['value']}" for f in fields)
    return ActionResult(True, json.dumps({"prepared": title, "task_id": f"{ACTION}{aid}", "fields": fields, "note": (
        f"Prepared, NOT sent: {title} waits for the user's OK"
        + (" (a card with Send is in the chat)" if cid else "") + ". Tell the user who it goes to and the gist "
        "of what it says, and ask whether to send it. Their yes for this exact message is "
        f"task_respond(task_id='{ACTION}{aid}', approve=true); a change means calling the tool again with the "
        f"corrected details. Never say it was sent before the result says so.\n{shown}")}, ensure_ascii=False),
        harness=True)


def ask_tap(name: str, args: dict, why: str, sources: list[str]) -> ActionResult:
    """A tool call that only a human tap may approve (core/approvals.py): held
    back by the taint check (execution/provenance.py; `sources` names the
    content), a destructive local action, or a yes that does not count. It
    waits as a confirm card in its conversation, like a prepared MyApi
    action, and only a tap on Send runs it -- a spoken yes is relayed by a
    model, so it does not count here (myapi_agent.decide refuses it)."""
    from ..core import conversations, pending_actions
    cid = conversations.CURRENT.get()
    title = f"Run {name.replace('_', ' ')}"
    fields = [{"label": k.replace("_", " ").capitalize(),
               "value": (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))[:4000]}
              for k, v in (args or {}).items() if v not in (None, "")]
    came = ", ".join(sources) or "content"
    if not sources:
        # Not the taint check: a destructive action, or a yes that does not count.
        if cid:
            aid = pending_actions.create({"service": "tool", "method": name, "arguments": args, "tool": name,
                                          "title": title, "fields": fields, "conversation": cid, "tap_only": True,
                                          "reason": why[:300]})
            expires = pending_actions.expires_at(pending_actions.get(aid) or {})
            conversations.add_card(cid, {"kind": "confirm", "task_id": f"{ACTION}{aid}",
                                         "text": f"{title}? {why}", "expires_at": expires,
                                         "action": {"id": aid, "title": title, "service": "tool", "method": name,
                                                    "fields": fields}})
            how = "A card with Send is in the chat: only the user's tap on it runs this; a spoken yes does not count."
        else:
            how = "There is no chat to put its card in, so it cannot run from here; the user can do it themselves."
        return ActionResult(False, (
            f"NOT run: {name} needs the user's tap ({why}). {how} Tell the user in one sentence what it would do "
            "and why it waits for their tap. Do not retry it another way."), harness=True)
    if cid:
        aid = pending_actions.create({"service": "tool", "method": name, "arguments": args, "tool": name,
                                      "title": title, "fields": fields, "conversation": cid, "tap_only": True,
                                      "reason": why[:300]})
        expires = pending_actions.expires_at(pending_actions.get(aid) or {})
        conversations.add_card(cid, {"kind": "confirm", "task_id": f"{ACTION}{aid}",
                                     "text": f"{title}? It came after content from {came}: {why}",
                                     "expires_at": expires,
                                     "action": {"id": aid, "title": title, "service": "tool", "method": name,
                                                "fields": fields}})
        how = ("A card with Send is in the chat: only the user's tap on it runs this; a spoken yes does not "
               "count, and task_respond cannot approve it.")
    else:
        how = ("Only the user can supply it: ask them to say the exact target themselves; the identical call "
               "runs once they have said it.")
    return ActionResult(False, (
        f"NOT run: {name} came after untrusted content in this conversation ({came}), and {why}. {how} Tell "
        "the user in one or two sentences what it would do and where that target came from. Do not retry it "
        "another way."), harness=True)


def _by(how: str, by: str | None) -> str:
    """tap (a button on the card) or voice (a yes relayed by a model, typed in chat included)."""
    from ..core import approvals
    return by or (approvals.TAP_BY if "phone" in how or "desktop" in how else approvals.VOICE_BY)


def _refusal(action: dict, by: str) -> str | None:
    """Why `by` cannot send this prepared action (core/approvals.py), or None."""
    from ..core import approvals
    verdict = approvals.Verdict(approvals.TAP if action.get("tap_only") else approvals.VOICE,
                                str(action.get("reason") or "it came after untrusted content"))
    why = approvals.accepts(verdict, by, risk=approvals.ELEVATED,
                            tainted=approvals.current_session().tainted or bool(action.get("tainted")),
                            asked_at=float(action.get("created") or 0) or None)
    if why is None:
        return None
    return (f"Not sent: a spoken yes does not approve this ({why}). Only the user's tap on Send on its card in the "
            "chat sends it. Tell the user that in one sentence.")


def _grant(action: dict, by: str) -> None:
    """The approval record the send itself checks for (core/approvals.py)."""
    from ..core import approvals
    if not action.get("tool"):
        approvals.grant(approvals.send_key(str(action.get("service") or ""), str(action.get("method") or ""),
                                           action.get("arguments") or {}), by)


def decide(aid: str, go: bool, how: str, by: str | None = None) -> tuple[bool, str]:
    """The user's Send (go) or Cancel for a prepared action. Send runs it and
    checks it worked; email must show up in Sent. `by` (tap or voice;
    from `how` when not given) must be allowed to approve it."""
    from ..core import pending_actions
    from ..core.crashpoints import crashpoint
    by = _by(how, by)
    if go:
        current = pending_actions.get(aid)
        if current and current.get("status") == pending_actions.WAITING and (why := _refusal(current, by)):
            return False, why
    action = pending_actions.claim(aid)
    if action is None:
        current = pending_actions.get(aid)
        if not current:
            return False, "No such action."
        if current.get("status") == pending_actions.UNCERTAIN:
            if not go:
                return resolve(aid, "drop", how)
            # A Send meant for the first try must never become a second send.
            return False, ("It may already have been sent. To send it again on purpose, choose Resend on its card "
                           "(or answer it with resend); Check looks for it first.")
        if pending_actions.expired(current):
            return False, "That expired before it was answered, so it was not sent."
        return False, f"That was already {current.get('outcome') or 'decided'}."
    crashpoint("pending.after_claim")
    if not go:
        pending_actions.finish(aid, "cancelled", how)
        return True, "Cancelled."
    return _send(action, by)


def _send(action: dict, by: str = "tap") -> tuple[bool, str]:
    """Run a claimed action under its origin (the action itself, or a Resend's
    new one): the same approved send goes out once, however often its Send is
    pressed or retried."""
    from ..core import conversations, effects, pending_actions
    aid, cid = action["id"], action.get("conversation") or ""
    if action.get("tool"):
        return _run_tool(action, by)
    _grant(action, by)
    result = _call(action["service"], action["method"], action["arguments"],
                   origin=pending_actions.origin_of(action))
    effect = None if result.ok else effects.lookup(pending_actions.effect_key(action))
    if effect is not None and effect.state == effects.UNCERTAIN:
        # No answer came back, so it may have gone out: the user decides, never a retry.
        pending_actions.mark_uncertain(aid, f"I may have sent this ({action['title']}): no answer came back from "
                                            "MyApi. Check whether it went out, then resend or drop it.")
        current = pending_actions.get(aid) or action
        if cid:
            conversations.add_card(cid, pending_actions.uncertain_card(current))
        return False, str(current.get("message") or "")
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


def _run_tool(action: dict, by: str = "tap") -> tuple[bool, str]:
    """A tool call the user approved on its card (ask_tap, or a REST write
    from hold), once, under the action's origin (core/effects.py), with the
    approval of exactly these arguments (core/approvals.py)."""
    from ..core import approvals, conversations, effects, pending_actions
    from . import provenance, runner
    aid, cid = action["id"], action.get("conversation") or ""
    tool, arguments = action["tool"], dict(action.get("arguments") or {})
    with effects.origin(pending_actions.origin_of(action)), approvals.approved_call(tool, arguments, by):
        result = runner.default_runner().run(tool, arguments)
    if result.error_class in ("timeout", "cancelled", "worker_lost", "uncertain"):
        pending_actions.mark_uncertain(aid, f"{action['title']} may have run: {result.message[:200]} Check "
                                            "whether it happened, then resend or drop it.")
        current = pending_actions.get(aid) or action
        if cid:
            conversations.add_card(cid, pending_actions.uncertain_card(current))
        return False, str(current.get("message") or "")
    message = (f"{action['title']}: done ✓" if result.ok else
               f"{action['title']} failed: {provenance.clip(result.message, 200)}")
    pending_actions.finish(aid, "done" if result.ok else "failed", message)
    if cid:
        conversations.add_card(cid, {"kind": "result", "task_id": f"{ACTION}{aid}",
                                     "event": "certified" if result.ok else "failed", "text": message})
    return result.ok, message


CHOICES = ("check", "resend", "drop")
_EMAIL = ("GMAIL_SEND_EMAIL", "GMAIL_REPLY_TO_THREAD")


def resolve(aid: str, choice: str, how: str, by: str | None = None) -> tuple[bool, str]:
    """The user's answer to an action that may already have been sent
    (core/pending_actions.py): check for it, resend it on purpose (a new
    origin, so a deliberate second send), or drop it."""
    from ..core import conversations, pending_actions
    action = pending_actions.get(aid)
    if not action:
        return False, "No such action."
    if action.get("status") != pending_actions.UNCERTAIN:
        return False, f"That is not waiting for a check, resend or drop (it is {action.get('outcome') or 'open'})."
    cid = action.get("conversation") or ""

    def settle(outcome: str, event: str, message: str) -> tuple[bool, str]:
        if not pending_actions.settle_uncertain(aid, outcome, message):
            return False, "That was already decided."
        if cid:
            conversations.add_card(cid, {"kind": "result", "task_id": f"{ACTION}{aid}", "event": event,
                                         "text": message})
        return True, message

    if choice == "drop":
        return settle("dropped", "cancelled", f"{action['title']}: dropped {how}. If it did go out, it stays sent.")
    if choice == "check":
        if action.get("method") not in _EMAIL:
            return False, (f"I can't check {action.get('service') or 'that service'} for it from here. Look there, "
                           "then resend or drop it.")
        message = _sent_check(action) or ""
        if not message.startswith("Sent ✓"):
            return False, f"{message} Resend it, or drop it if you can see it went out."
        return settle("sent", "certified", message)
    if choice == "resend":
        by = _by(how, by)
        if why := _refusal({**action, "created": time.time()}, by):
            return False, why
        claimed = pending_actions.claim_resend(aid)
        if claimed is None:
            return False, "That was already decided."
        return _send(claimed, by)
    return False, f"Answer with one of: {', '.join(CHOICES)}."


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
