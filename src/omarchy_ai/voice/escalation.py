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
    r"^Not executed:|judged the request .* ambiguous|^Not run: Jev checked|^Input NOT sent"
    r'|name one|does not exist|similar names|"status": "handoff"', re.S)

_GIVE_UP = re.compile(
    r"\b(?:i can(?:'|no)t|i cannot|i'?m (?:not able|unable)|i am (?:not able|unable)|i (?:was|wasn'?t) (?:not )?able"
    r"|i couldn'?t|i don'?t have (?:access|permission)|no access|there'?s no way|not possible|it failed|did not work"
    r"|didn'?t work)\b"
    r"|אני לא יכול|אינני יכול|איני יכול|לא הצלחתי|לא הצלחנו|אין לי גישה|אין דרך|לא ניתן|לא אפשרי|נכשל",
    re.I)
_WHY = re.compile(
    r"\b(?:why|figure (?:it )?out|find out|debug|understand|what went wrong|what'?s wrong)\b"
    r"|למה|תביני|תבין|להבין|תבדקי|תבדוק|מה הבעיה|מה קרה",
    re.I)


@dataclass
class Failure:
    tool: str
    args: dict
    message: str
    at: float


class Escalator:
    def __init__(self):
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

    def due(self, now: float, user_turns: list[str], last_reply: str) -> str | None:
        """The start_task goal if this is the moment to escalate, else None."""
        self._failures = [f for f in self._failures if now - f.at <= FAILURE_WINDOW]
        if not self._failures:
            return None
        if now - self._last_escalation < COOLDOWN or now - self._own_task_at < COOLDOWN:
            return None
        latest = user_turns[-1] if user_turns else ""
        reason = None
        if len(self._failures) >= FAILURES_TO_ESCALATE:
            reason = f"{len(self._failures)} attempts failed"
        elif last_reply and _GIVE_UP.search(last_reply):
            reason = "she told the user she could not do it"
        elif latest and _WHY.search(latest):
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
        "What the user said, oldest first. This is speech recognition: Hebrew is often misrecognized as words in "
        "other languages, so read it for meaning and use the earlier turns:\n"
        f"{said}\n\n"
        f"What the voice assistant tried, which failed:\n{tried}\n\n"
        + (f"What she last told the user: {last_reply[-300:]}\n\n" if last_reply else "")
        + "Do the user's actual request end to end with whatever works on this machine: logged-in CLIs (gh is "
        "signed in to GitHub), logs, files, installed tools. Do not repeat the approach that already failed "
        "unless you found and fixed why it failed. If it truly cannot be done, find out exactly why from evidence "
        "and say what would make it possible. Anything public or irreversible (posting, sending, deleting) needs "
        "the user's approval first.\n\n"
        "Afterwards, if you solved it with a procedure the voice assistant lacked a tool for, package it as a "
        f"reusable tool so next time it is one call: see `{sys.executable} -m omarchy_ai.cli.tools format`, then "
        "`propose <dir>` and `install <name>` (install asks the user; once approved, use the tool to finish)."
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
