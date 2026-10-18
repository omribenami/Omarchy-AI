"""minisign verification of release bundles (core/signing.py).

Keys are generated per test run; nothing here is a real release key.
"""

import base64
import hashlib
import io
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from omarchy_ai.core import signing


class ThrowawayKey:
    """A throwaway minisign key pair that signs like `minisign -S`."""

    def __init__(self):
        self.private = Ed25519PrivateKey.generate()
        self.key_id = os.urandom(8)
        public = self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.public_line = base64.b64encode(b"Ed" + self.key_id + public).decode()

    def public(self):
        return signing.parse_public_key(self.public_line)

    def sign(self, path, *, file_name=None, prehashed=True, comment=None):
        data = Path(path).read_bytes()
        message = hashlib.blake2b(data, digest_size=64).digest() if prehashed else data
        algorithm = b"ED" if prehashed else b"Ed"
        signature = self.private.sign(message)
        if comment is None:
            comment = f"timestamp:{int(time.time())}\tfile:{file_name or Path(path).name}\thashed"
        global_signature = self.private.sign(signature + comment.encode())
        return (
            "untrusted comment: signature from minisign secret key\n"
            + base64.b64encode(algorithm + self.key_id + signature).decode()
            + "\n"
            + f"trusted comment: {comment}\n"
            + base64.b64encode(global_signature).decode()
            + "\n"
        )


class SigningTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.file = self.base / "omarchy-ai-0.4.0-linux-x86_64.tar.gz"
        self.file.write_bytes(b"bundle bytes")
        self.key = ThrowawayKey()

    def test_valid_prehashed_and_legacy_signatures_verify(self):
        for prehashed in (True, False):
            text = self.key.sign(self.file, prehashed=prehashed)
            comment = signing.verify_file(
                self.file, text, [self.key.public()], expect_file=self.file.name
            )
            self.assertIn(f"file:{self.file.name}", comment)

    def test_tampered_file_is_refused(self):
        text = self.key.sign(self.file)
        self.file.write_bytes(b"bundle bytes, changed")
        with self.assertRaisesRegex(signing.SignatureError, "does not match"):
            signing.verify_file(self.file, text, [self.key.public()])

    def test_unknown_key_is_refused(self):
        text = ThrowawayKey().sign(self.file)
        with self.assertRaisesRegex(signing.SignatureError, "unknown key"):
            signing.verify_file(self.file, text, [self.key.public()])

    def test_swapped_trusted_comment_is_refused(self):
        lines = self.key.sign(self.file).splitlines()
        lines[2] = "trusted comment: timestamp:1\tfile:other.tar.gz\thashed"
        with self.assertRaisesRegex(signing.SignatureError, "trusted comment"):
            signing.verify_file(self.file, "\n".join(lines), [self.key.public()])

    def test_signature_for_another_file_name_is_refused(self):
        text = self.key.sign(
            self.file, file_name="omarchy-ai-0.3.0-linux-x86_64.tar.gz"
        )
        with self.assertRaisesRegex(signing.SignatureError, "not omarchy-ai-0.4.0"):
            signing.verify_file(
                self.file, text, [self.key.public()], expect_file=self.file.name
            )

    def test_malformed_signatures_are_refused(self):
        for text in (
            "",
            "garbage",
            "untrusted comment: x\n!!!\ntrusted comment: y\nzz\n",
        ):
            with self.assertRaises(signing.SignatureError):
                signing.verify_file(self.file, text, [self.key.public()])
        with self.assertRaisesRegex(signing.SignatureError, "no trusted release key"):
            signing.verify_file(self.file, self.key.sign(self.file), [])

    def test_keys_file_skips_comments_and_missing_file_is_empty(self):
        keys = self.base / "keys.pub"
        keys.write_text(
            "# comment\nuntrusted comment: minisign public key\n\n"
            + self.key.public_line
            + "\n"
        )
        self.assertEqual(
            [k.key_id for k in signing.trusted_keys(keys)], [self.key.key_id]
        )
        self.assertEqual(signing.trusted_keys(self.base / "missing.pub"), [])

    def test_shipped_keys_file_parses(self):
        # It may be empty (no key yet) but must never be malformed: a bad line
        # would break every update check of the release that ships it.
        signing.trusted_keys()

    def test_cli_verifies_with_one_key_line(self):
        sig = self.base / (self.file.name + ".minisig")
        sig.write_text(self.key.sign(self.file))
        out = io.StringIO()
        with redirect_stdout(out):
            code = signing.main(
                ["verify", str(self.file), str(sig), "--key", self.key.public_line]
            )
        self.assertEqual(code, 0)
        self.assertIn("signature OK", out.getvalue())
        self.file.write_bytes(b"other")
        with redirect_stderr(io.StringIO()):
            code = signing.main(
                ["verify", str(self.file), str(sig), "--key", self.key.public_line]
            )
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
