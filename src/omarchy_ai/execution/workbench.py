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


# Requested name -> the terminal it runs in (terminal_task reuses an idle one).
aliases: dict[str, str] = {}


def resolve(name: str) -> str:
    """The terminal a name refers to, following reuse aliases."""
    value = slug(name)
    return aliases.get(value, value)


def _session(name: str) -> str:
    return PREFIX + resolve(name)


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
    _fixed_size(session)
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


def _fixed_size(session: str) -> None:
    """Keep the session 200x50 whatever its viewer window is. Journal
    2026-09-27 08:18-08:20: ten viewer windows tiled on one workspace shrank
    their sessions to 1x1 and 1x26 (tmux follows the attached client), so git
    output was wrapped one character per line and terminal_read returned
    'und.', 'main', '~ ❯~ ❯': she never saw what her commands printed. A
    smaller viewer now pans over the fixed window instead."""
    _tmux("set-option", "-w", "-t", session, "window-size", "manual")
    _tmux("resize-window", "-t", session, "-x", "200", "-y", "50")


READ_WAIT_SECONDS = 8.0


def _capture(session: str, lines: int):
    out = _tmux("capture-pane", "-p", "-J", "-t", session, "-S", f"-{int(lines)}")
    return out.returncode, "\n".join(l.rstrip() for l in out.stdout.splitlines()).strip()


def _children(pid: int) -> list[int]:
    try:
        return [int(c) for c in Path(f"/proc/{pid}/task/{pid}/children").read_text().split()]
    except (OSError, ValueError):
        return []


def _busy(session: str) -> bool:
    """A command is running: the pane's shell has a child process. The pane
    process may be a `script` wrapper around the shell (it is on this
    machine), so step through it. #{pane_current_command} is no use here: an
    idle pane reports 'tmux'."""
    out = _tmux("display-message", "-p", "-t", session, "#{pane_pid}")
    try:
        pid = int(out.stdout.strip())
        if Path(f"/proc/{pid}/comm").read_text().strip() == "script":
            pid = _children(pid)[0]
    except (ValueError, OSError, IndexError):
        return False
    return bool(_children(pid))


def read(name: str, lines: int = 120, settle: bool = False) -> ActionResult:
    """The pane's text; with `settle`, after the running command has returned
    to the shell (up to READ_WAIT_SECONDS). The live model reads straight
    after terminal_task (0-1s in every journal read), before git, find or
    grep have printed anything."""
    if not exists(name):
        return ActionResult(False, f"no assistant terminal named {slug(name)!r}")
    session = _session(name)
    _fixed_size(session)
    deadline = time.monotonic() + (READ_WAIT_SECONDS if settle else 0)
    time.sleep(0.3 if settle else 0)  # a command sent just now may not have started yet
    while _busy(session) and time.monotonic() < deadline:
        time.sleep(0.25)
    code, text = _capture(session, max(int(lines), 40))
    if settle and _busy(session):
        text += "\n[still running: read again later for the rest]"
    return ActionResult(code == 0, text[-6000:] or "(no output yet)")


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
    actual = resolve(name)
    _tmux("kill-session", "-t", _session(name))
    for alias in [a for a, target in aliases.items() if target == actual or a == actual]:
        del aliases[alias]
    return ActionResult(True, f"closed assistant terminal {actual!r}")


def idle_terminal() -> str | None:
    """The most recently used assistant terminal with no command running.
    Journal 2026-09-27: one terminal per command (check-git-remote, git-log,
    git-pull-fix, git-push, ... ten by 08:20), all tiled on the user's
    screen and never closed."""
    out = _tmux("list-sessions", "-F", "#{session_activity} #{session_name}")
    if out.returncode:
        return None
    rows = sorted((line.split(" ", 1) for line in out.stdout.splitlines() if " " in line), reverse=True)
    for _, session in rows:
        if session.startswith(PREFIX) and not _busy(session):
            return session[len(PREFIX):]
    return None


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
