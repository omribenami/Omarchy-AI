import unittest

from omarchy_ai.voice.satellite import level_summary


class LevelSummaryTests(unittest.TestCase):
    def test_room_and_speech_show_in_the_percentiles(self):
        # A quiet room for most of a conversation, speech for a tenth of it.
        levels = [20.0] * 900 + [260.0] * 90 + [420.0] * 10
        summary = level_summary(levels, 120.0, 300.0)
        self.assertIn("frames=1000", summary)
        self.assertIn("p50=20", summary)
        self.assertIn("p95=260", summary)
        self.assertIn("p99=420", summary)
        self.assertIn("max=420", summary)
        self.assertIn("at_or_above_voice_rms(120)=10.0%", summary)
        self.assertIn("at_or_above_speech_rms(300)=1.0%", summary)

    def test_voice_level_defaults_to_the_speech_level(self):
        self.assertIn("at_or_above_voice_rms(300)=50.0%", level_summary([100.0, 400.0], None, 300.0))

    def test_no_frames(self):
        self.assertEqual(level_summary([], 120.0, 300.0), "frames=0")


if __name__ == "__main__":
    unittest.main()
