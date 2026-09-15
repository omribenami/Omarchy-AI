"""Private local PCM handoff into the active casting sender's audio mixer."""
from __future__ import annotations

import os
import socket
from pathlib import Path


def socket_path() -> Path:
    directory = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    return directory / "omarchy-cast-voice.sock"


def send_pcm(data: bytes) -> None:
    if not data or len(data) > 8192 or len(data) % 2:
        raise ValueError("expected at most 8192 bytes of mono s16le PCM at 48000 Hz")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.settimeout(.2)
        sock.sendto(data, str(socket_path()))


def available() -> bool:
    # A zero-byte probe is ignored by the sender; detects stale sockets too.
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.settimeout(.1)
            sock.sendto(b"", str(socket_path()))
        return True
    except OSError:
        return False
