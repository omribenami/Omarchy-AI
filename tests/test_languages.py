"""The repository is not tied to the user's languages: English defaults,
every other language from the user's own config.yaml (2026-09-28)."""
import re
import unittest
from pathlib import Path

from omarchy_ai.config import Config
from omarchy_ai.voice.gemini_live import build_live_config
from omarchy_ai.voice.live import LiveSession
from omarchy_ai.voice.omarchy import OmarchySession

SRC = Path(__file__).resolve().parent.parent / "src" / "omarchy_ai"


class ConfiguredLanguageTests(unittest.TestCase):
    def test_exit_and_farewell_words_come_from_local_config(self):
        default = Config()
        mine = Config(extra_exit_phrases=["arrête"], extra_farewell_markers=["au revoir"])
        for session in (LiveSession, OmarchySession):
            self.assertTrue(session(default)._check_exit_phrase("goodbye"), session)
            self.assertFalse(session(default)._check_exit_phrase("arrête"), session)
            self.assertTrue(session(mine)._check_exit_phrase("arrête"), session)
        self.assertFalse(LiveSession(default)._check_farewell("D'accord, au revoir !"))
        self.assertTrue(LiveSession(mine)._check_farewell("D'accord, au revoir !"))

    def test_prompt_names_the_users_languages_only_when_configured(self):
        default = build_live_config(Config())["system_instruction"]
        self.assertIn("MISHEARD SPEECH: What you receive is speech recognition", default)
        mine = build_live_config(Config(user_languages=["Français", "English"]))["system_instruction"]
        self.assertIn("MISHEARD SPEECH: The user speaks Français, English.", mine)


class NoPersonalLanguageInDefaultsTests(unittest.TestCase):
    def test_defaults_and_prompt_code_hold_no_hebrew(self):
        # Comments may quote real incidents; code and strings may not assume a language.
        hebrew = re.compile(r"[֐-׿]")
        config = Config()
        for field in ("exit_phrases", "extra_exit_phrases", "extra_farewell_markers", "user_languages"):
            self.assertFalse(any(hebrew.search(p) for p in getattr(config, field)), field)
        self.assertFalse(any(hebrew.search(m) for m in LiveSession._FAREWELL_MARKERS))
        for path in ("voice/live.py", "voice/omarchy.py", "voice/jev_fast.py", "voice/escalation.py",
                     "voice/fallback.py", "core/quota.py", "core/alert_clips.py"):
            code = [line for line in (SRC / path).read_text().splitlines() if not line.lstrip().startswith("#")]
            self.assertFalse([line for line in code if hebrew.search(line)], path)


if __name__ == "__main__":
    unittest.main()
