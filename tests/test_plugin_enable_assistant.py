"""Enabling omarchy-ai.settings installs and starts the assistant.

`omarchy plugin add` clones the listing and, with `--enable`, loads the
plugin. It does not run a hook of its own. AssistantService.qml is that
load, and it runs start-assistant.sh. These tests run that script the way
the service does.
"""

import hashlib
import json
import os
import stat
import subprocess
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "quickshell/plugins/omarchy-ai.settings"
STARTER = PLUGIN / "start-assistant.sh"
RESOLVER = PLUGIN / "resolve-settings.sh"
SERVICE = PLUGIN / "AssistantService.qml"
PANEL = PLUGIN / "Panel.qml"
PINNED_SHA256 = "44238a9887c6f16bd33ad7ce123974357f4d51a618d34826483ee3121652eb42"
PINNED_VERSION = "0.13.1"


def write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


class PluginEnableAssistantTests(unittest.TestCase):
    def test_enable_loads_a_service_that_starts_the_assistant_without_a_path_edit(self):
        manifest = json.loads((PLUGIN / "manifest.json").read_text())
        self.assertEqual(manifest["id"], "omarchy-ai.settings")
        self.assertEqual(manifest["kinds"], ["bar-widget", "service"])
        self.assertEqual(manifest["entryPoints"]["service"], "AssistantService.qml")
        self.assertTrue(manifest["keepLoaded"])
        service = SERVICE.read_text()
        self.assertIn('"/usr/bin/bash", root._localPath("start-assistant.sh")', service)
        self.assertNotIn("StdioCollector", service)
        panel = PANEL.read_text()
        self.assertNotIn("@OMARCHY_AI_SETTINGS@", panel)
        self.assertNotRegex(panel, r"@[A-Z0-9_]+@")
        self.assertIn('"/usr/bin/bash", root._localPath("resolve-settings.sh")', panel)
        starter = STARTER.read_text()
        self.assertIn(PINNED_SHA256, starter)
        self.assertIn("https://github.com/omribenami/Omarchy-AI/releases/download/v${PINNED_VERSION}/", starter)
        self.assertIn("omarchy-ai-${PINNED_VERSION}-linux-x86_64.tar.gz", starter)
        self.assertIn("systemctl --user enable --now omarchy-ai.service", starter)
        self.assertNotIn("qrencode", starter)
        self.assertNotIn("pair-phone", starter)
        self.assertNotIn("/pair?token=", starter)
        self.assertNotRegex(starter, r"curl[^\n]*\|")
        self.assertNotIn("sudo", starter)
        self.assertNotIn("pkexec", starter)
        self.assertTrue(STARTER.stat().st_mode & stat.S_IXUSR)

    def test_enabling_installs_the_assistant_and_the_panel_can_find_it(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = self._archive(base / "release.tar.gz")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            env = self._env(base)
            env["OMARCHY_AI_ASSISTANT_ARCHIVE"] = str(archive)
            env["OMARCHY_AI_ASSISTANT_SHA256"] = digest
            marker = base / "config/omarchy/plugins/omarchy-ai.settings/MARKER"
            marker.parent.mkdir(parents=True)
            marker.write_text("marketplace checkout")
            result = self._run(env)
            self.assertEqual(
                result.returncode,
                0,
                result.stderr.decode() + result.stdout.decode(),
            )
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["started"])
            self.assertEqual(payload["reason"], "installed")
            self.assertEqual(marker.read_text(), "marketplace checkout")
            release = (
                base / "data/omachy-ai-releases"
                / f"omarchy-ai-{PINNED_VERSION}-linux-x86_64"
            )
            cli = release / ".venv/bin/omarchy-ai-settings"
            self.assertTrue(cli.is_file())
            unit = (base / "config/systemd/user/omarchy-ai.service").read_text()
            self.assertIn(f"WorkingDirectory={release}", unit)
            self.assertIn(f"ExecStart={release}/.venv/bin/python -m omarchy_ai", unit)
            commands = (base / "commands").read_text()
            self.assertIn("daemon-reload", commands)
            self.assertIn("enable --now omarchy-ai.service", commands)
            watchdog = (
                base / "config/omarchy/plugins/omarchy-ai.watchdog/Watchdog.qml"
            ).read_text()
            self.assertIn(str(cli), watchdog)
            self.assertNotIn("@OMARCHY_AI_SETTINGS@", watchdog)
            self.assertIn("plugin enable omarchy-ai.watchdog", commands)
            self.assertNotIn("plugin enable omarchy-ai.settings", commands)
            found = subprocess.run(
                ["/usr/bin/bash", str(RESOLVER), "get"],
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(found.returncode, 0, found.stderr)
            self.assertEqual(json.loads(found.stdout), {"ok": True, "from": "release"})
            self.assertFalse((base / "curl-used").exists())

    def test_second_enable_starts_an_inactive_service_without_downloading(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = self._archive(base / "release.tar.gz")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            env = self._env(base)
            env["OMARCHY_AI_ASSISTANT_ARCHIVE"] = str(archive)
            env["OMARCHY_AI_ASSISTANT_SHA256"] = digest
            first = self._run(env)
            self.assertEqual(first.returncode, 0, first.stderr.decode() + first.stdout.decode())
            (base / "curl-used").unlink(missing_ok=True)
            (base / "commands").write_text("")
            second = self._run(env)
            self.assertEqual(second.returncode, 0, second.stderr.decode() + second.stdout.decode())
            payload = json.loads(second.stdout)
            self.assertEqual(payload["reason"], "started")
            self.assertTrue(payload["started"])
            self.assertFalse((base / "curl-used").exists())
            self.assertIn("enable --now omarchy-ai.service", (base / "commands").read_text())

    def test_second_enable_starts_nothing_when_the_service_is_already_active(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = self._archive(base / "release.tar.gz")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            env = self._env(base)
            env["OMARCHY_AI_ASSISTANT_ARCHIVE"] = str(archive)
            env["OMARCHY_AI_ASSISTANT_SHA256"] = digest
            first = self._run(env)
            self.assertEqual(first.returncode, 0, first.stderr)
            (base / "service-active").write_text("yes")
            (base / "curl-used").unlink(missing_ok=True)
            second = self._run(env)
            self.assertEqual(second.returncode, 0, second.stderr)
            payload = json.loads(second.stdout)
            self.assertEqual(payload["reason"], "already-running")
            self.assertFalse(payload["started"])
            self.assertFalse((base / "curl-used").exists())

    def test_checksum_mismatch_does_not_unpack_or_start(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = self._archive(base / "release.tar.gz")
            env = self._env(base)
            env["OMARCHY_AI_ASSISTANT_ARCHIVE"] = str(archive)
            env["OMARCHY_AI_ASSISTANT_SHA256"] = "0" * 64
            result = self._run(env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"checksum", result.stderr)
            self.assertFalse((base / "data/omachy-ai-releases").exists())
            self.assertFalse((base / "config/systemd/user/omarchy-ai.service").exists())
            self.assertFalse((base / "commands").exists())

    def _run(self, env):
        return subprocess.run(
            ["/usr/bin/bash", str(STARTER)],
            capture_output=True,
            env=env,
            check=False,
            timeout=20,
        )

    def _env(self, base: Path) -> dict:
        bin_dir = base / "bin"
        bin_dir.mkdir()
        write_executable(
            bin_dir / "systemctl",
            textwrap.dedent(
                """\
                #!/bin/sh
                printf '%s\\n' "$*" >> "$TEST_COMMAND_LOG"
                case "$*" in
                  *is-active*)
                    if [ -f "$TEST_SERVICE_ACTIVE" ]; then
                      exit 0
                    fi
                    exit 1
                    ;;
                esac
                exit 0
                """
            ),
        )
        write_executable(
            bin_dir / "curl",
            "#!/bin/sh\ntouch \"$TEST_CURL_USED\"\nexit 1\n",
        )
        write_executable(
            bin_dir / "omarchy",
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$TEST_COMMAND_LOG\"\n",
        )
        write_executable(
            bin_dir / "omarchy-shell",
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$TEST_COMMAND_LOG\"\n",
        )
        runtime = base / "runtime"
        runtime.mkdir()
        return {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(base / "home"),
            "XDG_CONFIG_HOME": str(base / "config"),
            "XDG_DATA_HOME": str(base / "data"),
            "XDG_STATE_HOME": str(base / "state"),
            "XDG_RUNTIME_DIR": str(runtime),
            "TEST_COMMAND_LOG": str(base / "commands"),
            "TEST_SERVICE_ACTIVE": str(base / "service-active"),
            "TEST_CURL_USED": str(base / "curl-used"),
            "LC_ALL": "C",
        }

    def _archive(self, dest: Path) -> Path:
        root_name = f"omarchy-ai-{PINNED_VERSION}-linux-x86_64"
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory) / root_name
            write_executable(
                tree / ".venv/bin/omarchy-ai-settings",
                "#!/bin/sh\nprintf '%s\\n' '{\"ok\":true,\"from\":\"release\"}'\n",
            )
            write_executable(tree / ".venv/bin/python", "#!/bin/sh\nexit 0\n")
            (tree / "pyproject.toml").write_text("[project]\nname = 'omarchy-ai'\n")
            (tree / "systemd").mkdir()
            (tree / "systemd/omarchy-ai.service").write_text(
                "\n".join(
                    [
                        "[Service]",
                        "ExecStart=@VENV@/bin/python -m omarchy_ai",
                        "WorkingDirectory=@PROJECT_DIR@",
                        "",
                    ]
                )
            )
            qml = tree / "quickshell/plugins/omarchy-ai.watchdog/Watchdog.qml"
            qml.parent.mkdir(parents=True)
            qml.write_text('property string helper: "@OMARCHY_AI_SETTINGS@"\n')
            settings = tree / "quickshell/plugins/omarchy-ai.settings/Panel.qml"
            settings.parent.mkdir(parents=True)
            settings.write_text("DO-NOT-COPY\n")
            with tarfile.open(dest, "w:gz") as archive:
                archive.add(tree, arcname=root_name)
        return dest


if __name__ == "__main__":
    unittest.main()
