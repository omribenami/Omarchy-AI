"""Correlation IDs for every log line, and the structured log sinks.

    with logs.bind(session_id=..., turn_id=...):
        log.info("...")            # carries both IDs in the structured sinks

The IDs live in contextvars (FIELDS), so asyncio tasks inherit them and
each thread or task binds its own. A thread does not inherit the context of
the thread that starts it: execution/runner.py copies it into each tool
call's thread, and carry(fn) takes the IDs bound now into another thread.
A tool worker process (execution/worker_main.py) gets them with each request
and exports them to its environment (env()), so whatever it starts has them
too.

Sinks (setup()):
- stderr, as before: the same human-readable line, byte for byte, with no
  IDs in it (people and scripts grep the journal for these lines). When
  python-systemd is installed and stderr is the journal, the lines go to the
  journal natively instead, with the same MESSAGE text and the IDs as fields
  (SESSION_ID=, TURN_ID=, ...). Without python-systemd nothing changes.
- STATE_DIR/logs/omarchy-ai.jsonl: one JSON object per record (ts, level,
  logger, msg, pid, every ID field, exc when there is one, and the tool
  metrics of tool_result()). It rolls over at MAX_BYTES or when the day
  changes, and rolled files older than KEEP_DAYS are deleted.

The RedactingFilter (execution/passwords.py) is on every sink, in the daemon
and in its worker processes.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime
import functools
import importlib
import json
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, TypeVar

__all__ = [
    "FIELDS",
    "JsonlFormatter",
    "JsonlHandler",
    "bind",
    "carry",
    "current",
    "env",
    "from_env",
    "new_id",
    "setup",
    "tool_result",
]

FIELDS = (
    "session_id",
    "turn_id",
    "tool_call_id",
    "task_id",
    "step_id",
    "effect_id",
    "run_id",
)
ENV_PREFIX = "OMARCHY_LOG_"
JSONL_ENV = "OMARCHY_LOG_JSONL"  # a worker process appends to this file
FILE_NAME = "omarchy-ai.jsonl"
MAX_BYTES = 10 * 1024 * 1024
KEEP_DAYS = 14
MAX_ROLLED = 40  # at most ~MAX_ROLLED * MAX_BYTES on disk whatever the age

_VARS: dict[str, contextvars.ContextVar[str | None]] = {
    name: contextvars.ContextVar(f"omarchy_log_{name}", default=None) for name in FIELDS
}
_IDS_ATTR = "omarchy_ids"
_EVENT_ATTR = "omarchy_event"

metrics = logging.getLogger("omarchy_ai.metrics")
metrics.propagate = False  # the JSONL sink only: no new lines in the journal

T = TypeVar("T")


# ------------------------------------------------------------------- IDs


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def current() -> dict[str, str]:
    """The IDs bound now (unbound ones left out)."""
    ids = {}
    for name, var in _VARS.items():
        value = var.get()
        if value:
            ids[name] = value
    return ids


def _set(
    ids: Mapping[str, object],
) -> list[tuple[contextvars.ContextVar, contextvars.Token]]:
    tokens = []
    for name, value in ids.items():
        var = _VARS.get(name)
        if var is None:
            raise TypeError(f"unknown log ID {name!r}")
        tokens.append((var, var.set(str(value) if value not in (None, "") else None)))
    return tokens


def set_ids(**ids: object) -> None:
    """Bind IDs for the rest of the current context (an asyncio task, a
    thread). Use bind() where the binding must end."""
    _set(ids)


@contextlib.contextmanager
def bind(**ids: object) -> Iterator[None]:
    """Bind IDs (None unbinds one) until the block ends."""
    tokens = _set(ids)
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def bind_only(ids: Mapping[str, object]) -> contextlib.AbstractContextManager[None]:
    """Bind exactly `ids`: every other field is unbound in the block."""
    return bind(**{name: ids.get(name) for name in FIELDS})


def carry(fn: Callable[..., T]) -> Callable[..., T]:
    """`fn` with the IDs bound now, for another thread (which does not
    inherit this context). Only the IDs travel, nothing else in the context."""
    ids = current()

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> T:
        with bind_only(ids):
            return fn(*args, **kwargs)

    return run


def env(ids: Mapping[str, str] | None = None) -> dict[str, str]:
    """The IDs as environment variables, for a child process."""
    ids = current() if ids is None else ids
    return {ENV_PREFIX + name.upper(): ids[name] for name in FIELDS if ids.get(name)}


def from_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    environ = os.environ if environ is None else environ
    ids = {}
    for name in FIELDS:
        value = environ.get(ENV_PREFIX + name.upper())
        if value:
            ids[name] = value
    return ids


def export(ids: Mapping[str, str]) -> None:
    """Make `ids` this process's environment for children (a tool worker,
    between calls): set the bound ones, drop the rest."""
    for name in FIELDS:
        key = ENV_PREFIX + name.upper()
        if ids.get(name):
            os.environ[key] = str(ids[name])
        else:
            os.environ.pop(key, None)


# --------------------------------------------------------------- metrics


def tool_result(
    tool: str,
    *,
    ok: bool,
    error_class: str | None,
    duration: float,
    tool_call_id: str,
    effect_id: str | None = None,
    **fields: object,
) -> None:
    """One record per tool result, in the JSONL stream."""
    if not metrics.handlers:
        return
    outcome = "ok" if ok else (error_class or "failed")
    event = {
        "event": "tool_result",
        "tool": tool,
        "ok": ok,
        "outcome": outcome,
        "error_class": error_class,
        "duration_ms": round(duration * 1000, 1),
        **fields,
    }
    with bind(
        tool_call_id=tool_call_id, effect_id=effect_id or _VARS["effect_id"].get()
    ):
        metrics.info(
            "tool result: %s %s in %.0f ms",
            tool,
            outcome,
            duration * 1000,
            extra={_EVENT_ATTR: event},
        )


# ----------------------------------------------------------------- sinks


class _CaptureIds(logging.Filter):
    """Takes the IDs into the record, in the thread that logged it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, _IDS_ATTR):
            setattr(record, _IDS_ATTR, current())
        return True


def _redacting() -> logging.Filter:
    from ..execution.passwords import RedactingFilter

    return RedactingFilter()


class JsonlFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ids = getattr(record, _IDS_ATTR, None)
        if ids is None:
            ids = current()
        created = datetime.datetime.fromtimestamp(record.created, datetime.timezone.utc)
        entry: dict[str, Any] = {
            "ts": created.isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "pid": record.process,
        }
        for name in FIELDS:
            entry[name] = ids.get(name)
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            entry["exc"] = record.exc_text
        event = getattr(record, _EVENT_ATTR, None)
        if isinstance(event, dict):
            for key, value in event.items():
                entry.setdefault(key, value)
        return json.dumps(entry, ensure_ascii=False, default=str)


class JsonlHandler(logging.Handler):
    """Appends one JSON line per record to `path`.

    The owner (the daemon) rolls the file over: at `max_bytes`, and when the
    day changes, to omarchy-ai.<time>.jsonl next to it, deleting rolled
    files older than `keep_days` (and beyond `max_rolled`). Other processes
    (tool workers) only append (`rotate=False`): when the file has been
    rolled over under them they reopen the new one. Each record is one
    write() to an O_APPEND file, so lines from several processes never
    interleave. A write that fails is dropped, never raised."""

    def __init__(
        self,
        path: Path | str,
        *,
        rotate: bool = True,
        max_bytes: int = MAX_BYTES,
        keep_days: float = KEEP_DAYS,
        max_rolled: int = MAX_ROLLED,
    ) -> None:
        super().__init__()
        self.path = Path(path)
        self.rotate = rotate
        self.max_bytes = max_bytes
        self.keep_days = keep_days
        self.max_rolled = max_rolled
        self.setFormatter(JsonlFormatter())
        self._fd: int | None = None
        self._ident: tuple[int, int] | None = None
        self._size = 0
        self._day_ends = 0.0  # the end of the day the open file started on
        self._checked = 0.0
        if rotate:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self.lock or contextlib.nullcontext():
                self._open()
                if self._size and time.time() >= self._day_ends:
                    self._roll()
                self._purge()

    # file
    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(
            self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600
        )
        st = os.fstat(fd)
        self._fd, self._ident, self._size = fd, (st.st_dev, st.st_ino), st.st_size
        mtime = st.st_mtime if st.st_size else time.time()
        self._day_ends = _day_end(mtime)

    def _close(self) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fd)
        self._fd = None

    def _moved(self) -> bool:
        """Whether the path no longer names the open file (a worker's view)."""
        now = time.monotonic()
        if now - self._checked < 1.0:
            return False
        self._checked = now
        try:
            st = os.stat(self.path)
        except OSError:
            return True
        return (st.st_dev, st.st_ino) != self._ident

    def rolled(self) -> list[Path]:
        stem, suffix = self.path.stem, self.path.suffix
        return sorted(
            p
            for p in self.path.parent.glob(f"{stem}.*{suffix}")
            if p.name != self.path.name
        )

    def _roll(self) -> None:
        self._close()
        stamp = time.strftime("%Y%m%dT%H%M%S")
        target = self.path.with_name(f"{self.path.stem}.{stamp}{self.path.suffix}")
        n = 1
        while target.exists():
            n += 1
            target = self.path.with_name(
                f"{self.path.stem}.{stamp}-{n}{self.path.suffix}"
            )
        with contextlib.suppress(FileNotFoundError):
            os.replace(self.path, target)
        self._open()
        self._purge()

    def _purge(self) -> None:
        cutoff = time.time() - self.keep_days * 86400
        rolled = self.rolled()
        for index, path in enumerate(rolled):
            try:
                old = path.stat().st_mtime < cutoff
            except OSError:
                continue
            if old or index < len(rolled) - self.max_rolled:
                with contextlib.suppress(OSError):
                    path.unlink()

    # logging
    def emit(self, record: logging.LogRecord) -> None:
        try:
            data = (self.format(record) + "\n").encode("utf-8", "replace")
            if self._fd is None or (not self.rotate and self._moved()):
                self._close()
                self._open()
            elif self.rotate and (
                self._size + len(data) > self.max_bytes
                and self._size
                or time.time() >= self._day_ends
            ):
                self._roll()
            assert self._fd is not None
            os.write(self._fd, data)
            self._size += len(data)
        except Exception:  # noqa: BLE001 -- a log write must never fail the caller
            self.handleError(record)

    def close(self) -> None:
        with self.lock or contextlib.nullcontext():
            self._close()
        super().close()


def _day_end(at: float) -> float:
    """The next local midnight after `at`."""
    day = datetime.datetime.fromtimestamp(at).date() + datetime.timedelta(days=1)
    return datetime.datetime.combine(day, datetime.time()).timestamp()


class _JournalHandler(logging.Handler):
    """Native journald records (python-systemd): the same MESSAGE as the
    stderr line, plus PRIORITY, LOGGER and the IDs as fields."""

    PRIORITY = {
        logging.CRITICAL: 2,
        logging.ERROR: 3,
        logging.WARNING: 4,
        logging.INFO: 6,
        logging.DEBUG: 7,
    }

    def __init__(self, send: Callable[..., Any], identifier: str) -> None:
        super().__init__()
        self._send = send
        self.identifier = identifier

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            ids = getattr(record, _IDS_ATTR, None)
            if ids is None:
                ids = current()
            level = max(
                (lv for lv in self.PRIORITY if lv <= record.levelno),
                default=logging.DEBUG,
            )
            fields = {name.upper(): value for name, value in ids.items()}
            self._send(
                message,
                PRIORITY=str(self.PRIORITY[level]),
                SYSLOG_IDENTIFIER=self.identifier,
                LOGGER=record.name,
                THREAD_NAME=record.threadName or "",
                CODE_FILE=record.pathname,
                CODE_LINE=str(record.lineno),
                CODE_FUNC=record.funcName,
                **fields,
            )
        except Exception:  # noqa: BLE001
            self.handleError(record)


def _journal_send() -> Callable[..., Any] | None:
    """systemd.journal.send when python-systemd is installed and stderr is
    the journal (JOURNAL_STREAM names it); else None."""
    stream = os.environ.get("JOURNAL_STREAM", "")
    try:
        st = os.fstat(sys.stderr.fileno())
        dev, _, ino = stream.partition(":")
        if not stream or (int(dev), int(ino)) != (st.st_dev, st.st_ino):
            return None
    except (OSError, ValueError, AttributeError):
        return None
    try:
        journal = importlib.import_module("systemd.journal")
    except ImportError:
        return None
    send = getattr(journal, "send", None)
    return send if callable(send) else None


_installed: list[logging.Handler] = []
_setup_lock = threading.Lock()


def setup(
    level: int | str,
    *,
    fmt: str,
    log_dir: Path | None = None,
    jsonl_path: Path | None = None,
    rotate: bool = True,
    stream: Any = None,
    journal: bool = True,
) -> list[logging.Handler]:
    """Install the sinks on the root logger (replacing ones installed
    before). `fmt` is the stderr line format. The JSONL file is
    `jsonl_path`, or `log_dir`/FILE_NAME; neither: no file. `rotate=False`
    for a process that only appends (a tool worker)."""
    root = logging.getLogger()
    with _setup_lock:
        _remove()
        if isinstance(level, str):
            level = logging.getLevelName(level.upper())
            if not isinstance(level, int):
                level = logging.INFO
        root.setLevel(level)
        capture = _CaptureIds()
        send = _journal_send() if journal and stream is None else None
        console: logging.Handler
        if send is not None:
            console = _JournalHandler(
                send, os.path.basename(sys.executable) or "python"
            )
        else:
            console = logging.StreamHandler(stream)
        console.setFormatter(logging.Formatter(fmt))
        handlers: list[logging.Handler] = [console]
        path = jsonl_path or (log_dir / FILE_NAME if log_dir is not None else None)
        file: JsonlHandler | None = None
        if path is not None:
            try:
                file = JsonlHandler(path, rotate=rotate)
            except OSError:
                logging.getLogger(__name__).warning(
                    "could not open the structured log %s", path, exc_info=True
                )
        if file is not None:
            handlers.append(file)
        for handler in handlers:
            handler.addFilter(capture)
            handler.addFilter(_redacting())
            root.addHandler(handler)
            _installed.append(handler)
        if file is not None:
            metrics.addHandler(file)
            metrics.setLevel(logging.INFO)
        return handlers


def _remove() -> None:
    root = logging.getLogger()
    for handler in _installed:
        root.removeHandler(handler)
        metrics.removeHandler(handler)
        handler.close()
    _installed.clear()


def shutdown() -> None:
    """Remove the sinks setup() installed (tests)."""
    with _setup_lock:
        _remove()


def jsonl_file() -> Path | None:
    """The JSONL file this process writes, for its tool workers."""
    for handler in _installed:
        if isinstance(handler, JsonlHandler):
            return handler.path
    return None


def worker_env() -> dict[str, str]:
    path = jsonl_file()
    return {JSONL_ENV: str(path)} if path is not None else {}
