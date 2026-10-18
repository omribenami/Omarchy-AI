"""Files a task makes for the user: reports, screenshots, CSVs (harness item 4.7).

A task writes what it hands over under DATA_DIR/outputs/<task_id>/ (DATA_DIR
is $XDG_DATA_HOME/omarchy-ai, resolved at each use). When the task ends the
runtime lists that folder into the task record (Task.artifacts) and sends
each file through the outbox as an `attachment` message (core/outbox.py):
into the chat, revealed on the desktop, or summed up in voice.

describe() is the one place that decides how a file travels: a known type
within the phone's 20 MB limit is copied into the chat as an attachment;
anything else (an unknown type, a larger file) is sent as a link to its
desktop path, never inlined.

Retention: the heartbeat (core/daemon.py) calls cleanup(), which removes a
task's folder KEEP_DAYS after it was last written and, past MAX_BYTES in
all, the oldest folders first. cleanup() never creates the folder.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import re
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)

KEEP_DAYS = 14
MAX_BYTES = 1024 * 1024 * 1024  # all task outputs together
MAX_FILES = 20  # listed per task
INLINE_BYTES = 20 * 1024 * 1024  # the phone's attachment limit (core/conversations.py)
PREVIEW_CHARS = 160
# Types the chat shows or opens itself; anything else goes as a link.
INLINE_TYPES = re.compile(
    r"image/(png|jpeg|gif|webp)|application/pdf|text/(plain|csv|markdown)|application/json"
)
TEXT_TYPES = re.compile(r"text/.+|application/json")

# The outputs folder; None: DATA_DIR/outputs, resolved at each use (tests set it).
ROOT: Path | None = None


def data_dir() -> Path:
    """$XDG_DATA_HOME/omarchy-ai, as config.py derives the other XDG folders."""
    return (
        Path(os.environ.get("XDG_DATA_HOME") or "~/.local/share").expanduser()
        / "omarchy-ai"
    )


def root() -> Path:
    return ROOT if ROOT is not None else data_dir() / "outputs"


def _valid(task_id: str) -> bool:
    return (
        isinstance(task_id, str)
        and 1 <= len(task_id) <= 64
        and bool(re.fullmatch(r"[A-Za-z0-9_-]+", task_id))
    )


def task_dir(task_id: str, create: bool = False) -> Path:
    """Where task `task_id` puts files for the user (created only when asked)."""
    if not _valid(task_id):
        raise ValueError("invalid task id")
    path = root() / task_id
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def collect(task_id: str) -> list[str]:
    """The files in a task's folder (sorted, at most MAX_FILES; none if it is absent)."""
    try:
        folder = task_dir(task_id)
    except ValueError:
        return []
    if not folder.is_dir():
        return []
    found = sorted(p for p in folder.rglob("*") if p.is_file() and not p.is_symlink())
    return [str(p) for p in found[:MAX_FILES]]


def mime_of(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _preview(path: Path, mime: str) -> str:
    if not TEXT_TYPES.fullmatch(mime):
        return ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            text = stream.read(PREVIEW_CHARS * 2)
    except OSError:
        return ""
    return " ".join(text.split())[:PREVIEW_CHARS]


def describe(path: str | Path, *, untrusted: bool = False) -> dict:
    """{name, mime, path, size, preview, inline, untrusted} of one file.
    `inline`: it may be copied into the chat (known type, within 20 MB)."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise ValueError("file does not exist")
    size = path.stat().st_size
    mime = mime_of(path)
    return {
        "name": path.name,
        "mime": mime,
        "path": str(path),
        "size": size,
        "preview": _preview(path, mime),
        "inline": 0 < size <= INLINE_BYTES and bool(INLINE_TYPES.fullmatch(mime)),
        "untrusted": bool(untrusted),
    }


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} bytes"


def summary(meta: dict) -> str:
    """What voice says about a file: its name, kind and size, never its bytes."""
    kind = str(meta.get("mime") or "").split("/")[-1].upper() or "file"
    return f"{meta.get('name', 'a file')}, a {kind} file of {human_size(int(meta.get('size') or 0))}"


def _folder_size(folder: Path) -> int:
    return sum(
        p.stat().st_size
        for p in folder.rglob("*")
        if p.is_file() and not p.is_symlink()
    )


def _last_written(folder: Path) -> float:
    times = [folder.stat().st_mtime] + [p.stat().st_mtime for p in folder.rglob("*")]
    return max(times)


def cleanup(
    now: float | None = None,
    *,
    keep_days: float = KEEP_DAYS,
    max_bytes: int = MAX_BYTES,
) -> list[str]:
    """Remove task folders past retention; returns the task ids removed."""
    base = root()
    if not base.is_dir():
        return []
    now = time.time() if now is None else now
    folders = []
    for folder in base.iterdir():
        if not folder.is_dir() or folder.is_symlink():
            continue
        try:
            folders.append((_last_written(folder), _folder_size(folder), folder))
        except OSError:
            continue
    folders.sort(key=lambda item: item[0])
    removed: list[str] = []
    total = sum(size for _, size, _ in folders)
    for written, size, folder in folders:
        if now - written > keep_days * 86400 or total > max_bytes:
            shutil.rmtree(folder, ignore_errors=True)
            removed.append(folder.name)
            total -= size
    if removed:
        log.info("outputs: removed %d task folder(s) past retention", len(removed))
    return removed
