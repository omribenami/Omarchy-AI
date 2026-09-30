"""Pairing a phone through Flux instead of a QR code.

Flux (github.com/bjarneo/flux) pairs a phone with this computer on its own
and keeps each paired phone's certificate in `$XDG_DATA_HOME/flux/devices.json`.
Its Omarchy AI screen proves it is one of those phones: it asks for a
one-time challenge, signs it with the Flux key of the phone, and the server
checks the signature against the certificate that desktop Flux pinned. A
phone that Flux has not paired with this computer gets nothing, and
unpairing it in Flux stops new sessions.

The signed text also carries the key that the phone pinned for this server,
so a look-alike server on the same address cannot pass the signature on:
it would carry the look-alike's key, not ours.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

_CHALLENGE_TTL_SECONDS = 60
_MAX_CHALLENGES = 32  # a handful of phones at once; bounds memory if something loops
_challenges: dict[str, float] = {}
_lock = threading.Lock()


def devices_path() -> Path:
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data) / "flux" / "devices.json"


def key_pin(cert_path: Path) -> str:
    """SHA-256 of the certificate's SubjectPublicKeyInfo, base64 — the same
    value Flux's `OmarchyAi.keyPin` pins, and stable across restarts because
    the server keeps its key."""
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    spki = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(hashlib.sha256(spki).digest()).decode()


def new_challenge() -> str:
    nonce = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        for old, expires in list(_challenges.items()):
            if expires < now:
                del _challenges[old]
        while len(_challenges) >= _MAX_CHALLENGES:
            del _challenges[min(_challenges, key=_challenges.__getitem__)]
        _challenges[nonce] = now + _CHALLENGE_TTL_SECONDS
    return nonce


def _take_challenge(nonce: str) -> bool:
    with _lock:
        expires = _challenges.pop(nonce, None)
    return expires is not None and expires >= time.time()


def signed_text(nonce: str, pin: str, device_id: str) -> bytes:
    # A fixed first line keeps this signature from meaning anything to any
    # other protocol the Flux key is used for.
    return f"omarchy-ai-flux-pair\n{nonce}\n{pin}\n{device_id}".encode()


def _paired_certificate(device_id: str) -> x509.Certificate | None:
    try:
        devices = json.loads(devices_path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(devices, list):
        return None
    for device in devices:
        if isinstance(device, dict) and device.get("id") == device_id and device.get("certificate"):
            try:
                return x509.load_pem_x509_certificate(device["certificate"].encode())
            except ValueError:
                return None
    return None


def verify(device_id: str, nonce: str, signature_b64: str, pin: str) -> bool:
    """True when a phone that desktop Flux paired signed this server's fresh
    challenge. The challenge is spent either way."""
    if not (isinstance(device_id, str) and isinstance(nonce, str) and isinstance(signature_b64, str)):
        return False
    if not _take_challenge(nonce):
        return False
    cert = _paired_certificate(device_id)
    if cert is None:
        return False
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except ValueError:
        return False
    text = signed_text(nonce, pin, device_id)
    key = cert.public_key()
    try:
        if isinstance(key, rsa.RSAPublicKey):
            key.verify(signature, text, padding.PKCS1v15(), hashes.SHA256())
        elif isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(signature, text, ec.ECDSA(hashes.SHA256()))
        else:
            return False
    except InvalidSignature:
        return False
    return True
