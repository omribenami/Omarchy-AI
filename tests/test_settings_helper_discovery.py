"""Runtime lookup for the settings panel's CLI.

`omarchy plugin add` installs the bar widget without rewriting a helper
path. resolve-settings.sh has to find omarchy-ai-settings, or tell the
panel the assistant is not installed, without hanging or installing it.
"""

import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESOLVER = ROOT / "quickshell/plugins/omarchy-ai.settings/resolve-settings.sh"
BOUNDED = ROOT / "quickshell/plugins/omarchy-ai.settings/bounded-stdio.sh"
PLUGIN = ROOT / "quickshell/plugins/omarchy-ai.settings"
MISSING = {
    "assistant_installed": False,
    "error": (
        "Omarchy AI is not installed. Install the full assistant from "
        "GitHub Releases or with install.sh, then reopen this panel."
    ),
}


def write_cli(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{marker}'\n")
    path.chmod(0o755)


def isolated_env(base: Path, **extra) -> dict:
    empty = base / "empty-path"
    empty.mkdir()
    for name in ("home", "config", "data"):
        (base / name).mkdir()
    env = {
        "PATH": str(empty),
        "HOME": str(base / "home"),
        "XDG_CONFIG_HOME": str(base / "config"),
        "XDG_DATA_HOME": str(base / "data"),
        "LC_ALL": "C",
    }
    env.update(extra)
    return env


def run_resolver(env, *args, stdin=None, timeout=5):
    return subprocess.run(
        ["/usr/bin/bash", str(RESOLVER), *args],
        input=stdin,
        capture_output=True,
        env=env,
        timeout=timeout,
        check=False,
    )


class SettingsHelperDiscoveryTests(unittest.TestCase):
    def test_resolver_is_executable_and_the_plugin_has_no_path_token(self):
        mode = RESOLVER.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)
        text = RESOLVER.read_text()
        self.assertIn("assistant_installed", text)
        self.assertNotIn("curl ", text)
        self.assertNotIn("pacman ", text)
        for path in PLUGIN.iterdir():
            if path.suffix in {".qml", ".sh", ".json"}:
                self.assertNotIn("@OMARCHY_AI_SETTINGS@", path.read_text())
        panel = (PLUGIN / "Panel.qml").read_text()
        self.assertNotRegex(panel, r"@[A-Z0-9_]+@")
        self.assertIn('"/usr/bin/bash", root._localPath("resolve-settings.sh")', panel)
        self.assertIn("Omarchy AI is not installed", panel)
        self.assertIn("install.sh", panel)
        self.assertIn("https://github.com/omribenami/Omarchy-AI#installation", panel)
        self.assertIn("does not install the voice assistant", panel)

    def test_missing_assistant_is_json_and_does_not_hang_or_install(self):
        with self._temp() as base:
            base = Path(base)
            env = isolated_env(base)
            installer = base / "empty-path" / "install.sh"
            installer.write_text('#!/bin/sh\ntouch "$HOME/ran-install"\n')
            installer.chmod(0o755)
            env["PATH"] = str(installer.parent)
            result = run_resolver(env, "get")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), MISSING)
            self.assertFalse((base / "home" / "ran-install").exists())

            framed = subprocess.run(
                ["/usr/bin/bash", str(BOUNDED), "helper", "--", "/usr/bin/bash", str(RESOLVER), "get"],
                capture_output=True,
                env=env,
                timeout=5,
                check=False,
            )
            self.assertEqual(framed.returncode, 0, framed.stderr)
            self.assertTrue(framed.stdout.startswith(b"0\n"), framed.stdout)
            self.assertEqual(json.loads(framed.stdout.split(b"\n", 1)[1]), MISSING)

    def test_explicit_executable_wins_and_forwards_stdin(self):
        with self._temp() as base:
            base = Path(base)
            env = isolated_env(base)
            chosen = base / "chosen"
            write_cli(chosen, "from-env")
            chosen.write_text(
                "#!/bin/sh\nprintf 'arg:%s:' \"$1\"\nIFS= read -r line || true\nprintf '%s\\n' \"$line\"\n"
            )
            chosen.chmod(0o755)
            unit = base / "config/systemd/user/omarchy-ai.service"
            write_cli(base / "unit/.venv/bin/omarchy-ai-settings", "from-unit")
            unit.parent.mkdir(parents=True)
            unit.write_text(f"WorkingDirectory={base / 'unit'}\n")
            env["OMARCHY_AI_SETTINGS"] = str(chosen)
            result = run_resolver(env, "set-api-key", stdin=b"secret\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b"arg:set-api-key:secret\n")

    def test_invalid_override_falls_through_to_the_systemd_unit(self):
        with self._temp() as base:
            base = Path(base)
            env = isolated_env(base)
            root = base / "My Project"
            write_cli(root / ".venv/bin/omarchy-ai-settings", "from-unit")
            unit = base / "config/systemd/user/omarchy-ai.service"
            unit.parent.mkdir(parents=True)
            unit.write_text(
                f"WorkingDirectory={root}\n"
                f"ExecStart={root}/.venv/bin/python -m omarchy_ai\n"
            )
            env["OMARCHY_AI_SETTINGS"] = str(base / "missing")
            result = run_resolver(env, "get")
            self.assertEqual(result.stdout, b"from-unit\n")

    def test_execstart_with_spaces_is_used_when_the_unit_has_no_workdir(self):
        with self._temp() as base:
            base = Path(base)
            env = isolated_env(base)
            root = base / "path with spaces"
            write_cli(root / ".venv/bin/omarchy-ai-settings", "from-exec")
            unit = base / "config/systemd/user/omarchy-ai.service"
            unit.parent.mkdir(parents=True)
            unit.write_text(f"ExecStart={root}/.venv/bin/python -m omarchy_ai\n")
            result = run_resolver(env, "get")
            self.assertEqual(result.stdout, b"from-exec\n")

    def test_unreplaced_unit_is_ignored_in_favor_of_path(self):
        with self._temp() as base:
            base = Path(base)
            env = isolated_env(base)
            unit = base / "config/systemd/user/omarchy-ai.service"
            unit.parent.mkdir(parents=True)
            unit.write_text(
                "WorkingDirectory=@PROJECT_DIR@\n"
                "ExecStart=@VENV@/bin/python -m omarchy_ai\n"
            )
            binary = base / "bin/omarchy-ai-settings"
            write_cli(binary, "from-path")
            env["PATH"] = str(binary.parent)
            result = run_resolver(env, "get")
            self.assertEqual(result.stdout, b"from-path\n")

    def test_unit_wins_over_path_and_a_newer_release_tree(self):
        with self._temp() as base:
            base = Path(base)
            env = isolated_env(base)
            write_cli(base / "installed/.venv/bin/omarchy-ai-settings", "from-unit")
            unit = base / "config/systemd/user/omarchy-ai.service"
            unit.parent.mkdir(parents=True)
            unit.write_text(f"WorkingDirectory={base / 'installed'}\n")
            binary = base / "bin/omarchy-ai-settings"
            write_cli(binary, "from-path")
            env["PATH"] = str(binary.parent)
            release = (
                base / "data/omachy-ai-releases/omarchy-ai-9.0.0-linux-x86_64/.venv/bin/omarchy-ai-settings"
            )
            write_cli(release, "from-release")
            result = run_resolver(env, "get")
            self.assertEqual(result.stdout, b"from-unit\n")

    def test_newest_executable_release_is_used_and_skips_a_newer_non_executable(self):
        with self._temp() as base:
            base = Path(base)
            env = isolated_env(base)
            data = base / "data/omachy-ai-releases"
            write_cli(data / "omarchy-ai-0.9.0-linux-x86_64/.venv/bin/omarchy-ai-settings", "old")
            write_cli(data / "omarchy-ai-0.11.3-linux-x86_64/.venv/bin/omarchy-ai-settings", "mid")
            newest = data / "omarchy-ai-0.11.10-linux-x86_64/.venv/bin/omarchy-ai-settings"
            write_cli(newest, "newest")
            newest.chmod(0o644)
            updated = (
                base / "data/omarchy-ai/releases/0.4.0-tmp/"
                "omarchy-ai-0.4.0-linux-x86_64/.venv/bin/omarchy-ai-settings"
            )
            write_cli(updated, "updated")
            result = run_resolver(env, "get")
            self.assertEqual(result.stdout, b"mid\n")

    def test_self_update_tree_is_found_when_nothing_else_is_installed(self):
        with self._temp() as base:
            base = Path(base)
            env = isolated_env(base)
            binary = (
                base / "data/omarchy-ai/releases/0.11.3-abcd/"
                "omarchy-ai-0.11.3-linux-x86_64/.venv/bin/omarchy-ai-settings"
            )
            write_cli(binary, "updated")
            decoy = base / "home/.local/share/omarchy-ai/releases/9.9.9-decoy/omarchy-ai-9.9.9-linux-x86_64/.venv/bin/omarchy-ai-settings"
            write_cli(decoy, "home-decoy")
            result = run_resolver(env, "get")
            self.assertEqual(result.stdout, b"updated\n")

    def test_directory_override_is_not_executed(self):
        with self._temp() as base:
            base = Path(base)
            env = isolated_env(base)
            directory = base / "not-a-binary"
            directory.mkdir()
            env["OMARCHY_AI_SETTINGS"] = str(directory)
            result = run_resolver(env, "get")
            self.assertEqual(json.loads(result.stdout), MISSING)

    def _temp(self):
        return tempfile.TemporaryDirectory()


if __name__ == "__main__":
    unittest.main()
