"""Thin wrapper around the omarchy-ai.tv-discovery Quickshell plugin's IPC
-- the small centered "which TV?" overlay shown while `start_casting` is
resolving a target. Same IPC pattern as `voice/watchdog.py` (`omarchy-shell
-q tvDiscovery <method> '<json>'`), and the same "this is a cosmetic
add-on, never allowed to block or take down a real cast attempt" discipline
-- every call here logs and moves on rather than raising.

Owns the "real-time while the window is open" requirement as a background
poll thread (registry.refresh() + push every poll_interval seconds) rather
than a real mDNS event stream -- see registry.py's module docstring and
the plan this was built from for why that's the deliberate trade-off here.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading

from . import registry

log = logging.getLogger("omarchy_ai.display.tv_overlay")

_TIMEOUT = 5
_TARGET = "tvDiscovery"

_poll_stop: threading.Event | None = None
_poll_thread: threading.Thread | None = None
# Reentrant: show_and_track() calls hide_tracking() itself to stop any
# previous poll loop before starting a new one, both under this lock.
_lock = threading.RLock()


def _ipc(*args: str) -> None:
    argv = ["omarchy-shell", "-q", _TARGET, *args]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=_TIMEOUT, check=False
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            log.warning("tvDiscovery ipc %r failed: %s", args[0] if args else "?", err)
    except FileNotFoundError:
        log.warning("tvDiscovery ipc skipped: omarchy-shell not found")
    except subprocess.TimeoutExpired:
        log.warning("tvDiscovery ipc %r timed out", args[0] if args else "?")
    except Exception:  # noqa: BLE001 -- cosmetic overlay, never take casting down
        log.debug("tvDiscovery ipc %r raised", args[0] if args else "?", exc_info=True)


def _push(method: str, devices: list[dict]) -> None:
    _ipc(method, json.dumps({"devices": devices}))


def _poll_loop(stop: threading.Event, poll_interval: float) -> None:
    while not stop.wait(poll_interval):
        _push("update", registry.refresh())


def show_and_track(poll_interval: float = 4.0) -> None:
    """Opens the overlay with a real, fresh snapshot and starts a
    background thread keeping it live-updated until hide_tracking()."""
    global _poll_stop, _poll_thread
    with _lock:
        hide_tracking()
        devices = registry.refresh()
        _push("show", devices)
        _poll_stop = threading.Event()
        _poll_thread = threading.Thread(
            target=_poll_loop, args=(_poll_stop, poll_interval), daemon=True
        )
        _poll_thread.start()


def push_update() -> None:
    """Immediate out-of-band update -- used right after mark_connecting()
    so the overlay reflects a resolved target instantly rather than
    waiting for the next poll tick."""
    _push("update", registry.snapshot())


def select(address: str) -> None:
    """A target was resolved (voice or a manual click) -- the overlay
    highlights that row and self-times its own dismiss animation."""
    _ipc("select", json.dumps({"address": address}))


def hide() -> None:
    """Stop tracking and close immediately."""
    hide_tracking()
    _ipc("hide")


def hide_tracking() -> None:
    global _poll_stop, _poll_thread
    with _lock:
        if _poll_stop is not None:
            _poll_stop.set()
        if _poll_thread is not None:
            _poll_thread.join(timeout=1)
        _poll_stop = None
        _poll_thread = None
