import unittest
from unittest.mock import patch

from omarchy_ai.execution import actions
from omarchy_ai.execution.actions import ActionResult

VLC = "org.mpris.MediaPlayer2.vlc"
SPOTIFY = "org.mpris.MediaPlayer2.spotify"


def bus(players: dict):
    """A fake busctl: `players` maps a bus name to its PlaybackStatus."""
    calls = []

    def run(argv, timeout=None, cwd=None):
        calls.append(argv)
        if argv[2] == "list":
            names = [":1.5", "org.freedesktop.DBus", *players]
            return ActionResult(True, "\n".join(f"{name} 1 proc user :1.5 - -" for name in names))
        if argv[2] == "get-property":
            return ActionResult(True, f's "{players[argv[3]]}"')
        return ActionResult(True, "")

    return run, calls


class MediaActionsTest(unittest.TestCase):
    def test_no_player_points_at_other_devices(self):
        run, calls = bus({})
        with patch.object(actions, "_run", run):
            result = actions.media_play_pause({})
        self.assertFalse(result.ok)
        self.assertEqual(result.message, actions.NO_MEDIA_PLAYER)
        self.assertIn("another device", result.message)
        self.assertEqual(len(calls), 1)

    def test_pauses_the_player_that_is_playing(self):
        run, calls = bus({VLC: "Paused", SPOTIFY: "Playing"})
        with patch.object(actions, "_run", run):
            result = actions.media_play_pause({})
        self.assertTrue(result.ok)
        self.assertEqual(calls[-1][2:], ["call", SPOTIFY, "/org/mpris/MediaPlayer2",
                                         "org.mpris.MediaPlayer2.Player", "PlayPause"])
        self.assertIn("spotify", result.message)

    def test_resumes_a_paused_player_before_a_stopped_one(self):
        run, calls = bus({VLC: "Stopped", SPOTIFY: "Paused"})
        with patch.object(actions, "_run", run):
            self.assertTrue(actions.media_play_pause({}).ok)
        self.assertEqual(calls[-1][3], SPOTIFY)

    def test_next_and_previous(self):
        run, calls = bus({VLC: "Playing"})
        with patch.object(actions, "_run", run):
            self.assertTrue(actions.media_next({}).ok)
            self.assertTrue(actions.media_prev({}).ok)
        self.assertEqual([c[-1] for c in calls if c[2] == "call"], ["Next", "Previous"])

    def test_bus_failure_is_reported(self):
        with patch.object(actions, "_run", lambda *a, **k: ActionResult(False, "busctl timed out")):
            result = actions.media_play_pause({})
        self.assertEqual((result.ok, result.message), (False, "busctl timed out"))


if __name__ == "__main__":
    unittest.main()
