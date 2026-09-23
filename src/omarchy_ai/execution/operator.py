"""Who is operating the desktop right now: the user, or only the assistant.

Co-pilot rule (user, 2026-09-23): while the assistant is the only operator
her actions must be shown on the user's screen; while the user is working,
she continues in the background so they never interrupt each other; after
~30s without user input, her work is handed over to the user's screen.

The signal is the watchdog Quickshell plugin's IdleMonitor (ext-idle-notify,
timeout 30s). Her own terminal and browser work goes through tmux and CDP,
not synthetic keystrokes, so it does not count as user activity.
"""
from __future__ import annotations

import subprocess
import time

_CACHE_SECONDS = 1.5
_cache: tuple[float, str | None] = (0.0, None)


def _state() -> str | None:
    """"active" (input in the last 5s), "recent" (5-30s) or "idle" (30s+);
    None when the shell or plugin is unavailable."""
    global _cache
    now = time.monotonic()
    if now - _cache[0] < _CACHE_SECONDS:
        return _cache[1]
    try:
        # No -q: omarchy-shell -q suppresses ALL output, including the answer.
        proc = subprocess.run(["omarchy-shell", "watchdog", "operator"], capture_output=True,
                              text=True, timeout=3, check=False)
        answer = proc.stdout.strip().strip('"') if proc.returncode == 0 else ""
        value = answer if answer in ("active", "recent", "idle") else None
    except (OSError, subprocess.SubprocessError):
        value = None
    _cache = (now, value)
    return value


def user_active() -> bool | None:
    """True: the user is operating right now (input in the last 5s).
    False: not. None: unknown."""
    state = _state()
    return None if state is None else state == "active"


def user_idle() -> bool:
    """30s without input: the moment to hand background work over."""
    return _state() == "idle"


def visible(show) -> bool:
    """Whether an action should be shown on the user's screen.

    show: True/"yes" (the user asked to see it), False/"no" (the user asked
    for background), or "auto": visible unless the user is working. Unknown
    counts as visible, which is how the assistant always behaved before."""
    if show in (True, "yes", "true", "show"):
        return True
    if show in (False, "no", "false", "background"):
        return False
    return user_active() is not True
