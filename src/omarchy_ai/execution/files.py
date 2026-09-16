"""Small, bounded local-file actions used by the voice assistant.

These actions deliberately operate on the user's files (their home
directory and /tmp by default), rather than becoming a general-purpose
root filesystem or shell interface.  The configurable roots make the
boundary visible and keep a spoken request from accidentally reaching
machine configuration or another user's private files.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..config import Config

_MAX_LIST_ITEMS = 200
_MAX_READ_CHARS = 12_000


class FileAccessError(ValueError):
    pass


def allowed_roots(config: Config) -> list[Path]:
    roots = getattr(config, "file_access_roots", None) or [str(Path.home()), "/tmp"]
    return [Path(root).expanduser().resolve() for root in roots]


def resolve_path(value: object, config: Config, *, must_exist: bool = False) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise FileAccessError("a file path is required")
    path = Path(value).expanduser().resolve(strict=False)
    if not any(path == root or root in path.parents for root in allowed_roots(config)):
        raise FileAccessError("that path is outside Omarchy AI's allowed file locations")
    if must_exist and not path.exists():
        raise FileAccessError("that file or folder does not exist")
    return path


def list_files(value: object, config: Config, recursive: bool = False) -> list[dict]:
    root = resolve_path(value or str(Path.home()), config, must_exist=True)
    if not root.is_dir():
        raise FileAccessError("that path is a file; use read_file for its contents")
    entries: list[dict] = []
    iterator = root.rglob("*") if recursive else root.iterdir()
    try:
        for path in iterator:
            if len(entries) >= _MAX_LIST_ITEMS:
                break
            # Never follow directory links while enumerating a tree.
            if path.is_symlink():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append({
                "path": str(path), "type": "directory" if path.is_dir() else "file",
                "size": stat.st_size if path.is_file() else None,
            })
    except OSError as exc:
        raise FileAccessError(f"couldn't list that folder: {exc}") from exc
    return entries


def read_file(value: object, config: Config, start_line: int = 1, max_chars: int = 4_000) -> str:
    path = resolve_path(value, config, must_exist=True)
    if not path.is_file():
        raise FileAccessError("that path is not a regular file")
    if path.stat().st_size > 10 * 1024 * 1024:
        raise FileAccessError("that file is over 10 MB; use file_info or ask for a smaller text file")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise FileAccessError("that looks like a binary file, so it can't be read as text") from exc
    except OSError as exc:
        raise FileAccessError(f"couldn't read that file: {exc}") from exc
    start = max(1, int(start_line)) - 1
    limit = min(_MAX_READ_CHARS, max(1, int(max_chars)))
    result = "\n".join(text.splitlines()[start:])
    return result[:limit] + ("\n…(truncated)" if len(result) > limit else "")


def write_file(value: object, content: object, config: Config, overwrite: bool = False) -> Path:
    if not isinstance(content, str):
        raise FileAccessError("file content must be text")
    path = resolve_path(value, config)
    if path.exists() and not overwrite:
        raise FileAccessError("that file already exists; ask the user before overwriting it")
    if path.exists() and not path.is_file():
        raise FileAccessError("that path is not a regular file")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # An atomic replacement means a partial write is never exposed if
        # the assistant process stops halfway through saving a document.
        temporary = path.with_name(f".{path.name}.omarchy-ai-writing")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
        os.replace(temporary, path)
    except OSError as exc:
        raise FileAccessError(f"couldn't write that file: {exc}") from exc
    return path


def write_bytes(value: object, content: bytes, config: Config, overwrite: bool = False) -> Path:
    path = resolve_path(value, config)
    if path.exists() and not overwrite:
        raise FileAccessError("that file already exists; choose another name or ask the user before overwriting it")
    if path.exists() and not path.is_file():
        raise FileAccessError("that path is not a regular file")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.omarchy-ai-writing")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(content)
        os.replace(temporary, path)
    except OSError as exc:
        raise FileAccessError(f"couldn't save that file: {exc}") from exc
    return path
