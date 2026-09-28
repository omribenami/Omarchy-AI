import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omarchy_ai.config import Config
from omarchy_ai.core import alert_clips


def history(folder: Path, *user_turns: str) -> None:
    lines = [json.dumps({"turns": [{"role": "user", "text": t}, {"role": "assistant", "text": "OK"}]})
             for t in user_turns]
    (folder / "conversation_history.jsonl").write_text("\n".join(lines) + "\n")


class LanguageTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        p = patch.object(alert_clips, "STATE_DIR", self.state)
        p.start()
        self.addCleanup(p.stop)

    def test_detects_any_language_not_only_hebrew_or_english(self):
        for text, code in [("Check if you can see the email and tell me", "en"),
                           ("תגיד לי מה הוא רשם בהודעה האחרונה שלו וממתי", "he"),
                           ("Bonjour, peux-tu vérifier mes emails de ce matin", "fr"),
                           ("Hola, ¿puedes revisar mi correo de esta mañana?", "es"),
                           ("تحقق من بريدي الإلكتروني من فضلك", "ar"),
                           ("今朝のメールを確認してください", "ja")]:
            history(self.state, text)
            self.assertEqual(alert_clips.language(), code, text)

    def test_hebrew_with_english_names_is_hebrew(self):
        history(self.state, "תעבירי ל-Workspace 5", "תודה")
        self.assertEqual(alert_clips.language(), "he")

    def test_one_misheard_fragment_does_not_flip_it(self):
        # 2026-09-28: short Hebrew came back as "scommettiti", "Chiamati.".
        history(self.state, "תגיד לי מה הוא רשם בהודעה האחרונה שלו וממתי", "scommettiti", "Chiamati.")
        self.assertEqual(alert_clips.language(), "he")

    def test_too_little_or_no_history_is_english(self):
        self.assertEqual(alert_clips.language(), "en")
        history(self.state, "ok")
        self.assertEqual(alert_clips.language(), "en")


class FindTests(unittest.TestCase):
    def setUp(self):
        self.local, self.packaged = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        for p in (patch.object(alert_clips, "LOCAL", self.local), patch.object(alert_clips, "PACKAGED", self.packaged)):
            p.start()
            self.addCleanup(p.stop)
        for name in ("a-en.ogg", "b-en.ogg"):
            (self.packaged / name).touch()

    def test_users_language_first_then_english(self):
        self.assertEqual(alert_clips.find("a", "fr"), self.packaged / "a-en.ogg")
        (self.local / "a-fr.ogg").touch()
        self.assertEqual(alert_clips.find("a", "fr"), self.local / "a-fr.ogg")

    def test_stem_order_within_a_language(self):
        (self.local / "b-fr.ogg").touch()
        # A generic clip in the user's language beats a specific one in English.
        self.assertEqual(alert_clips.find(["a", "b"], "fr"), self.local / "b-fr.ogg")
        self.assertIsNone(alert_clips.find("c", "fr"))

    def test_missing_lists_what_this_language_lacks(self):
        with patch.object(alert_clips, "texts", return_value={"a": "A", "b": "B"}):
            self.assertEqual(alert_clips.missing(Config(), "en"), {})
            (self.local / "a-fr.ogg").touch()
            self.assertEqual(alert_clips.missing(Config(), "fr"), {"b": "B"})


class TakeTests(unittest.TestCase):
    TEXT = "Sorry, falling back did not fix the problem, please try a different provider."

    def pcm(self, seconds):
        return b"\0" * int(seconds * 48000)

    def test_english_take_must_say_the_text(self):
        self.assertTrue(alert_clips.good(self.TEXT, "en", self.pcm(5.3), self.TEXT))
        self.assertFalse(alert_clips.good(self.TEXT, "en", self.pcm(5.3), "Sorry"))
        self.assertFalse(alert_clips.good(self.TEXT, "en", self.pcm(0.7), self.TEXT))  # the real 0.7s take

    def test_translated_take_must_be_that_language_and_complete(self):
        spanish = "Lo siento, retroceder no solucionó el problema, por favor, prueba con un proveedor diferente."
        self.assertTrue(alert_clips.good(self.TEXT, "es", self.pcm(6.4), spanish))
        self.assertFalse(alert_clips.good(self.TEXT, "fr", self.pcm(6.4), spanish))
        self.assertFalse(alert_clips.good(self.TEXT, "es", self.pcm(6.4), "a un modelo más antiguo."))


class PrepareTests(unittest.TestCase):
    def setUp(self):
        alert_clips._tried.clear()
        self.addCleanup(alert_clips._tried.clear)

    def test_renders_a_missing_language_once_in_the_background(self):
        started = []
        with patch.object(alert_clips, "language", return_value="de"), \
                patch.object(alert_clips, "missing", return_value={"a": "A"}), \
                patch.object(alert_clips.threading, "Thread",
                             lambda **kw: type("T", (), {"start": lambda self: started.append(kw["name"])})()):
            alert_clips.prepare_in_background(Config())
            alert_clips.prepare_in_background(Config())
        self.assertEqual(started, ["alert-clips-de"])

    def test_nothing_missing_nothing_rendered(self):
        with patch.object(alert_clips, "language", return_value="en"), \
                patch.object(alert_clips, "missing", return_value={}), \
                patch.object(alert_clips.threading, "Thread") as thread:
            alert_clips.prepare_in_background(Config())
        thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
