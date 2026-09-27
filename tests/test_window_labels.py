import unittest
from pathlib import Path


class WindowLabelsTest(unittest.TestCase):
    def test_labels_auto_hide_after_twenty_seconds(self):
        qml = (Path(__file__).parents[1] /
               "quickshell/plugins/omarchy-ai.window-labels/WindowLabels.qml").read_text()
        self.assertIn("interval: 20000", qml)
        self.assertIn("if (opened) hideTimer.restart()", qml)
        self.assertIn("onTriggered: root.close()", qml)


if __name__ == "__main__":
    unittest.main()
