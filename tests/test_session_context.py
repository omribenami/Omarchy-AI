import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omarchy_ai.config import Config
from omarchy_ai.execution import tile_logs
from omarchy_ai.voice.live import build_session_config
from omarchy_ai.core import memory


class ContextTests(unittest.TestCase):
    def test_standing_preference_is_persisted_and_reloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "learned_preferences.yaml"
            with patch.object(memory, "PREFERENCES_PATH", path), patch.object(memory, "STATE_DIR", Path(directory)):
                memory.add_preference("Always type terminal commands in English.")
                self.assertEqual(memory.load_preferences(), ["Always type terminal commands in English."])

    def test_history_is_inactive_at_new_session(self):
        with patch('omarchy_ai.voice.live.load_recent_context', return_value='user: install yesterday'), patch('omarchy_ai.voice.live.load_preferences', return_value=[]):
            payload = str(build_session_config(Config()))
        self.assertIn('NOT pending tasks', payload)
        self.assertIn('NEW SESSION', payload)
        self.assertNotIn('or you wake while/after', payload)

    def test_exact_window_never_falls_back_to_unrelated_history(self):
        with patch.object(tile_logs, '_live_transcripts', return_value=[]), patch.object(tile_logs, '_manual_contexts', return_value=[('context 0x12345', Path('/wrong'))]), patch.object(tile_logs, '_completed_transcripts', return_value=[]):
            self.assertIsNone(tile_logs.find_log('0x12346'))

    def test_focused_tile_uses_live_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'output.log'
            path.write_text('running: first\n')
            with patch.object(tile_logs, '_clients', return_value=[{'address': '0xabc', 'focusHistoryID': 0}]), patch.object(tile_logs, '_live_transcripts', return_value=[('0xabc terminal', path)]):
                self.assertIn('first', tile_logs.read_log(None))
                with path.open('a') as stream:
                    stream.write('running: second\n')
                self.assertIn('second', tile_logs.read_log(None))
