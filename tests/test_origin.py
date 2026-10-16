import unittest
from types import SimpleNamespace
from unittest.mock import patch

from omarchy_ai.core import conversations, identities, origin


class OriginTests(unittest.TestCase):
    def current(self, source, room="", person=None, how=None):
        token = conversations.CURRENT.set("c1")
        who = identities.set_current(identities.Identity(person, how))
        try:
            with patch.object(conversations, "source", return_value=source), \
                    patch.object(origin, "load_config", return_value=SimpleNamespace(satellite_room=room)), \
                    patch.object(identities, "name", side_effect=lambda key: key.title()):
                return origin.current()
        finally:
            identities.reset_current(who)
            conversations.CURRENT.reset(token)

    def test_a_speaker_request_is_from_its_room_and_not_from_the_desk(self):
        info = self.current("satellite", room="living room", person="alex", how="voice")
        self.assertEqual((info["channel"], info["room"], info["at_this_computer"], info["person"]),
                         ("satellite", "living room", False, "Alex"))
        text = origin.describe(info)
        self.assertIn("voice speaker in the living room", text)
        self.assertIn("not at this computer", text)
        self.assertIn("Alex", text)

    def test_the_desktop_is_at_the_computer_and_unknown_stays_unknown(self):
        self.assertTrue(self.current("desktop-voice")["at_this_computer"])
        self.assertIsNone(self.current("satellite")["room"])
        self.assertEqual(self.current("test-satellite")["channel"], "satellite")
        unknown = self.current("")
        self.assertEqual((unknown["channel"], origin.describe(unknown)), (None, ""))


if __name__ == "__main__":
    unittest.main()
