"""The daemon's single TaskRuntime and the voice-facing tool handlers.

The live voice model starts, checks and answers tasks through three small
tools (start_task, task_status, task_respond); the work itself happens in
runtime threads, so the conversation is never blocked. Results reach an
open conversation through the daemon's announce hook and always arrive as
a desktop notification.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
import subprocess
import threading
import time

from ..execution.actions import ActionResult

log = logging.getLogger(__name__)

_runtime = None
_lock = threading.Lock()


def get_runtime():
    global _runtime
    with _lock:
        if _runtime is None:
            from ..core import conversations
            from .runtime import TaskRuntime
            _runtime = TaskRuntime()
            _runtime.listeners.append(conversations.task_event)  # its cards go back to the talk it came from
            # TaskRuntime marks work interrupted during construction, before a
            # conversation listener exists.  Replay that recovery state once
            # so a restart cannot leave the originating chat looking frozen.
            from .task import INTERRUPTED
            for task in _runtime.store.list(50):
                if task.status == INTERRUPTED and task.conversation:
                    conversations.task_event(task, "interrupted")
            # Also cover a task marked interrupted by the immediately prior
            # daemon instance before this recovery policy existed.  A recent
            # interruption is restart fallout, not a user cancellation.
            recent = [task.id for task in _runtime.store.list(50)
                      if task.status == INTERRUPTED and time.time() - task.updated_at < 3600]
            recovered = _runtime.recover_after_restart(list(dict.fromkeys([*_runtime._restart_recoveries, *recent])))
            if recovered:
                log.info("resumed %d task(s) after daemon restart: %s", len(recovered), ", ".join(recovered))
        return _runtime


# A voice task this recent and still open may be the same job as a new
# request (real case 2026-09-27 10:40-10:49: four escalations of one README
# video job ran at once, plus a fifth waiting for approval).
SAME_JOB_HOURS = 6
SAME_JOB_P = 0.6


def _job_text(goal: str) -> str:
    """What the user wants, without an escalation brief's boilerplate."""
    if goal.startswith("Escalated from the voice assistant"):
        said = goal.split("What the user said", 1)[-1].split("What the voice assistant tried", 1)[0]
        turns = [line[2:] for line in said.splitlines() if line.startswith("- ")]
        return " / ".join(turns[-3:])[:400] or goal[:400]
    return goal[:400]


def _same_job(runtime, goal: str, jev=None):
    """The open voice task already doing this job, if any (Jev decides)."""
    from .task import ACTIVE, INTERRUPTED, WAITING_APPROVAL, WAITING_USER
    open_states = ACTIVE | {WAITING_APPROVAL, WAITING_USER, INTERRUPTED}
    cutoff = time.time() - SAME_JOB_HOURS * 3600
    candidates = [t for t in runtime.store.list(20)
                  if t.status in open_states and t.source == "voice" and t.updated_at >= cutoff]
    if not candidates:
        return None
    from ..core.jev import Jev, JevError, choice
    criteria = {t.id: f"{_job_text(t.goal)} (status: {t.status})" for t in candidates}
    criteria["new"] = "None of these: a different job, or one of these was already finished or given up"
    try:
        answer = (jev or Jev()).ask(
            {"new_request": _job_text(goal)},
            {"job": choice("Is `new_request` the same job as one of these open tasks, i.e. does it want the same "
                           "end result (even if worded differently, a retry, a follow-up about its progress, or "
                           "with more detail)? Speech recognition may garble words.", criteria)},
            timeout=5, retries=1)["job"]
    except (JevError, KeyError, ValueError, Exception) as exc:  # noqa: BLE001 -- unsure: start the task
        log.warning("same-job check unavailable, starting a new task: %s", str(exc)[:160])
        return None
    if answer["choice"] == "new" or answer["p"] < SAME_JOB_P:
        return None
    return next(t for t in candidates if t.id == answer["choice"])


_AGENTS = {"codex": "CODEX", "claude_code": "CLAUDE_CODE", "claude": "CLAUDE_CODE", "claude code": "CLAUDE_CODE"}
_NAMED = [(re.compile(r"(?i)(?<![\w-])codex(?![\w-])"), "codex"),
          (re.compile(r"(?i)(?<![\w-])claude(?:[ -]?code)?(?![\w-])"), "claude_code")]
_UNSANDBOXED = re.compile(r"(?i)--?yolo\b|\byolo\b|dangerously|skip[- ]permissions|without (?:the |its )?sandbox|full access")


def named_agent(text: str) -> tuple[str, bool]:
    """The coding agent the user asked for by name in `text` ("open codex
    --yolo and ask him ..."), and whether without its sandbox."""
    for pattern, agent in _NAMED:
        if pattern.search(text or ""):
            return agent, bool(_UNSANDBOXED.search(text))
    return "", False


def start_task(args: dict) -> ActionResult:
    from ..config import load_config
    if not getattr(load_config(), "task_runtime_enabled", True):
        return ActionResult(False, "The task runtime is disabled in settings (task_runtime_enabled).")
    goal = str(args.get("goal") or "").strip()
    if not goal or len(goal) > 4000:
        return ActionResult(False, "start_task needs the user's complete goal (1-4000 characters).")
    if not args.get("agent") and _myapi_job(goal):
        return ActionResult(False, "Not started: this job is in the user's connected services, so it goes to the "
                                   "myapi tool (Jev runs it) -- call myapi with the request. Coding agents never do "
                                   "email, calendar or other MyApi work.")
    runtime = get_runtime()
    requested = str(args.get("agent") or "").strip().lower()
    unsandboxed = bool(args.get("unsandboxed"))
    # Tool-call fields are produced by the live model, not a reliable record
    # of the user's intent.  Pin only an agent the user explicitly named in
    # the goal; every other task goes to Jev with all available capabilities.
    named, named_unsandboxed = named_agent(goal)
    agent = _AGENTS.get(named, "")
    if requested and not agent:
        log.info("ignoring model-suggested task agent %s; Jev will route", requested)
    unsandboxed = unsandboxed and bool(agent) or named_unsandboxed
    existing = _same_job(runtime, goal)
    if existing is not None and agent and existing.agent != agent:
        # Same job, but the user wants it done by a named agent: a new task,
        # not a steer into one that another worker is doing.
        existing = None
    if existing is not None:
        runtime.steer(existing.id, _job_text(goal))
        summary = existing.summary()
        return ActionResult(True, json.dumps({
            "task_id": existing.id, "status": existing.status, "existing": True,
            "last_step": summary.get("last_step"), "pending_approval": summary.get("pending_approval"),
            "question": summary.get("question"),
            "note": ("NOT started again: this task is already doing that job, and the new request was added to it. "
                     "Tell the user it is already on it and what it is doing now (status, last step). If it is "
                     "going the wrong way, correct it with task_respond(task_id, guidance=...), or cancel it and "
                     "start one with a better goal. If it waits for approval or an answer, say so."),
        }, ensure_ascii=False))
    try:
        # The daemon's own cwd is this project's checkout; a voice task must not
        # silently work (and checkpoint/roll back) inside it (code review 2026-09-23).
        from ..core import conversations
        task = runtime.start(goal, args.get("workspace") or str(Path.home()), source="voice", agent=agent,
                             unsandboxed=unsandboxed, conversation=conversations.CURRENT.get())
    except ValueError as exc:
        return ActionResult(False, str(exc))
    return ActionResult(True, json.dumps({
        "task_id": task.id, "status": "started", "workspace": task.workspace, "agent": task.agent or "chosen by Jev",
        "note": ("Running in the background. Tell the user it has started; its result, any approval it needs and "
                 "any question will be announced. Do not claim it is done until task_status says certified."),
    }))


def _git(workspace: str, *argv: str) -> str:
    try:
        out = subprocess.run(["git", "-C", workspace, *argv], capture_output=True, text=True, timeout=5,
                             check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def change_preview(workspace: str, limit: int = 3000) -> dict | None:
    """What approving would publish or keep: the workspace's unpushed commits
    and uncommitted changes, so she can explain the change when asked. Real
    case (2026-09-27): a task waited to `git push` a README edit that swapped
    the demo video for a tag GitHub strips; the approval showed only the
    command, so nobody could see what was being pushed."""
    if not workspace or _git(workspace, "rev-parse", "--is-inside-work-tree") != "true":
        return None
    upstream = _git(workspace, "rev-parse", "--abbrev-ref", "@{u}")
    base = upstream or "HEAD"
    preview = {
        "unpushed_commits": _git(workspace, "log", "--oneline", f"{upstream}..HEAD") if upstream else "",
        "uncommitted": _git(workspace, "status", "--short"),
        "stat": _git(workspace, "diff", "--stat", base),
        "diff": _git(workspace, "diff", base)[:limit],
    }
    return {k: v for k, v in preview.items() if v} or None


def task_status(args: dict) -> ActionResult:
    runtime = get_runtime()
    if args.get("list"):
        # Compact: 2026-09-27 19:21 the full summaries were 22,254 chars, she
        # acted on none of it and never saw a waiting task's what_to_do.
        return ActionResult(True, json.dumps([_brief(t.summary()) for t in runtime.store.list(8)], ensure_ascii=False))
    requested_id = str(args.get("task_id") or "")
    summary = runtime.status(requested_id or None)
    if summary is None and requested_id:
        # Speech models occasionally reverse the date portion of an id, while
        # retaining its time/random suffix. Recover only when that suffix maps
        # to exactly one task; never guess for a mutating task operation.
        parts = requested_id.split("-")
        suffix = "-".join(parts[-2:]) if len(parts) >= 3 else ""
        matches = [task for task in runtime.store.list(50) if suffix and task.id.endswith(suffix)]
        if len(matches) == 1:
            log.warning("task_status repaired malformed task id %s -> %s", requested_id, matches[0].id)
            summary = runtime.status(matches[0].id)
    if summary is None:
        return ActionResult(False, "No task found.")
    if summary.get("pending_approval"):
        task = runtime.store.load(summary["id"])
        summary["change_preview"] = change_preview(task.workspace if task else "")
    return ActionResult(True, json.dumps(_with_next_step(summary), ensure_ascii=False))


def _brief(summary: dict) -> dict:
    brief = {"id": summary["id"], "goal": _job_text(summary.get("goal", ""))[:240], "status": summary.get("status")}
    if summary.get("pending_approval"):
        request = summary["pending_approval"]
        brief["pending_approval"] = {k: request.get(k) for k in ("kind", "subject", "risk")}
    if summary.get("question"):
        brief["question"] = summary["question"][:400]
    if summary.get("result"):
        brief["result"] = str(summary["result"])[:240]
    return _with_next_step(brief)


def _with_next_step(summary: dict) -> dict:
    if summary.get("status") == "waiting_user" and summary.get("question"):
        # Real case 2026-09-27 18:55: the task asked the user to set up an SSH
        # key; she called it "waiting for your approval", then answered "I
        # gave you credentials" / "so you run it" herself with "I cannot"
        # instead of passing the replies on, and the task stayed stuck.
        summary["what_to_do"] = (
            f"Task {summary['id']} is NOT waiting for approval: it asked the user a question. Say the question in "
            "plain words. Whatever the user replies (an answer, details, credentials, or 'you do it') goes to the "
            f"task word for word: task_respond with task_id {summary['id']} and answer=<their reply>. The task "
            "does the work; never tell the user you can't or that they must do it themselves.")
    return summary


def _myapi_job(goal: str) -> bool:
    """Whether a job lives only in the user's online accounts (Jev judges it).
    2026-09-30: emailing Chris went to a Codex task three times and was never
    sent; MyApi work is Jev's (execution/myapi_agent.py)."""
    try:
        from .. import myapi
        if not myapi.is_connected():
            return False
        from ..core.jev import Jev, boolean
        answer = Jev().ask({"goal": goal[-1500:]}, {"q": boolean(
            "Is `goal` only about the user's online accounts or services (email, calendar, cloud files, GitHub, "
            "messaging, social...), with nothing to do on this computer (no files, programs, terminals, system "
            "settings or local code)?")}, timeout=4, retries=0, fail_fast=True)
        return answer["q"]["p"] >= 0.8
    except Exception:  # noqa: BLE001 -- without Jev the task runs as before
        return False


def task_respond(args: dict) -> ActionResult:
    runtime = get_runtime()
    if args.get("guidance"):
        task_id = args.get("task_id") or ""
        if not task_id:
            return ActionResult(False, "guidance needs the task_id of the task to correct")
        result = runtime.steer(task_id, str(args["guidance"]))
        return ActionResult(result["ok"], result["message"])
    if args.get("cancel"):
        result = runtime.cancel(args.get("task_id") or None)
        return ActionResult(result["ok"], result["message"])
    if args.get("agent"):
        agent = _AGENTS.get(str(args["agent"]).strip().lower(), "")
        if not agent:
            return ActionResult(False, f"agent must be one of: {', '.join(sorted(_AGENTS))}")
        result = runtime.reassign(args.get("task_id") or None, agent, unsandboxed=bool(args.get("unsandboxed")))
        return ActionResult(result["ok"], result["message"])
    approve = args.get("approve")
    approve = approve if isinstance(approve, bool) else None
    task_id = args.get("task_id") or None
    if approve is not None or args.get("answer"):
        task_id, refusal = _in_this_conversation(runtime, task_id)
        if refusal:
            return ActionResult(False, refusal)
    if task_id and task_id.startswith(conversations_tool_install()):
        return _answer_install(task_id, approve)
    if task_id and task_id.startswith("action:"):
        # A prepared MyApi action (execution/myapi_agent.py): the user's yes in its chat sends it.
        from ..execution import myapi_agent
        if approve is None:
            return ActionResult(False, "Answer the prepared action with approve=true (send it) or false (cancel).")
        ok, message = myapi_agent.decide(task_id[len("action:"):], approve, "by voice in the chat")
        return ActionResult(ok, message)
    result = runtime.respond(task_id, approve=approve, answer=args.get("answer") or None, channel="voice")
    return ActionResult(result["ok"], result["message"])


def conversations_tool_install() -> str:
    from ..core import conversations
    return conversations.TOOL_INSTALL


def _in_this_conversation(runtime, task_id):
    """(task_id, refusal): an approval or answer given in a conversation goes
    only to a request of that conversation. 2026-09-30: the user typed
    "approved" in a chat opened for a tool install, and it answered the Chris
    email task's question from another talk -- she started sending the email."""
    from ..core import conversations
    from .task import WAITING_APPROVAL, WAITING_USER
    current = conversations.CURRENT.get()
    if not current:
        return task_id, None
    ask = ("Ask the user which request they mean; do not answer any other. Each request's card is in its own chat "
           "in the phone's Text with Omarchy.")
    if task_id and (task_id.startswith(conversations.TOOL_INSTALL) or task_id.startswith(conversations.ACTION)):
        return task_id, None
    if task_id:
        task = runtime.store.load(task_id)
        # Tasks from before conversations existed have none: they stay answerable.
        if task is not None and task.conversation and task.conversation != current:
            return None, f"Not answered: task {task_id} belongs to another conversation. {ask}"
        return task_id, None
    waiting = [t for t in runtime.store.list(50)
               if t.status in (WAITING_APPROVAL, WAITING_USER) and t.conversation == current]
    installs = [line for line in (conversations.get(current) or {}).get("lines", [])
                if line.get("role") == "card" and line.get("status") == "waiting"
                and (str(line.get("task_id", "")).startswith(conversations.TOOL_INSTALL)
                     or line.get("kind") == "confirm")]
    if len(waiting) + len(installs) == 1:
        return (waiting[0].id if waiting else installs[0]["task_id"]), None
    if not waiting and not installs:
        return None, f"Not answered: nothing in this conversation waits for an approval or an answer. {ask}"
    return None, f"Not answered: several requests wait in this conversation; name the task. {ask}"


def _answer_install(task_id: str, approve: bool | None) -> ActionResult:
    """A tool install waiting in this chat: a yes sends the fingerprint prompt
    (a model-relayed yes never installs code by itself), a no declines it."""
    from ..execution import flux_approve, user_tools
    from ..phone import server
    request = server._install_request(task_id)
    if request is None:
        return ActionResult(False, "That install is no longer waiting.")
    if approve is False:
        user_tools.decide_install(request["id"], False, "declined by the user in the chat")
        return ActionResult(True, "Declined: the tool will not be installed.")
    if not flux_approve.available():
        return ActionResult(False, "Not installed: approve it on the desktop notification (no phone is set up for "
                                   "fingerprint approval). Tell the user that in one sentence.")
    import threading
    threading.Thread(target=server._fingerprint_install, args=(request,), daemon=True).start()
    return ActionResult(True, "Not installed yet: a fingerprint prompt went to the phone; it installs when the user "
                              "touches it. Tell the user that in one sentence.")


# Outcomes the user must hear even if they hung up before the task ended.
_OUTCOMES = {"certified": "finished and verified", "unverified": "finished, but the result could not be verified",
             "failed": "failed", "interrupted": "was cut off by a restart before it finished",
             "waiting_approval": "is paused, waiting for your approval", "waiting_user": "is paused with a question for you"}


def _remember(task, event):
    """Queue a spoken-task outcome for the user's next conversation (heard
    now if one is open). CLI tasks are watched in their own terminal."""
    if task.source == "cli" or event not in _OUTCOMES:
        return None
    from ..core import agenda
    detail = {"waiting_approval": f"needs approval: {json.dumps(task.pending_approval)}",
              "waiting_user": f"question: {task.question}"}.get(event, task.result)
    return agenda.task_result(task.id, task.goal, event,
                              f"Task {task.id} {_OUTCOMES[event]}. {detail or ''}".strip())


# Events that wake the assistant to say them now, even with no conversation
# open: the task is stuck until the user answers (the user's request,
# 2026-09-27, after a push approval sat unnoticed).
WAKE_EVENTS = frozenset({"waiting_approval"})


def announce_to(session_getter, loop, wake=None) -> None:
    """Forward task events to an open voice conversation (daemon hook), and
    keep the ones the user must hear until a conversation delivers them.
    Task events fire on runtime threads; the session's announcement queue
    belongs to the daemon's event loop, so hand over with call_soon_threadsafe.
    `wake(entry)` (the daemon's _on_agenda_result) says a WAKE_EVENTS entry
    now: in the open conversation or phone call, else by starting one."""
    from ..core import agenda

    def listener(task, event):
        entry = _remember(task, event)
        if entry is not None and wake is not None and event in WAKE_EVENTS:
            loop.call_soon_threadsafe(wake, entry)
            return
        if entry is None:
            detail = {"waiting_approval": f"needs approval: {json.dumps(task.pending_approval)}",
                      "waiting_user": f"has a question: {task.question}"}.get(event, task.result)
            entry = {"id": f"task-{task.id}-{event}-{len(task.steps)}", "title": f"Task {event.replace('_', ' ')}",
                     "detail": f"Task {task.id} ({task.goal[:120]}) {detail}"}
        agenda.announce_to_calls(entry)  # a phone call hears it now too
        session = session_getter()
        if session is None or not hasattr(session, "announce"):
            return  # stays in the agenda inbox: the next conversation's prompt carries it
        loop.call_soon_threadsafe(session.announce, entry)
    runtime = get_runtime()
    runtime.listeners.append(listener)
    # Tasks a restart cut off never emit an event (the runtime marks them
    # interrupted before anyone listens): queue those once as well.
    for task in runtime.store.list(20):
        if task.status == "interrupted" and time.time() - task.updated_at < 86400:
            _remember(task, "interrupted")
