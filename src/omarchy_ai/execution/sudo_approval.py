"""One-time, panel-approved sudo credentials.

The password never enters a model tool argument.  The settings panel writes
it to the per-user runtime directory (0600), and the submit action consumes
and deletes it before typing it into an already focused sudo prompt.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..config import RUNTIME_DIR

APPROVAL_PATH = RUNTIME_DIR / "sudo-approval.json"
TTL_SECONDS = 120


def _write(data: dict) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(APPROVAL_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(data, output)
    os.chmod(APPROVAL_PATH, 0o600)


def approve(password: str) -> None:
    if not password:
        raise ValueError("password is empty")
    _write({"password": password, "expires_at": time.time() + TTL_SECONDS})


def _load() -> dict | None:
    try:
        data = json.loads(APPROVAL_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data.get("password"), str) or not isinstance(data.get("expires_at"), (int, float)) or data["expires_at"] < time.time():
        APPROVAL_PATH.unlink(missing_ok=True)
        return None
    return data


def status() -> dict:
    data = _load()
    return {"approved": data is not None, "expires_in": max(0, int(data["expires_at"] - time.time())) if data else 0}


def consume() -> str | None:
    data = _load()
    APPROVAL_PATH.unlink(missing_ok=True)
    return data["password"] if data else None
