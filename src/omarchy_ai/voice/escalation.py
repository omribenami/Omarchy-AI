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

Nothing failing is not the same as being right (journal 2026-10-05
16:04-16:09, phone): asked to ssh to the home server and find a folder, she
ran eleven finds and listings on THIS computer, every one ok=True, and told
the user three times it was not there while they answered "are you sure that
is the path?", "that cannot be, the folder exists for certain" and finally
the server's address again. With no failure on record nothing above could
fire. So when the user disputes a result she got from her own tool calls:
- the first time, her next tool result carries a self-check listing those
  calls (each says where it ran) and what to compare them against;
- the second time within REVIEW_WINDOW, the job is escalated with those calls.

Saying it is not doing it (2026-10-12): asked to start a saved procedure of
five timed steps, she read it (load_skill), told the user "on it, starting
the countdown" and made no other call. Nothing ran. Two minutes later the
user wrote that nothing had been heard and she answered that she was making
sure it was running, again without a call; earlier that day the same request
got "I'll take care of it" and no call at all. No tool failed and no result
was disputed, so nothing above could fire. So when a reply to the user's own
words is complete and no tool that acts ran for them (none, or only ones
that read), Jev judges two things: does her reply say she is doing, has
started, will do or has done something, and did the user ask for something
to be done or say it did not happen. Both -> the job is escalated.

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
REVIEW_WINDOW = 300.0
REVIEW_NOTE_TTL = 120.0
REVIEW_STEPS = 8

# Calls whose failure is not the job failing: control flow, the escalation
# target itself, and HUD cosmetics.
IGNORED_TOOLS = frozenset({
    "end_conversation", "start_task", "task_status", "task_respond", "show_window_labels",
    "hide_window_labels", "get_recent_actions", "remember_preference",
    # MyApi is Jev's (execution/myapi_agent.py): a miss there is said, never handed to a coding worker.
    "myapi",
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
    r'|name one|does not exist|similar names|"status": "handoff"'
    # Waiting on the user, not failing: which TV (2026-09-27 02:06), and a
    # sign-in only they can finish (2026-09-27 00:44, 10:24).
    r"|ask the user which one|needs the user's password|needs a human", re.S)

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
DISPUTES_P = 0.8
CLAIMS_P = 0.8
ASKS_ACTION_P = 0.8


def proactive_goal(user_turns: list[str]) -> str:
    """Build a self-contained handoff for a newly recognized whole task.

    The latest command is often only "give it a try" or "do that now".  A
    background worker does not share the live model's hidden context, so give
    it the recent user-authored requirements instead of inventing a summary.
    """
    turns = [" ".join(str(turn).split()) for turn in user_turns if str(turn).strip()]
    latest = turns[-1] if turns else ""
    context = "\n".join(f"- {turn[-700:]}" for turn in turns[-6:-1])
    goal = (
        "Continue this user-requested job autonomously until it is completed, or until a real approval or "
        "missing input requires the user. Do not stop merely to report an intermediate successful step. "
        "Respect all permission and confirmation gates, and report the verified result.\n\n"
        + (f"Relevant earlier user requirements, oldest first:\n{context}\n\n" if context else "")
        + f"Current request:\n{latest}"
    )
    return goal[:3900]


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


def jev_disputes(last_reply: str, latest: str) -> bool:
    """The user says her result is wrong, in any language. False when Jev is
    unavailable: then she is simply not prompted to check herself."""
    from ..core.jev import Jev, JevError, boolean
    questions = {"disputes": boolean(
        "Does the user's latest message tell the assistant that the result or answer it just gave is wrong or "
        "cannot be right: that something it could not find does exist, that it looked in the wrong place or at "
        "the wrong thing, or a doubt such as asking whether it is sure? Judge the meaning, in any language. A new "
        "request, an ordinary follow-up question, a correction of the user's own earlier words, or thanks is not.")}
    try:
        answers = Jev().ask({"assistant_reply": last_reply[-400:], "user_latest_message": latest[-400:]}, questions,
                            timeout=3, retries=0, fail_fast=True)
    except JevError:
        return False
    return answers["disputes"]["p"] >= DISPUTES_P


def jev_unbacked(last_reply: str, latest: str) -> tuple[bool, dict]:
    """(her reply says an action is under way or done AND the user asked for
    one, the two probabilities), judged in any language. False when Jev is
    unavailable: then a reply with nothing behind it is not caught."""
    from ..core.jev import Jev, JevError, boolean
    questions = {
        # Worded as one judgment. The first wording listed what counts (doing,
        # started, will take care of, checking, completed) and what does not;
        # replayed on the 2026-10-12 exchanges it scored the two replies that
        # began this, "on it, I'll remind them every minute" and "on it,
        # starting the countdown", 0.74 and 0.62: below the bar. This one
        # scored them 0.92 and 0.95, and a clarifying question or "I couldn't
        # connect" under 0.5. It also says yes to a plain answer (0.94 for a
        # description of what a camera shows): asks_action is what rules
        # those out (0.09 there).
        "claims_action": boolean(
            "Does the assistant's reply accept the user's request and say or imply that the assistant is carrying "
            "it out, will carry it out, or has carried it out? Judge the meaning, in any language."),
        "asks_action": boolean(
            "Does the user's latest message ask for something to be done, or say that something that should have "
            "happened did not? Judge the meaning, in any language. Chat, a question that can be answered by "
            "talking, or thanks is not."),
    }
    try:
        answers = Jev().ask({"assistant_reply": last_reply[-400:], "user_latest_message": latest[-400:]}, questions,
                            timeout=3, retries=0, fail_fast=True)
    except JevError as exc:
        return False, {"unavailable": str(exc)[:120]}
    claims, asks = answers["claims_action"]["p"], answers["asks_action"]["p"]
    return claims >= CLAIMS_P and asks >= ASKS_ACTION_P, {"claims_action": claims, "asks_action": asks}


def review_note(steps: list) -> str:
    tried = "\n".join(f"- {s.tool}({_short(s.args)[:160]}) -> {' '.join(s.message.split())[:260]}" for s in steps)
    return ("\n\n[assistant note, automatic self-check] The user says the result you gave is wrong, and none of "
            "your steps reported an error, so one of your own assumptions is wrong. Before you answer or search "
            "again, compare your steps below with the user's words: (1) the machine -- each result says WHERE IT "
            "RUNS; is that the machine the user named? (2) the exact host, path and name the user gave, against "
            "the ones you used; (3) whether the output you read belongs to the command you ran. Then tell the "
            "user in one sentence what you got wrong and redo it correctly. Do not repeat a check that already "
            f"came back empty. Your recent steps, oldest first:\n{tried}")


@dataclass
class Failure:
    tool: str
    args: dict
    message: str
    at: float


class Escalator:
    def __init__(self, judge=jev_judge, disputes=jev_disputes, unbacked=jev_unbacked):
        self._judge = judge
        self._disputes = disputes
        self._unbacked = unbacked
        self._failures: list[Failure] = []
        self._steps: list[Failure] = []  # her recent calls, failed or not
        self._reviewed_at: float | None = None
        self._review_note = ""
        self._last_escalation = -COOLDOWN
        self._own_task_at = -COOLDOWN

    def observe_call(self, tool: str, args: dict, ok: bool, message: str, now: float) -> None:
        if tool == "start_task":
            # She delegated herself: this incident is handled.
            self._own_task_at = now
            self._failures.clear()
            self._steps.clear()
            return
        if tool in IGNORED_TOOLS:
            return
        self._steps = [*self._steps, Failure(tool, dict(args or {}), message or "", now)][-REVIEW_STEPS:]
        if ok or _NOT_A_FAILURE.search(message or ""):
            return
        self._failures.append(Failure(tool, dict(args or {}), message or "", now))

    def pending(self, now: float) -> bool:
        """A recent failure that could escalate (cheap; due() may call Jev)."""
        return (any(now - f.at <= FAILURE_WINDOW for f in self._failures)
                and now - self._last_escalation >= COOLDOWN and now - self._own_task_at >= COOLDOWN)

    def reviewable(self, now: float) -> bool:
        """She has recent work a user could be disputing (cheap; disputed() calls Jev)."""
        return (any(now - s.at <= REVIEW_WINDOW for s in self._steps)
                and now - self._last_escalation >= COOLDOWN and now - self._own_task_at >= COOLDOWN)

    def disputed(self, now: float, user_turns: list[str], last_reply: str) -> str | None:
        """Ask Jev whether the user's latest words dispute her result. The
        first dispute arms the self-check note; the second within
        REVIEW_WINDOW returns the start_task goal. Run it off the event loop."""
        steps = [s for s in self._steps if now - s.at <= REVIEW_WINDOW]
        latest = user_turns[-1] if user_turns else ""
        if not steps or not latest or not self.reviewable(now) or not self._disputes(last_reply, latest):
            return None
        if self._reviewed_at is not None and now - self._reviewed_at <= REVIEW_WINDOW:
            goal = brief(user_turns, steps, last_reply, "the user said twice that her result is wrong",
                         outcome="each reported success, but the user says the result is wrong")
            self._steps, self._failures, self._reviewed_at, self._review_note = [], [], None, ""
            self._last_escalation = now
            return goal
        self._reviewed_at, self._review_note = now, review_note(steps)
        return None

    def claimable(self, now: float) -> bool:
        """Nothing was just handed over, by her or by an escalation (cheap; unbacked() calls Jev)."""
        return now - self._last_escalation >= COOLDOWN and now - self._own_task_at >= COOLDOWN

    def unbacked(self, now: float, user_turns: list[str], last_reply: str,
                 since: float = 0.0) -> tuple[str | None, dict]:
        """(the start_task goal, Jev's probabilities) when her reply tells the
        user something is being done that no tool did. The caller has
        established that no tool that acts ran since the user's latest words
        (`since`); the calls that only read are listed in the brief. Run it
        off the event loop."""
        latest = user_turns[-1] if user_turns else ""
        if not latest or not last_reply or not self.claimable(now):
            return None, {}
        claimed, evidence = self._unbacked(last_reply, latest)
        if not claimed:
            return None, evidence
        goal = brief(user_turns, [s for s in self._steps if s.at >= since], last_reply,
                     "she told the user it was being done, but no tool that does it ran",
                     outcome="none of which carried it out")
        self._failures = []
        self._last_escalation = now
        return goal, evidence

    def take_review_note(self, now: float) -> str:
        """The pending self-check, once, for the tool result she reads next."""
        note, self._review_note = self._review_note, ""
        return note if note and self._reviewed_at is not None and now - self._reviewed_at <= REVIEW_NOTE_TTL else ""

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


def brief(user_turns: list[str], failures: list[Failure], last_reply: str, reason: str,
          outcome: str = "which failed") -> str:
    said = "\n".join(f"- {t[-300:]}" for t in user_turns[-5:]) or "- (nothing transcribed)"
    tried = "\n".join(f"- {f.tool}({_short(f.args)}) -> {f.message[:300]}" for f in failures[-6:])
    goal = (
        f"Escalated from the voice assistant ({reason}). Finish what the user asked, properly.\n\n"
        "What the user said, oldest first. This is speech recognition: words are often misrecognized as words in "
        "other languages, so read it for meaning and use the earlier turns:\n"
        f"{said}\n\n"
        + (f"What the voice assistant tried, {outcome}:\n{tried}\n\n" if tried else
           "What the voice assistant tried: nothing. No tool was called for this request.\n\n")
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


def notice(task_id: str, existing: bool = False, proactive: bool = False, unbacked: bool = False) -> str:
    if unbacked:
        doing = "already doing this job was given the request" if existing else "is doing it now"
        return ("You told the user this was happening, but no tool ran, so nothing happened. Background task "
                f"{task_id} {doing}. Say that in one sentence. Do not also do it yourself; its result, and any "
                "approval or question it needs, will be announced.")
    if proactive:
        # 2026-10-08 20:22: a job handed over at once, before anything was
        # tried, was announced as "This kept failing".
        doing = "is already working on this; the new request was added to it" if existing else "is now working on this"
        return (f"This is a job with several steps, so background task {task_id} {doing}. Say that in one "
                "sentence. Do not do the job yourself as well; its result, and any approval or question it needs, "
                "will be announced.")
    if existing:
        # 2026-09-27: each failure streak started one more task for the same job.
        return (f"Background task {task_id} is already working on this; what just failed was added to it instead of "
                "starting another. Say that in one sentence, with what it is doing now if you know (task_status). "
                "Do not retry it yourself.")
    return (f"This kept failing, so I handed it to a background task that can dig deeper (task {task_id}). "
            "Say that in one sentence. Do not retry it yourself; its result, and any approval or question it "
            "needs, will be announced.")
