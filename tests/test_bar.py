import json
import unittest
from unittest.mock import patch
from omarchy_ai.execution import bar

class BarTests(unittest.TestCase):
    def test_live_layout_order_and_disabled_filter(self):
        plugins = [{'id': 'a', 'name': 'Settings', 'enabled': True}, {'id': 'b', 'enabled': False}]
        config = {'bar': {'layout': {'right': [{'id': 'a'}, {'id': 'b'}]}}}
        with patch.object(bar, '_ipc', side_effect=[json.dumps(plugins), json.dumps(config)]):
            self.assertEqual(bar.entries(), [{'number': 1, 'id': 'a', 'name': 'Settings', 'section': 'right', 'position': 1}])
    def test_unknown_target_never_dispatched(self):
        with patch.object(bar, 'entries', return_value=[{'id': 'omarchy-ai.settings'}]), patch.object(bar, '_ipc') as ipc:
            self.assertFalse(bar.open_panel('Agent')[0])
            ipc.assert_not_called()
    def test_unknown_shell_reply_is_failure(self):
        with patch.object(bar, 'entries', return_value=[{'id': 'a', 'name': 'A'}]), patch.object(bar, '_ipc', return_value='unknown'):
            self.assertFalse(bar.open_panel('a')[0])
    def test_exact_summon_and_qualified_success(self):
        with patch.object(bar, 'entries', return_value=[{'id': 'a', 'name': 'A'}]), patch.object(bar, '_ipc', return_value='ok') as ipc:
            ok, message = bar.open_panel('a')
            self.assertTrue(ok)
            self.assertIn('not independently verified', message)
            ipc.assert_called_once_with('summon', 'a', '{}')
    def test_tools_registered_for_both_providers(self):
        from omarchy_ai.execution.actions import ACTIONS
        from omarchy_ai.execution.tools import TOOLS
        from omarchy_ai.voice.gemini_live import build_live_config
        from omarchy_ai.config import Config
        from google.genai import types
        config = types.LiveConnectConfig(**build_live_config(Config()))
        for name in ('list_bar_icons', 'open_bar_panel', 'close_bar_panel'):
            self.assertIn(name, ACTIONS)
            self.assertIn(name, [t['name'] for t in TOOLS])
            self.assertIn(name, [t.name for t in config.tools[0].function_declarations])

    def test_connected_myapi_has_unique_tools_for_both_providers(self):
        from omarchy_ai.config import Config
        from omarchy_ai.voice.live import build_session_config
        from omarchy_ai.voice.gemini_live import build_live_config
        for enabled in (False, True):
            with self.subTest(myapi_enabled=enabled), patch('omarchy_ai.voice.live.myapi.is_connected', return_value=True):
                config = Config(myapi_enabled=enabled)
                names = [t['name'] for t in build_session_config(config)['delegation']['responses']['tools']]
                self.assertEqual(len(names), len(set(names)))
                gemini_names = [t['name'] for t in build_live_config(config)['tools'][0]['function_declarations']]
                self.assertEqual(len(gemini_names), len(set(gemini_names)))
                for name in ('list_bar_icons', 'open_bar_panel', 'close_bar_panel'):
                    self.assertEqual(names.count(name), 1)
                    self.assertEqual(gemini_names.count(name), 1)
                self.assertEqual('myapi_list_services' in names, enabled)

    def test_close_uses_hide_and_accepts_void_reply(self):
        with patch.object(bar, 'entries', return_value=[{'id': 'omarchy-ai.settings', 'name': 'AI Settings'}]), patch.object(bar, '_ipc', return_value='') as ipc:
            ok, message = bar.close_panel('omarchy-ai.settings')
            self.assertTrue(ok)
            self.assertIn('not independently verified', message)
            ipc.assert_called_once_with('hide', 'omarchy-ai.settings')

    def test_unknown_close_target_never_dispatched(self):
        with patch.object(bar, 'entries', return_value=[]), patch.object(bar, '_ipc') as ipc:
            self.assertFalse(bar.close_panel('missing')[0])
            ipc.assert_not_called()

    def test_close_unexpected_reply_is_failure(self):
        with patch.object(bar, 'entries', return_value=[{'id': 'a', 'name': 'A'}]), patch.object(bar, '_ipc', return_value='unknown'):
            self.assertFalse(bar.close_panel('a')[0])

    def test_close_ipc_failure_is_reported(self):
        from omarchy_ai.execution.actions import run_action
        with patch.object(bar, 'close_panel', side_effect=RuntimeError('shell unavailable')):
            result = run_action('close_bar_panel', {'id': 'omarchy-ai.settings'})
            self.assertFalse(result.ok)
            self.assertIn('shell unavailable', result.message)
