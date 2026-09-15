"""Thin wrapper around the omarchy-ai.settings Quickshell panel's IPC,
for exactly one thing: the bar icon's live-status dot (red = idle, green
= an actual gpt-live-1 conversation is connected right now). User's own
request. Same `omarchy-shell -q <target> <method> '<json>'` pattern as
watchdog.py, but a deliberately separate module/IPC target — this
indicator must reflect the daemon's true connection state regardless of
whether the user has the optional Watch Dogs overlay
(watchdog_enabled) turned on or off, so it can't live behind that gate.

Called only twice per session (connect, hangup) — not a tight loop like
watchdog.level() — so a brief blocking subprocess.run is fine here,
same as watchdog.py's own state()/tool_call()/start()/stop(). Every
call is wrapped so a missing/failed IPC call just logs and moves on:
this is a cosmetic indicator, never allowed to affect the real
conversation.
"""

from __future__ import annotations

import json
import logging
import subprocess

log = logging.getLogger("omarchy_ai.voice.status_icon")

_TIMEOUT = 5


def set_live(is_live: bool) -> None:
    payload = json.dumps({"live": bool(is_live)})
    argv = ["omarchy-shell", "-q", "omarchy-ai.settings", "setLive", payload]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=_TIMEOUT, check=False
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            log.warning("status_icon setLive ipc failed: %s", err)
    except FileNotFoundError:
        log.warning("status_icon ipc skipped: omarchy-shell not found")
    except subprocess.TimeoutExpired:
        log.warning("status_icon setLive ipc timed out")
    except Exception:  # noqa: BLE001 — cosmetic, never take the session down
        log.debug("status_icon setLive ipc raised", exc_info=True)
