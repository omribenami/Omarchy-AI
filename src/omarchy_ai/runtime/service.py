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
import threading

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


def task_status(args: dict) -> ActionResult:
    runtime = get_runtime()
    if args.get("list"):
        return ActionResult(True, json.dumps([t.summary() for t in runtime.store.list(8)], ensure_ascii=False))
    summary = runtime.status(args.get("task_id") or None)
    if summary is None:
        return ActionResult(False, "No task found.")
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


def announce_to(session_getter, loop) -> None:
    """Forward task events to an open voice conversation (daemon hook).
    Task events fire on runtime threads; the session's announcement queue
    belongs to the daemon's event loop, so hand over with call_soon_threadsafe."""
    def listener(task, event):
        session = session_getter()
        if session is None or not hasattr(session, "announce"):
            return
        detail = {"waiting_approval": f"needs approval: {json.dumps(task.pending_approval)}",
                  "waiting_user": f"has a question: {task.question}"}.get(event, task.result)
        entry = {"id": f"task-{task.id}-{event}-{len(task.steps)}", "title": f"Task {event.replace('_', ' ')}",
                 "detail": f"Task {task.id} ({task.goal[:120]}) {detail}"}
        loop.call_soon_threadsafe(session.announce, entry)
    get_runtime().listeners.append(listener)
