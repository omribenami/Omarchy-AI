"""Harness probes that do not depend on the prompt text.

These call the current runtime. A failure here is missing enforcement from
the harness plan, not a difference between legacy and v2.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def hanging_tool_cancelled(deadline: float = 0.4) -> tuple[bool, str]:
    """True when the live-session dispatcher finishes a hung tool within ``deadline``."""
    from omarchy_ai.voice.tool_dispatch import ToolDispatchMixin

    stop = threading.Event()

    def run_action(name, args=None):
        if name == "list_windows":
            return SimpleNamespace(ok=True, message="[]")
        stop.wait(2.0)
        return SimpleNamespace(ok=True, message="late")

    class Session(ToolDispatchMixin):
        def __init__(self):
            self._tool_lock = None
            self._hangup = asyncio.Event()
            self._demo_waiting_for_action = asyncio.Event()
            self._watchdog_on = False
            self._action_log: list = []
            self._dc = None
            self._input_guard = SimpleNamespace(
                run=lambda execute, name, args: execute(name, args)
            )

        def _set_watchdog_state(self, state):
            return None

    async def probe() -> bool:
        session = Session()
        with patch("omarchy_ai.voice.tool_dispatch.run_action", run_action):
            task = asyncio.create_task(
                session._run_tool_call("c1", "browser_task", {"goal": "hang"})
            )
            done, _pending = await asyncio.wait({task}, timeout=deadline)
            task.cancel()
            return task in done

    try:
        finished = asyncio.run(probe())
    finally:
        stop.set()
    if finished:
        return True, f"hung tool finished within {deadline}s"
    return False, f"hung tool still running after {deadline}s; dispatch has no deadline"


def crash_does_not_double_send(directory: Path) -> tuple[bool, str]:
    """True when a second attempt of the same send is not delivered.

    The current pending-action store has no idempotency key. A crash after
    the provider accepts the send and before ``finish`` leaves the action
    ``running``, and a retry ``create``s a new action and sends again.
    """
    from omarchy_ai.core import pending_actions

    sent: list[str] = []
    path = directory / "pending_actions.json"

    def attempt() -> None:
        with patch.object(pending_actions, "PATH", path):
            aid = pending_actions.create(
                {
                    "kind": "gmail_send",
                    "recipient": "alex@example.com",
                    "subject": "Running late",
                    "body": "I'll be late",
                }
            )
            claimed = pending_actions.claim(aid)
            if claimed is None:
                return
            sent.append(str(claimed["id"]))
            # Crash window: the provider has the message, finish() never runs.

    attempt()
    attempt()
    if len(sent) == 1:
        return True, "one delivery"
    return False, f"{len(sent)} deliveries for one logical send"


def high_risk_not_approved_by_voice(directory: Path) -> tuple[bool, str]:
    """True when a spoken yes does not grant a HIGH-risk approval."""
    from omarchy_ai.runtime.runtime import TaskRuntime
    from omarchy_ai.runtime.task import WAITING_APPROVAL, Task, TaskStore

    store = TaskStore(directory)
    task = Task(
        id="t-high",
        goal="remove the package",
        workspace=str(directory),
        status=WAITING_APPROVAL,
        pending_approval={
            "fingerprint": "fp-high",
            "kind": "command",
            "subject": "sudo pacman -Rns foobar",
            "risk": "HIGH",
            "reasons": ["runs as root"],
        },
    )
    store.save(task)
    runtime = TaskRuntime(store=store, executors={}, notify=False)
    result = runtime.respond("t-high", approve=True, channel="voice", background=False)
    reloaded = store.load("t-high")
    granted = "fp-high" in (reloaded.grants if reloaded else [])
    message = str(result.get("message") or "")
    if not granted and "HIGH" in message:
        return True, message
    return False, message or "voice approval granted the HIGH action"
