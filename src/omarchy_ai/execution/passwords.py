"""Passwords: kept out of every file and log, and supplied to SSH by the harness.

Real case 2026-09-27 18:55: the user said "User: user Pass: <password>" on
a phone call so she could SSH to their Home Assistant box. The password was
then stored in plain text in conversation_history.jsonl, and she still could
not use it (no sshpass; the saved Sudo Access password only fed `sudo -S`).

- A password the user speaks or types ("pass: X", "password is X") is
  captured from the transcript into GNOME Keyring (`capture_spoken`) and
  replaced by "[password]" wherever it would be written (`redact`): the
  conversation history, task files, the agenda and the daemon's log.
- SSH that needs a password runs as `sshpass <cmd>` or `ssh-copy-id`: the
  harness strips any literal password from the command (`sanitize_command`,
  before it is classified, recorded or shown for approval) and answers the
  password prompt itself through SSH_ASKPASS (`ssh_env`, execution/askpass.py)
  with the spoken password, else the saved Sudo Access password (the user's
  "same password as root"). The password never enters a command line, a
  model tool argument, or a file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import sys
import threading
import time
from pathlib import Path

from . import sudo_approval

_SPOKEN_ATTRIBUTES = ("application", "omarchy-ai", "purpose", "spoken-password")
_SPOKEN_LABEL = "Omarchy AI password the user gave for a login"
MASK = "[password]"
_MIN_SECRET = 4
_CACHE_SECONDS = 60

# "pass: X", "password is X", "pwd=X", Hebrew "סיסמה: X". Without an explicit
# separator the value must look like a password (digit or symbol), so
# "password manager" or "same password as root" capture nothing.
_KEYWORD = r"(?:pass(?:word)?|passwd|pwd|סיסמה|סיסמא)"
_SPOKEN = re.compile(
    rf"(?i)(?<![\w-]){_KEYWORD}(?![\w-])(?P<sep>\s*(?:is|היא|:|=)\s*|\s+)[\"'“”]?(?P<value>[^\s\"'“”]{{{_MIN_SECRET},128}})")
_NOT_A_PASSWORD = {"for", "to", "as", "is", "the", "a", "an", "my", "your", "our", "same", "saved", "correct",
                   "wrong", "that", "this", "it", "and", "or", "in", "on", "of", "with", "manager", "prompt",
                   "field", "again", "please", "needed", "required", "reset", "change", "protected", "less"}

_lock = threading.Lock()
_cache: tuple[float, list[str]] = (0.0, [])


def _run(args: list[str], password: str | None = None):
    return sudo_approval._run(args, password=password)


def spoken_password() -> str | None:
    try:
        result = _run(["lookup", *_SPOKEN_ATTRIBUTES])
    except OSError:
        return None
    value = result.stdout.rstrip("\n") if result.returncode == 0 else ""
    return value or None


def store_spoken(password: str) -> bool:
    global _cache
    if not password or len(password) < _MIN_SECRET or password == spoken_password():
        return False
    try:
        result = _run(["store", f"--label={_SPOKEN_LABEL}", *_SPOKEN_ATTRIBUTES], password=password)
    except OSError:
        return False
    with _lock:
        _cache = (0.0, [])
    return result.returncode == 0


def ssh_password() -> str | None:
    """For an SSH password prompt: what the user said, else the saved Sudo
    Access password (only while Sudo Access is enabled)."""
    spoken = spoken_password()
    if spoken:
        return spoken
    try:
        from ..config import load_config
        if not load_config().sudo_access_enabled:
            return None
    except Exception:  # noqa: BLE001
        return None
    return sudo_approval.retrieve()


def known_secrets() -> list[str]:
    """The saved passwords, cached briefly (redaction runs on every log line)."""
    global _cache
    with _lock:
        if time.monotonic() - _cache[0] < _CACHE_SECONDS:
            return _cache[1]
    values = []
    for getter in (sudo_approval.retrieve, spoken_password):
        try:
            value = getter()
        except Exception:  # noqa: BLE001
            value = None
        if value and len(value) >= _MIN_SECRET:
            values.append(value)
    with _lock:
        _cache = (time.monotonic(), values)
    return values


def _spoken_values(text: str) -> list[str]:
    values = []
    for match in _SPOKEN.finditer(text or ""):
        value = match.group("value").rstrip(".,;!?") if not match.group("sep").strip() else match.group("value")
        if value.lower() in _NOT_A_PASSWORD or len(value) < _MIN_SECRET:
            continue
        if not match.group("sep").strip() and not re.search(r"[\d\W_]", value):
            continue
        values.append(value)
    return values


def redact(text: str, *, speech: bool = False) -> str:
    """Mask saved passwords (also JSON-escaped); with `speech`, also a
    password stated in the text ("pass: X")."""
    if not text:
        return text
    secrets = list(known_secrets())
    if speech:
        secrets += _spoken_values(text)
    for secret in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(secret, MASK)
        escaped = json.dumps(secret, ensure_ascii=False)[1:-1]
        if escaped != secret:
            text = text.replace(escaped, MASK)
    return text


def capture_spoken(text: str) -> bool:
    """Keep a password the user just said, so SSH can use it and redaction
    can mask it everywhere. Called on each transcript fragment of an
    utterance; the last (complete) value wins."""
    values = _spoken_values(text)
    return bool(values) and store_spoken(values[-1])


# ── SSH ────────────────────────────────────────────────────────────────────

_SSHPASS = re.compile(r"(?<![\w/.-])sshpass\s+(?P<opt>-p\s*(?:'[^']*'|\"[^\"]*\"|\S+)|-e|-f\s*\S+|-d\s*\d+)\s+")
_SSH_WORDS = re.compile(r"(?<![\w/.-])(?:sshpass|ssh-copy-id)(?![\w-])")


def sanitize_command(command: str) -> str:
    """`sshpass -p SECRET cmd` -> `sshpass cmd` (the password is captured into
    the keyring first), so a literal password never reaches a record, an
    approval, a log or a terminal."""
    def strip(match: re.Match) -> str:
        opt = match.group("opt")
        if opt.startswith("-p"):
            try:
                value = shlex.split(opt[2:].strip())[0]
            except (ValueError, IndexError):
                value = opt[2:].strip().strip("'\"")
            # Kept only when the user has not stated one: the model's literal
            # may be misheard or invented, and must never replace what the
            # user said (found testing 2026-09-27: a made-up literal did).
            if value and spoken_password() is None:
                store_spoken(value)
        return "sshpass "
    return _SSHPASS.sub(strip, command or "")


def needs_password(command: str) -> bool:
    return bool(_SSH_WORDS.search(command or ""))


def askpass_helper() -> str:
    """An executable SSH_ASKPASS in the private runtime dir (0700)."""
    from ..config import RUNTIME_DIR
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    try:
        RUNTIME_DIR.chmod(0o700)
    except OSError:
        pass
    path = Path(RUNTIME_DIR) / "askpass.sh"
    body = f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -m omarchy_ai.execution.askpass \"$@\"\n"
    if not path.exists() or path.read_text() != body:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(body)
        tmp.chmod(0o700)
        os.replace(tmp, path)
    return str(path)


def _unwrap(command: str) -> str:
    """`sshpass cmd` runs cmd: sshpass is only the marker for "log in with
    the password" (it is not installed here)."""
    return re.sub(r"(?<![\w/.-])sshpass\s+", "", command)


def ssh_env(command: str, env: dict) -> tuple[str, dict]:
    """The command to run and its environment, with the harness answering
    SSH's password prompt."""
    if not needs_password(command):
        return command, env
    env = dict(env)
    env.update(SSH_ASKPASS=askpass_helper(), SSH_ASKPASS_REQUIRE="force")
    env.setdefault("DISPLAY", ":0")
    return _unwrap(command), env


def ssh_terminal_command(command: str) -> str:
    """The same for a command typed into an assistant terminal (tmux)."""
    command = sanitize_command(command)
    if not needs_password(command):
        return command
    return (f"SSH_ASKPASS={shlex.quote(askpass_helper())} SSH_ASKPASS_REQUIRE=force DISPLAY=${{DISPLAY:-:0}} "
            + _unwrap(command))


class RedactingFilter(logging.Filter):
    """Masks saved passwords in every log record (the daemon logs tool calls
    with their arguments)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            masked = redact(message)
            if masked != message:
                record.msg, record.args = masked, None
        except Exception:  # noqa: BLE001 -- logging must never fail
            pass
        return True
