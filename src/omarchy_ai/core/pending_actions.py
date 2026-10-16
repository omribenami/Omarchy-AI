"""Actions Omarchy prepared that wait for the user's OK (execution/myapi_agent.py).

A MyApi call that sends, changes or deletes something is not run when it is
prepared: it waits here, and as a confirm card in its conversation, until the
user taps Send or Cancel on the phone or says yes or no in that chat. Kept in
the daemon's memory and on disk, so a restart does not lose one. Every change
is a locked read-modify-write (core/statefile.py), so the daemon and a CLI
cannot both claim one.
"""
from __future__ import annotations

import secrets
import time

from ..config import STATE_DIR
from . import statefile

PATH = STATE_DIR / "pending_actions.json"
KEEP_SECONDS = 7 * 86400


def _load() -> dict:
    return statefile.read_json(PATH, {}, expect=dict)


def _locked():
    """The file under its cross-process lock (core/statefile.py); same shape as ever."""
    return statefile.locked_json(PATH, {}, expect=dict)


def _pruned(data: dict) -> dict:
    now = time.time()
    return {k: v for k, v in data.items() if now - float(v.get("created") or 0) < KEEP_SECONDS}


def create(action: dict) -> str:
    aid = secrets.token_hex(6)
    with _locked() as doc:
        doc.data[aid] = {**action, "id": aid, "created": time.time(), "status": "waiting", "outcome": ""}
        doc.data = _pruned(doc.data)
    return aid


def get(aid) -> dict | None:
    if not isinstance(aid, str):
        return None
    return _load().get(aid)


def claim(aid: str) -> dict | None:
    """The action, marked as being decided, if it still waits (only one caller wins)."""
    with _locked() as doc:
        action = doc.data.get(aid)
        if not action or action.get("status") != "waiting":
            return None
        action["status"] = "running"
        doc.data = _pruned(doc.data)
        return dict(action)


def finish(aid: str, outcome: str, message: str = "") -> None:
    with _locked() as doc:
        if aid in doc.data:
            doc.data[aid].update(status="done", outcome=outcome, message=message[:500], decided=time.time())
            doc.data = _pruned(doc.data)
