"""Regression: approved commands must not retain conflicting sudo flags."""

import unittest
from unittest.mock import patch

import no_live_desktop  # noqa: F401 -- run alone too: no real notification, push or desktop command
from omarchy_ai.runtime.runtime import _sudo


class SudoOptionsTests(unittest.TestCase):
    def test_phone_approval_normalizes_authentication_flags(self):
        with patch("omarchy_ai.execution.flux_approve.sudo_ready", return_value=True):
            for prefix in ("sudo", "sudo -n", "sudo -S -p ''", "sudo --non-interactive"):
                with self.subTest(prefix=prefix):
                    command, stdin = _sudo(prefix + " pacman -Syu 2>&1 | tail -15")
                    self.assertEqual(command, "sudo -S -p '' pacman -Syu 2>&1 | tail -15")
                    self.assertEqual(stdin, "")

    def test_program_options_are_preserved(self):
        with patch("omarchy_ai.execution.flux_approve.sudo_ready", return_value=True):
            command, _ = _sudo("sudo -n echo -n 'sudo -p example'")
            self.assertEqual(command, "sudo -S -p '' echo -n 'sudo -p example'")


class OwnerSudoTests(unittest.TestCase):
    """The user, 2026-10-25: recognized by voice or by phone, she gets the job
    done even with sudo: no approval and no fingerprint."""

    def setUp(self):
        import secrets
        from types import SimpleNamespace
        self.secret = secrets.token_hex(8)  # never a literal password
        for p in (patch("omarchy_ai.execution.flux_approve.sudo_ready", return_value=True),
                  patch("omarchy_ai.config.load_config", return_value=SimpleNamespace(sudo_access_enabled=True)),
                  patch("omarchy_ai.execution.sudo_approval.retrieve", side_effect=lambda: self.secret)):
            p.start()
            self.addCleanup(p.stop)

    def test_owner_feeds_the_saved_password_instead_of_the_phone(self):
        command, stdin = _sudo("sudo pacman -Syu", owner=True)
        self.assertEqual(command, "sudo -S -p '' pacman -Syu")
        self.assertEqual(stdin, self.secret + "\n")

    def test_others_keep_the_phone(self):
        self.assertEqual(_sudo("sudo pacman -Syu"), ("sudo -S -p '' pacman -Syu", ""))

    def test_owner_without_a_saved_password_falls_back_to_the_phone(self):
        self.secret = None
        with patch("omarchy_ai.execution.sudo_approval.retrieve", return_value=None):
            self.assertEqual(_sudo("sudo pacman -Syu", owner=True), ("sudo -S -p '' pacman -Syu", ""))

    def test_recognized_owner_is_voice_or_phone_only(self):
        from omarchy_ai.core import identities
        with patch.object(identities, "owner", return_value="owner"):
            for who, expected in ((identities.Identity("owner", "voice"), True),
                                  (identities.Identity("owner", "phone"), True),
                                  (identities.Identity("owner", "typed"), False),
                                  (identities.Identity("guest", "voice"), False),
                                  (identities.Identity(), False)):
                with self.subTest(who=who):
                    self.assertIs(who.recognized_owner, expected)
        with patch.object(identities, "owner", return_value=None):
            self.assertFalse(identities.Identity("owner", "voice").recognized_owner)

    def test_permission_decision(self):
        from omarchy_ai.runtime.permissions import Assessment, Risk, decide
        allow = lambda a, owner: decide("command", "sudo x", a, auto_approve=Risk.NORMAL, grants=set(),
                                        owner_sudo=owner).behavior
        self.assertEqual(allow(Assessment(Risk.HIGH, ["runs as root"]), True), "allow")
        self.assertEqual(allow(Assessment(Risk.HIGH, ["runs as root"]), False), "ask")
        self.assertEqual(allow(Assessment(Risk.HIGH, ["removes packages"], always_ask=True), True), "ask")
        self.assertEqual(allow(Assessment(Risk.BLOCKED, ["formats a filesystem"]), True), "deny")

    def test_live_terminal_sudo_after_untrusted_content(self):
        from omarchy_ai.core import approvals, identities
        with patch.object(identities, "owner", return_value="owner"):
            for who, level in ((identities.Identity("owner", "voice"), approvals.AUTO),
                               (identities.Identity("owner", "phone"), approvals.AUTO),
                               (identities.Identity("guest", "voice"), approvals.TAP),
                               (identities.Identity(), approvals.TAP)):
                with self.subTest(who=who), patch.object(identities, "current", return_value=who):
                    verdict = approvals.decide("terminal_sudo", {"name": "build"}, tainted=True, said="update it")
                    self.assertEqual(verdict.level, level, verdict)
            # The password typed into the user's own windows stays strict.
            with patch.object(identities, "current", return_value=identities.Identity("owner", "voice")):
                self.assertEqual(approvals.decide("submit_sudo_password", {}, tainted=True).level, approvals.TAP)
