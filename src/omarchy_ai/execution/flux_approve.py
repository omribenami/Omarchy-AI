"""Approve with a fingerprint on the phone, through Flux.

Flux (github.com/bjarneo/flux) can approve `sudo` with a fingerprint: its
phone app keeps an EC P-256 key in the Android Keystore that signs only
after a strong biometric, and `sudo flux-cli approve setup` writes the
matching public key to `/etc/flux/approve/<user>.pub`, owned by root. Flux's
own PAM helper then asks the phone before the password.

Omarchy AI uses the same key for its own approvals (a waiting task), in
place of the approval PIN: it asks fluxd, over the user's fluxd socket, to
show the request on the phone, then checks the signature itself, over the
message it built itself, with the root-owned key. This mirrors Flux's
`internal/approve` (docs/approve.md there): fluxd and anything else running
as the user only carry messages, and cannot make a valid signature.

With Flux's PAM helper on for `sudo`, a `sudo` that Omarchy runs asks the
phone too (`sudo_ready`), so the saved Sudo Access password is not needed.
"""

from __future__ import annotations

import base64
import getpass
import json
import logging
import os
import re
import secrets
import socket
import stat
import struct
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_der_public_key

log = logging.getLogger(__name__)

KEY_DIR = Path("/etc/flux/approve")
PAM_SUDO = Path("/etc/pam.d/sudo")
_MAX_KEY_FILE = 16 << 10
_MAX_FIELD = 256
_MAX_LINE = 64 << 10
# fluxd's approve_timeout is 5-120 s (20 by default); the phone answers within it.
_MAX_WAIT = 130
_FUTURE = 5


@dataclass(frozen=True)
class Key:
    public: ec.EllipticCurvePublicKey
    device_id: str
    device_name: str


def key_path(user: str | None = None) -> Path:
    return KEY_DIR / f"{user or getpass.getuser()}.pub"


def _owned_by_root_only(st: os.stat_result, directory: bool) -> bool:
    if st.st_uid != 0:
        return False
    if st.st_mode & 0o022:
        # Flux allows a world-writable folder only with the sticky bit (/tmp style).
        return directory and bool(st.st_mode & stat.S_ISVTX)
    return True


def read_key(path: Path | None = None) -> Key | None:
    """The phone's approval key, or None when there is none or it is not
    safe: the same checks as Flux's ReadKey (a regular root-owned file that
    nobody else can write, in root-owned folders, opened without following
    links, an EC P-256 PUBLIC KEY)."""
    path = Path(path or key_path())
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or not _owned_by_root_only(st, False):
            return None
        for folder in path.parents:
            if not _owned_by_root_only(os.lstat(folder), True):
                return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
                return None
            data = handle.read(_MAX_KEY_FILE + 1)
    except OSError:
        return None
    if len(data) > _MAX_KEY_FILE:
        return None
    return parse_key(data)


def parse_key(data: bytes) -> Key | None:
    """A PEM PUBLIC KEY block with optional `Name: value` headers
    (Device-Id, Device-Name) before a blank line, as Flux writes it."""
    try:
        text = data.decode()
    except UnicodeDecodeError:
        return None
    match = re.search(r"-----BEGIN PUBLIC KEY-----\n(.*?)-----END PUBLIC KEY-----", text, re.S)
    if not match:
        return None
    headers, body = {}, match.group(1)
    if "\n\n" in body:
        head, body = body.split("\n\n", 1)
        for line in head.splitlines():
            name, _, value = line.partition(":")
            headers[name.strip()] = value.strip()
    try:
        public = load_der_public_key(base64.b64decode("".join(body.split()), validate=True))
    except ValueError:
        return None
    if not isinstance(public, ec.EllipticCurvePublicKey) or public.curve.name != "secp256r1":
        return None
    device = headers.get("Device-Id", "")
    if not device:
        return None
    return Key(public, device, headers.get("Device-Name") or "your phone")


def available() -> bool:
    """A phone can approve for this user: Flux enrolled one."""
    return read_key() is not None


def sudo_ready() -> bool:
    """`sudo` itself asks the phone first (Flux's PAM helper is on for it)."""
    try:
        return available() and "flux-approve" in PAM_SUDO.read_text()
    except OSError:
        return False


def _clean(value: str) -> str:
    """A field Flux accepts: no control characters (so no newline can add a
    line to the signed message), at most 256 bytes of UTF-8."""
    value = "".join(ch for ch in value if ch.isprintable()).strip()
    raw = value.encode()[:_MAX_FIELD]
    return raw.decode(errors="ignore")


def message(host: str, user: str, service: str, tty: str, rhost: str, when: int, nonce: str) -> bytes:
    """The exact bytes the phone signs (Flux's Request.Message)."""
    return (f"flux-approve-v1\nhost={host}\nuser={user}\nservice={service}\ntty={tty}\n"
            f"rhost={rhost}\ntime={when}\nnonce={nonce}\n").encode()


def verify(key: ec.EllipticCurvePublicKey, signed: bytes, signature: bytes) -> bool:
    try:
        key.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False


def _socket_path() -> Path:
    if path := os.environ.get("FLUX_SOCKET"):
        return Path(path)
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "flux" / "fluxd.sock"


class Fluxd:
    """A JSON-line client of the fluxd socket, which must belong to this user."""

    def __init__(self, timeout: float):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.settimeout(timeout)
            self.sock.connect(str(_socket_path()))
            creds = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", creds)
            if uid != os.getuid():
                raise OSError(f"the fluxd socket belongs to user {uid}")
        except OSError:
            self.sock.close()
            raise
        self.reader = self.sock.makefile("rb")
        self.next = 0

    def call(self, method: str, params: dict, timeout: float) -> dict:
        self.next += 1
        self.sock.settimeout(timeout)
        self.sock.sendall(json.dumps({"id": self.next, "method": method, "params": params}).encode() + b"\n")
        while True:
            line = self.reader.readline(_MAX_LINE + 1)
            if not line or len(line) > _MAX_LINE:
                raise OSError("fluxd closed the connection")
            reply = json.loads(line)
            if reply.get("id") != self.next:
                continue  # an event, or an old answer
            if reply.get("error"):
                raise OSError(reply["error"].get("message") or "fluxd refused")
            return reply.get("result") or {}

    def close(self) -> None:
        try:
            self.reader.close()
        finally:
            self.sock.close()


def request(what: str) -> tuple[bool | None, str]:
    """Show `what` on the phone and wait for the fingerprint. (True, _) only
    for a valid signature over this request; (False, _) when the user denied
    it; (None, reason) when no answer could be had (no key, fluxd or phone
    unreachable, timeout, a bad signature), so the caller falls back to the
    PIN or the desktop notification."""
    key = read_key()
    if key is None:
        return None, "no phone is enrolled for fingerprint approval"
    host, user = _clean(socket.gethostname()), _clean(getpass.getuser())
    service = _clean(what) or "Omarchy AI"
    nonce, started = secrets.token_hex(32), int(time.time())
    try:
        fluxd = Fluxd(timeout=2)
    except OSError as exc:
        return None, f"fluxd is not reachable: {exc}"
    try:
        begun = fluxd.call("approve.request", {"device": key.device_id, "host": host, "user": user,
                                               "service": service, "tty": "", "rhost": "",
                                               "time": started, "nonce": nonce}, timeout=3)
        request_id, wait = begun.get("id"), min(max(int(begun.get("timeout") or 20), 5), _MAX_WAIT)
        if not request_id:
            return None, "fluxd did not start the request"
        deadline = started + wait + 2
        while True:
            left = deadline - time.time()
            if left <= 0:
                return None, "no answer on the phone in time"
            result = fluxd.call("approve.wait", {"id": request_id}, timeout=left)
            state = result.get("state")
            if state == "approved":
                try:
                    signature = base64.b64decode(result.get("signature") or "", validate=True)
                except ValueError:
                    return None, "the phone sent a malformed signature"
                # Rebuilt from this process's own fields: nothing fluxd or the phone sent back.
                signed = message(host, user, service, "", "", started, nonce)
                if time.time() > started + wait + 3 or time.time() < started - _FUTURE:
                    return None, "the approval came too late"
                if not verify(key.public, signed, signature):
                    log.warning("flux approval: a signature that does not match the enrolled key")
                    return None, "the signature does not match the enrolled phone"
                return True, f"approved with a fingerprint on {key.device_name}"
            if state == "denied":
                return False, f"denied on {key.device_name}"
            if state == "failed":
                return None, f"the phone could not approve: {result.get('message', '')}"
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, f"fingerprint approval failed: {exc}"
    finally:
        fluxd.close()
