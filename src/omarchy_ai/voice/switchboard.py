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

# No side effects: these may start while Jev is still deciding, and their
# result is dropped if Jev rejects the call.
READ_ONLY = frozenset({
    "task_status", "search_os_knowledge", "get_release_notes", "list_scheduled_tasks", "find_skill",
    "load_skill", "list_skills", "check_assistant_updates", "get_update_status", "list_bar_icons",
    "list_files", "read_file", "read_tile_log", "list_windows", "list_commands", "get_recent_actions",
    "describe_screen", "terminal_read", "battery_status", "list_cast_targets", "list_reminders",
})

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
REJECT_MATCH = 0.3    # ... and only if the call itself does not look right
HARD_MISMATCH = 0.1   # a call this far from the request never runs
READ_MISMATCH = 0.05  # reads are harmless: reject only obvious nonsense
ASK_P = 0.85
CACHE_SECONDS = 30
TIMEOUT = 3.0


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


def questions(tool: str, ctx: Context) -> dict:
    return {
        "route": choice(
            "The live model wants to make `call` for the user's latest `request` (earlier turns and the calls it "
            "already made this turn are context). Decide where this goes. Relaying or typing text the user "
            "dictated into a terminal or to a coding agent (Claude Code, Codex) is exactly what they asked: "
            "execute it.", _options(tool, ctx)),
        "matches": boolean(
            "Does `call` carry out what the user's latest `request` asks, or a sensible step toward it, with the "
            "right target and values (numbers, names, text)?"),
        "gap": choice(
            "If `call` does not fit the request, what is wrong with it?",
            {"none": "Nothing, it fits.",
             "values": "Wrong number, name, window, path or text.",
             "tool": "The wrong kind of action for this request.",
             "not_asked": "The user did not ask for this.",
             "ambiguous": "The request does not say clearly what to act on."}),
    }


def state(tool: str, args: dict, ctx: Context) -> dict:
    return {
        "request": ctx.request[-600:],
        "earlier_user_turns": [t[-200:] for t in ctx.earlier[-3:]],
        "fast_pass_route": ctx.hint,
        "calls_this_turn": [{"tool": n, "args": _short(a)} for n, a in ctx.calls[-6:]],
        "call": {"tool": tool, "args": _short(args), "what_it_does": ctx.description[:240]},
    }


def _short(args: dict) -> dict:
    return {k: (v[:300] if isinstance(v, str) else v) for k, v in (args or {}).items()}


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
    confident_hint = bool(ctx.hint and ctx.hint.get("p", 0) >= 0.8)
    bar = DESKTOP_REROUTE_P if route == "desktop_task" and confident_hint else REROUTE_P
    if route in REROUTES and p >= bar and _hint_allows(route, ctx.hint):
        goal = ctx.request.strip()
        return Verdict("reroute", route, {"goal": goal}, evidence=evidence,
                       message=f"Jev routed this request to {route} instead of {tool}.")
    if route == "ask_user" and p >= ASK_P:
        return Verdict("ask", evidence=evidence, message=(
            f"Not run: Jev judged the request {ctx.request[-200:]!r} ambiguous about what {tool} should act on. "
            "Ask the user one short question, then act."))
    floor = READ_MISMATCH if tool in READ_ONLY else HARD_MISMATCH
    if match < floor or (tool not in READ_ONLY and route == "reject" and p >= REJECT_P and match < REJECT_MATCH):
        why = {"values": "the target or values are wrong", "tool": "it is the wrong kind of action",
               "not_asked": "the user did not ask for this", "ambiguous": "the request is ambiguous"}.get(gap, "it does not fit")
        return Verdict("reject", evidence=evidence, message=(
            f"Not run: Jev checked {tool} {json.dumps(_short(args), ensure_ascii=False)[:200]} against the user's "
            f"request {ctx.request[-200:]!r} and {why}. Re-read the request and make the right call, or ask."))
    return Verdict("execute", tool, dict(args or {}), evidence=evidence)


class Switchboard:
    def __init__(self, jev: Jev | None = None):
        self._jev = jev
        self._cache: dict[str, tuple[float, Verdict]] = {}
        self._lock = threading.Lock()

    def review(self, tool: str, args: dict, ctx: Context) -> Verdict:
        key = json.dumps([tool, args, ctx.request], sort_keys=True, ensure_ascii=False, default=str)
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] < CACHE_SECONDS:
                return cached[1]
        started = time.monotonic()
        if not ctx.request.strip():
            verdict = Verdict("execute", tool, dict(args or {}), evidence={"skipped": "no user request"})
        else:
            try:
                answers = (self._jev or Jev()).ask(state(tool, args, ctx), questions(tool, ctx),
                                                   timeout=TIMEOUT, retries=0)
                verdict = decide(answers, tool, args, ctx)
            except (JevError, KeyError) as exc:
                log.warning("Switchboard unavailable, running %s unreviewed: %s", tool, str(exc)[:160])
                verdict = Verdict("execute", tool, dict(args or {}), evidence={"unavailable": str(exc)[:120]})
        verdict.ms = (time.monotonic() - started) * 1000
        with self._lock:
            self._cache = {k: v for k, v in self._cache.items() if now - v[0] < CACHE_SECONDS}
            self._cache[key] = (now, verdict)
        return verdict
