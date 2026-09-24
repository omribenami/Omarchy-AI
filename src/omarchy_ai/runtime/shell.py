"""Structured shell execution: exit code + bounded output, never a scraped screen.

Unlike `terminal_task` (a visible tmux terminal the live model reads back as
screen text), this is the harness's own command runner for agents: every
call returns the exit code, a tail-truncated output, the duration and where
the full output was saved. Adapted from pi-mono's bash-executor
(third_party/pi-mono in MiniMax Code, MIT): tail truncation with a full-output
file, ANSI stripping, process-group kill on timeout/cancel.

It does NOT decide whether a command may run -- callers go through
TaskRuntime.run_command(), which checks permissions first.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import threading
import time

from .permissions import scrubbed_env

MAX_OUTPUT_CHARS = 12_000      # what an agent sees (tail)
MAX_OUTPUT_LINES = 400
MAX_CAPTURE_BYTES = 8_000_000  # hard cap kept in memory/file
DEFAULT_TIMEOUT = 120.0
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Za-z0-9]")


@dataclass
class CommandResult:
    command: str
    cwd: str
    exit_code: int | None
    output: str
    duration: float
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False
    full_output_path: str | None = None
    error: str | None = None
    started_at: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.error

    def as_dict(self, max_chars: int = MAX_OUTPUT_CHARS) -> dict:
        out = self.output if len(self.output) <= max_chars else "…" + self.output[-max_chars:]
        return {"command": self.command, "cwd": self.cwd, "exit_code": self.exit_code,
                "timed_out": self.timed_out, "cancelled": self.cancelled, "error": self.error,
                "duration_s": round(self.duration, 2), "output": out,
                "truncated": self.truncated or len(self.output) > max_chars,
                "full_output_path": self.full_output_path}


def clean(text: str) -> str:
    text = _ANSI.sub("", text).replace("\r\n", "\n")
    # Progress bars: keep only what a carriage return left on screen.
    text = "\n".join(line.rsplit("\r", 1)[-1] for line in text.split("\n"))
    return "".join(ch for ch in text if ch in "\n\t" or ch.isprintable())


def truncate_tail(text: str, max_chars: int = MAX_OUTPUT_CHARS, max_lines: int = MAX_OUTPUT_LINES) -> tuple[str, bool]:
    lines = text.split("\n")
    truncated = False
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[-max_chars:]
        truncated = True
    return text, truncated


def _base_env() -> dict:
    try:
        from ..execution.actions import _desktop_env
        env = _desktop_env()
    except Exception:  # noqa: BLE001 -- desktop env is best effort outside a session
        env = dict(os.environ)
    env = scrubbed_env(env)
    env.setdefault("PAGER", "cat")
    env["SYSTEMD_PAGER"] = ""
    env["GIT_PAGER"] = "cat"
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    return env


def run(command: str | list[str], cwd: str | Path | None = None, *, timeout: float = DEFAULT_TIMEOUT,
        stdin_text: str | None = None, cancel: threading.Event | None = None,
        output_dir: Path | None = None, env: dict | None = None) -> CommandResult:
    """`command` is bash text, or an argv list run without a shell."""
    cwd = str(Path(cwd or Path.home()).expanduser())
    started = time.time()
    argv = ["bash", "-c", command] if isinstance(command, str) else list(command)
    command = command if isinstance(command, str) else " ".join(command)[:2000]
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env or _base_env(),
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return CommandResult(command, cwd, None, "", 0.0, error=str(exc), started_at=started)

    chunks: list[bytes] = []
    size = [0]

    def pump():
        assert proc.stdout is not None
        for chunk in iter(lambda: proc.stdout.read1(65536), b""):
            if size[0] < MAX_CAPTURE_BYTES:
                chunks.append(chunk)
            size[0] += len(chunk)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    if stdin_text is not None and proc.stdin:
        try:
            proc.stdin.write(stdin_text.encode())
            proc.stdin.close()
        except OSError:
            pass
    timed_out = cancelled = False
    deadline = started + timeout
    while proc.poll() is None:
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        if time.time() > deadline:
            timed_out = True
            break
        time.sleep(0.05)
    if timed_out or cancelled:
        _kill_group(proc)
    reader.join(timeout=2)
    try:
        proc.stdout.close()
        proc.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        pass
    raw = b"".join(chunks).decode(errors="replace")
    text = clean(raw)
    shown, truncated = truncate_tail(text)
    full_path = None
    if truncated or size[0] > MAX_CAPTURE_BYTES:
        full_path = _save_full(text, output_dir)
    return CommandResult(command, cwd, None if (timed_out or cancelled) else proc.returncode, shown,
                         time.time() - started, timed_out=timed_out, cancelled=cancelled,
                         truncated=truncated or size[0] > MAX_CAPTURE_BYTES, full_output_path=full_path,
                         started_at=started)


def _kill_group(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            continue


def _save_full(text: str, output_dir: Path | None) -> str | None:
    try:
        directory = output_dir or Path(tempfile.gettempdir())
        directory.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="cmd-", suffix=".log", dir=directory)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        return path
    except OSError:
        return None
