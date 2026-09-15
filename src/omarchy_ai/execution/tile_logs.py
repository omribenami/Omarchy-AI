"""Per-terminal-window ("tile") output logs.

User's own framing of the real problem: to answer "what happened in that
terminal" the assistant otherwise has to fall back to describe_screen (a
multi-second vision API call) — expensive and slow for something that's
just text. The fix: a cache dir, one log file per open tile, named after
the tile, auto-created when it opens and auto-deleted when it closes, so
the assistant can just read a file instead.

Mechanism: `script(1)` (util-linux, already on every Arch/Omarchy install)
records a full pty session — everything printed, plus local echo of what
was typed — to a plain text file, in real time (`--flush`). A background
thread correlates the newly-launched terminal with its real Hyprland
window (via `hyprctl clients -j`, comparing before/after snapshots — the
process this module launches execs through several layers before
Hyprland maps an actual window, so matching by "new address of a known
terminal class" is more reliable than trying to track a single PID
through that chain) to rename the log to the window's real title, then
polls for that window disappearing to delete it.

Scope, deliberately: only terminals opened via this project's own
open_terminal action, end to end within this process's control. Covering
every terminal the user opens by hand too would mean changing what
command actually runs when a new terminal window starts — editing the
terminal emulator's own shell-launch config, or a shell rc hook — a real,
live change to the user's actual terminal/shell environment that wasn't
made here; see STATUS.md for the concrete next step if that's wanted.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from rapidfuzz import fuzz

from ..config import TILE_LOG_DIR

log = logging.getLogger("omarchy_ai.execution.tile_logs")

_DISCOVERY_POLL_SECONDS = 0.5
_DISCOVERY_TIMEOUT_SECONDS = 8.0
_CLOSE_POLL_SECONDS = 2.0
# Every terminal actually available on this machine (see STATUS.md's
# toolchain notes) plus the other common ones, so this doesn't silently
# stop working if the user switches terminals later.
_TERMINAL_CLASSES = {"foot", "footclient", "ghostty", "Alacritty", "kitty"}

_lock = threading.Lock()
_tracked: dict[str, Path] = {}  # Hyprland window address -> log file path
_titles: dict[str, str] = {}  # window address -> last known title


def _sanitize(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._ -]+", "", name).strip()
    name = re.sub(r"\s+", "_", name)
    return name[:80] or "terminal"


def _clients() -> list[dict]:
    try:
        r = subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return []
        return json.loads(r.stdout or "[]")
    except (subprocess.TimeoutExpired, ValueError, FileNotFoundError):
        return []


def start_terminal_log() -> tuple[Path, list[str]]:
    """Call before launching a terminal. Returns (initial_log_path,
    argv_prefix) — prepend argv_prefix to the terminal's launch command
    (it runs `script`, recording the session to initial_log_path instead
    of a bare shell) — and starts the background thread that renames the
    log to the real window's title once it appears, then deletes it once
    that window closes."""
    TILE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    initial = TILE_LOG_DIR / f".pending-{uuid.uuid4().hex[:8]}.log"
    initial.touch()
    before = {c.get("address") for c in _clients()}
    threading.Thread(target=_track, args=(initial, before), daemon=True).start()
    shell = os.environ.get("SHELL", "/bin/bash")
    # -q quiet (no Script started/done banner noise), -e propagate the
    # shell's own exit code rather than script's, -f flush after every
    # write so the log is readable in real time, not just after the
    # session ends, -c run this command instead of an interactive-shell
    # default (bundled short opts; script's own getopt allows it, -c's
    # argument is the next token).
    argv_prefix = ["script", "-qefc", shell, str(initial)]
    return initial, argv_prefix


def _track(initial: Path, before: set) -> None:
    address = None
    title = None
    deadline = time.monotonic() + _DISCOVERY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(_DISCOVERY_POLL_SECONDS)
        for c in _clients():
            addr = c.get("address")
            if addr and addr not in before and c.get("class") in _TERMINAL_CLASSES:
                address = addr
                title = c.get("title") or c.get("class") or "terminal"
                break
        if address:
            break

    if address is None:
        log.debug(
            "tile_logs: no new terminal window detected within %.0fs, leaving untracked at %s",
            _DISCOVERY_TIMEOUT_SECONDS, initial,
        )
        # No window ever showed up (launch failed, or wasn't actually a
        # terminal) — nothing to track or clean up later, so don't leave
        # a stray .pending-*.log sitting in the cache dir forever.
        try:
            initial.unlink()
        except OSError:
            pass
        return

    final = TILE_LOG_DIR / f"{_sanitize(title)}.log"
    n = 2
    while final.exists():
        final = TILE_LOG_DIR / f"{_sanitize(title)}-{n}.log"
        n += 1
    try:
        initial.rename(final)
    except OSError:
        final = initial  # keep logging under the original name rather than fail

    with _lock:
        _tracked[address] = final
        _titles[address] = title
    log.info("tile_logs: tracking terminal %r (%s) -> %s", title, address, final)

    while True:
        time.sleep(_CLOSE_POLL_SECONDS)
        clients = _clients()
        match = next((c for c in clients if c.get("address") == address), None)
        if match is None:
            break
        new_title = match.get("title")
        if new_title and new_title != _titles.get(address):
            # Title changed (new cwd, a running command, ...) — keep the
            # in-memory record current for fuzzy lookups, but don't rename
            # the file mid-session; a tool call reading it mid-command
            # shouldn't have the path move under it.
            with _lock:
                _titles[address] = new_title

    with _lock:
        _tracked.pop(address, None)
        _titles.pop(address, None)
    try:
        final.unlink()
        log.info("tile_logs: tile closed, deleted %s", final)
    except OSError:
        pass


_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[a-zA-Z]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][0-9A-Za-z])")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def list_tiles() -> list[str]:
    with _lock:
        return list(_titles.values())


def find_log(query: str | None) -> Path | None:
    with _lock:
        items = list(_tracked.items())
        titles = dict(_titles)
    if not items:
        return None
    if not query:
        return items[0][1] if len(items) == 1 else None
    needle = query.strip().lower()
    best_addr, best_score = None, 0.0
    for addr, _path in items:
        score = fuzz.WRatio(needle, titles.get(addr, "").lower())
        if score > best_score:
            best_addr, best_score = addr, score
    if best_addr is not None and best_score > 50:
        return dict(items)[best_addr]
    return None


def read_log(query: str | None, tail_chars: int = 4000) -> str:
    """What the read_tile_log tool actually calls. Real text, not a
    screenshot — strips the ANSI/OSC escape sequences script(1) faithfully
    records (prompt colors, terminal title updates) so the model reads
    plain text, and returns only the tail since a long-running session's
    log can grow well past what's useful for "what just happened"."""
    path = find_log(query)
    if path is None:
        available = list_tiles()
        if not available:
            return "no tracked terminal tiles are currently open"
        if query:
            return f"no tracked tile matches {query!r} -- currently open: {', '.join(available)}"
        return f"more than one tile is open, name one -- currently open: {', '.join(available)}"
    try:
        raw = path.read_text(errors="replace")
    except OSError as e:
        return f"failed to read log: {e}"
    text = _strip_ansi(raw).strip()
    if len(text) > tail_chars:
        text = "...(truncated)...\n" + text[-tail_chars:]
    return text or "(no output yet)"


def sweep_stale() -> None:
    """Called once at daemon startup. Any files already in TILE_LOG_DIR
    are necessarily stale — no tracking thread survives a process
    restart to ever delete them on close — so start clean rather than
    accumulate orphaned logs across restarts."""
    if not TILE_LOG_DIR.is_dir():
        return
    for p in TILE_LOG_DIR.glob("*"):
        try:
            p.unlink()
        except OSError:
            pass
