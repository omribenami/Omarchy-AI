import asyncio
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from omarchy_ai.config import Config
from omarchy_ai.voice import extensions, fallback, gemini_live
from omarchy_ai.voice.gemini_live import GeminiLiveSession


def said(text):
    content = SimpleNamespace(input_transcription=SimpleNamespace(text=text), output_transcription=None,
                              interrupted=False, turn_complete=True, model_turn=None)
    return SimpleNamespace(tool_call=None, tool_call_cancellation=None, server_content=content)


class Extensions:
    def load(self, **files):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for name, source in files.items():
            (Path(folder.name) / f"{name}.py").write_text(source)
            (Path(folder.name) / f"{name}.py").chmod(0o644)      # whatever the umask of the test run
        for target, value in (("EXTENSIONS_DIR", Path(folder.name)), ("_modules", None)):
            patcher = patch.object(extensions, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return Path(folder.name)


class VoiceExtensionTests(Extensions, unittest.IsolatedAsyncioTestCase):
    def test_without_extensions_the_model_is_connected_as_before(self):
        self.load()
        client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=lambda **kw: ("google", kw["model"]))))
        self.assertEqual(gemini_live.live_connect(client, Config(), "gemini-3.8-live", {}), ("google", "gemini-3.8-live"))

    async def test_an_extension_may_hold_the_conversation_itself(self):
        self.load(mine="from contextlib import asynccontextmanager\n@asynccontextmanager\n"
                       "async def session(model):\n    yield ('mine', model)\n"
                       "def live_connect(config, model, live_config):\n    return session(model)\n")
        async with gemini_live.live_connect(None, Config(), "gemini-3.8-live", {}) as session:
            self.assertEqual(session, ("mine", "gemini-3.8-live"))

    def test_a_broken_or_foreign_file_is_skipped_and_named_in_the_log(self):
        folder = self.load(broken="raise RuntimeError('no')\n", _private="def live_connect(*a):\n    return 1\n",
                           shared="def live_connect(*a):\n    return 2\n",
                           fails="def live_connect(*a):\n    raise ValueError('no')\n")
        (folder / "shared.py").chmod(0o666)     # writable by others: not the user's own code any more
        (folder / "gone.py").symlink_to(folder / "nowhere.py")
        with self.assertLogs("omarchy_ai.voice.extensions", "WARNING") as logs:
            self.assertEqual([m.__name__ for m in extensions.modules()], ["omarchy_ai_extension_fails"])
            self.assertIsNone(extensions.live_connect(Config(), "gemini-3.8-live", {}))
        text = "\n".join(logs.output)
        for name in ("broken.py could not be loaded", "gone.py could not be loaded", "shared.py skipped"):
            self.assertIn(name, text)

    async def test_a_session_that_cannot_be_entered_gives_way_to_the_regular_connection(self):
        self.load(mine="from contextlib import asynccontextmanager\n@asynccontextmanager\n"
                       "async def session():\n    raise OSError('my own server is down')\n    yield\n"
                       "def live_connect(config, model, live_config):\n    return session()\n")
        closed = []

        @asynccontextmanager
        async def google(**kw):
            try:
                yield ("google", kw["model"])
            finally:
                closed.append(kw["model"])
        client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=google)))
        with self.assertLogs("omarchy_ai.voice.extensions", "ERROR"):
            async with gemini_live.live_connect(client, Config(), "gemini-3.8-live", {}) as session:
                self.assertEqual(session, ("google", "gemini-3.8-live"))
        self.assertEqual(closed, ["gemini-3.8-live"])

    async def test_a_failure_inside_an_extensions_session_is_not_the_voice_models(self):
        self.load(mine="from contextlib import asynccontextmanager\n@asynccontextmanager\n"
                       "async def session():\n    yield 'mine'\n"
                       "def live_connect(config, model, live_config):\n    return session()\n")
        with self.assertLogs("omarchy_ai.voice.extensions", "ERROR"), self.assertRaises(extensions.SessionFailed) as raised:
            async with gemini_live.live_connect(None, Config(), "gemini-3.8-live", {}):
                raise ConnectionResetError("connection reset by my own server")
        self.assertFalse(fallback.is_provider_failure(raised.exception))
        with self.assertRaises(fallback.Reconnect):             # asking for a new connection still passes
            async with gemini_live.live_connect(None, Config(), "gemini-3.8-live", {}):
                raise fallback.Reconnect("again")

    async def test_heard_gets_the_users_words_and_may_take_them(self):
        self.load(mine="seen = []\nasync def heard(session, text):\n    seen.append(text)\n    return text == 'mine'\n")
        adapter = GeminiLiveSession(Config())

        async def receive():
            yield said("for the fast path")
            adapter._hangup.set()
        await adapter._receive(SimpleNamespace(receive=receive))
        self.assertEqual(adapter._utterance, ["for the fast path"])
        self.assertEqual(extensions.modules()[0].seen, ["for the fast path"])
        self.assertTrue(await extensions.heard(adapter, "mine"))

    async def test_heard_may_ask_for_a_new_connection_and_nothing_else_ends_the_conversation(self):
        self.load(mine="from omarchy_ai.voice import fallback\ndef heard(session, text):\n"
                       "    if text == 'again':\n        raise fallback.Reconnect('again')\n"
                       "    if text == 'refused':\n        raise ConnectionRefusedError('my own server')\n"
                       "    raise ValueError('bug')\n")
        for text in ("anything", "refused"):
            with self.assertLogs("omarchy_ai.voice.extensions", "ERROR"):
                self.assertFalse(await extensions.heard(None, text))
        with self.assertRaises(fallback.Reconnect):
            await extensions.heard(None, "again")

    async def test_words_that_ask_for_a_new_connection_are_not_left_for_the_fast_path(self):
        self.load(mine="from omarchy_ai.voice import fallback\ndef heard(session, text):\n"
                       "    raise fallback.Reconnect('again')\n")

        async def receive():
            yield said("again")
        reconnecting = GeminiLiveSession(Config())
        reconnecting.restart_stalled = True                      # as run() and the phone bridge set it
        with self.assertRaises(fallback.Reconnect):
            await reconnecting._receive(SimpleNamespace(receive=receive))
        self.assertEqual(reconnecting._utterance, [])
        satellite = GeminiLiveSession(Config())                  # no reconnect loop: the conversation ends instead
        await satellite._receive(SimpleNamespace(receive=receive))
        self.assertEqual((satellite._utterance, satellite._hangup.is_set()), ([], True))


TYPED = """
from types import SimpleNamespace
from omarchy_ai.voice import fallback
def typed(session, text):
    if text == "mine":
        content = SimpleNamespace(input_transcription=None, interrupted=False, turn_complete=True, model_turn=None,
                                  output_transcription=SimpleNamespace(text="Taken."))
        session.on_message(SimpleNamespace(server_content=content, tool_call=None, tool_call_cancellation=None))
        return True
    if text == "again":
        raise fallback.Reconnect("again")
    if text == "bug":
        raise ValueError("bug")
"""


class TypedMessageTests(Extensions, unittest.IsolatedAsyncioTestCase):
    """The desktop chat, with a model that answers every turn it is sent."""

    def chat(self, session_of=None):
        import tempfile
        from omarchy_ai.core import agenda
        from omarchy_ai.voice import text_chat
        from test_text_chat import FakeLive
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for patcher in (patch.object(agenda, "INBOX_PATH", Path(temp.name) / "inbox.jsonl"),
                        patch("omarchy_ai.core.history.append_session"), patch.object(text_chat, "PUSH_DELAY", 0.01)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.lives = []

        @asynccontextmanager
        async def google(**kw):
            self.lives.append(FakeLive(["From the model."] * 3))
            yield self.lives[-1]
        client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=google), aclose=lambda: asyncio.sleep(0)))
        patcher = patch.object(gemini_live, "gemini_client", lambda config: client)
        patcher.start()
        self.addCleanup(patcher.stop)
        return text_chat.TextChat(Config(provider="gemini"), push=lambda state: None)

    async def until(self, check):
        for _ in range(150):
            if check():
                return
            await asyncio.sleep(0.02)
        self.fail("timed out")

    async def test_a_typed_message_an_extension_takes_never_reaches_the_model(self):
        self.load(mine=TYPED)
        chat = self.chat()
        chat.send("mine")
        await self.until(lambda: chat.status == "ready" and len(chat.messages) == 2)
        with self.assertLogs("omarchy_ai.voice.extensions", "ERROR"):
            chat.send("bug")                                     # a hook that breaks takes nothing
            await self.until(lambda: len(chat.messages) == 4 and chat.status == "ready")
        self.assertEqual([m["text"] for m in chat.messages], ["mine", "Taken.", "bug", "From the model."])
        self.assertEqual(self.lives[0].sent, ["bug"])
        self.assertEqual(chat._adapter._transcript[0], {"role": "user", "text": "bug"})
        chat.close()
        await self.until(lambda: chat._task.done())

    async def test_a_chat_that_cannot_reconnect_opens_a_new_session_with_the_next_message(self):
        self.load(mine=TYPED)
        chat = self.chat()
        chat.send("again")
        await self.until(lambda: chat._task.done())
        self.assertEqual((chat.status, self.lives[0].sent), ("idle", []))
        chat.send("hello")
        await self.until(lambda: chat.messages[-1]["text"] == "From the model.")
        self.assertEqual((len(self.lives), self.lives[1].sent), (2, ["hello"]))
        chat.close()
        await self.until(lambda: chat._task.done())
        reconnecting = SimpleNamespace(restart_stalled=True)     # the phone's chat connects again itself
        with self.assertRaises(fallback.Reconnect):
            await extensions.typed(reconnecting, "again")

    async def test_the_desktop_chat_is_held_by_an_extensions_session_too(self):
        from test_text_chat import FakeLive
        self.load(mine="from contextlib import asynccontextmanager\nlive = None\n@asynccontextmanager\n"
                       "async def session():\n    yield live\n"
                       "def live_connect(config, model, live_config):\n    return session()\n")
        chat = self.chat()
        extensions.modules()[0].live = FakeLive(["From the extension."])
        chat.send("hello")
        await self.until(lambda: chat.messages[-1]["text"] == "From the extension.")
        self.assertEqual(self.lives, [])
        chat.close()
        await self.until(lambda: chat._task.done())
