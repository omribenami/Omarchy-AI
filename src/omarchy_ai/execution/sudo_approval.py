"""Persistent, user-controlled sudo access stored in GNOME Keyring.

The password never enters a model tool argument, config.yaml, a log, or a
regular file. libsecret encrypts it in the user's desktop keyring; this
module only retrieves it when the user has explicitly enabled Sudo Access.
"""

from __future__ import annotations

import subprocess

_ATTRIBUTES = ("application", "omarchy-ai", "purpose", "sudo-password")
_LABEL = "Omarchy AI Sudo Access"


def _run(args: list[str], *, password: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["secret-tool", *args], input=password, capture_output=True,
            text=True, timeout=10, check=False,
        )
    except FileNotFoundError as error:
        raise OSError("secret-tool is unavailable; install libsecret for Sudo Access") from error
    except subprocess.TimeoutExpired as error:
        raise OSError("GNOME Keyring did not respond") from error


def store(password: str) -> None:
    if not password:
        raise ValueError("password is empty")
    result = _run(["store", f"--label={_LABEL}", *_ATTRIBUTES], password=password)
    if result.returncode != 0:
        raise OSError((result.stderr or result.stdout or "could not access GNOME Keyring").strip())


def retrieve() -> str | None:
    try:
        result = _run(["lookup", *_ATTRIBUTES])
    except OSError:
        return None
    if result.returncode != 0:
        return None
    password = result.stdout.rstrip("\n")
    return password or None


def clear() -> None:
    result = _run(["clear", *_ATTRIBUTES])
    if result.returncode != 0:
        raise OSError((result.stderr or result.stdout or "could not clear GNOME Keyring").strip())


def status() -> dict:
    return {"stored": retrieve() is not None}
