"""Task and routine HUDs for the desktop assistant.

The panels are deliberately read-only views over the Task Runtime and agenda
stores.  Like the watchdog, IPC failures are cosmetic and must never break a
conversation or a tool call.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading

log = logging.getLogger("omarchy_ai.display.assistant_huds")
_TARGET = "assistantHuds"
_TIMEOUT = 5


def _ipc(method: str, payload: dict | None = None) -> None:
    argv = ["omarchy-shell", "-q", _TARGET, method]
    if payload is not None:
        argv.append(json.dumps(payload, ensure_ascii=False))
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=_TIMEOUT, check=False)
        if proc.returncode:
            log.warning("assistant HUD ipc %s failed: %s", method, (proc.stderr or proc.stdout).strip())
    except (OSError, subprocess.SubprocessError):
        log.debug("assistant HUD ipc %s unavailable", method, exc_info=True)


def _ipc_async(method: str, payload: dict) -> None:
    """Best-effort state push; never add shell IPC latency to real work."""
    threading.Thread(target=_ipc, args=(method, payload), daemon=True,
                     name=f"assistant-hud-{method}").start()


def task_items() -> list[dict]:
    from ..runtime.task import ACTIVE, INTERRUPTED, WAITING_APPROVAL, WAITING_USER, TaskStore
    from ..core.conversations import task_progress

    visible = ACTIVE | {INTERRUPTED, WAITING_APPROVAL, WAITING_USER}
    items = []
    for task in TaskStore().list(50):
        if task.status not in visible:
            continue
        detail = task.question or (task.pending_approval or {}).get("subject") or task_progress(task)
        items.append({"id": task.id, "title": task.goal, "status": task.status, "detail": detail or ""})
    return items


def approval_items() -> list[dict]:
    """Tasks paused for the user's approval: the floating envelope's rows."""
    from ..runtime.task import WAITING_APPROVAL, TaskStore

    items = []
    for task in TaskStore().list(50):
        request = task.pending_approval or {}
        if task.status != WAITING_APPROVAL or not request:
            continue
        items.append({"id": task.id, "title": task.goal, "subject": request.get("subject", ""),
                      "risk": request.get("risk", ""), "reasons": "; ".join(request.get("reasons", [])),
                      "task_summary": request.get("task_summary", ""), "sudo_action": request.get("sudo_action", "")})
    return items


def routine_items() -> list[dict]:
    from ..core.agenda import list_jobs

    return [{"id": job.get("id", ""), "title": job.get("title", ""),
             "status": job.get("kind", "routine"), "detail": job.get("schedule", ""),
             "next": job.get("next_run", "")} for job in list_jobs(False)]


def show_tasks() -> None:
    _ipc("showTasks", {"items": task_items()})


def refresh_tasks() -> None:
    """Replace task rows without opening a HUD the user already closed, and
    show or clear the approval envelope (it is visible while any is waiting)."""
    _ipc_async("updateTasks", {"items": task_items()})
    _ipc_async("updateApprovals", {"items": approval_items()})


def show_routines() -> None:
    _ipc("showRoutines", {"items": routine_items()})


def refresh_routines() -> None:
    """Replace the rows without opening a HUD the user already closed."""
    _ipc_async("updateRoutines", {"items": routine_items()})


def hide_tasks() -> None:
    _ipc("hideTasks")


def hide_routines() -> None:
    _ipc("hideRoutines")


def open_automatic(config) -> None:
    if getattr(config, "tasks_hud_on_call", False):
        show_tasks()
    if getattr(config, "routines_hud_on_call", False):
        show_routines()
