"""Daemon liveness for systemd, and crash-loop safe mode.

systemd (systemd/omarchy-ai.service, Type=notify + WatchdogSec):
- `sd_notify()` writes one datagram to $NOTIFY_SOCKET (a path, or an
  abstract socket when it starts with "@"); without that variable -- not
  under systemd, tests, CLIs -- it does nothing. No python-systemd needed.
- `Watchdog` is a thread that sends WATCHDOG=1 only while every check it
  was given passes (the daemon's: event loop lag < 5 s, agenda heartbeat
  recent, tool runner able to start a call). A hang stops the pings, and
  systemd kills and restarts the daemon within WatchdogSec.

Safe mode (STATE_DIR/health.json, core/statefile.py):
- `on_start()` marks the process running. If the previous process never
  reached `on_clean_stop()` (SIGTERM, Ctrl-C, a normal exit), it crashed,
  was killed by the watchdog or the OOM killer: that start counts a crash.
- CRASHES_FOR_SAFE_MODE crashes within CRASH_WINDOW start the daemon in safe
  mode: voice conversations only -- no agenda heartbeat (scheduled actions,
  watches), no task resume after restart, no co-pilot handover, no update
  checks -- and the user gets an urgent notification.
- `mark_stable()` after STABLE_SECONDS of uptime forgets the crashes and
  leaves safe mode; the daemon then starts what it held back.
- `status()` is the read API for doctor (3.2), which stays available in
  safe mode.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import socket
import threading
import time
from typing import Any, Callable, Mapping

from . import statefile

log = logging.getLogger("omarchy_ai.core.health")

FILE_NAME = "health.json"
VERSION = 1
CRASH_WINDOW = 5 * 60.0
CRASHES_FOR_SAFE_MODE = 3
STABLE_SECONDS = 10 * 60.0
LOOP_LAG_LIMIT = 5.0


# ------------------------------------------------------------------ sd_notify


def _notify_address(env: Mapping[str, str]) -> str | None:
    address = env.get("NOTIFY_SOCKET", "")
    if address.startswith("@"):
        return "\0" + address[1:]  # Linux abstract namespace
    if address.startswith("/"):
        return address
    return None  # unset, or a transport (vsock:) the daemon never runs under


def sd_notify(message: str, env: Mapping[str, str] | None = None) -> bool:
    """Send `message` ("READY=1", "WATCHDOG=1\\nSTATUS=...") to systemd.
    True when it was sent; False, silently, when not under systemd."""
    address = _notify_address(os.environ if env is None else env)
    if address is None:
        return False
    try:
        with socket.socket(
            socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC
        ) as sock:
            sock.sendto(message.encode("utf-8"), address)
        return True
    except OSError:
        log.debug("sd_notify(%r) failed", message, exc_info=True)
        return False


def watchdog_seconds(env: Mapping[str, str] | None = None) -> float | None:
    """systemd's WatchdogSec for this process, or None when it has none."""
    env = os.environ if env is None else env
    try:
        usec = int(env.get("WATCHDOG_USEC", ""))
    except ValueError:
        return None
    pid = env.get("WATCHDOG_PID")
    if usec <= 0 or (pid and pid != str(os.getpid())):
        return None
    return usec / 1_000_000


Check = Callable[[], str | None]


def loop_check(loop: asyncio.AbstractEventLoop, limit: float = LOOP_LAG_LIMIT) -> Check:
    """A check that fails when `loop` takes longer than `limit` to run a
    callback (blocked by a synchronous call, deadlocked, or closed)."""

    def check() -> str | None:
        ran = threading.Event()
        try:
            loop.call_soon_threadsafe(ran.set)
        except RuntimeError:
            return "event loop closed"
        if not ran.wait(limit):
            return f"event loop lag over {limit:g} s"
        return None

    return check


class Watchdog:
    """Pings WATCHDOG=1 every `interval` seconds while all `checks` pass."""

    def __init__(
        self,
        checks: list[Check],
        interval: float,
        notify: Callable[[str], bool] = sd_notify,
    ) -> None:
        self.checks = checks
        self.interval = interval
        self._notify = notify
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.failing: str | None = None

    def beat(self) -> bool:
        """Run the checks once; ping only when all pass."""
        for check in self.checks:
            try:
                problem = check()
            except Exception as exc:  # noqa: BLE001 -- a broken check is a failing one
                problem = f"{type(exc).__name__}: {exc}"
            if problem:
                if problem != self.failing:
                    log.warning("watchdog: not pinging systemd (%s)", problem)
                self.failing = problem
                return False
        if self.failing:
            log.info("watchdog: healthy again (%s)", self.failing)
            self.failing = None
        self._notify("WATCHDOG=1")
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            self.beat()
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="systemd-watchdog", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def start_watchdog(
    checks: list[Check], env: Mapping[str, str] | None = None
) -> Watchdog | None:
    """A running Watchdog pinging three times per WatchdogSec, or None when
    systemd set no watchdog for this process."""
    seconds = watchdog_seconds(env)
    if seconds is None:
        return None
    dog = Watchdog(checks, interval=max(0.5, seconds / 3))
    dog.start()
    return dog


# ------------------------------------------------------------------ safe mode


# This process runs in safe mode (set by the daemon; read by what it holds
# back, e.g. runtime/service.py's task resume after a restart).
active = False


def path() -> Path:
    from .. import config

    return config.STATE_DIR / FILE_NAME


def _doc(target: Path | None):
    return statefile.locked_json(target or path(), {}, expect=dict, version=VERSION)


def on_start(
    now: float | None = None, pid: int | None = None, target: Path | None = None
) -> dict[str, Any]:
    """Record this start; returns status(), with "entered" True when this
    start put the daemon into safe mode."""
    now = time.time() if now is None else now
    with _doc(target) as doc:
        data = doc.data
        crashes = [
            float(t) for t in data.get("crashes", []) if isinstance(t, (int, float))
        ]
        previous = data.get("running")
        if previous:
            crashes.append(now)
            log.warning(
                "the previous daemon (pid %s) did not stop cleanly: counted as a crash",
                previous.get("pid") if isinstance(previous, dict) else "?",
            )
        crashes = [t for t in crashes if now - t <= CRASH_WINDOW]
        entered = False
        if not data.get("safe_mode") and len(crashes) >= CRASHES_FOR_SAFE_MODE:
            data["safe_mode"] = {"since": now, "crashes": len(crashes)}
            entered = True
        data["crashes"] = crashes
        data["running"] = {
            "pid": os.getpid() if pid is None else pid,
            "started_at": now,
        }
    result = status(target)
    result["entered"] = entered
    return result


def on_clean_stop(target: Path | None = None) -> None:
    """The daemon is stopping on purpose: its next start is not a crash."""
    try:
        with _doc(target) as doc:
            doc.data["running"] = None
    except (OSError, statefile.StateFileError):
        log.warning("could not record a clean stop", exc_info=True)


def mark_stable(target: Path | None = None) -> bool:
    """STABLE_SECONDS of uptime: forget the crashes and leave safe mode.
    True when safe mode was on."""
    with _doc(target) as doc:
        was = bool(doc.data.get("safe_mode"))
        doc.data["crashes"] = []
        doc.data["safe_mode"] = None
    return was


def status(target: Path | None = None) -> dict[str, Any]:
    """{"safe_mode": bool, "since", "crashes": recent crash count} for doctor."""
    data = statefile.read_json(target or path(), {}, expect=dict, version=VERSION)
    safe = data.get("safe_mode") or None
    return {
        "safe_mode": bool(safe),
        "since": safe.get("since") if isinstance(safe, dict) else None,
        "crashes": len(data.get("crashes") or []),
    }


SAFE_MODE_TITLE = "Omarchy AI is in safe mode"
SAFE_MODE_BODY = (
    "The assistant crashed {crashes} times in {minutes} minutes. Voice still works; "
    "scheduled actions, watches and automatic task resume are off until it has run "
    "{stable} minutes without a crash. Check: journalctl --user -u omarchy-ai"
)


def notify_safe_mode(crashes: int) -> None:
    """The urgent notification for entering safe mode (core/outbox.py: desktop and phone)."""
    from . import outbox

    outbox.alert(
        SAFE_MODE_TITLE,
        SAFE_MODE_BODY.format(
            crashes=crashes,
            minutes=int(CRASH_WINDOW // 60),
            stable=int(STABLE_SECONDS // 60),
        ),
        urgent=True,
    )
