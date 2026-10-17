"""Cooperative cancellation for one tool call.

execution/runner.py gives every call a CancelToken and makes it current in
the thread that runs the call. A deadline, the user's "stop", a hangup or a
task cancel calls `cancel()`; whatever the call registered with
`on_cancel()` then runs (a worker process group is killed, a browser or
desktop loop is told to stop, core/effects.py marks what may have happened
as uncertain). Code inside a call can also check it: `checkpoint()` raises
`Cancelled` once the current call is cancelled.

Kept in core/ because core/effects.py consults it; execution/runner.py
re-exports it.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from typing import Callable

log = logging.getLogger("omarchy_ai.core.cancel")


class Cancelled(Exception):
    """The current call was cancelled (`reason`: timeout, stop, hangup, ...)."""

    def __init__(self, reason: str = "cancelled") -> None:
        super().__init__(reason)
        self.reason = reason


class CancelToken:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._callbacks: list[Callable[[], None]] = []
        self.reason = ""
        # Effects (core/effects.py) performed under this token, oldest first.
        self.effect_ids: list[str] = []

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str = "cancelled") -> bool:
        """Cancel once; returns whether this call did it. Callbacks run here,
        in the caller's thread, each on its own (one failing does not stop
        the others)."""
        with self._lock:
            if self._event.is_set():
                return False
            self.reason = reason
            self._event.set()
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            _call(callback)
        return True

    def on_cancel(self, callback: Callable[[], None]) -> None:
        """Run `callback` when this token is cancelled (at once if it is)."""
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(callback)
                return
        _call(callback)

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled(self.reason)

    def wait(self, timeout: float | None = None) -> bool:
        """Wait until cancelled or `timeout` passes; returns `cancelled`."""
        return self._event.wait(timeout)


def _call(callback: Callable[[], None]) -> None:
    try:
        callback()
    except Exception:  # noqa: BLE001 -- one hook must not keep the others from running
        log.warning("cancel callback failed", exc_info=True)


CURRENT: contextvars.ContextVar[CancelToken | None] = contextvars.ContextVar(
    "omarchy_cancel_token", default=None
)


def current() -> CancelToken | None:
    """The token of the tool call running in this thread, if any."""
    return CURRENT.get()


def checkpoint() -> None:
    """Raise Cancelled if the current call was cancelled; else nothing."""
    token = CURRENT.get()
    if token is not None:
        token.check()
