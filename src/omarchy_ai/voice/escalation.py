"""Code-owned escalation from the live voice model to the Task Runtime.

Real sessions 2026-09-26 23:05-23:41: asked four times to file the iwlwifi
freeze on Omarchy's GitHub, the live model tried report_issue and
browser_task, each failed, and she told the user "I can't, no access" --
while start_task (Claude Code workers with a shell, gh logged in, and an
approval gate) could have done it. A prompt rule asking her to delegate is
a suggestion to the very model that gave up; here the harness decides.

It escalates when, within FAILURE_WINDOW seconds:
- tool calls fail twice (ok=False; not guard messages she corrects herself), or
- one fails and she then tells the user she can't / it failed, or
- one fails and the user asks her to find out why.

"Can't" and "why" are judged by Jev from the meaning, in any language (they
were English and Hebrew regexes until 2026-09-28).

Then the session starts a task itself with a brief of what the user said and
what was tried, and she tells the user it was handed over. Once per incident:
the failures are cleared, and nothing escalates again for COOLDOWN seconds or
while she has started a task herself.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import sys

FAILURE_WINDOW = 180.0
COOLDOWN = 120.0
FAILURES_TO_ESCALATE = 2

# Calls whose failure is not the job failing: control flow, the escalation
# target itself, and HUD cosmetics.
IGNORED_TOOLS = frozenset({
    "end_conversation", "start_task", "task_status", "task_respond", "show_window_labels",
    "hide_window_labels", "get_recent_actions", "remember_preference",
})
# Failures that are not the job failing: the user cut in, or a guard telling
# her how to correct the call (focus first, pick a terminal, the real file
# name, re-read the request). Replayed on 2026-09-24..26 journals, counting
# these escalated 23 times in 50 sessions, almost all while she was about to
# correct herself; the real incidents (the Omarchy issue) all had a genuine
# failure followed by "I can't".
# 2026-09-27 01:14: a desktop_task handoff back to her plus the "you just read
# a multi-step script, use run_mission" guard escalated the user's live
# commercial to a background task, and the notice told her to stop -- so the
# commercial never ran. Every "Not executed:" is a guard, and a handoff is the
# job coming back to her on purpose.
_NOT_A_FAILURE = re.compile(
    r"^Not executed:|judged the request .* ambiguous|^Not run: Jev checked|^Not run: the screen is locked|^Input NOT sent"
    r'|name one|does not exist|similar names|"status": "handoff"', re.S)

# One of these means the assistant lacks a capability, rather than that one
# ordinary attempt happened to fail. Waiting for a second identical failure
# only makes it repeat itself. Escalate immediately so a coding worker can
# add a reusable, approval-gated tool and resume the original goal.
_CAPABILITY_GAP = re.compile(
    r"No catalog tool does this|no (?:available|approved) tool|not an approved tool|"
    r"isn't voice-reachable|not voice-reachable|unsupported (?:action|operation)|"
    r"cannot .* with (?:the )?(?:available|current) tools|missing capability",
    re.I,
)

GIVE_UP_P = 0.8
ASKS_WHY_P = 0.8


def jev_judge(last_reply: str, latest: str) -> tuple[bool, bool]:
    """(she told the user she can't / it failed, the user asks her to find
    out why), judged in any language. Both False when Jev is unavailable:
    then only repeated failures escalate."""
    from ..core.jev import Jev, JevError, boolean
    questions = {
        "gave_up": boolean("Does the assistant's reply tell the user that it cannot do the task, has no access or "
                           "permission, that there is no way, or that the attempt failed? Judge the meaning, in any "
                           "language. A reply that is still trying, or correcting itself, is not giving up."),
        "asks_why": boolean("Does the user's latest message ask the assistant to find out why something failed, "
                            "to investigate, debug or figure it out? Judge the meaning, in any language."),
    }
    try:
        answers = Jev().ask({"assistant_reply": last_reply[-400:], "user_latest_message": latest[-400:]}, questions,
                            timeout=3, retries=0, fail_fast=True)
    except JevError:
        return False, False
    return answers["gave_up"]["p"] >= GIVE_UP_P, answers["asks_why"]["p"] >= ASKS_WHY_P


@dataclass
class Failure:
    tool: str
    args: dict
    message: str
    at: float


class Escalator:
    def __init__(self, judge=jev_judge):
        self._judge = judge
        self._failures: list[Failure] = []
        self._last_escalation = -COOLDOWN
        self._own_task_at = -COOLDOWN

    def observe_call(self, tool: str, args: dict, ok: bool, message: str, now: float) -> None:
        if tool == "start_task":
            # She delegated herself: this incident is handled.
            self._own_task_at = now
            self._failures.clear()
            return
        if ok or tool in IGNORED_TOOLS or _NOT_A_FAILURE.search(message or ""):
            return
        self._failures.append(Failure(tool, dict(args or {}), message or "", now))

    def pending(self, now: float) -> bool:
        """A recent failure that could escalate (cheap; due() may call Jev)."""
        return (any(now - f.at <= FAILURE_WINDOW for f in self._failures)
                and now - self._last_escalation >= COOLDOWN and now - self._own_task_at >= COOLDOWN)

    def due(self, now: float, user_turns: list[str], last_reply: str) -> str | None:
        """The start_task goal if this is the moment to escalate, else None.
        May call Jev: run it off the event loop."""
        self._failures = [f for f in self._failures if now - f.at <= FAILURE_WINDOW]
        if not self._failures:
            return None
        if now - self._last_escalation < COOLDOWN or now - self._own_task_at < COOLDOWN:
            return None
        latest = user_turns[-1] if user_turns else ""
        reason = None
        capability = next((f for f in reversed(self._failures) if _CAPABILITY_GAP.search(f.message)), None)
        if capability is not None:
            reason = f"missing capability: {capability.message[:180]}"
        elif len(self._failures) >= FAILURES_TO_ESCALATE:
            reason = f"{len(self._failures)} attempts failed"
        elif last_reply or latest:
            gave_up, asks_why = self._judge(last_reply, latest)
            if gave_up:
                reason = "she told the user she could not do it"
            elif asks_why:
                reason = "the user asked her to find out why it failed"
        if not reason:
            return None
        goal = brief(user_turns, self._failures, last_reply, reason)
        self._failures = []
        self._last_escalation = now
        return goal


def _short(args: dict) -> str:
    text = json.dumps({k: (v[:200] if isinstance(v, str) else v) for k, v in (args or {}).items()},
                      ensure_ascii=False)
    return text[:400]


def brief(user_turns: list[str], failures: list[Failure], last_reply: str, reason: str) -> str:
    said = "\n".join(f"- {t[-300:]}" for t in user_turns[-5:]) or "- (nothing transcribed)"
    tried = "\n".join(f"- {f.tool}({_short(f.args)}) -> {f.message[:300]}" for f in failures[-6:])
    goal = (
        f"Escalated from the voice assistant ({reason}). Finish what the user asked, properly.\n\n"
        "What the user said, oldest first. This is speech recognition: words are often misrecognized as words in "
        "other languages, so read it for meaning and use the earlier turns:\n"
        f"{said}\n\n"
        f"What the voice assistant tried, which failed:\n{tried}\n\n"
        + (f"What she last told the user: {last_reply[-300:]}\n\n" if last_reply else "")
        + "Do the user's actual request end to end with whatever works on this machine: logged-in CLIs (gh is "
        "signed in to GitHub), logs, files, installed tools. Do not repeat the approach that already failed "
        "unless you found and fixed why it failed. If it truly cannot be done, find out exactly why from evidence "
        "and say what would make it possible. Anything public or irreversible (posting, sending, deleting) needs "
        "the user's approval first.\n\n"
        "CAPABILITY INVARIANT: this accepted task may not be abandoned merely because a tool is missing. First "
        "use the System agent's installed commands and APIs. If Omarchy lacks a reusable capability, route the "
        "implementation to Claude Code or Codex, build and test a narrowly scoped user tool, then run "
        f"`{sys.executable} -m omarchy_ai.cli.tools format`, `propose <dir>`, and `install <name>`. Installation "
        "must wait for the user's approval. After approval, resume this SAME task and use the installed tool to "
        "finish the original goal. A final failure is acceptable only with concrete evidence of an external "
        "blocker that code or a new tool cannot overcome; otherwise remain running or waiting for the specific "
        "approval/input needed."
    )
    return goal[:3900]


def notice(task_id: str, existing: bool = False) -> str:
    if existing:
        # 2026-09-27: each failure streak started one more task for the same job.
        return (f"Background task {task_id} is already working on this; what just failed was added to it instead of "
                "starting another. Say that in one sentence, with what it is doing now if you know (task_status). "
                "Do not retry it yourself.")
    return (f"This kept failing, so I handed it to a background task that can dig deeper (task {task_id}). "
            "Say that in one sentence. Do not retry it yourself; its result, and any approval or question it "
            "needs, will be announced.")
