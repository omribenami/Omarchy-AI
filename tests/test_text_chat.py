"""Desktop text chat (voice/text_chat.py): typed turns through the same Gemini
Live session as voice, replies from the output transcription, audio dropped."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omarchy_ai.config import Config
from omarchy_ai.core import agenda
from omarchy_ai.voice import text_chat


def message(text=None, done=False, audio=None):
    content = SimpleNamespace(input_transcription=None, interrupted=False, turn_complete=done,
                              output_transcription=SimpleNamespace(text=text) if text else None,
                              model_turn=SimpleNamespace(parts=[SimpleNamespace(inline_data=SimpleNamespace(data=audio))])
                              if audio else None)
    return SimpleNamespace(server_content=content, tool_call=None, tool_call_cancellation=None)


class FakeLive:
    """Answers every typed turn with a streamed reply, like gemini-3.8-live."""

    def __init__(self, replies):
        self.replies, self.sent, self.inbox = list(replies), [], asyncio.Queue()

    async def send_client_content(self, turns, turn_complete):
        self.sent.append(turns["parts"][0]["text"])
        reply = self.replies.pop(0)
        for i in range(0, len(reply), 6):
            await self.inbox.put(message(reply[i:i + 6], audio=b"\0" * 960))
        await self.inbox.put(message(done=True))

    async def receive(self):
        while True:
            m = await self.inbox.get()
            yield m
            if m.server_content.turn_complete:
                return


def api_error(code, text):
    from google.genai import errors
    return errors.APIError(code, {"error": {"message": text}})


class DroppingLive(FakeLive):
    """FakeLive whose connection Gemini closes with `error` when typed turn
    number `drop_at` (0-based) arrives, after the tool calls in `calls`."""

    def __init__(self, replies, drop_at=None, error=None, calls=()):
        super().__init__(replies)
        self.drop_at, self.error, self.calls, self.context, self.turns = drop_at, error, list(calls), [], 0

    async def send_client_content(self, turns, turn_complete):
        if not turn_complete:  # the carried-over conversation: context, no reply
            self.context.append(turns["parts"][0]["text"])
            return
        if self.turns == self.drop_at:
            self.sent.append(turns["parts"][0]["text"])
            for call in self.calls:
                await self.inbox.put(SimpleNamespace(
                    server_content=None, tool_call_cancellation=None,
                    tool_call=SimpleNamespace(function_calls=[SimpleNamespace(id=call, name="use_tool", args={})])))
            await self.inbox.put(self.error)
            return
        self.turns += 1
        await super().send_client_content(turns, turn_complete)

    async def receive(self):
        while True:
            m = await self.inbox.get()
            if isinstance(m, BaseException):
                raise m
            yield m
            if m.server_content is not None and m.server_content.turn_complete:
                return


class TextChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patch.object(agenda, "INBOX_PATH", Path(temp.name) / "inbox.jsonl").start()
        self.archived = []
        patch("omarchy_ai.core.history.append_session",
              side_effect=lambda turns, hours: self.archived.append(list(turns))).start()
        patch.object(text_chat, "PUSH_DELAY", 0.01).start()
        self.addCleanup(patch.stopall)
        agenda._calls.clear()

    def chat(self, replies, **config):
        self.live, self.connects, self.pushed = FakeLive(replies), [], []

        class Connection:
            async def __aenter__(inner):
                return self.live

            async def __aexit__(inner, *exc):
                return False

        class Client:
            aio = SimpleNamespace(aclose=lambda: asyncio.sleep(0))

        def connect(cfg, live_config):
            self.connects.append(live_config)
            return Client(), Connection()
        return text_chat.TextChat(Config(provider="gemini", **config), push=self.pushed.append, connect=connect)

    def chat_on(self, lives, **config):
        """A chat whose n-th connection is lives[n]."""
        self.lives, self.connects, self.pushed = list(lives), [], []
        test = self

        class Connection:
            def __init__(inner, live):
                inner.live = live

            async def __aenter__(inner):
                if isinstance(inner.live, BaseException):
                    raise inner.live
                return inner.live

            async def __aexit__(inner, *exc):
                return False

        class Client:
            aio = SimpleNamespace(aclose=lambda: asyncio.sleep(0))

        def connect(cfg, live_config):
            test.connects.append(live_config)
            return Client(), Connection(test.lives[len(test.connects) - 1])
        return text_chat.TextChat(Config(provider="gemini", **config), push=self.pushed.append, connect=connect)

    async def until(self, check, timeout=3):
        for _ in range(int(timeout / 0.02)):
            if check():
                return
            await asyncio.sleep(0.02)
        self.fail("timed out")

    async def test_typed_turns_share_one_session_and_show_the_transcript(self):
        chat = self.chat(["Two plus two is four.", "That would be forty."])
        self.assertTrue(chat.send("What is 2 plus 2?")["ok"])
        await self.until(lambda: chat.status == "ready" and len(chat.messages) == 2)
        chat.send("And times 10?")
        await self.until(lambda: len(chat.messages) == 4 and chat.messages[-1]["text"].endswith("forty."))
        self.assertEqual([m["role"] for m in chat.messages], ["user", "assistant", "user", "assistant"])
        self.assertEqual(chat.messages[1]["text"], "Two plus two is four.")
        self.assertEqual(len(self.connects), 1, "one Gemini session for the whole chat")
        self.assertIn("TEXT CHAT", self.connects[0]["system_instruction"])
        self.assertIn("live stage indicator", self.connects[0]["system_instruction"])
        await self.until(lambda: self.pushed and self.pushed[-1]["messages"][-1]["text"].endswith("forty."))
        self.assertTrue(chat._adapter._audio.empty(), "her audio is drained, never played")
        chat.close()
        await self.until(lambda: chat._task.done())
        self.assertEqual(chat.status, "idle")
        self.assertEqual(self.archived[0][0], {"role": "user", "text": "What is 2 plus 2?"})
        self.assertEqual(agenda._calls, {}, "a closed chat no longer receives announcements")

    async def test_a_note_shows_in_an_open_chat_as_its_own_kind_of_line(self):
        from omarchy_ai.core import conversations
        chat = self.chat(["Saved."])
        conversations.add_note("", "Skill created: early")  # no chat open yet: nowhere to show it
        chat.send("save that as a skill")
        await self.until(lambda: chat.status == "ready" and len(chat.messages) == 2)
        await asyncio.to_thread(conversations.add_note, "", "Skill created: water-plants")  # from a worker thread
        conversations.add_note("20261012-000000-abcdef", "Skill created: elsewhere")  # another talk's
        await self.until(lambda: self.pushed and self.pushed[-1]["messages"][-1]["role"] == "note")
        self.assertEqual([(m["role"], m["text"]) for m in chat.messages][1:],
                         [("assistant", "Saved."), ("note", "Skill created: water-plants")])
        chat.close()
        await self.until(lambda: chat._task.done())

    async def test_next_message_after_close_opens_a_new_session(self):
        chat = self.chat(["One.", "Two."])
        chat.send("first")
        await self.until(lambda: len(chat.messages) == 2)
        chat.close()
        chat.send("second")
        await self.until(lambda: len(chat.messages) == 4 and chat.messages[-1]["text"] == "Two.")
        self.assertEqual(len(self.connects), 2)
        self.assertEqual(self.live.sent, ["first", "second"], "nothing lost between sessions")

    async def test_announcements_reach_an_open_chat(self):
        chat = self.chat(["Hi.", "Here is the summary."])
        chat.send("hello")
        await self.until(lambda: len(chat.messages) == 2)
        self.assertEqual(list(agenda._calls), [chat._adapter])
        chat.close()

    async def test_other_providers_are_told_plainly(self):
        chat = text_chat.TextChat(Config(provider="openai"), push=lambda s: None)
        result = chat.send("hello")
        self.assertFalse(result["ok"])
        self.assertIn("Gemini", result["error"])
        self.assertEqual(chat.messages[-1]["role"], "error")

    async def test_connection_failure_is_shown_and_the_next_message_retries(self):
        chat = self.chat(["Back."])
        calls = []

        def broken(cfg, live_config):
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionError("1011 Internal error")
            return self.connect_ok(cfg, live_config)
        self.connect_ok, chat._connect = chat._connect, broken
        chat.send("hello")
        await self.until(lambda: chat.status == "error")
        self.assertIn("reconnect", chat.messages[-1]["text"])
        chat.send("again")
        await self.until(lambda: chat.messages[-1]["text"] == "Back.")
        chat.close()


    async def test_a_1011_reconnects_once_and_sends_the_unanswered_line_again(self):
        # Journal 2026-10-17 08:09 and 09:50: "received 1011 (internal error)"
        # right after the typed line, which was never acted on.
        first = DroppingLive(["Done."], drop_at=1, error=api_error(1011, "Internal error encountered."))
        second = DroppingLive(["The backyard fan is on."])
        chat = self.chat_on([first, second])
        chat.send("Turn the volume down a little")
        await self.until(lambda: len(chat.messages) == 2)
        with self.assertLogs("omarchy_ai.voice.text_chat", "WARNING") as logged:
            chat.send("Turn on the backyard fan")
            await self.until(lambda: chat.messages[-1]["text"] == "The backyard fan is on.")
        self.assertEqual(len(self.connects), 2)
        self.assertIs(self.connects[1], self.connects[0], "the same config")
        self.assertEqual(second.sent, ["Turn on the backyard fan"])
        self.assertIn("user: Turn the volume down a little\nassistant: Done.", second.context[0])
        self.assertNotIn("backyard fan", second.context[0], "the resent line is the request, not context")
        self.assertEqual([m["role"] for m in chat.messages], ["user", "assistant", "user", "assistant"])
        self.assertNotEqual(chat.status, "error")
        self.assertTrue(any("Gemini session reconnected after 1011" in line for line in logged.output))
        chat.close()
        await self.until(lambda: chat._task.done())
        self.assertEqual([t["text"] for t in self.archived[0] if t["role"] == "user"],
                         ["Turn the volume down a little", "Turn on the backyard fan"])

    async def test_a_line_whose_tool_call_started_is_not_sent_again(self):
        first = DroppingLive([], drop_at=0, error=api_error(1006, "Abnormal closure."), calls=["c1"])
        second = DroppingLive([])
        ran = []

        async def run_call(adapter, session, call):
            ran.append(call.id)  # it ran; its answer is lost with the connection
        with patch("omarchy_ai.voice.gemini_live.GeminiLiveSession._run_call", run_call):
            chat = self.chat_on([first, second])
            chat.send("Turn on the backyard fan")
            await self.until(lambda: chat.messages[-1]["role"] == "note")
            await self.until(lambda: len(self.connects) == 2 and chat._adapter._drop_restarts == 1)
        self.assertEqual(ran, ["c1"])
        self.assertEqual(second.sent, [], "it may have acted: not sent again")
        self.assertIn("may or may not have finished", chat.messages[-1]["text"])
        self.assertEqual(chat._adapter._running_blocking, 0)
        chat.close()
        await self.until(lambda: chat._task.done())

    async def test_when_the_reconnect_fails_too_the_reason_is_shown(self):
        drop = api_error(1011, "Internal error encountered.")
        chat = self.chat_on([DroppingLive([], drop_at=0, error=drop), drop])
        chat.send("Turn on the backyard fan")
        await self.until(lambda: chat.status == "error")
        self.assertEqual(len(self.connects), 2)
        self.assertEqual(chat.messages[-1]["text"], "Connection to Gemini ended (Gemini closed it: 1011 internal "
                                                    "error) and reconnecting did not help. Send again to reconnect.")

    async def test_a_policy_close_does_not_reconnect(self):
        chat = self.chat_on([DroppingLive([], drop_at=0, error=api_error(1008, "Policy violation."))])
        chat.send("hello")
        await self.until(lambda: chat.status == "error")
        self.assertEqual(len(self.connects), 1)
        self.assertEqual(chat.messages[-1]["text"],
                         "Connection to Gemini ended (Gemini refused it: 1008). Send again to reconnect.")


class DaemonAndSettingsTests(unittest.TestCase):
    def test_daemon_routes_chat_commands(self):
        from omarchy_ai.core.daemon import OmaDaemon
        d = OmaDaemon.__new__(OmaDaemon)
        d.config = Config(provider="gemini")
        sent = []
        d._text_chat = SimpleNamespace(send=lambda t: sent.append(t) or {"ok": True}, close=lambda: {"ok": True},
                                       clear=lambda: {"ok": True}, state=lambda: {"messages": []})
        self.assertEqual(d.panel_command('chat-send {"text": "-rf starts with a dash"}'), {"ok": True})
        self.assertEqual(sent, ["-rf starts with a dash"])
        self.assertEqual(d.panel_command("chat-send not json")["ok"], False)
        self.assertEqual(d.panel_command("chat-state"), {"messages": []})

    def test_mode_is_validated_and_pushed_to_the_hud(self):
        from omarchy_ai.cli import settings
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(settings, "USER_CONFIG_PATH", Path(tmp) / "config.yaml"), \
             patch.object(settings, "CONFIG_DIR", Path(tmp)), \
             patch.object(settings, "_snapshot", return_value={}), \
             patch.object(settings, "_chat_hud") as hud:
            self.assertIn("error", settings.cmd_set(SimpleNamespace(key="text_chat_mode", value='"sometimes"')))
            settings.cmd_set(SimpleNamespace(key="text_chat_mode", value='"always"'))
            hud.assert_called_once_with("setPinned", "true")
            self.assertIn("text_chat_mode: always", (Path(tmp) / "config.yaml").read_text())

    def test_send_reads_the_message_from_the_environment(self):
        from omarchy_ai.cli import settings
        with patch.dict("os.environ", {"OMARCHY_AI_CHAT_TEXT": "--help"}), \
             patch.object(settings.control, "request", return_value={"ok": True}) as request:
            self.assertEqual(settings.cmd_chat_send(SimpleNamespace()), {"ok": True})
        request.assert_called_once_with('chat-send {"text": "--help"}')

    def test_keybinding_is_the_voice_key_plus_ctrl(self):
        script = (Path(__file__).parents[1] / "scripts/install-keybinding.sh").read_text()
        self.assertIn('o.bind("SUPER + CTRL + GRAVE", "Omarchy AI text chat", "@ACTIVATE_CMD@ chat-toggle")', script)
        self.assertIn('hl.unbind("SUPER + CTRL + GRAVE")', script)


if __name__ == "__main__":
    unittest.main()
