"""OmaStore listing and the release launcher it runs."""

import os
from pathlib import Path
import stat
import subprocess
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = (
    "Agentic AI harness for Omarchy: voice/text desktop co-pilot with task "
    "runtime, Claude Code/Codex, watches & routines, deep system control, "
    "and risk-gated approvals."
)


def _write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


class OmaStoreManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest = tomllib.loads((ROOT / "omastore.toml").read_text())

    def test_summary_matches_the_store_card_and_stays_within_300_characters(self):
        self.assertEqual(self.manifest["summary"], SUMMARY)
        self.assertLessEqual(len(self.manifest["summary"]), 300)
        self.assertEqual(self.manifest["name"], "Omarchy AI")
        self.assertEqual(self.manifest["kind"], "app")
        self.assertEqual(self.manifest["categories"], ["AudioVideo", "System"])
        self.assertTrue(self.manifest["terminal"])

    def test_release_asset_and_exec_match_the_package_layout(self):
        target = self.manifest["linux"]["x86_64"]
        version = "0.12.0"
        asset = target["asset"].replace("{version}", version)
        executable = target["exec"].replace("{version}", version)
        self.assertEqual(asset, "omarchy-ai-0.12.0-linux-x86_64.tar.gz")
        self.assertEqual(executable, "omarchy-ai-0.12.0-linux-x86_64/bin/omarchy-ai")
        package = (ROOT / "scripts/build-install-package.sh").read_text()
        self.assertIn("omarchy-ai-${version}-linux-x86_64", package)
        self.assertIn('chmod 0755 "$package_dir/install.sh" "$package_dir/bin/omarchy-ai"', package)

    def test_icon_and_screenshots_are_real_images_under_the_store_limit(self):
        icon = ROOT / self.manifest["icon"]
        self.assertEqual(icon.suffix, ".png")
        self.assertLess(icon.stat().st_size, 15 * 1024 * 1024)
        self.assertGreaterEqual(len(self.manifest["screenshots"]), 1)
        self.assertLessEqual(len(self.manifest["screenshots"]), 8)
        for relative in self.manifest["screenshots"]:
            path = ROOT / relative
            self.assertTrue(path.is_file(), relative)
            self.assertIn(path.suffix, {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"})
            self.assertLess(path.stat().st_size, 15 * 1024 * 1024)
            self.assertNotIn(path.suffix, {".mp4"})


class OmaStoreLauncherTests(unittest.TestCase):
    def test_launcher_is_an_executable_shebang_script(self):
        launcher = ROOT / "bin/omarchy-ai"
        text = launcher.read_text()
        self.assertTrue(text.startswith("#!/usr/bin/env bash\n"))
        self.assertIn("bash \"$root/install.sh\"", text)
        self.assertIn("systemctl --user enable --now omarchy-ai.service", text)
        self.assertIn('omarchy-ai-settings" activate', text)
        mode = launcher.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)

    def test_first_launch_bootstraps_then_starts_the_service(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._install_launcher(root)
            install_log = root / "install.log"
            systemctl_log = root / "systemctl.log"
            settings_log = root / "settings.log"
            _write_executable(
                root / "install.sh",
                "#!/bin/sh\nprintf 'install\\n' >> \"$INSTALL_LOG\"\n",
            )
            _write_executable(
                root / ".venv/bin/omarchy-ai-settings",
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SETTINGS_LOG\"\nprintf '%s\\n' '{\"state\": \"listening\"}'\n",
            )
            bin_dir = root / "stub-bin"
            _write_executable(
                bin_dir / "systemctl",
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\n",
            )
            completed = self._run(root, install_log, systemctl_log, settings_log, bin_dir)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(install_log.read_text(), "install\n")
            self.assertIn("--user enable --now omarchy-ai.service", systemctl_log.read_text())
            self.assertEqual(settings_log.read_text(), "activate\n")
            self.assertIn("Omarchy AI is running.", completed.stdout)

    def test_later_launch_skips_install_when_this_tree_is_already_active(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._install_launcher(root)
            install_log = root / "install.log"
            systemctl_log = root / "systemctl.log"
            settings_log = root / "settings.log"
            _write_executable(root / "install.sh", "#!/bin/sh\nprintf 'install\\n' >> \"$INSTALL_LOG\"\n")
            _write_executable(root / ".venv/bin/omarchy-ai", "#!/bin/sh\nexit 0\n")
            _write_executable(
                root / ".venv/bin/omarchy-ai-settings",
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SETTINGS_LOG\"\nprintf '%s\\n' '{\"state\": \"listening\"}'\n",
            )
            config = root / "config"
            unit = config / "systemd/user/omarchy-ai.service"
            unit.parent.mkdir(parents=True)
            unit.write_text(f"WorkingDirectory={root}\n")
            bin_dir = root / "stub-bin"
            _write_executable(
                bin_dir / "systemctl",
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\n",
            )
            completed = self._run(
                root, install_log, systemctl_log, settings_log, bin_dir, config_home=config,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(install_log.exists())
            self.assertIn("enable --now", systemctl_log.read_text())
            self.assertEqual(settings_log.read_text(), "activate\n")

    def test_arguments_exec_the_venv_entry_point(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._install_launcher(root)
            daemon_log = root / "daemon.log"
            _write_executable(root / "install.sh", "#!/bin/sh\nprintf 'install\\n' > \"$INSTALL_LOG\"\n")
            _write_executable(
                root / ".venv/bin/omarchy-ai",
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$DAEMON_LOG\"\n",
            )
            config = root / "config"
            unit = config / "systemd/user/omarchy-ai.service"
            unit.parent.mkdir(parents=True)
            unit.write_text(f"WorkingDirectory={root}\n")
            bin_dir = root / "stub-bin"
            _write_executable(bin_dir / "systemctl", "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\n")
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
            env["INSTALL_LOG"] = str(root / "install.log")
            env["SYSTEMCTL_LOG"] = str(root / "systemctl.log")
            env["DAEMON_LOG"] = str(daemon_log)
            env["XDG_CONFIG_HOME"] = str(config)
            env["HOME"] = str(root / "home")
            completed = subprocess.run(
                [str(root / "bin/omarchy-ai"), "--version"],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(daemon_log.read_text(), "--version\n")
            self.assertFalse((root / "install.log").exists())
            self.assertFalse((root / "systemctl.log").exists())

    def test_offline_activate_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._install_launcher(root)
            _write_executable(root / "install.sh", "#!/bin/sh\nexit 0\n")
            _write_executable(
                root / ".venv/bin/omarchy-ai-settings",
                "#!/bin/sh\nprintf '%s\\n' '{\"state\": \"offline\", \"error\": \"Assistant unavailable.\"}'\n",
            )
            bin_dir = root / "stub-bin"
            _write_executable(bin_dir / "systemctl", "#!/bin/sh\nexit 0\n")
            completed = self._run(
                root,
                root / "install.log",
                root / "systemctl.log",
                root / "settings.log",
                bin_dir,
            )
            self.assertEqual(completed.returncode, 1)
            self.assertIn("Assistant unavailable.", completed.stderr)
            self.assertNotIn("Omarchy AI is running.", completed.stdout)

    def _install_launcher(self, root: Path) -> None:
        destination = root / "bin/omarchy-ai"
        destination.parent.mkdir(parents=True)
        destination.write_bytes((ROOT / "bin/omarchy-ai").read_bytes())
        destination.chmod(0o755)

    def _run(self, root, install_log, systemctl_log, settings_log, bin_dir, config_home=None):
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
        env["INSTALL_LOG"] = str(install_log)
        env["SYSTEMCTL_LOG"] = str(systemctl_log)
        env["SETTINGS_LOG"] = str(settings_log)
        env["XDG_CONFIG_HOME"] = str(config_home or (root / "unused-config"))
        env["HOME"] = str(root / "home")
        return subprocess.run(
            [str(root / "bin/omarchy-ai")],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
