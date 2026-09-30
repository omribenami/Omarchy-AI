"""Actions Omarchy prepared that wait for the user's OK (execution/myapi_agent.py).

A MyApi call that sends, changes or deletes something is not run when it is
prepared: it waits here, and as a confirm card in its conversation, until the
user taps Send or Cancel on the phone or says yes or no in that chat. Kept in
the daemon's memory and on disk, so a restart does not lose one.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time

from ..config import STATE_DIR

PATH = STATE_DIR / "pending_actions.json"
KEEP_SECONDS = 7 * 86400

_lock = threading.RLock()


def _load() -> dict:
    try:
        data = json.loads(PATH.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    now = time.time()
    data = {k: v for k, v in data.items() if now - float(v.get("created") or 0) < KEEP_SECONDS}
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, PATH)


def create(action: dict) -> str:
    aid = secrets.token_hex(6)
    with _lock:
        data = _load()
        data[aid] = {**action, "id": aid, "created": time.time(), "status": "waiting", "outcome": ""}
        _write(data)
    return aid


def get(aid) -> dict | None:
    if not isinstance(aid, str):
        return None
    with _lock:
        return _load().get(aid)


def claim(aid: str) -> dict | None:
    """The action, marked as being decided, if it still waits (only one caller wins)."""
    with _lock:
        data = _load()
        action = data.get(aid)
        if not action or action.get("status") != "waiting":
            return None
        action["status"] = "running"
        _write(data)
        return action


def finish(aid: str, outcome: str, message: str = "") -> None:
    with _lock:
        data = _load()
        if aid in data:
            data[aid].update(status="done", outcome=outcome, message=message[:500], decided=time.time())
            _write(data)
