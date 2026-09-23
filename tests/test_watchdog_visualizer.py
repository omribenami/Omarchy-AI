import unittest
from pathlib import Path


class WatchdogVisualizerTests(unittest.TestCase):
    def test_visualizer_binding_explicitly_depends_on_levels(self):
        text = (Path(__file__).parents[1] / 'quickshell/plugins/omarchy-ai.watchdog/Watchdog.qml').read_text()
        # Both reads must be explicit in the binding: QML does not infer
        # dependencies read inside visualizerLine(). thinkTick drives the
        # THINKING animation, which has no audio levels to redraw on.
        self.assertIn('var samples = root.levels; var t = root.thinkTick; return root.visualizerLine()', text)

    def test_thinking_and_connecting_states_are_rendered(self):
        text = (Path(__file__).parents[1] / 'quickshell/plugins/omarchy-ai.watchdog/Watchdog.qml').read_text()
        self.assertIn('if (root.convState === "thinking")', text)
        self.assertIn('running: root.opened && root.convState === "thinking"', text)
        self.assertIn('if (s === "connecting") return rainLime', text)

    def test_visualizer_matches_server_glyph_mix_and_outcome_colors(self):
        text = (Path(__file__).parents[1] / 'quickshell/plugins/omarchy-ai.watchdog/Watchdog.qml').read_text()
        self.assertIn('root.brailleChars[idx]', text)
        self.assertIn('root.glitchChars.charAt', text)
        self.assertIn('property string outcomeTone', text)
        self.assertIn('if (root.outcomeTone === "err") return root.rainErr', text)
        self.assertIn('if (root.outcomeTone === "ok") return root.rainGreen', text)
        self.assertIn('if (speechFinished) root.outcomeTone = ""', text)
