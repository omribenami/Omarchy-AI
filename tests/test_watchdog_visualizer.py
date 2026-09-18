import unittest
from pathlib import Path


class WatchdogVisualizerTests(unittest.TestCase):
    def test_visualizer_binding_explicitly_depends_on_levels(self):
        text = (Path(__file__).parents[1] / 'quickshell/plugins/omarchy-ai.watchdog/Watchdog.qml').read_text()
        self.assertIn('var samples = root.levels; return root.visualizerLine()', text)
