"""Verify minisign signatures on release bundles.

The release workflow (.github/workflows/release.yml) signs each tarball with
minisign and publishes `<tarball>.minisig` next to it. The updater
(core/updates.py) checks that file against the public keys pinned in
release-keys.pub, which ships inside the running install, so a release can
never vouch for itself with a key of its own.

minisign is Ed25519, which `cryptography` (already a direct dependency)
provides; BLAKE2b is in hashlib. The format (https://jedisct1.github.io/minisign/):

    public key line:  base64("Ed" + key_id[8] + public_key[32])
    signature file:   untrusted comment: <text>
                      base64(alg[2] + key_id[8] + signature[64])
                      trusted comment: <text>
                      base64(global_signature[64])

alg "ED" signs BLAKE2b-512(file) (minisign's default); legacy "Ed" signs the
file bytes. The global signature covers signature + trusted comment, so the
trusted comment (timestamp and file name) cannot be swapped.

    python -m omarchy_ai.core.signing verify FILE SIG [--keys-file F | --key LINE]
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

KEYS_FILE = Path(__file__).with_name("release-keys.pub")
SIGNATURE_SUFFIX = ".minisig"
MAX_SIGNATURE_BYTES = 4096
_TRUSTED_PREFIX = "trusted comment: "


class SignatureError(ValueError):
    """A signature that is malformed, from an unknown key, or does not verify."""


@dataclass(frozen=True)
class PublicKey:
    key_id: bytes
    key: Ed25519PublicKey

    @property
    def key_id_hex(self) -> str:
        # minisign prints the key id as little-endian hex.
        return self.key_id[::-1].hex().upper()


def _b64(text: str, what: str) -> bytes:
    try:
        return base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SignatureError(f"{what} is not valid base64") from exc


def parse_public_key(line: str) -> PublicKey:
    raw = _b64(line, "public key")
    if len(raw) != 42 or raw[:2] != b"Ed":
        raise SignatureError("not a minisign Ed25519 public key")
    return PublicKey(raw[2:10], Ed25519PublicKey.from_public_bytes(raw[10:]))


def parse_public_keys(text: str) -> list[PublicKey]:
    """Every key line in a .pub file; comments and blank lines are skipped."""
    keys = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("untrusted comment:"):
            continue
        keys.append(parse_public_key(line))
    return keys


def trusted_keys(path: Path | None = None) -> list[PublicKey]:
    """The keys this install accepts release signatures from (may be empty)."""
    try:
        return parse_public_keys((path or KEYS_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def _blake2b(path: Path) -> bytes:
    digest = hashlib.blake2b(digest_size=64)
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.digest()


def verify_file(
    path: Path,
    signature: str,
    keys: list[PublicKey],
    *,
    expect_file: str | None = None,
) -> str:
    """Raise SignatureError unless `signature` (minisig text) signs `path`
    with one of `keys`. Returns the trusted comment. With `expect_file`, the
    trusted comment must name that file (minisign's default `file:` field)."""
    if not keys:
        raise SignatureError("no trusted release key to verify against")
    if len(signature.encode("utf-8", errors="replace")) > MAX_SIGNATURE_BYTES:
        raise SignatureError("signature file too large")
    lines = [line.rstrip("\r") for line in signature.splitlines()]
    if len(lines) < 4 or not lines[0].startswith("untrusted comment:"):
        raise SignatureError("not a minisign signature file")
    if not lines[2].startswith(_TRUSTED_PREFIX):
        raise SignatureError("signature has no trusted comment")
    raw = _b64(lines[1], "signature")
    if len(raw) != 74 or raw[:2] not in (b"ED", b"Ed"):
        raise SignatureError("unsupported signature algorithm")
    algorithm, key_id, sig = raw[:2], raw[2:10], raw[10:]
    key = next((k for k in keys if k.key_id == key_id), None)
    if key is None:
        raise SignatureError(
            f"signed by unknown key {key_id[::-1].hex().upper()}, not a pinned release key"
        )
    message = _blake2b(path) if algorithm == b"ED" else path.read_bytes()
    try:
        key.key.verify(sig, message)
    except InvalidSignature as exc:
        raise SignatureError("signature does not match the file") from exc
    comment = lines[2][len(_TRUSTED_PREFIX) :]
    global_sig = _b64(lines[3], "trusted comment signature")
    try:
        key.key.verify(global_sig, sig + comment.encode("utf-8"))
    except InvalidSignature as exc:
        raise SignatureError("trusted comment signature does not verify") from exc
    if expect_file is not None:
        fields = dict(part.split(":", 1) for part in comment.split("\t") if ":" in part)
        if fields.get("file") != expect_file:
            raise SignatureError(
                f"signature is for {fields.get('file') or 'an unnamed file'}, not {expect_file}"
            )
    return comment


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omarchy_ai.core.signing")
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify", help="verify a minisign signature")
    verify.add_argument("file", type=Path)
    verify.add_argument("signature", type=Path)
    source = verify.add_mutually_exclusive_group()
    source.add_argument("--keys-file", type=Path, help=f"default: {KEYS_FILE}")
    source.add_argument("--key", help="one public key line instead of a file")
    args = parser.parse_args(argv)
    keys = [parse_public_key(args.key)] if args.key else trusted_keys(args.keys_file)
    try:
        comment = verify_file(
            args.file,
            args.signature.read_text(encoding="utf-8"),
            keys,
            expect_file=args.file.name,
        )
    except SignatureError as exc:
        print(f"{args.file.name}: signature REFUSED: {exc}", file=sys.stderr)
        return 1
    print(f"{args.file.name}: signature OK ({comment})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
