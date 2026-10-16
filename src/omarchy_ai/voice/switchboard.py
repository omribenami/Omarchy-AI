"""Jev switchboard: the final routing decision for every live-model tool call.

Two passes (docs/ADR-0003-jev-switchboard.md):

1. Fast pass (voice/jev_fast.py). As soon as the user stops talking, one Jev
   call runs a clear simple command at once and classifies the request into
   a route (instant, desktop goal, web, whole task, terminal, files, info,
   conversation). That route is the hint below.
2. Accurate pass (here). Gemini (or the phone's Gemini session) generates the
   payload -- the tool call. Before it runs, Jev decides: execute it, send it
   back because it does not match what the user asked, have the model ask
   the user, or reroute it to a better executor (the Task Runtime for a
   multi-step job, the Jev desktop loop for a native desktop goal).

Jev decides the tool, not only yes or no: every non-read review also offers
a short list of other tools that could carry out the request (a shortlist
code builds from the tool catalog), and gets where the request came from
(core/origin.py: the speaker in a room, the phone, this computer). 2026-10-15
19:58: "make it a bit calmer here", said to the speaker, ran the night light
on this computer's screen (execute p=0.9): Jev saw neither where the user
was nor any other tool.

Code owns everything exact: which reroutes a tool may take, their arguments
(the user's own words, never generated text), the thresholds, and when a
reroute is allowed at all (never in the middle of a chain the model already
started). Jev only answers typed questions. If Jev is unreachable the call
runs as before and the log says so; the assistant never stops working
because the Gateway is down.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import threading
import time

from ..core.jev import Jev, JevError, boolean, choice

log = logging.getLogger("omarchy_ai.voice.switchboard")

MYAPI_WRITES = frozenset({"myapi_write", "myapi_execute", "myapi_gmail_send", "myapi_gmail_reply"})


def _writes(tool: str, args: dict | None) -> bool:
    """A MyApi write now, or schedule_task carrying one for later."""
    from ..execution.verified_input import writes_externally
    return tool in MYAPI_WRITES or writes_externally(tool, args or {})


# No side effects: these may start while Jev is still deciding, and their
# result is dropped if Jev rejects the call.
READ_ONLY = frozenset({
    "task_status", "search_os_knowledge", "get_release_notes", "list_scheduled_tasks", "find_skill",
    "load_skill", "list_skills", "check_assistant_updates", "get_update_status", "list_bar_icons",
    "list_files", "read_file", "read_tile_log", "list_windows", "list_commands", "get_recent_actions",
    "describe_screen", "terminal_read", "battery_status", "list_cast_targets", "list_reminders",
    "inspect_browser",
})



def _reads(tool: str, args: dict | None) -> bool:
    """A core read-only tool, or a lookup operation a user tool declares
    (tool.json "read_operations"). 2026-10-08: after a smart-home user tool
    refused "turn off the living room TV" (two devices with that name), her
    three `list` calls to it were rejected as "the user did not ask for
    this" (matches 0.24-0.27), so she never saw the candidates."""
    if tool in READ_ONLY:
        return True
    operation = (args or {}).get("operation")
    if not isinstance(operation, str):
        return False
    try:
        from ..execution import user_tools
        return operation in user_tools.read_operations(tool)
    except ImportError:
        return False


# Code-owned reroutes: tool -> executors it may be handed to, and why.
REROUTES = {
    "start_task": {"terminal_task", "terminal_type", "type_text", "open_terminal"},
    "desktop_task": {"list_commands", "execute_command"},
}
EXECUTORS = {
    "start_task": "hand the whole request to the Task Runtime: a job with several steps, investigation, "
                  "diagnosis, an install or configuration, or a code change with tests",
    "desktop_task": "hand the request to the Jev desktop loop: a native desktop goal (workspaces, moving "
                    "or focusing windows, volume, brightness, theme, bar panels)",
}

REROUTE_P = 0.9       # a reroute replaces the model's plan: only when clear
# Handing a desktop request to the verified desktop loop is low-risk, but
# only with a confident fast pass that agrees (live Jev, 2026-09-24: "move
# this window to workspace 4" via list_commands -> desktop_task at 0.81).
DESKTOP_REROUTE_P = 0.75
REJECT_P = 0.8
BETTER_P = 0.8        # Jev's other tool replaces the model's: only when clear
SHORTLIST = 8         # other tools offered with each review
USER_TOOLS_OFFERED = 3
REJECT_MATCH = 0.3    # ... and only if the call itself does not look right
HARD_MISMATCH = 0.1   # a call this far from the request never runs
READ_MISMATCH = 0.05  # reads are harmless: reject only obvious nonsense
ASK_P = 0.85
CACHE_SECONDS = 30
TIMEOUT = 1.5         # 651 live reviews to 2026-09-28: p99 0.77s, max 0.91s


@dataclass
class Verdict:
    action: str                       # execute | reject | ask | reroute
    tool: str = ""
    args: dict = field(default_factory=dict)
    message: str = ""
    evidence: dict = field(default_factory=dict)
    ms: float = 0.0


@dataclass
class Context:
    request: str                      # the user's latest words
    earlier: list[str]                # a few previous user turns
    hint: dict | None                 # the fast pass's route for this request
    calls: list[tuple[str, dict]]     # the model's calls since the request
    description: str = ""             # the tool's own description
    failed: dict | None = None        # her last call, when it failed: {"tool", "args", "error"}
    origin: dict | None = None        # where the request came from (core/origin.py), None fields dropped


def _options(tool: str, ctx: Context) -> dict:
    options = {
        "execute": "Run the call as it is: it does what the user asked (or is a sensible step toward it, such as "
                   "listing, reading or focusing first), with the right target and values.",
        "reject": "Do not run it: it does something the user did not ask for, or has the wrong target, number, "
                  "window, text or tool.",
        "ask_user": "Do not run it yet: the request is ambiguous about what to act on, so the user must be asked.",
    }
    started = any(name not in READ_ONLY for name, _ in ctx.calls)
    for executor, tools in REROUTES.items():
        if tool in tools and not started:
            options[executor] = EXECUTORS[executor] + " instead of this call."
    return options


# Said in every question that reads `origin`: what "here" means depends on it.
ORIGIN_NOTE = (
    "`origin` says where the request came from (channel, what the user talks to, room) and whether the user is "
    "at this computer. When at_this_computer is false (the voice speaker in a room, or the phone), 'here', 'this "
    "room', 'the lights', 'the atmosphere' mean the place where the user is, not this computer: a call that only "
    "changes this computer's screen (night light, windows, a desktop notification) does not carry it out unless "
    "they named the computer or its screen.")


def _pool() -> dict[str, dict]:
    """Every tool the model can reach: built-in and the user's own."""
    from ..execution.tools import TOOLS
    pool = {t["name"]: t for t in TOOLS}
    try:
        from ..execution import user_tools
        pool.update({t["name"]: t for t in user_tools.schemas()})
    except Exception:  # noqa: BLE001 -- the shortlist only informs the review
        pass
    return pool


# Executors for a whole kind of job: always worth offering.
ALWAYS_OFFERED = ("start_task", "desktop_task", "browser_task", "run_skill")


def _skills_summary(base: str) -> str:
    """run_skill with the saved procedures by name. 2026-10-15 19:52: "tell
    the kids they have 2 minutes, then lock" went to set_reminder and a
    desktop notification; the saved countdown skill was never among
    Jev's options, since nothing said which skills exist."""
    try:
        from ..core import skills
        listed = skills.index_text(limit=12, width=220)
    except Exception:  # noqa: BLE001 -- the skills only inform the choice
        listed = ""
    return f"{base} Saved skills:\n{listed}"[:1800] if listed.strip() else base


def shortlist(tool: str, args: dict, ctx: Context) -> dict[str, str]:
    """{tool: one-line description}: the called tool, the tools whose words
    match the request (catalog keyword rank over the request, the call's text
    arguments and this turn's use_tool requests), the user's own tools (their
    house, their services: what 'the lights' usually means) and the
    whole-job executors. Code chooses the candidates; Jev chooses among them."""
    from ..execution import catalog
    pool = _pool()
    try:
        from ..execution import user_tools
        added = {t["name"] for t in user_tools.schemas()}
    except Exception:  # noqa: BLE001
        added = set()
    words = " ".join([ctx.request, *ctx.earlier[-1:],
                      *(v for v in (args or {}).values() if isinstance(v, str)),
                      *(str(a.get("request") or "") for _, a in ctx.calls)])
    matched = [t["name"] for t in catalog._keyword_rank(words, pool, matched_only=True)]
    ranked = [t["name"] for t in catalog._keyword_rank(words, pool)]
    names = [tool] if tool in pool else []
    names += [n for n in ranked if n in added][:USER_TOOLS_OFFERED]
    names += [n for n in ALWAYS_OFFERED if n in pool]
    names += matched
    picked = list(dict.fromkeys(names))[:SHORTLIST + 1]
    if tool not in picked:
        picked = [tool, *picked[:SHORTLIST]]
    offered = {n: (catalog._summary(pool[n]) if n in pool else ctx.description[:180] or n) for n in picked}
    if "run_skill" in offered:
        offered["run_skill"] = _skills_summary(offered["run_skill"])
    return offered


def questions(tool: str, ctx: Context, args: dict | None = None, offered: dict | None = None) -> dict:
    writes = _writes(tool, args)
    result = {
        "route": choice(
            "The live model wants to make `call` for the user's latest `request` (earlier turns and the calls it "
            "already made this turn are context). Decide where this goes. " + ORIGIN_NOTE + " Relaying or typing text the user "
            "dictated into a terminal or to a coding agent (Claude Code, Codex) is exactly what they asked: "
            "execute it. A request that is cut off mid-sentence, or the user describing or explaining how "
            "something works, asks for nothing: reject a call that acts on it. The request is speech recognition "
            "output and often garbled: read it with the earlier turns. A short request that only makes sense as "
            "a follow-up (e.g. 'No ssh to it' right after 'So you run it', really 'Now ssh to it') asks for that "
            "follow-up; do not reject on a misheard word. If `failed_just_before` is given, the model's "
            "previous call failed with that error and the user is still waiting for that job: a call that looks "
            "up what is needed to recover, or does the same job on the right target, is a sensible step, even "
            "when the latest request is only a short reply such as 'it is clear enough' or 'the first one'.",
            _options(tool, ctx)),
        "matches": boolean(
            "Does `call` carry out what the user's latest `request` asks, or a sensible step toward it, with the "
            "right target and values (numbers, names, text), in the place the user means (`origin`)? Read the "
            "request as the user meant it given the earlier turns, not its misheard words."),
        "gap": choice(
            "If `call` does not fit the request, what is wrong with it?",
            {"none": "Nothing, it fits.",
             "values": "Wrong number, name, window, path or text.",
             "tool": "The wrong kind of action for this request.",
             "not_asked": "The user did not ask for this.",
             "ambiguous": "The request does not say clearly what to act on."}),
    }
    if offered and len(offered) > 1:
        result["better_tool"] = choice(
            "The user's latest `request` must be carried out where and for whom they meant (see `origin`: they "
            "may not be at this computer, and a request about other people or another room must reach them there). "
            "Which one of these tools does that best? Judge from the request itself, not from how `call` worded it.", offered)
    if writes:
        result["confirmed"] = boolean(
            "Did the user explicitly instruct or approve this exact external change? A direct command such as "
            "'send this email to X' is approval; a later clear yes after a read-back is also approval. Merely "
            "asking to write, compose, prepare, or draft an email is NOT approval to send it. Check recipient, "
            "subject/body, service target, and values against latest request plus earlier user turns. A refusal, "
            "correction not yet incorporated, ambiguity, or unrelated answer is false."
        )
    return result


def state(tool: str, args: dict, ctx: Context) -> dict:
    result = {
        "request": ctx.request[-600:],
        "earlier_user_turns": [t[-200:] for t in ctx.earlier[-3:]],
        "fast_pass_route": ctx.hint,
        **({"origin": ctx.origin} if ctx.origin else {}),
        "calls_this_turn": [{"tool": n, "args": _short(a)} for n, a in ctx.calls[-6:]],
        "call": {"tool": tool, "args": _short(args, 2_000 if _writes(tool, args) else 300),
                 "what_it_does": ctx.description[:240]},
    }
    if ctx.failed:
        result["failed_just_before"] = {"tool": ctx.failed.get("tool"), "args": _short(ctx.failed.get("args") or {}, 120),
                                        "error": str(ctx.failed.get("error") or "")[:300]}
    return result


def _short(args: dict, limit: int = 300) -> dict:
    return {k: (v[:limit] if isinstance(v, str) else v) for k, v in (args or {}).items()}


def _hint_allows(executor: str, hint: dict | None) -> bool:
    """A reroute must not contradict a confident fast-pass route."""
    if not hint or hint.get("p", 0) < 0.8:
        return True
    # "terminal" means typing or relaying into a terminal: never a Task Runtime job.
    wanted = {"start_task": {"whole_task"}, "desktop_task": {"desktop_goal", "instant"}}[executor]
    return hint.get("route") in wanted


def decide(answers: dict, tool: str, args: dict, ctx: Context) -> Verdict:
    """Code-owned policy over Jev's answers."""
    route, p = answers["route"]["choice"], answers["route"]["p"]
    match, gap = answers["matches"]["p"], answers["gap"]["choice"]
    evidence = {"route": route, "p": round(p, 2), "matches": round(match, 2), "gap": gap}
    if _writes(tool, args):
        confirmed = answers.get("confirmed", {}).get("p", 0)
        evidence["confirmed"] = round(confirmed, 2)
        if confirmed < 0.8:
            return Verdict("reject", evidence=evidence, message=(
                "MyApi action not run: Jev did not find an explicit instruction or confirmation for this exact "
                "external change. Read back the exact target and content, ask one short confirmation question, "
                "and retry unchanged only after the user clearly says yes."))
    confident_hint = bool(ctx.hint and ctx.hint.get("p", 0) >= 0.8)
    bar = DESKTOP_REROUTE_P if route == "desktop_task" and confident_hint else REROUTE_P
    if route in REROUTES and p >= bar and _hint_allows(route, ctx.hint):
        goal = ctx.request.strip()
        rerouted = {"goal": goal}
        if route == "start_task":
            from ..runtime.service import named_agent
            agent, unsandboxed = named_agent(goal)
            if agent:
                rerouted.update(agent=agent, unsandboxed=unsandboxed)
        return Verdict("reroute", route, rerouted, evidence=evidence,
                       message=f"Jev routed this request to {route} instead of {tool}.")
    if route == "ask_user" and p >= ASK_P:
        return Verdict("ask", evidence=evidence, message=(
            f"Not run: Jev judged the request {ctx.request[-200:]!r} ambiguous about what {tool} should act on. "
            "Ask the user one short question, then act."))
    reads = _reads(tool, args)
    floor = READ_MISMATCH if reads else HARD_MISMATCH
    why = {"values": "the target or values are wrong", "tool": "it is the wrong kind of action",
           "not_asked": "the user did not ask for this", "ambiguous": "the request is ambiguous"}.get(gap, "it does not fit")
    # A call that does not fit: Jev thinks it more likely wrong than right
    # (matches < 0.5) AND names it the wrong kind of action or one nobody
    # asked for. Two of Jev's own answers agreeing, not one more threshold.
    # Reads stay lenient: a lookup is how a follow-up starts (2026-10-08).
    misfit = not reads and match < 0.5 and gap in ("tool", "not_asked")
    better = answers.get("better_tool") or {}
    if better:
        evidence.update(better=better.get("choice"), better_p=round(better.get("p", 0), 2))
    # How much Jev's own tool choice backs the called tool. 2026-10-15 replays
    # (live Jev): "tell the kids they have 2 minutes, then lock" as
    # set_reminder got matches 0.6 but run_skill 0.93 / set_reminder 0.03;
    # "make it calmer here" said to the speaker as nightlight_toggle got route
    # reject 0.95, matches 0.72 and only 0.22 for the night light. matches
    # reads the call's own wording; the choice weighs it against the others.
    support = (better.get("probabilities") or {}).get(tool, 1.0) if better else 1.0
    if better:
        evidence["call_support"] = round(support, 2)
    displaced = (not reads and better.get("choice") not in (None, tool) and better.get("p", 0) >= BETTER_P
                 and support < 0.2)
    doubted = not reads and better and route == "reject" and p >= REJECT_P and support < 0.3
    if doubted and not displaced:
        ranked = sorted((better.get("probabilities") or {}).items(), key=lambda kv: -kv[1])
        likely = ", ".join([n for n, _ in ranked if n != tool][:2])
        where = ""
        if ctx.origin and ctx.origin.get("at_this_computer") is False:
            where = f" The user is talking through {ctx.origin.get('talking_to') or ctx.origin.get('channel')}, not at this computer."
        return Verdict("reject", evidence=evidence, message=(
            f"Not run: Jev judged {tool} wrong for the user's request {ctx.request[-200:]!r}.{where} "
            f"More likely: {likely}. Make the right call for what and where the user meant, or ask them."))
    if (misfit or displaced) and better.get("choice") not in (None, tool) and better.get("p", 0) >= BETTER_P:
        other = better["choice"]
        from ..execution import catalog
        direct = other in catalog.CORE or other in catalog.MYAPI_CORE
        where = ""
        if ctx.origin and ctx.origin.get("at_this_computer") is False:
            where = f" The user is talking through {ctx.origin.get('talking_to') or ctx.origin.get('channel')}, not at this computer."
        how = f"call {other}" if direct else f"call {other} (through use_tool with name='{other}' if it is not declared to you)"
        return Verdict("reject", evidence=evidence, message=(
            f"Not run: Jev checked {tool} against the user's request {ctx.request[-200:]!r}: {why}.{where} "
            f"The tool for this is {other}. Instead, {how} with arguments for what the user asked."))
    # 2026-10-15 20:48:02: hide_window_labels with route=reject p=0.72,
    # matches=0.16, gap=not_asked ran, because p was under REJECT_P. When the
    # route's own top choice is reject and the call is a misfit, three answers
    # agree: it does not run.
    if match < floor or (misfit and route == "reject") or (
            not reads and route == "reject" and p >= REJECT_P and match < REJECT_MATCH):
        return Verdict("reject", evidence=evidence, message=(
            f"Not run: Jev checked {tool} {json.dumps(_short(args), ensure_ascii=False)[:200]} against the user's "
            f"request {ctx.request[-200:]!r} and {why}. Re-read the request and make the right call, or ask."))
    return Verdict("execute", tool, dict(args or {}), evidence=evidence)


def _missing_agent(tool: str, args: dict, ctx: Context) -> Verdict | None:
    """Code-owned: the user named a coding agent, the task must use it.
    2026-09-27 15:45: "open codex --yolo and ask him as follows: ..." became
    start_task without Codex (Jev voted reject p=0.84 but matches 0.32 passed
    the 0.30 bar); the job ran on qwen, then grok."""
    if tool != "start_task" or (args or {}).get("agent"):
        return None
    from ..runtime.service import named_agent
    said = " ".join([*ctx.earlier[-1:], ctx.request])
    agent, unsandboxed = named_agent(said)
    if not agent:
        return None
    return Verdict("reject", evidence={"missing_agent": agent}, message=(
        f"Not run: the user asked for {agent} by name. Call start_task again with agent='{agent}'"
        + (", unsandboxed=true (they asked for it without its sandbox)" if unsandboxed else "")
        + ", the directory they named as workspace (absolute path), and their full request as the goal."))


def _relays_to_task(tool: str, args: dict) -> bool:
    args = args or {}
    return (tool == "task_respond" and "approve" not in args and not args.get("cancel")
            and bool(args.get("answer") or args.get("guidance")))


class Switchboard:
    def __init__(self, jev: Jev | None = None):
        self._jev = jev
        self._cache: dict[str, tuple[float, Verdict]] = {}
        self._lock = threading.Lock()

    def review(self, tool: str, args: dict, ctx: Context) -> Verdict:
        key = json.dumps([tool, args, ctx.request, ctx.failed, ctx.origin], sort_keys=True, ensure_ascii=False, default=str)
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] < CACHE_SECONDS:
                return cached[1]
        started = time.monotonic()
        if not ctx.request.strip():
            if _writes(tool, args):
                verdict = Verdict("reject", evidence={"confirmation": "no user request"}, message=(
                    "MyApi action not run: there is no current user confirmation for this external change."))
            else:
                verdict = Verdict("execute", tool, dict(args or {}), evidence={"skipped": "no user request"})
        elif (missing := _missing_agent(tool, args, ctx)) is not None:
            verdict = missing
        elif _relays_to_task(tool, args):
            # 2026-09-27 19:33: "I want you to do it. SSH to it yourself" relayed
            # verbatim as a task answer was rejected (matches 0.07: Jev saw
            # "send a message", not "SSH"), and she told the user she couldn't.
            # A relay is harmless: what the task then does passes its own
            # permission checks. Approve/deny stays reviewed.
            verdict = Verdict("execute", tool, dict(args or {}), evidence={"skipped": "relays the user's words to a task"})
        else:
            try:
                # Reads are never replaced by another tool, and a write's
                # question is its confirmation: neither gets a shortlist.
                offered = None
                if not (_reads(tool, args) or _writes(tool, args)):
                    try:
                        offered = shortlist(tool, args, ctx)
                    except Exception as exc:  # noqa: BLE001 -- the review still runs without it
                        log.warning("Switchboard shortlist failed: %s", str(exc)[:160])
                answers = (self._jev or Jev()).ask(state(tool, args, ctx), questions(tool, ctx, args, offered),
                                                   timeout=TIMEOUT, retries=0, fail_fast=True)
                verdict = decide(answers, tool, args, ctx)
            except (JevError, KeyError) as exc:
                if _writes(tool, args):
                    log.warning("Switchboard unavailable, refusing %s: %s", tool, str(exc)[:160])
                    verdict = Verdict("reject", evidence={"unavailable": str(exc)[:120]}, message=(
                        "MyApi action not run: confirmation could not be verified because Jev is unavailable."))
                else:
                    log.warning("Switchboard unavailable, running %s unreviewed: %s", tool, str(exc)[:160])
                    verdict = Verdict("execute", tool, dict(args or {}), evidence={"unavailable": str(exc)[:120]})
        verdict.ms = (time.monotonic() - started) * 1000
        with self._lock:
            self._cache = {k: v for k, v in self._cache.items() if now - v[0] < CACHE_SECONDS}
            self._cache[key] = (now, verdict)
        return verdict
