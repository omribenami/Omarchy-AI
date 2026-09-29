"""Producer caps for the settings panel's clipboard and helper streams.

Marketplace review of omarchy-ai.settings (omacom/omarchy-plugin-marketplace#9139)
blocked verification because Panel.qml collected `wl-paste` and the settings
helper with StdioCollector and no producer-side byte limit or deadline.
"""

import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
SCRIPT = ROOT / "quickshell/plugins/omarchy-ai.settings/bounded-stdio.sh"
PANEL = ROOT / "quickshell/plugins/omarchy-ai.settings/Panel.qml"
SEED_PANEL = ROOT / "scripts/marketplace-plugin/seed/Panel.qml"
SEED_SCRIPT = ROOT / "scripts/marketplace-plugin/seed/bounded-stdio.sh"


def run_bounded(mode, command, *, stdin=None, env=None, timeout=15):
    return subprocess.run(
        ["/usr/bin/bash", str(SCRIPT), mode, "--", *command],
        input=stdin,
        capture_output=True,
        env=env,
        timeout=timeout,
        check=False,
    )


class BoundedStdioTests(unittest.TestCase):
    def test_script_is_executable_and_pins_the_supervisor(self):
        mode = SCRIPT.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)
        text = SCRIPT.read_text()
        self.assertIn("MAX=4096", text)
        self.assertIn("DEADLINE=2", text)
        self.assertIn("KILL_AFTER=1", text)
        self.assertIn("MAX=262144", text)
        self.assertIn("DEADLINE=20", text)
        self.assertIn("KILL_AFTER=2", text)
        self.assertIn("/usr/bin/setsid -w /usr/bin/timeout -k", text)
        self.assertIn("/usr/bin/head -c $((MAX + 1))", text)

    def test_paste_returns_a_short_clipboard_value(self):
        result = run_bounded("paste", ["/usr/bin/printf", "%s", "sk-live-key"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"0\nsk-live-key")

    def test_paste_rejects_overflow_without_forwarding_it(self):
        result = run_bounded(
            "paste",
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'A'*8000)"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"1\nClipboard text is too long to be an API key\n")
        self.assertNotIn(b"AAAA", result.stdout)

    def test_environment_cannot_raise_the_cap(self):
        env = dict(os.environ, MAX="8", DEADLINE="1")
        result = run_bounded("paste", ["/usr/bin/printf", "%s", "sk-live-key"], env=env)
        self.assertEqual(result.stdout, b"0\nsk-live-key")

    def test_helper_stdin_reaches_the_child_unchanged(self):
        result = run_bounded("helper", ["/usr/bin/cat"], stdin=b"p@ss'word\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"0\np@ss'word")

    def test_hung_producer_that_ignores_term_is_killed(self):
        result = run_bounded(
            "paste",
            ["/usr/bin/bash", "-c", "trap '' TERM; sleep 30"],
            timeout=8,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"1\nClipboard read timed out\n")

    def test_helper_overflow_is_a_small_error(self):
        result = run_bounded(
            "helper",
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'Z'*300000)"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"1\nSettings helper output exceeded 262144 bytes\n")
        self.assertNotIn(b"ZZZZ", result.stdout)
        self.assertLess(len(result.stdout), 200)

    def test_child_failure_does_not_parse_partial_output(self):
        result = run_bounded(
            "helper",
            [sys.executable, "-c", "import sys; sys.stdout.write('not-json'); sys.exit(1)"],
        )
        self.assertEqual(result.stdout, b"1\nSettings helper failed\n")
        self.assertNotIn(b"not-json", result.stdout)

    def test_panel_routes_both_streams_through_the_supervisor(self):
        text = PANEL.read_text()
        self.assertEqual(text, SEED_PANEL.read_text())
        self.assertEqual(SCRIPT.read_bytes(), SEED_SCRIPT.read_bytes())
        self.assertIn('readonly property int apiKeyMaxBytes: 4096', text)
        self.assertIn('readonly property int pasteDeadlineSec: 2', text)
        self.assertIn('readonly property int settingsMaxBytes: 262144', text)
        self.assertIn('readonly property int settingsDeadlineSec: 20', text)
        self.assertIn('root._boundedCommand("paste", ["/usr/bin/wl-paste", "--no-newline", "--type", "text"])', text)
        self.assertIn('root._boundedCommand("helper", next.argv)', text)
        self.assertIn('"/usr/bin/bash", root._localPath("resolve-settings.sh")', text)
        self.assertNotIn("@OMARCHY_AI_SETTINGS@", text)
        self.assertEqual(
            (ROOT / "quickshell/plugins/omarchy-ai.settings/resolve-settings.sh").read_bytes(),
            (ROOT / "scripts/marketplace-plugin/seed/resolve-settings.sh").read_bytes(),
        )
        self.assertIn("configure-sudo", text)
        self.assertIn("forget-sudo", text)
        self.assertIn('stdinEnabled: true', text)
        self.assertIn('write(_stdinText + "\\n")', text)
        self.assertNotIn('command: ["wl-paste"', text)
        for value in (
            "OMARCHY_AI_SUDO_PASSWORD",
            "OMARCHY_AI_APPROVAL_PIN",
            "OMARCHY_AI_API_KEY",
            "settingsProc.environment",
        ):
            self.assertNotIn(value, text)

    def test_settings_helper_refuses_to_emit_an_oversize_document(self):
        from omarchy_ai.cli import settings

        self.assertEqual(settings.HELPER_STDOUT_MAX_BYTES, 262144)
        small = settings._response_bytes({"ok": True})
        self.assertEqual(json.loads(small), {"ok": True})
        huge = settings._response_bytes({"blob": "x" * (settings.HELPER_STDOUT_MAX_BYTES + 10)})
        self.assertLessEqual(len(huge), settings.HELPER_STDOUT_MAX_BYTES)
        self.assertEqual(
            json.loads(huge),
            {"error": "Settings helper output exceeded 262144 bytes"},
        )

    def test_wrapper_frames_a_stdin_secret_without_echoing_it(self):
        # Same shape as set-api-key: one stdin line in, one JSON object out,
        # and the secret must not be copied onto the stream the shell parses.
        key = "sk-" + ("a" * 40)
        child = (
            "import json,sys\n"
            "secret=sys.stdin.readline().rstrip('\\n')\n"
            "assert secret==sys.argv[1]\n"
            "json.dump({'api_key':{'set':True}}, sys.stdout)\n"
            "sys.stdout.write('\\n')\n"
        )
        result = run_bounded(
            "helper",
            [sys.executable, "-c", child, key],
            stdin=(key + "\n").encode(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith(b"0\n"), result.stdout)
        payload = json.loads(result.stdout.split(b"\n", 1)[1])
        self.assertTrue(payload["api_key"]["set"])
        self.assertNotIn(key.encode(), result.stdout)


if __name__ == "__main__":
    unittest.main()
