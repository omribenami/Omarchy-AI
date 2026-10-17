"""runtime/sandbox.py: the bubblewrap layer under the Task Runtime and user tools.

The tests marked by `needs_bwrap` start real bwrap with harmless commands
(`cat`, `sh -c`, `true`) in a fake home; they skip where bwrap or user
namespaces are unavailable (some CI runners). The fake home is created
under this repository, not /tmp, because /tmp is writable in the sandbox.
"""

import json
import os
import secrets
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import no_live_desktop
from omarchy_ai.execution import user_tools
from omarchy_ai.runtime import sandbox, shell
from omarchy_ai.runtime.permissions import Risk, Scope, classify

REPO = Path(__file__).resolve().parent.parent


def _bwrap_works() -> bool:
    sandbox.available.cache_clear()
    try:
        return sandbox.available()
    finally:
        sandbox.available.cache_clear()


needs_bwrap = unittest.skipUnless(
    _bwrap_works(), "bwrap or user namespaces unavailable here"
)


class FakeHome(unittest.TestCase):
    """A home directory with a fake key in ~/.ssh, a project workspace and
    a folder outside it; HOME points at it for the test."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix=".sandbox-test-", dir=REPO))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        (self.home / ".ssh").mkdir(mode=0o700)
        self.secret = secrets.token_hex(16)  # generated, never a literal
        (self.home / ".ssh" / "id_ed25519").write_text(self.secret)
        self.ws = self.home / "project"
        self.ws.mkdir()
        self.other = self.home / "elsewhere"
        self.other.mkdir()
        env = patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        on = patch.object(sandbox, "enabled", return_value=True)
        on.start()
        self.addCleanup(on.stop)
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "LANG": "C",
        }

    def run_at(self, command: str, risk: Risk, cwd: Path | None = None, **kw):
        cwd = cwd or self.ws
        assessment = classify(command, cwd, Scope(workspaces=[self.ws]))
        policy = sandbox.for_command(
            command, risk, cwd=cwd, workspace=self.ws, writes=assessment.writes, **kw
        )
        return shell.run(command, cwd, timeout=20, env=self.env, sandbox=policy), policy


@needs_bwrap
class SandboxedCommandTests(FakeHome):
    def test_private_key_is_unreadable_at_low(self):
        result, policy = self.run_at(f"cat {self.home}/.ssh/id_ed25519", Risk.LOW)
        self.assertIsNotNone(policy)
        self.assertNotEqual(result.exit_code, 0)
        self.assertNotIn(self.secret, result.output)

    def test_obfuscated_read_of_the_key_is_unreadable_too(self):
        command = 'f=$(printf \'%s/.s%s/id_ed25519\' "$HOME" sh); cat "$f"'
        result, _ = self.run_at(command, Risk.NORMAL)
        self.assertNotIn(self.secret, result.output)

    def test_ssh_sees_the_folder_read_only(self):
        policy = sandbox.for_command(
            "ssh -V", Risk.NORMAL, cwd=self.ws, workspace=self.ws
        )
        self.assertIn((self.home / ".ssh", False), policy.expose)
        self.assertNotIn(self.home / ".ssh", policy.hidden)

    def test_write_outside_the_workspace_fails(self):
        target = self.other / "note.txt"
        # Not a write target the classifier can see: only the sandbox stops it.
        result, _ = self.run_at(
            f"python3 -c \"open('{target}', 'w').write('x')\"", Risk.NORMAL
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Read-only file system", result.output)
        self.assertFalse(target.exists())

    def test_workspace_write_succeeds(self):
        result, _ = self.run_at("echo done > note.txt && cat note.txt", Risk.LOW)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual((self.ws / "note.txt").read_text().strip(), "done")

    def test_a_write_target_the_command_names_is_writable(self):
        result, _ = self.run_at(f"echo kept > {self.other}/named.txt", Risk.NORMAL)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual((self.other / "named.txt").read_text().strip(), "kept")

    def test_config_is_read_only_below_elevated_and_approved_at_elevated(self):
        (self.home / ".config" / "app").mkdir(parents=True)
        command = f"echo x > {self.home}/.config/app/settings.conf"
        result, _ = self.run_at(command, Risk.NORMAL)
        self.assertNotEqual(result.exit_code, 0)
        result, _ = self.run_at(command, Risk.ELEVATED)
        self.assertEqual(result.exit_code, 0, result.output)

    def test_network_allowed_unless_the_policy_says_otherwise(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        self.addCleanup(server.close)
        threading.Thread(target=lambda: server.accept()[0].close(), daemon=True).start()
        probe = f"python3 -c \"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=5)\""
        result, _ = self.run_at(probe, Risk.LOW)
        self.assertEqual(result.exit_code, 0, result.output)
        result, _ = self.run_at(probe, Risk.LOW, network=False)
        self.assertNotEqual(result.exit_code, 0)

    def test_high_runs_unsandboxed(self):
        self.assertIsNone(
            sandbox.for_command("true", Risk.HIGH, cwd=self.ws, workspace=self.ws)
        )
        result, policy = self.run_at("echo ${OMARCHY_AI_SANDBOX:-none}", Risk.HIGH)
        self.assertIsNone(policy)
        self.assertEqual(result.output.strip(), "none")
        result, _ = self.run_at("echo ${OMARCHY_AI_SANDBOX:-none}", Risk.LOW)
        self.assertEqual(result.output.strip(), "LOW")

    def test_session_sockets_are_hidden(self):
        runtime = self.home / "run"
        runtime.mkdir()
        (runtime / "control.sock").write_text("")
        self.env["XDG_RUNTIME_DIR"] = str(runtime)
        result, _ = self.run_at(f"ls -A {runtime}", Risk.LOW)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output.strip(), "")

    def test_read_only_failure_explains_the_sandbox(self):
        result, policy = self.run_at(
            f"python3 -c \"open('{self.other}/x', 'w')\"", Risk.NORMAL
        )
        self.assertIn("[sandbox]", sandbox.explain_failure(result.output, policy))


@needs_bwrap
class SandboxedToolTests(FakeHome):
    def setUp(self):
        super().setUp()
        self.tools = self.home / "tools"
        self.tools.mkdir()
        for attr, value in (
            ("TOOLS_DIR", self.tools),
            ("PROPOSED_DIR", self.tools / "_proposed"),
            ("LEDGER", self.tools / "approvals.json"),
        ):
            p = patch.object(user_tools, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def install(self, name: str, run: str, spec_extra: dict | None = None) -> None:
        folder = self.tools / name
        folder.mkdir()
        (folder / "tool.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "description": "Reads things for a sandbox test, nothing else.",
                    "parameters": {"type": "object", "properties": {}},
                    **(spec_extra or {}),
                }
            )
        )
        (folder / "run").write_text(run)
        (folder / "test").write_text("#!/bin/sh\nexit 0\n")
        for part in ("run", "test"):
            (folder / part).chmod(0o755)
        user_tools.LEDGER.write_text(
            json.dumps(
                {name: {"approved_at": "now", "sha256": user_tools._hashes(folder)}}
            )
        )

    def test_tool_cannot_read_keys_or_write_home(self):
        self.install(
            "probe_tool",
            f"#!/bin/sh\ncat {self.home}/.ssh/id_ed25519; touch {self.home}/x || echo ro\n",
        )
        result = user_tools.run("probe_tool", {})
        self.assertNotIn(self.secret, result.message)
        self.assertIn("ro", result.message)
        self.assertFalse((self.home / "x").exists())

    def test_tool_reads_its_own_folder(self):
        self.install(
            "own_config",
            '#!/bin/sh\ncat "$(dirname "$0")/tool.json" >/dev/null && echo read-ok\n',
        )
        result = user_tools.run("own_config", {})
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.message, "read-ok")

    def test_opt_out_runs_unsandboxed(self):
        self.install(
            "no_box",
            "#!/bin/sh\necho ${OMARCHY_AI_SANDBOX:-none}\n",
            {"sandbox": False},
        )
        self.assertEqual(user_tools.run("no_box", {}).message, "none")


class PolicyTests(unittest.TestCase):
    """No bwrap needed: what each policy would bind."""

    def test_programs_sees_through_wrappers_and_substitutions(self):
        names = sandbox.programs(
            "timeout 5 bash -c 'wpctl status' && echo $(hyprctl -j monitors) | xargs notify-send"
        )
        self.assertTrue({"wpctl", "hyprctl", "notify-send", "timeout"} <= names)

    def test_desktop_programs_get_only_their_sockets(self):
        ws = Path(tempfile.gettempdir())
        audio = sandbox.for_command(
            "wpctl get-volume @DEFAULT_AUDIO_SINK@", Risk.LOW, cwd=ws, workspace=ws
        )
        self.assertEqual(audio.sockets, {"pipewire", "dbus"})
        self.assertIsNotNone(audio.dbus_talk)  # filtered, not the raw bus
        notify = sandbox.for_command(
            "notify-send hi", Risk.NORMAL, cwd=ws, workspace=ws
        )
        self.assertEqual(notify.dbus_talk, {"org.freedesktop.Notifications"})
        plain = sandbox.for_command("ls -la", Risk.LOW, cwd=ws, workspace=ws)
        self.assertEqual(plain.sockets, set())
        user_unit = sandbox.for_command(
            "systemctl --user restart thing", Risk.ELEVATED, cwd=ws, workspace=ws
        )
        self.assertIn("dbus", user_unit.sockets)
        self.assertIsNone(user_unit.dbus_talk)

    def test_argv_binds_runtime_tmpfs_and_hides_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "run"
            runtime.mkdir()
            (runtime / "pipewire-0").write_text("")
            hidden = Path(tmp) / "secret-dir"
            hidden.mkdir()
            policy = sandbox.Policy(
                level=Risk.LOW,
                writable=[Path(tmp)],
                hidden=[hidden],
                sockets={"pipewire"},
            )
            argv = sandbox.build_argv(
                ["true"], policy, env={"XDG_RUNTIME_DIR": str(runtime)}, cwd=tmp
            )
        joined = " ".join(argv)
        self.assertIn(f"--tmpfs {runtime}", joined)
        self.assertIn(f"--bind {runtime}/pipewire-0 {runtime}/pipewire-0", joined)
        self.assertIn(f"--tmpfs {hidden} --remount-ro {hidden}", joined)
        self.assertEqual(argv[-2:], ["--", "true"])
        self.assertNotIn("--unshare-net", argv)

    def test_pure_tools_cli_is_left_to_the_cli(self):
        self.assertIsNone(
            sandbox.for_command(
                "omarchy-ai-tool run some_tool '{}'",
                Risk.NORMAL,
                cwd="/tmp",
                workspace="/tmp",
            )
        )
        self.assertIsNotNone(
            sandbox.for_command(
                "omarchy-ai-tool run some_tool '{}'; cat x",
                Risk.NORMAL,
                cwd="/tmp",
                workspace="/tmp",
            )
        )

    def test_tool_sandbox_field(self):
        folder = Path(tempfile.gettempdir())
        self.assertIsNone(sandbox.for_tool(folder, {"sandbox": False}))
        default = sandbox.for_tool(folder, {})
        self.assertEqual(default.dbus_talk, {"org.freedesktop.secrets"})
        desktop = sandbox.for_tool(folder, {"sandbox": {"needs": ["desktop"]}})
        self.assertIn("wayland", desktop.sockets)
        self.assertFalse(
            sandbox.for_tool(folder, {"sandbox": {"network": False}}).network
        )

    def test_tool_json_sandbox_field_is_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for part in ("run", "test"):
                (folder / part).write_text("#!/bin/sh\n")
                (folder / part).chmod(0o755)
            base = {
                "name": "box_check",
                "description": "Checks the sandbox field validation only.",
            }
            (folder / "tool.json").write_text(
                json.dumps({**base, "sandbox": {"needs": ["root"]}})
            )
            self.assertTrue(any("sandbox" in p for p in user_tools.validate(folder)[1]))
            (folder / "tool.json").write_text(
                json.dumps({**base, "sandbox": {"needs": ["assistant_keys"]}})
            )
            self.assertEqual(user_tools.validate(folder)[1], [])

    def test_disabled_or_missing_bwrap_runs_the_command_as_is(self):
        policy = sandbox.Policy(level=Risk.LOW)
        with patch.object(sandbox, "enabled", return_value=False):
            with sandbox.wrapped(["true"], policy, env={}, cwd="/") as argv:
                self.assertEqual(argv, ["true"])
        sandbox.available.cache_clear()
        self.addCleanup(sandbox.available.cache_clear)
        with (
            patch.object(sandbox, "enabled", return_value=True),
            patch.object(sandbox.shutil, "which", return_value=None),
            self.assertLogs("omarchy_ai.runtime.sandbox", "WARNING") as logs,
        ):
            with sandbox.wrapped(["true"], policy, env={}, cwd="/") as argv:
                self.assertEqual(argv, ["true"])
        self.assertIn("bwrap is not installed", logs.output[0])

    def test_test_guard_checks_what_bwrap_runs(self):
        with self.assertRaises(no_live_desktop.LiveDesktopCall):
            no_live_desktop.check(
                ["bwrap", "--ro-bind", "/", "/", "--", "hyprctl", "monitors"]
            )
        no_live_desktop.check(["bwrap", "--ro-bind", "/", "/", "--", "true"])


if __name__ == "__main__":
    unittest.main()
