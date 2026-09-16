"""Cross-process casting ownership and readiness, independent of the voice daemon."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import threading

UNIT = "omarchy-ai-cast.service"
_state_lock = threading.Lock()


def runtime_dir() -> Path:
    path = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "omarchy-ai-cast"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


@contextlib.contextmanager
def locked():
    with (runtime_dir() / "control.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another casting operation is in progress; try again shortly") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def read_state() -> dict:
    try:
        return json.loads((runtime_dir() / "state.json").read_text())
    except (OSError, ValueError):
        return {}


def write_state(state: str, detail: str = "") -> None:
    path = runtime_dir() / "state.json"
    tmp = path.with_suffix(".tmp")
    with _state_lock:
        tmp.write_text(json.dumps({"state": state, "detail": detail, "time": time.time()}))
        tmp.replace(path)


def target() -> str | None:
    try:
        return (runtime_dir() / "target").read_text().strip() or None
    except OSError:
        return None


def active() -> bool:
    result = subprocess.run(["systemctl", "--user", "show", UNIT, "-p", "ActiveState", "--value"],
                            capture_output=True, text=True, timeout=5)
    return result.returncode == 0 and result.stdout.strip() in {"active", "activating"}


def stop() -> None:
    if active():
        subprocess.run(["systemctl", "--user", "stop", UNIT], check=True, timeout=15,
                       capture_output=True, text=True)
    (runtime_dir() / "target").unlink(missing_ok=True)
    write_state("stopped")


def desktop_environment() -> dict[str, str]:
    env = os.environ.copy()
    runtime = Path(env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    display = env.get("WAYLAND_DISPLAY")
    if not display or not (runtime / display).exists():
        sockets = sorted((p for p in runtime.glob("wayland-*") if p.is_socket()),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        if not sockets:
            raise RuntimeError("no running Wayland desktop found for screen capture")
        env["WAYLAND_DISPLAY"] = sockets[0].name
    env["XDG_RUNTIME_DIR"] = str(runtime)
    return env


def local_address(host: str) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((host, 5555))
        return sock.getsockname()[0]


def start(argv: list[str], cwd: str, host: str) -> None:
    stop()
    env = desktop_environment()
    (runtime_dir() / "target").write_text(host)
    write_state("starting")
    subprocess.run(["systemctl", "--user", "reset-failed", UNIT], capture_output=True, timeout=5)
    cmd = ["systemd-run", "--user", "--collect", f"--unit={UNIT}",
           f"--working-directory={cwd}", "--property=Restart=on-failure",
           "--property=RestartSec=2", "--property=StartLimitIntervalSec=180",
           "--property=StartLimitBurst=3", "--property=TimeoutStopSec=8"]
    for key in ("PATH", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "PULSE_SERVER"):
        if key in env:
            cmd.append(f"--setenv={key}={env[key]}")
    subprocess.run([*cmd, "--", *argv], check=True, capture_output=True, text=True, timeout=10)


def wait_connected(timeout: float = 25) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = read_state()
        if active() and state.get("state") == "connected":
            return True, "WebRTC connected"
        if not active():
            return False, state.get("detail") or "sender exited; see journalctl --user -u " + UNIT
        time.sleep(.25)
    return False, read_state().get("detail") or "timed out waiting for the receiver's WebRTC connection"
