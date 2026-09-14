"""Thin wrapper around the omarchy-ai.watchdog Quickshell plugin's IPC —
the "Watch Dogs" hacking-terminal overlay that shows live conversation
state and a scrolling feed of real tool calls while a voice session is
open. Same IPC pattern as omarchy-osd / omarchy-ai.window-labels (see
~/.config/omarchy/plugins/omarchy-ai.watchdog/Watchdog.qml and
execution/actions.py's show_window_labels/hide_window_labels), driven via
`omarchy-shell -q watchdog <method> '<json>'`.

Unlike window-labels (a tool the model calls on its own), this is wired
into live.py's own lifecycle — session connect/hangup, real tool calls in
_run_tool_call, and state transitions in _on_data_message — so it needs to
be safe to call from several places without ever risking the actual
conversation. Every call here is wrapped so a missing/failed IPC call just
logs and moves on: this is a cosmetic add-on layered on top of a live voice
session, never allowed to interrupt or crash it.

Also mirrors actions.py's payload-shape gotcha: the underlying `qs ipc
call` CLI splats a top-level JSON *array* into multiple positional args, so
every payload here is a JSON object, never a bare array (moot for now,
since no payload here carries an array, but keeping the same convention
this repo already learned the hard way).
"""

from __future__ import annotations

import json
import logging
import subprocess

log = logging.getLogger("omarchy_ai.voice.watchdog")

_TIMEOUT = 5


def _ipc(*args: str) -> None:
    argv = ["omarchy-shell", "-q", "watchdog", *args]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=_TIMEOUT, check=False
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            log.warning("watchdog ipc %r failed: %s", args[0] if args else "?", err)
    except FileNotFoundError:
        log.warning("watchdog ipc skipped: omarchy-shell not found")
    except subprocess.TimeoutExpired:
        log.warning("watchdog ipc %r timed out", args[0] if args else "?")
    except Exception:  # noqa: BLE001 — cosmetic overlay, never take the session down
        log.debug("watchdog ipc %r raised", args[0] if args else "?", exc_info=True)


def _event(payload: dict) -> None:
    _ipc("event", json.dumps(payload))


def _truncate(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _format_value(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return json.dumps(str(value), ensure_ascii=False)


def _format_call(name: str, args: dict) -> str:
    if not args:
        return f"{name}()"
    if len(args) == 1:
        # Matches the ask's own examples: execute_command({'title': 'Browser'})
        # renders as execute_command("Browser"), not execute_command(title="Browser").
        return f"{name}({_format_value(next(iter(args.values())))})"
    inner = ", ".join(f"{k}={_format_value(v)}" for k, v in args.items())
    return f"{name}({inner})"


def start() -> None:
    """Show the overlay and reset its scroll content for a fresh session."""
    _ipc("start", "{}")


def stop() -> None:
    """Hide the overlay — the conversation ended."""
    _ipc("stop")


def state(label: str) -> None:
    """Update the state indicator (connecting/listening/thinking/speaking)."""
    _event({"kind": "state", "state": label, "text": f"[{label}]"})


def tool_call(name: str, args: dict) -> None:
    """A tool call is starting — reuses the exact name/args already logged
    and fed into LiveSession._action_log in _run_tool_call, just also
    rendered into the overlay's feed."""
    text = _truncate(f"> {_format_call(name, args)}", 72)
    _event({"kind": "tool_call", "text": text})


def tool_result(name: str, args: dict, ok: bool, message: str) -> None:
    """A tool call just finished — same data already logged as 'tool
    result: ok=%s message=%r' in _run_tool_call."""
    if ok:
        outcome = "ok"
    else:
        outcome = f"error: {_truncate(message, 30)}" if message else "error"
    text = _truncate(f"> {_format_call(name, args)} -> {outcome}", 72)
    _event({"kind": "tool_result", "text": text, "tone": "ok" if ok else "err"})
