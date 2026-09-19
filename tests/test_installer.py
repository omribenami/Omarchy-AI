import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def test_setup_verifies_jev_ultrafast_install(self):
        setup = (ROOT / 'scripts/setup.sh').read_text()
        self.assertIn('from jev_ultrafast import Agent', setup)
        self.assertIn('python/jev_ultrafast-*.whl', setup)
        project = (ROOT / 'pyproject.toml').read_text()
        self.assertIn('browser-use/jev-ultrafast.git', project)

    def test_setup_installs_assistant_activation_keybinding(self):
        setup = (ROOT / 'scripts/setup.sh').read_text()
        self.assertIn('scripts/install-keybinding.sh', setup)
        script = (ROOT / 'scripts/install-keybinding.sh').read_text()
        self.assertIn('SUPER + GRAVE', script)
        self.assertIn('SUPER + SHIFT + GRAVE', script)
        self.assertIn('omarchy-ai-settings', script)
        self.assertIn('activate', script)
        self.assertIn('.venv/bin/omarchy-ai-settings', script)
        self.assertIn('hl.unbind', script)
    def test_upgrade_replaces_old_panel_and_refreshes_shell(self):
        with tempfile.TemporaryDirectory(prefix='omachy install ') as directory:
            base = Path(directory)
            old_panel = base / 'config/omarchy/plugins/omarchy-ai.settings/Panel.qml'
            old_panel.parent.mkdir(parents=True)
            old_panel.write_text('old Settings panel')
            bin_dir = base / 'bin'
            bin_dir.mkdir()
            for command in ('omarchy', 'omarchy-shell'):
                path = bin_dir / command
                path.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_COMMAND_LOG"\n')
                path.chmod(0o755)
            unlocked = bin_dir / 'omarchy-hyprland-session-locked'
            unlocked.write_text('#!/bin/sh\nexit 1\n')
            unlocked.chmod(0o755)
            env = dict(os.environ, PATH=str(bin_dir) + ':' + os.environ['PATH'],
                       XDG_CONFIG_HOME=str(base / 'config'), XDG_STATE_HOME=str(base / 'state'),
                       TEST_COMMAND_LOG=str(base / 'commands'))
            subprocess.run(['bash', str(ROOT / 'scripts/install-plugins.sh')], env=env, check=True, capture_output=True)
            plugins = base / 'config/omarchy/plugins'
            for name in ('omarchy-ai.settings/Panel.qml', 'omarchy-ai.myapi/Panel.qml', 'omarchy-ai.tv-discovery/TvDiscovery.qml'):
                text = (plugins / name).read_text()
                self.assertNotIn('@OMARCHY_AI_SETTINGS@', text)
                self.assertIn(str(ROOT / '.venv/bin/omarchy-ai-settings'), text)
            settings_panel = (plugins / 'omarchy-ai.settings/Panel.qml').read_text()
            self.assertNotIn('old Settings panel', settings_panel)
            self.assertIn('value: root.selectedProvider', settings_panel)
            self.assertIn('Jev / Vercel AI Gateway key', settings_panel)
            self.assertEqual(len(list((base / 'state/omarchy-ai/plugin-backups').glob('*/omarchy-ai.settings/Panel.qml'))), 1)
            commands = (base / 'commands').read_text()
            self.assertIn('bar move omarchy-ai.settings --section right', commands)
            self.assertIn('bar move omarchy-ai.myapi --section right', commands)
            self.assertGreater(commands.rfind('restart shell'), commands.rfind('bar move omarchy-ai.myapi'))

    def test_locked_session_rescans_plugins_without_restarting_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name, script in (
                ('omarchy', '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_COMMAND_LOG"\n'),
                ('omarchy-shell', '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_COMMAND_LOG"\n'),
                ('omarchy-hyprland-session-locked', '#!/bin/sh\nexit 0\n'),
            ):
                path = base / name
                path.write_text(script)
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(base) + ':' + os.environ['PATH'],
                       XDG_CONFIG_HOME=str(base / 'config'), XDG_STATE_HOME=str(base / 'state'),
                       TEST_COMMAND_LOG=str(base / 'commands'))
            subprocess.run(['bash', str(ROOT / 'scripts/install-plugins.sh')], env=env, check=True, capture_output=True)
            commands = (base / 'commands').read_text()
            self.assertIn('shell rescanPlugins', commands)
            self.assertNotIn('restart shell', commands)

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
