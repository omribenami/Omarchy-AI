"""Keep the desktop awake while a paired phone is connected.

2026-09-27 08:50: the user closed the lid and opened the phone's Mirror.
The idle timer had locked the screen (lock-timeout at 08:50:07) and the
panel was powered off (LVDS-1 dpmsStatus 0), so every screenshot the mirror
took hung until its timeout and the phone showed nothing. While a phone is
connected the idle lock and screensaver are now held off with Omarchy's own
Stay Awake toggle (`omarchy toggle idle stay-awake`), and the panel is
powered on. When the last phone goes quiet, idle is allowed again -- only if
we turned Stay Awake on: a Stay Awake the user set themselves is left alone.

"Connected" = a paired request within CONNECTED_SECONDS. The open phone
page polls /api/audio/status every 2s; a locked phone or closed tab stops.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path

from ..execution.desktop_env import desktop_env

log = logging.getLogger(__name__)

CONNECTED_SECONDS = 20.0
CHECK_SECONDS = 5.0
# Present while *we* hold Stay Awake: survives a daemon restart or crash, so
# the next start can release it instead of leaving the machine awake forever.
_OWNED = Path.home() / ".local/state/omarchy-ai/phone-stay-awake"

_lock = threading.Lock()
_last_seen = 0.0
_thread: threading.Thread | None = None
_stop = threading.Event()


def seen() -> None:
    """A paired phone just made a request."""
    global _last_seen
    with _lock:
        _last_seen = time.monotonic()
    if _thread is not None and not _holding:
        # First sign of a phone: react now, not at the next check.
        threading.Thread(target=_update, daemon=True, name="phone-presence-now").start()


def connected() -> bool:
    with _lock:
        return bool(_last_seen) and time.monotonic() - _last_seen < CONNECTED_SECONDS


def _run(*argv: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False, env=desktop_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("phone presence: %s failed: %s", " ".join(argv), exc)
        return None


def _stay_awake_on() -> bool:
    r = _run("omarchy", "toggle", "idle", "status")
    return bool(r and '"enabled":true' in r.stdout.replace(" ", ""))


def _hold() -> None:
    if not _stay_awake_on():
        _run("omarchy", "toggle", "idle", "stay-awake")
        _OWNED.parent.mkdir(parents=True, exist_ok=True)
        _OWNED.touch()
        log.info("phone presence: phone connected; Stay Awake on (idle lock held off)")
    else:
        log.info("phone presence: phone connected; Stay Awake was already on, left as is")
    # A panel already powered off stays off without input; power it on.
    from ..execution.actions import _hyprctl_dispatch
    _hyprctl_dispatch('hl.dsp.dpms({ action = "enable" })', ["dpms", "on"])


def _release() -> None:
    if _OWNED.exists():
        _run("omarchy", "toggle", "idle", "allow-idle")
        _OWNED.unlink(missing_ok=True)
        log.info("phone presence: no phone connected; Stay Awake off (idle lock allowed again)")


_update_lock = threading.Lock()
_holding = False


def _update() -> None:
    global _holding
    with _update_lock:
        now = connected()
        if now and not _holding:
            _hold()
        elif not now and (_holding or _OWNED.exists()):
            _release()
        _holding = now


def _loop() -> None:
    while not _stop.wait(CHECK_SECONDS):
        try:
            _update()
        except Exception:  # never take the phone bridge down over this
            log.exception("phone presence check failed")


def start() -> None:
    global _thread
    if _thread is not None:
        return
    _stop.clear()
    _update()  # releases a Stay Awake left held by a previous run
    _thread = threading.Thread(target=_loop, daemon=True, name="phone-presence")
    _thread.start()


def stop() -> None:
    global _thread, _holding
    _stop.set()
    with _update_lock:
        _release()
        _holding = False
    _thread = None
