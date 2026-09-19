import unittest
from pathlib import Path


class WatchdogVisualizerTests(unittest.TestCase):
    def test_visualizer_binding_explicitly_depends_on_levels(self):
        text = (Path(__file__).parents[1] / 'quickshell/plugins/omarchy-ai.watchdog/Watchdog.qml').read_text()
        self.assertIn('var samples = root.levels; return root.visualizerLine()', text)

    def test_visualizer_matches_server_glyph_mix_and_outcome_colors(self):
        text = (Path(__file__).parents[1] / 'quickshell/plugins/omarchy-ai.watchdog/Watchdog.qml').read_text()
        self.assertIn('root.brailleChars[idx]', text)
        self.assertIn('root.glitchChars.charAt', text)
        self.assertIn('property string outcomeTone', text)
        self.assertIn('if (root.outcomeTone === "err") return root.rainErr', text)
        self.assertIn('if (root.outcomeTone === "ok") return root.rainGreen', text)
        self.assertIn('if (speechFinished) root.outcomeTone = ""', text)
