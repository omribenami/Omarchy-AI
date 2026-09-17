import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def test_plugin_paths_and_right_side_on_clean_install(self):
        with tempfile.TemporaryDirectory(prefix='omachy install ') as directory:
            base = Path(directory)
            bin_dir = base / 'bin'
            bin_dir.mkdir()
            for command in ('omarchy', 'omarchy-shell'):
                path = bin_dir / command
                path.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_COMMAND_LOG"\n')
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(bin_dir) + ':' + os.environ['PATH'],
                       XDG_CONFIG_HOME=str(base / 'config'), XDG_STATE_HOME=str(base / 'state'),
                       TEST_COMMAND_LOG=str(base / 'commands'))
            subprocess.run(['bash', str(ROOT / 'scripts/install-plugins.sh')], env=env, check=True, capture_output=True)
            plugins = base / 'config/omarchy/plugins'
            for name in ('omarchy-ai.settings/Panel.qml', 'omarchy-ai.myapi/Panel.qml', 'omarchy-ai.tv-discovery/TvDiscovery.qml'):
                text = (plugins / name).read_text()
                self.assertNotIn('@OMARCHY_AI_SETTINGS@', text)
                self.assertIn(str(ROOT / '.venv/bin/omarchy-ai-settings'), text)
            commands = (base / 'commands').read_text()
            self.assertIn('bar move omarchy-ai.settings --section right', commands)
            self.assertIn('bar move omarchy-ai.myapi --section right', commands)

    def test_dependency_install_includes_audio_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for command, script in (
                ('pacman', '#!/bin/sh\nexit 1\n'),
                ('sudo', '#!/bin/sh\nprintf "%s\\n" "$*" > "$TEST_COMMAND_LOG"\n')):
                path = base / command
                path.write_text(script)
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(base) + ':' + os.environ['PATH'], TEST_COMMAND_LOG=str(base / 'commands'))
            subprocess.run(['bash', str(ROOT / 'scripts/install-dependencies.sh')], env=env, check=True, capture_output=True)
            command = (base / 'commands').read_text()
            for package in ('pipewire-audio', 'pipewire-pulse', 'libpulse', 'python-gobject', 'uv'):
                self.assertIn(package, command)
