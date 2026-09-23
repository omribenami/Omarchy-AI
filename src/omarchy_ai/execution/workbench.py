"""The assistant's own terminals: tmux sessions she types into without the keyboard.

Hyprland has one input seat, so injecting keystrokes (wtype) always lands in
the focused window: in parallel with the user that means typing into their
window or stealing focus. A tmux session takes keys through `tmux
send-keys`, needs no focus at all, and can be watched live in a terminal
window attached to it. So one mechanism serves both co-pilot modes: the
viewer window is on the user's workspace when she is the only operator, and
absent (or on another workspace) while the user works; the work itself never
depends on where the window is.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from .actions import ActionResult, _desktop_env, _run, _run_detached

PREFIX = "oai-"
# Auto-mode sessions started while the user was working: shown on the
# user's screen once they stop touching it (the daemon's handover loop).
pending_handover: set[str] = set()
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")


def _tmux(*args, input_text=None, timeout=5):
    return subprocess.run(["tmux", *args], capture_output=True, text=True, input=input_text,
                          timeout=timeout, check=False)


def slug(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", str(name or "task").lower()).strip("-")[:30].strip("-") or "task"
    if not NAME.fullmatch(value):
        raise ValueError("terminal name needs letters or digits")
    return value


def _session(name: str) -> str:
    return PREFIX + slug(name)


def exists(name: str) -> bool:
    return _tmux("has-session", "-t", _session(name)).returncode == 0


def sessions() -> list[str]:
    out = _tmux("list-sessions", "-F", "#{session_name}")
    return [s[len(PREFIX):] for s in out.stdout.split() if s.startswith(PREFIX)] if out.returncode == 0 else []


def start(name: str) -> ActionResult:
    if shutil.which("tmux") is None:
        return ActionResult(False, "tmux is not installed")
    session = _session(name)
    if exists(name):
        return ActionResult(True, f"terminal {slug(name)!r} already running")
    made = _tmux("new-session", "-d", "-s", session, "-x", "200", "-y", "50", "-c", str(Path.home()))
    if made.returncode:
        return ActionResult(False, f"could not start terminal: {made.stderr.strip()}")
    # A recognisable window title in the viewer: "Omarchy AI · <name>".
    _tmux("set-option", "-t", session, "set-titles", "on")
    _tmux("set-option", "-t", session, "set-titles-string", f"Omarchy AI · {slug(name)}")
    return ActionResult(True, f"terminal {slug(name)!r} started")


def send(name: str, text: str, enter: bool = True) -> ActionResult:
    if not exists(name):
        return ActionResult(False, f"no assistant terminal named {slug(name)!r}")
    session = _session(name)
    # Literal keys (-l): no tmux key-name interpretation of the payload.
    for line_no, line in enumerate(str(text).split("\n")):
        if line_no:
            _tmux("send-keys", "-t", session, "Enter")
        if line:
            sent = _tmux("send-keys", "-t", session, "-l", "--", line)
            if sent.returncode:
                return ActionResult(False, f"could not type into {slug(name)!r}: {sent.stderr.strip()}")
    if enter:
        _tmux("send-keys", "-t", session, "Enter")
    return ActionResult(True, f"typed into assistant terminal {slug(name)!r}")


def read(name: str, lines: int = 120) -> ActionResult:
    if not exists(name):
        return ActionResult(False, f"no assistant terminal named {slug(name)!r}")
    out = _tmux("capture-pane", "-p", "-J", "-t", _session(name), "-S", f"-{int(lines)}")
    text = "\n".join(l.rstrip() for l in out.stdout.splitlines()).strip()
    return ActionResult(out.returncode == 0, text[-6000:] or "(no output yet)")


def submit_password(name: str, password: str) -> ActionResult:
    """Paste the keyring password through a tmux buffer fed on stdin, so it
    never appears in any command line or process list, then delete it."""
    if not exists(name):
        return ActionResult(False, f"no assistant terminal named {slug(name)!r}")
    session, buffer = _session(name), "oai-secret"
    loaded = _tmux("load-buffer", "-b", buffer, "-", input_text=password)
    if loaded.returncode:
        return ActionResult(False, "could not hand the password to the terminal")
    _tmux("paste-buffer", "-d", "-b", buffer, "-t", session)
    _tmux("send-keys", "-t", session, "Enter")
    return ActionResult(True, "approved sudo password submitted")


def stop(name: str) -> ActionResult:
    if not exists(name):
        return ActionResult(True, "already closed")
    _tmux("kill-session", "-t", _session(name))
    return ActionResult(True, f"closed assistant terminal {slug(name)!r}")


def _clients():
    r = _run(["hyprctl", "clients", "-j"])
    try:
        return json.loads(r.message) if r.ok else []
    except ValueError:
        return []


def viewer(name: str) -> dict | None:
    """The terminal window attached to this session, if one is open."""
    out = _tmux("list-clients", "-t", _session(name), "-F", "#{client_pid}")
    pids = {int(p) for p in out.stdout.split() if p.isdigit()} if out.returncode == 0 else set()
    parents = set()
    for pid in pids:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            parents.add(int(stat[1]))
        except (OSError, ValueError, IndexError):
            continue
    return next((c for c in _clients() if c.get("pid") in pids | parents), None)


def show(name: str) -> ActionResult:
    """Make her work visible on the user's current workspace."""
    if not exists(name):
        return ActionResult(False, f"no assistant terminal named {slug(name)!r}")
    window = viewer(name)
    ws = _run(["hyprctl", "activeworkspace", "-j"])
    try:
        current = json.loads(ws.message).get("id") if ws.ok else None
    except ValueError:
        current = None
    if window is not None:
        if current is not None and (window.get("workspace") or {}).get("id") != current:
            from .actions import move_window_to_workspace
            move_window_to_workspace({"number": current, "target": window["address"]})
        return ActionResult(True, f"assistant terminal {slug(name)!r} is on screen")
    if shutil.which("omarchy-launch-terminal") is None:
        return ActionResult(False, "omarchy-launch-terminal is unavailable")
    _run_detached(["omarchy-launch-terminal", "tmux", "attach-session", "-t", _session(name)])
    for _ in range(30):
        if viewer(name) is not None:
            return ActionResult(True, f"assistant terminal {slug(name)!r} is on screen")
        time.sleep(0.1)
    return ActionResult(False, "viewer window did not appear")


def hide(name: str) -> ActionResult:
    """Close only the viewer window; the work keeps running in tmux."""
    window = viewer(name)
    if window is None:
        return ActionResult(True, "not on screen")
    _tmux("detach-client", "-s", _session(name))
    return ActionResult(True, f"assistant terminal {slug(name)!r} continues in the background")
