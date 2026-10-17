"""Legacy prompt stays byte-for-byte; v2 is opt-in."""

import datetime
import unittest
from pathlib import Path
from unittest.mock import patch

from omarchy_ai.config import Config
from omarchy_ai.execution.tools import MYAPI_TOOLS, TOOLS
from omarchy_ai.voice.live import build_session_config
from omarchy_ai.voice.prompt_version import resolve_prompt_version

GOLDEN = Path(__file__).parent / "fixtures" / "legacy_session_instructions.txt"
FROZEN = datetime.datetime(2026, 1, 15, 12, 0, 0)


class _FrozenDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN


def legacy_instructions() -> str:
    """The legacy session prompt under fixed inputs.

    Dynamic sources (clock, memory, history, agenda, skills, MyApi, update
    notice) are pinned so the golden file is the assembly itself.
    """
    config = Config(
        task_agent_model="gpt-test", myapi_enabled=False, prompt_version="legacy"
    )
    with (
        patch("datetime.datetime", _FrozenDateTime),
        patch("omarchy_ai.voice.live.load_preferences", return_value=[]),
        patch("omarchy_ai.voice.live.load_recent_context", return_value=""),
        patch("omarchy_ai.voice.live.updates.wake_notice", return_value=""),
        patch("omarchy_ai.core.agenda.briefing", return_value=[]),
        patch("omarchy_ai.core.skills.index_text", return_value=""),
        patch("omarchy_ai.voice.live.myapi.is_connected", return_value=False),
        patch.dict("os.environ", {}, clear=False),
    ):
        # An ambient OMARCHY_PROMPT_VERSION must not change this snapshot.
        import os

        os.environ.pop("OMARCHY_PROMPT_VERSION", None)
        return build_session_config(config, surface="phone")["instructions"]


class LegacyPromptTests(unittest.TestCase):
    def test_default_version_is_legacy(self):
        self.assertEqual(Config().prompt_version, "legacy")
        self.assertEqual(Config().assistant_name, "Omarchy")
        self.assertEqual(resolve_prompt_version(Config()), "legacy")

    def test_legacy_assembled_prompt_matches_golden(self):
        text = legacy_instructions()
        self.assertIn("Thursday 2026-01-15 12:00", text)
        self.assertEqual(text, GOLDEN.read_text())

    def test_legacy_tool_descriptions_are_the_shared_schemas(self):
        session = build_session_config(Config(myapi_enabled=False))
        by_name = {
            tool["name"]: tool["description"]
            for tool in session["delegation"]["responses"]["tools"]
        }
        for tool in TOOLS:
            self.assertEqual(by_name[tool["name"]], tool["description"])

    def test_surface_argument_does_not_change_legacy_text(self):
        with (
            patch("omarchy_ai.voice.live.load_preferences", return_value=[]),
            patch("omarchy_ai.voice.live.load_recent_context", return_value=""),
            patch("omarchy_ai.voice.live.updates.wake_notice", return_value=""),
            patch("omarchy_ai.core.agenda.briefing", return_value=[]),
            patch("omarchy_ai.core.skills.index_text", return_value=""),
            patch("omarchy_ai.voice.live.myapi.is_connected", return_value=False),
        ):
            voice = build_session_config(Config(task_agent_model="gpt-test"))[
                "instructions"
            ]
            phone = build_session_config(
                Config(task_agent_model="gpt-test"), surface="phone"
            )["instructions"]
        self.assertEqual(voice, phone)


class PromptV2Tests(unittest.TestCase):
    def _v2(self, **kwargs) -> str:
        config = Config(
            prompt_version="v2",
            task_agent_model="gpt-test",
            myapi_enabled=False,
            **kwargs,
        )
        with (
            patch(
                "omarchy_ai.voice.prompt_v2.load_preferences",
                return_value=["Call me Ari."],
            ),
            patch(
                "omarchy_ai.voice.prompt_v2.load_recent_context",
                return_value="user: yesterday",
            ),
            patch("omarchy_ai.voice.prompt_v2.updates.wake_notice", return_value=""),
            patch("omarchy_ai.voice.prompt_v2.agenda.briefing", return_value=[]),
            patch("omarchy_ai.voice.prompt_v2.skills.index_text", return_value=""),
            patch("omarchy_ai.voice.prompt_v2.myapi.is_connected", return_value=False),
            patch("omarchy_ai.core.identities.owner", return_value=None),
            patch("omarchy_ai.core.identities.people", return_value=[]),
        ):
            return build_session_config(config, surface=kwargs.get("surface"))[
                "instructions"
            ]

    def test_env_selects_v2_and_legacy(self):
        with patch.dict("os.environ", {"OMARCHY_PROMPT_VERSION": "v2"}):
            self.assertEqual(
                resolve_prompt_version(Config(prompt_version="legacy")), "v2"
            )
        with patch.dict("os.environ", {"OMARCHY_PROMPT_VERSION": "legacy"}):
            self.assertEqual(
                resolve_prompt_version(Config(prompt_version="v2")), "legacy"
            )
        self.assertEqual(
            resolve_prompt_version(Config(prompt_version="nope")), "legacy"
        )

    def test_memory_is_fenced_and_safety_follows_it(self):
        text = self._v2(
            assistant_name="Omarchy",
            user_name="Omri",
            user_languages=["Hebrew", "English"],
        )
        memory_at = text.index("<user_memory>")
        self.assertLess(memory_at, text.index("</user_memory>"))
        self.assertIn("- Call me Ari.", text[memory_at : text.index("</user_memory>")])
        self.assertLess(text.index("</user_memory>"), text.index("## Safety rules"))
        self.assertLess(
            text.index("## Safety rules"), text.index("## 3. Act, or ask first")
        )
        self.assertLess(
            text.index("## 5. Untrusted content"), text.index("## Safety restatement")
        )
        self.assertIn("Current surface: **voice**", text)
        self.assertIn("Omri speaks Hebrew and English", text)
        self.assertIn("auto-approve level: NORMAL", text)
        self.assertNotIn("{{", text)
        self.assertNotIn("ALL-KNOWING", text)

    def test_surface_and_assistant_name_are_filled(self):
        config = Config(
            prompt_version="v2",
            assistant_name="Nia",
            user_name="Sam",
            task_agent_model="gpt-test",
        )
        with (
            patch("omarchy_ai.voice.prompt_v2.load_preferences", return_value=[]),
            patch("omarchy_ai.voice.prompt_v2.load_recent_context", return_value=""),
            patch("omarchy_ai.voice.prompt_v2.updates.wake_notice", return_value=""),
            patch("omarchy_ai.voice.prompt_v2.agenda.briefing", return_value=[]),
            patch("omarchy_ai.voice.prompt_v2.skills.index_text", return_value=""),
            patch("omarchy_ai.voice.prompt_v2.myapi.is_connected", return_value=False),
        ):
            phone = build_session_config(config, surface="phone")["instructions"]
            chat = build_session_config(config, surface="chat")["instructions"]
        self.assertIn("You are Nia,", phone)
        self.assertIn("Current surface: **phone**", phone)
        self.assertIn("Current surface: **chat**", chat)
        self.assertIn("Sam", phone)

    def test_surface_context_is_read_when_the_call_stays_positional(self):
        from omarchy_ai.voice.prompt_version import prompt_surface

        config = Config(
            prompt_version="v2",
            assistant_name="Nia",
            user_name="Sam",
            task_agent_model="gpt-test",
        )
        with (
            patch("omarchy_ai.voice.prompt_v2.load_preferences", return_value=[]),
            patch("omarchy_ai.voice.prompt_v2.load_recent_context", return_value=""),
            patch("omarchy_ai.voice.prompt_v2.updates.wake_notice", return_value=""),
            patch("omarchy_ai.voice.prompt_v2.agenda.briefing", return_value=[]),
            patch("omarchy_ai.voice.prompt_v2.skills.index_text", return_value=""),
            patch("omarchy_ai.voice.prompt_v2.myapi.is_connected", return_value=False),
            prompt_surface("phone"),
        ):
            text = build_session_config(config)["instructions"]
        self.assertIn("Current surface: **phone**", text)

    def test_v2_tool_notes_do_not_touch_the_shared_schemas(self):
        original = next(
            tool["description"]
            for tool in MYAPI_TOOLS
            if tool["name"] == "myapi_gmail_send"
        )
        session = None
        with (
            patch("omarchy_ai.voice.prompt_v2.myapi.is_connected", return_value=True),
            patch("omarchy_ai.voice.prompt_v2.load_preferences", return_value=[]),
            patch("omarchy_ai.voice.prompt_v2.load_recent_context", return_value=""),
            patch("omarchy_ai.voice.prompt_v2.updates.wake_notice", return_value=""),
            patch("omarchy_ai.voice.prompt_v2.agenda.briefing", return_value=[]),
            patch("omarchy_ai.voice.prompt_v2.skills.index_text", return_value=""),
        ):
            session = build_session_config(
                Config(
                    prompt_version="v2", myapi_enabled=True, task_agent_model="gpt-test"
                )
            )
        described = next(
            tool["description"]
            for tool in session["delegation"]["responses"]["tools"]
            if tool["name"] == "myapi_gmail_send"
        )
        self.assertTrue(described.startswith(original))
        self.assertIn("Do not call this to send immediately", described)
        self.assertEqual(
            original,
            next(
                tool["description"]
                for tool in MYAPI_TOOLS
                if tool["name"] == "myapi_gmail_send"
            ),
        )
        legacy = build_session_config(Config(myapi_enabled=False))
        self.assertNotIn(
            "Do not call this to send immediately",
            " ".join(
                tool["description"]
                for tool in legacy["delegation"]["responses"]["tools"]
            ),
        )


if __name__ == "__main__":
    unittest.main()
