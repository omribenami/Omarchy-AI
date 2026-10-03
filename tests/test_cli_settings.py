import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omarchy_ai.cli import settings
from omarchy_ai.phone import server


class RestartTests(unittest.TestCase):
    # Real regression (STATUS.md, 2026-09-22): cmd_restart originally
    # refused to restart mid-conversation (c42af67) and a later unrelated
    # cleanup (a3cd8c7) accidentally dropped that check, so "Apply saved
    # changes" could force-restart the daemon out from under a live
    # session. These pin the restored behavior so it can't silently regress
    # again the same way.

    def test_refuses_to_restart_a_busy_conversation_without_touching_systemctl(self):
        with patch.object(settings, "_conversation_busy", return_value=(True, "a conversation appears to be in progress")), \
                patch.object(settings.subprocess, "run") as run:
            result = settings.cmd_restart(None)
        run.assert_not_called()
        self.assertFalse(result["restarted"])
        self.assertEqual(result["reason"], "a conversation appears to be in progress")

    def test_restarts_when_not_busy(self):
        with patch.object(settings, "_conversation_busy", return_value=(False, "no conversation currently in progress")), \
                patch.object(settings.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            result = settings.cmd_restart(None)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["systemctl", "--user", "restart", settings.SERVICE])
        self.assertTrue(result["restarted"])

    def test_reports_the_real_systemctl_failure_reason(self):
        with patch.object(settings, "_conversation_busy", return_value=(False, "no conversation currently in progress")), \
                patch.object(settings.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], 1, "", "start request repeated too quickly")):
            result = settings.cmd_restart(None)
        self.assertFalse(result["restarted"])
        self.assertIn("too quickly", result["reason"])


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class PairPhoneArgvTests(unittest.TestCase):
    """The pairing URL redeems a session. It must not appear on any child argv.

    Callers of the pairing path, checked here and by the assertions below:
    the settings panel enqueues only `pair-phone` (QML), bounded-stdio.sh
    and resolve-settings.sh forward that argv, and the helper then starts
    `tailscale status --json` (no secret) and `qrencode` (URL on stdin).
    """

    def _record(self, calls, tailscale_stdout):
        def run(argv, *args, **kwargs):
            command = argv if isinstance(argv, (str, bytes)) else list(argv)
            calls.append({
                "argv": command if isinstance(command, list) else [command],
                "input": kwargs.get("input"),
                "env": kwargs.get("env"),
                "shell": kwargs.get("shell", False),
            })
            name = os.path.basename(str(calls[-1]["argv"][0]))
            if name == "qrencode":
                return subprocess.CompletedProcess(calls[-1]["argv"], 0, stdout=b"\x89PNG\r\n", stderr=b"")
            if name == "tailscale":
                return subprocess.CompletedProcess(calls[-1]["argv"], 0, stdout=tailscale_stdout, stderr="")
            self.fail(f"unexpected subprocess: {calls[-1]['argv']!r}")
        return run

    def _pair(self, tailscale_stdout):
        calls = []
        recorder = self._record(calls, tailscale_stdout)
        log = logging.getLogger("omarchy_ai")
        previous_level = log.level
        handler = _LogCapture()
        log.setLevel(logging.DEBUG)
        log.addHandler(handler)
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                token_path = root / "pending_pair_token.json"
                cfg = SimpleNamespace(phone_bridge_enabled=True, phone_bridge_port=8766)
                with patch.object(settings, "load_config", return_value=cfg), \
                        patch.object(settings.shutil, "which", return_value="/usr/bin/qrencode"), \
                        patch.object(server, "_CERT_DIR", root), \
                        patch.object(server, "_PAIR_TOKEN_PATH", token_path), \
                        patch.object(server, "_primary_lan_ip", return_value="192.0.2.20"), \
                        patch("subprocess.run", side_effect=recorder), \
                        patch("subprocess.Popen", side_effect=recorder), \
                        patch("subprocess.call", side_effect=recorder), \
                        patch("subprocess.check_call", side_effect=recorder), \
                        patch("subprocess.check_output", side_effect=recorder):
                    result = settings.cmd_pair_phone(None)
                self.assertTrue(token_path.is_file())
                mode = token_path.stat().st_mode & 0o777
                pending = json.loads(token_path.read_text())
                files = sorted(path.name for path in root.iterdir())
        finally:
            log.removeHandler(handler)
            log.setLevel(previous_level)
        return result, calls, handler.messages, mode, pending, files

    def _assert_secret_stays_off_argv(self, result, calls, logs, mode, pending):
        self.assertNotIn("error", result)
        url = result["url"]
        token = pending["token"]
        self.assertIn(token, url)
        self.assertIn("/pair?token=", url)
        self.assertFalse(pending["used"])
        self.assertGreater(pending["expires_at"], time.time())
        self.assertLessEqual(pending["expires_at"] - time.time(), server._PAIR_TOKEN_TTL_SECONDS)
        self.assertEqual(mode, 0o600)
        self.assertEqual(result["expires_at"], pending["expires_at"])
        qr_calls = [call for call in calls if os.path.basename(str(call["argv"][0])) == "qrencode"]
        self.assertEqual(len(qr_calls), 1)
        self.assertEqual(qr_calls[0]["input"], url.encode())
        self.assertFalse(qr_calls[0]["input"].endswith(b"\n"))
        self.assertFalse(qr_calls[0]["shell"])
        for call in calls:
            blob = "\0".join(str(part) for part in call["argv"])
            self.assertNotIn(token, blob)
            self.assertNotIn(url, blob)
            self.assertNotIn("/pair?token=", blob)
            self.assertFalse(call["shell"])
            env = call["env"] or {}
            env_blob = "\0".join(f"{key}={value}" for key, value in env.items())
            self.assertNotIn(token, env_blob)
            self.assertNotIn(url, env_blob)
        for message in logs:
            self.assertNotIn(token, message)
            self.assertNotIn(url, message)

    def test_lan_pairing_url_is_stdin_not_argv(self):
        result, calls, logs, mode, pending, files = self._pair('{"BackendState":"Stopped"}')
        self._assert_secret_stays_off_argv(result, calls, logs, mode, pending)
        self.assertEqual(result["url"], f"https://192.0.2.20:8766/pair?token={pending['token']}")
        self.assertEqual(files, ["pending_pair_token.json"])
        names = [os.path.basename(str(call["argv"][0])) for call in calls]
        self.assertEqual(result["qr_png_base64"], "iVBORw0K")
        self.assertEqual(names, ["tailscale", "qrencode"])
        self.assertEqual(calls[0]["argv"], ["tailscale", "status", "--json"])
        self.assertEqual(calls[1]["argv"], [
            "qrencode", "-t", "PNG", "-s", "8",
            f"--foreground={settings.QR_FOREGROUND}", f"--background={settings.QR_BACKGROUND}",
            "-o", "-",
        ])

    def test_tailnet_pairing_url_is_stdin_not_argv(self):
        status = json.dumps({
            "BackendState": "Running",
            "Self": {"TailscaleIPs": ["100.64.0.8", "fd7a::1"], "DNSName": "host.tail.ts.net."},
        })
        result, calls, logs, mode, pending, files = self._pair(status)
        self._assert_secret_stays_off_argv(result, calls, logs, mode, pending)
        self.assertEqual(result["qr_png_base64"], "iVBORw0K")
        self.assertEqual(result["url"], f"https://100.64.0.8:8766/pair?token={pending['token']}")
        self.assertEqual(files, ["pending_pair_token.json"])
        self.assertTrue(all("100.64.0.8" not in " ".join(map(str, call["argv"])) for call in calls))

    def test_qrencode_failure_does_not_echo_the_token(self):
        calls = []

        def run(argv, *args, **kwargs):
            command = list(argv)
            calls.append(command)
            name = os.path.basename(command[0])
            if name == "tailscale":
                return subprocess.CompletedProcess(command, 0, stdout='{"BackendState":"Stopped"}', stderr="")
            if name == "qrencode":
                leaked = kwargs.get("input") or b""
                return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"failed: " + leaked)
            self.fail(f"unexpected subprocess: {command!r}")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = SimpleNamespace(phone_bridge_enabled=True, phone_bridge_port=8766)
            with patch.object(settings, "load_config", return_value=cfg), \
                    patch.object(settings.shutil, "which", return_value="/usr/bin/qrencode"), \
                    patch.object(server, "_CERT_DIR", root), \
                    patch.object(server, "_PAIR_TOKEN_PATH", root / "pending_pair_token.json"), \
                    patch.object(server, "_primary_lan_ip", return_value="192.0.2.20"), \
                    patch("subprocess.run", side_effect=run):
                result = settings.cmd_pair_phone(None)
            pending = json.loads((root / "pending_pair_token.json").read_text())
        self.assertIn("error", result)
        self.assertNotIn(pending["token"], result["error"])
        self.assertNotIn("/pair?token=", result["error"])
        self.assertNotIn("192.0.2.20", result["error"])
        for argv in calls:
            blob = " ".join(argv)
            self.assertNotIn(pending["token"], blob)
            self.assertNotIn("/pair?token=", blob)

    @unittest.skipUnless(shutil.which("qrencode"), "needs qrencode")
    def test_real_qrencode_reads_the_url_from_stdin(self):
        # qrencode 4.1.1 encodes a stdin payload with no trailing newline as
        # the same PNG it encodes from a STRING argument. This runs that
        # binary through pair-phone and checks the child argv anyway.
        calls = []
        real_run = subprocess.run

        def run(argv, *args, **kwargs):
            command = list(argv)
            calls.append({
                "argv": command,
                "input": kwargs.get("input"),
                "env": kwargs.get("env"),
                "shell": kwargs.get("shell", False),
            })
            if os.path.basename(command[0]) == "tailscale":
                return subprocess.CompletedProcess(command, 0, stdout='{"BackendState":"Stopped"}', stderr="")
            return real_run(argv, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = SimpleNamespace(phone_bridge_enabled=True, phone_bridge_port=8766)
            with patch.object(settings, "load_config", return_value=cfg), \
                    patch.object(settings.shutil, "which", return_value=shutil.which("qrencode")), \
                    patch.object(server, "_CERT_DIR", root), \
                    patch.object(server, "_PAIR_TOKEN_PATH", root / "pending_pair_token.json"), \
                    patch.object(server, "_primary_lan_ip", return_value="192.0.2.20"), \
                    patch("subprocess.run", side_effect=run):
                result = settings.cmd_pair_phone(None)
            pending = json.loads((root / "pending_pair_token.json").read_text())
            mode = (root / "pending_pair_token.json").stat().st_mode & 0o777
        self._assert_secret_stays_off_argv(result, calls, [], mode, pending)
        png = base64.b64decode(result["qr_png_base64"])
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        again = real_run(
            calls[-1]["argv"],
            input=result["url"].encode(),
            capture_output=True,
            check=False,
        )
        self.assertEqual(again.returncode, 0)
        self.assertEqual(png, again.stdout)

    def test_panel_and_shell_forward_pair_phone_without_a_url(self):
        root = Path(__file__).resolve().parents[1]
        panel = (root / "quickshell/plugins/omarchy-ai.settings/Panel.qml").read_text()
        seed = (root / "scripts/marketplace-plugin/seed/Panel.qml").read_text()
        resolver = (root / "quickshell/plugins/omarchy-ai.settings/resolve-settings.sh").read_text()
        bounded = (root / "quickshell/plugins/omarchy-ai.settings/bounded-stdio.sh").read_text()
        self.assertEqual(panel, seed)
        self.assertIn('root._enqueue(["pair-phone"], function(result) {', panel)
        self.assertNotIn("qrencode", panel)
        self.assertIn('exec "$target" "$@"', resolver)
        self.assertNotIn("qrencode", resolver)
        self.assertNotIn("qrencode", bounded)


if __name__ == "__main__":
    unittest.main()
