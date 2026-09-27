"""The daemon's single TaskRuntime and the voice-facing tool handlers.

The live voice model starts, checks and answers tasks through three small
tools (start_task, task_status, task_respond); the work itself happens in
runtime threads, so the conversation is never blocked. Results reach an
open conversation through the daemon's announce hook and always arrive as
a desktop notification.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import threading
import time

from ..execution.actions import ActionResult

_runtime = None
_lock = threading.Lock()


def get_runtime():
    global _runtime
    with _lock:
        if _runtime is None:
            from .runtime import TaskRuntime
            _runtime = TaskRuntime()
        return _runtime


def start_task(args: dict) -> ActionResult:
    from ..config import load_config
    if not getattr(load_config(), "task_runtime_enabled", True):
        return ActionResult(False, "The task runtime is disabled in settings (task_runtime_enabled).")
    goal = str(args.get("goal") or "").strip()
    if not goal or len(goal) > 4000:
        return ActionResult(False, "start_task needs the user's complete goal (1-4000 characters).")
    try:
        # The daemon's own cwd is this project's checkout; a voice task must not
        # silently work (and checkpoint/roll back) inside it (code review 2026-09-23).
        task = get_runtime().start(goal, args.get("workspace") or str(Path.home()), source="voice")
    except ValueError as exc:
        return ActionResult(False, str(exc))
    return ActionResult(True, json.dumps({
        "task_id": task.id, "status": "started", "workspace": task.workspace,
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
        return ActionResult(True, json.dumps([t.summary() for t in runtime.store.list(8)], ensure_ascii=False))
    summary = runtime.status(args.get("task_id") or None)
    if summary is None:
        return ActionResult(False, "No task found.")
    if summary.get("pending_approval"):
        task = runtime.store.load(summary["id"])
        summary["change_preview"] = change_preview(task.workspace if task else "")
    return ActionResult(True, json.dumps(summary, ensure_ascii=False))


def task_respond(args: dict) -> ActionResult:
    runtime = get_runtime()
    if args.get("cancel"):
        result = runtime.cancel(args.get("task_id") or None)
        return ActionResult(result["ok"], result["message"])
    approve = args.get("approve")
    result = runtime.respond(args.get("task_id") or None, approve=approve if isinstance(approve, bool) else None,
                             answer=args.get("answer") or None, channel="voice")
    return ActionResult(result["ok"], result["message"])


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
