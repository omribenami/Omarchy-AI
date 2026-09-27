"""The approval PIN a paired phone types to approve a waiting task.

Separate from the Linux login password on purpose: wrong guesses typed on
the phone must never count against the account's faillock, and a phone page
never sees the real password. Only a salted scrypt hash is stored (0600,
from creation); the PIN itself never reaches config.yaml, a log or a model.

Guessing is throttled here, not in the phone server, and the counter lives
in the same file so a daemon restart doesn't reset it: after
`MAX_FAILURES` wrong PINs in a row, every attempt is refused for
`LOCKOUT_SECONDS`, the right PIN included.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from ..config import CONFIG_DIR

PIN_PATH = CONFIG_DIR / "approval_pin.json"
MIN_LENGTH = 4
MAX_LENGTH = 128
MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
_lock = threading.Lock()


def _hash(pin: str, salt: bytes) -> bytes:
    return hashlib.scrypt(pin.encode(), salt=salt, dklen=32, **_SCRYPT)


def _load() -> dict | None:
    try:
        with open(PIN_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) and data.get("hash") else None
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write(data: dict) -> None:
    PIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PIN_PATH.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, PIN_PATH)


def store(pin: str) -> None:
    if not isinstance(pin, str) or not MIN_LENGTH <= len(pin) <= MAX_LENGTH:
        raise ValueError(f"the approval PIN must be {MIN_LENGTH}-{MAX_LENGTH} characters")
    salt = secrets.token_bytes(16)
    with _lock:
        _write({"salt": salt.hex(), "hash": _hash(pin, salt).hex(), "failures": 0, "locked_until": 0})


def clear() -> None:
    with _lock:
        try:
            PIN_PATH.unlink()
        except FileNotFoundError:
            pass


def is_set() -> bool:
    return _load() is not None


def status() -> dict:
    data = _load()
    return {"set": data is not None,
            "locked_until": data.get("locked_until", 0) if data and data.get("locked_until", 0) > time.time() else 0}


def verify(pin: str) -> tuple[bool, str]:
    """(ok, reason). Counts failures and enforces the lockout."""
    with _lock:
        data = _load()
        if data is None:
            return False, "no approval PIN is set: set one in Assistant Settings > Phone Bridge"
        now = time.time()
        if data.get("locked_until", 0) > now:
            minutes = int((data["locked_until"] - now) // 60) + 1
            return False, f"too many wrong PINs; try again in {minutes} min"
        ok = isinstance(pin, str) and 0 < len(pin) <= MAX_LENGTH and hmac.compare_digest(
            _hash(pin, bytes.fromhex(data["salt"])), bytes.fromhex(data["hash"]))
        if ok:
            if data.get("failures"):
                data["failures"] = 0
                _write(data)
            return True, "ok"
        data["failures"] = int(data.get("failures", 0)) + 1
        left = MAX_FAILURES - data["failures"]
        if left <= 0:
            data["failures"] = 0
            data["locked_until"] = now + LOCKOUT_SECONDS
            _write(data)
            return False, f"wrong PIN; approvals from the phone are locked for {LOCKOUT_SECONDS // 60} min"
        _write(data)
        return False, f"wrong PIN ({left} {'try' if left == 1 else 'tries'} left)"
