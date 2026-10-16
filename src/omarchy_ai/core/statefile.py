"""State files shared by the daemon and the CLIs, safe across processes.

The daemon, `omarchy-ai-task` and the settings panel's CLI all change the
same files under STATE_DIR. A thread lock only serializes one process, and a
plain write_text() that dies half-way leaves a file the next read cannot
parse -- which every loader then turned into an empty list, quietly losing
the user's agenda or preferences on the next save. Here:

- `locked(path)` is an flock on `<name>.lock` next to the file, re-entrant
  within a thread, so a read-modify-write in one process cannot interleave
  with another's.
- `atomic_write_text()` writes a temporary file in the same directory,
  fsyncs it, `os.replace`s it over the target and fsyncs the directory: a
  reader sees the old file or the new one, never half of one, even after a
  power cut.
- A file that cannot be parsed (or holds the wrong type) is moved aside to
  `<name>.corrupt-<timestamp>`, logged as a warning, and listed by
  `corrupt_files()` for doctor -- never silently replaced by an empty one.
- `locked_json` / `locked_yaml` combine the three: load under the lock,
  hand the document to the caller, write it back (only if it changed).

Schema versions: every file that existed before this module keeps its exact
shape, which is version 0 (no field). A file created with `version=N` is a
JSON object carrying `"schema_version": N`; one written by a newer version
than the caller knows is refused instead of being overwritten.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import json
import logging
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Any, Callable, Iterator, TextIO

import yaml

from ..config import STATE_DIR

log = logging.getLogger("omarchy_ai.core.statefile")

SCHEMA_KEY = "schema_version"
CORRUPT_MARK = ".corrupt-"


class StateFileError(Exception):
    """A state file that cannot be used as it is (it was not changed)."""


class _PathLock:
    def __init__(self) -> None:
        self.rlock = threading.RLock()
        self.depth = 0
        self.handle: TextIO | None = None


_registry_lock = threading.Lock()
_locks: dict[str, _PathLock] = {}


def lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


@contextmanager
def locked(path: Path) -> Iterator[None]:
    """Exclusive across threads and processes for `path`; re-entrant in a thread."""
    path = Path(path)
    key = str(path.absolute())
    with _registry_lock:
        entry = _locks.setdefault(key, _PathLock())
    with entry.rlock:
        if entry.depth == 0:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(lock_path(path), "a")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX)
            except BaseException:
                handle.close()
                raise
            entry.handle = handle
        entry.depth += 1
        try:
            yield
        finally:
            entry.depth -= 1
            if entry.depth == 0:
                held, entry.handle = entry.handle, None
                assert held is not None
                try:
                    fcntl.flock(held, fcntl.LOCK_UN)
                finally:
                    held.close()


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str) -> None:
    """Replace `path` with `text` all at once and durably (tmp + fsync +
    os.replace + directory fsync). New files get the usual umask mode; an
    existing file keeps its mode."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        mode = path.stat().st_mode & 0o7777
    except OSError:
        mode = None
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            if mode is not None:
                os.fchmod(stream.fileno(), mode)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


# ------------------------------------------------------------ quarantine


def quarantine(path: Path, reason: str = "") -> Path | None:
    """Move an unusable state file aside as `<name>.corrupt-<ts>` and warn.
    Call under `locked(path)`. Returns where it went (None if it was gone)."""
    path = Path(path)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}{CORRUPT_MARK}{stamp}")
    n = 1
    while target.exists():
        n += 1
        target = path.with_name(f"{path.name}{CORRUPT_MARK}{stamp}-{n}")
    try:
        os.replace(path, target)
    except FileNotFoundError:
        return None
    _fsync_dir(path.parent)
    log.warning(
        "state file %s could not be read (%s); kept it as %s and started a new one",
        path,
        reason or "corrupt",
        target.name,
    )
    return target


def quarantine_text(path: Path, text: str, reason: str = "") -> Path:
    """Keep unusable pieces of a file (bad JSONL lines) as `<name>.corrupt-<ts>`."""
    path = Path(path)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}{CORRUPT_MARK}{stamp}")
    n = 1
    while target.exists():
        n += 1
        target = path.with_name(f"{path.name}{CORRUPT_MARK}{stamp}-{n}")
    atomic_write_text(target, text)
    log.warning(
        "state file %s had unreadable records (%s); kept them as %s",
        path,
        reason or "corrupt",
        target.name,
    )
    return target


def corrupt_files(directory: Path | None = None) -> list[Path]:
    """Every quarantined state file under `directory` (STATE_DIR), oldest
    first: what doctor reports."""
    root = Path(directory or STATE_DIR)
    if not root.is_dir():
        return []
    found = [p for p in root.rglob(f"*{CORRUPT_MARK}*") if p.is_file()]
    return sorted(found, key=lambda p: p.stat().st_mtime)


# ---------------------------------------------------------------- documents


def _check_version(data: Any, version: int, path: Path) -> None:
    if not version:
        return
    found = data.get(SCHEMA_KEY, 0) if isinstance(data, dict) else 0
    if not isinstance(found, int) or found > version:
        raise StateFileError(
            f"{path} has schema version {found!r}; this version understands up to {version}"
        )


def schema_version(data: Any) -> int:
    """0 for every legacy file (no field)."""
    found = data.get(SCHEMA_KEY, 0) if isinstance(data, dict) else 0
    return found if isinstance(found, int) else 0


def _parse(path: Path, load: Callable[[str], Any], expect: type | tuple | None):
    """(data, text) from disk; (None, None) when the file is missing or empty;
    raises ValueError for content that cannot be used."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    if not text.strip():
        return None, None  # nothing to lose
    try:
        data = load(text)
    except Exception as exc:  # noqa: BLE001 -- json and yaml raise different types
        raise ValueError(f"{type(exc).__name__}: {str(exc)[:200]}") from None
    if data is None:
        return None, None
    if expect is not None and not isinstance(data, expect):
        raise ValueError(
            f"expected {getattr(expect, '__name__', expect)}, found {type(data).__name__}"
        )
    return data, text


def _load_locked(path, default, load, expect):
    """Under the lock: the document, quarantining a corrupt file."""
    try:
        data, text = _parse(path, load, expect)
    except UnicodeDecodeError as exc:
        quarantine(path, f"not UTF-8: {exc.reason}")
        data, text = None, None
    except ValueError as exc:
        quarantine(path, str(exc))
        data, text = None, None
    if data is None:
        return copy.deepcopy(default), None
    return data, text


def read_doc(
    path: Path,
    default: Any,
    *,
    load: Callable[[str], Any],
    expect=None,
    version: int = 0,
    lock: Path | None = None,
) -> Any:
    """Read-only load. Takes no lock on the normal path (writes are atomic);
    a file that does not parse is re-checked under the lock (the file's own,
    or `lock` when one lock guards several files), then quarantined. Never
    raises for missing or unreadable content."""
    path = Path(path)
    try:
        data, _ = _parse(path, load, expect)
    except (ValueError, UnicodeDecodeError):
        with locked(
            lock or path
        ):  # it may have been replaced since; only a real corrupt file moves aside
            data, _ = _load_locked(path, default, load, expect)
    except OSError:
        log.warning("state file %s could not be read", path, exc_info=True)
        return copy.deepcopy(default)
    if data is None:
        return copy.deepcopy(default)
    _check_version(data, version, path)
    return data


class Doc:
    """What a `locked_*` block works on: change `data` in place or assign it."""

    def __init__(self, data: Any):
        self.data = data


@contextmanager
def locked_doc(
    path: Path,
    default: Any,
    *,
    load: Callable[[str], Any],
    dump: Callable[[Any], str],
    expect=None,
    version: int = 0,
) -> Iterator[Doc]:
    """flock + read + yield + atomic write. The file is written only when the
    block finishes without an exception and `doc.data` serializes to
    something other than what was read."""
    path = Path(path)
    with locked(path):
        data, _ = _load_locked(path, default, load, expect)
        _check_version(data, version, path)
        before = dump(data) if path.exists() else None
        doc = Doc(data)
        yield doc
        if version and isinstance(doc.data, dict):
            doc.data[SCHEMA_KEY] = version
        after = dump(doc.data)
        if after != before:
            atomic_write_text(path, after)


def json_dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1)


def locked_json(
    path: Path,
    default: Any = None,
    *,
    expect=None,
    dump: Callable[[Any], str] = json_dump,
    version: int = 0,
):
    return locked_doc(
        path,
        {} if default is None else default,
        load=json.loads,
        dump=dump,
        expect=expect,
        version=version,
    )


def read_json(
    path: Path,
    default: Any = None,
    *,
    expect=None,
    version: int = 0,
    lock: Path | None = None,
) -> Any:
    return read_doc(
        path,
        {} if default is None else default,
        load=json.loads,
        expect=expect,
        version=version,
        lock=lock,
    )


def yaml_dump(data: Any) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def locked_yaml(
    path: Path,
    default: Any = None,
    *,
    expect=None,
    dump: Callable[[Any], str] = yaml_dump,
):
    return locked_doc(
        path,
        {} if default is None else default,
        load=yaml.safe_load,
        dump=dump,
        expect=expect,
    )


def read_yaml(path: Path, default: Any = None, *, expect=None) -> Any:
    return read_doc(
        path, {} if default is None else default, load=yaml.safe_load, expect=expect
    )


# ---------------------------------------------------------------- JSON lines


def read_jsonl(path: Path, bad: list[str] | None = None) -> list:
    """Every record that parses. Lines that do not (a torn last line) are
    skipped, and collected into `bad` when given."""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            if bad is not None:
                bad.append(line)
    return records


def append_jsonl(path: Path, line: str) -> None:
    """Append one already-serialized record durably, under the file's lock."""
    path = Path(path)
    if "\n" in line:
        raise ValueError("a JSONL record must be one line")
    with locked(path):
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def rewrite_jsonl(path: Path, text: str, bad: list[str] | None = None) -> None:
    """Replace a JSONL file atomically (call under `locked(path)`). Lines that
    did not parse are kept in a quarantine file, not dropped."""
    if bad:
        quarantine_text(
            path, "".join(line + "\n" for line in bad), f"{len(bad)} unreadable line(s)"
        )
    atomic_write_text(path, text)
