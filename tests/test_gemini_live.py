import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from google.genai import types
from omarchy_ai.config import Config
from omarchy_ai.execution.actions import ActionResult
from omarchy_ai.voice.gemini_live import GeminiLiveSession, build_live_config


class GeminiTests(unittest.IsolatedAsyncioTestCase):
    def test_config_exposes_nonblocking_actions_and_shared_context(self):
        with patch('omarchy_ai.voice.live.load_preferences', return_value=['Type English']), patch('omarchy_ai.voice.live.load_recent_context', return_value='old task'):
            config = types.LiveConnectConfig(**build_live_config(Config()))
        self.assertIn('Type English', config.system_instruction)
        self.assertIn('NOT pending tasks', config.system_instruction)
        declarations = config.tools[0].function_declarations
        self.assertIn('end_conversation', [d.name for d in declarations])
        self.assertTrue(all(d.behavior == 'NON_BLOCKING' for d in declarations))

    async def test_receive_crosses_turns_and_deduplicates_calls(self):
        adapter = GeminiLiveSession(Config())
        count = 0
        async def receive():
            nonlocal count
            count += 1
            if count == 3:
                adapter._hangup.set()
            yield types.LiveServerMessage(tool_call=types.LiveServerToolCall(function_calls=[types.FunctionCall(id='1', name='list_windows', args={})]))
        await adapter._receive(SimpleNamespace(receive=receive))
        self.assertEqual(count, 3)
        self.assertEqual(adapter._calls.qsize(), 1)

    async def test_actions_do_not_block_microphone(self):
        adapter = GeminiLiveSession(Config())
        entered, release = threading.Event(), threading.Event()
        def action(*args):
            entered.set()
            release.wait(2)
            return ActionResult(True, 'done')
        session = SimpleNamespace(send_tool_response=AsyncMock(), send_realtime_input=AsyncMock())
        adapter._calls.put_nowait(types.FunctionCall(id='1', name='list_windows', args={}))
        reader = asyncio.StreamReader()
        reader.feed_data(b'\0' * 640)
        with patch('omarchy_ai.voice.gemini_live.run_action', side_effect=action), patch('omarchy_ai.voice.gemini_live.LiveSession._current_window', return_value={}):
            worker = asyncio.create_task(adapter._tools(session))
            sender = asyncio.create_task(adapter._send_audio(session, SimpleNamespace(stdout=reader)))
            try:
                for _ in range(100):
                    if entered.is_set() and session.send_realtime_input.await_count:
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set())
                session.send_realtime_input.assert_awaited()
                session.send_tool_response.assert_not_awaited()
                release.set()
                for _ in range(100):
                    if session.send_tool_response.await_count:
                        break
                    await asyncio.sleep(.01)
                session.send_tool_response.assert_awaited_once()
            finally:
                release.set()
                worker.cancel()
                sender.cancel()
                await asyncio.gather(worker, sender, return_exceptions=True)

    async def test_interruption_discards_queued_speech(self):
        adapter = GeminiLiveSession(Config())
        adapter._audio.put_nowait((0, b'old'))
        async def receive():
            yield types.LiveServerMessage(server_content=types.LiveServerContent(interrupted=True))
            adapter._hangup.set()
        await adapter._receive(SimpleNamespace(receive=receive))
        self.assertTrue(adapter._audio.empty())
        self.assertEqual(adapter._generation, 1)

    async def test_microphone_eof_is_error(self):
        reader = asyncio.StreamReader()
        reader.feed_eof()
        with self.assertRaisesRegex(RuntimeError, 'Microphone'):
            await GeminiLiveSession(Config())._send_audio(None, SimpleNamespace(stdout=reader))

    async def test_empty_receive_is_disconnect_not_busy_loop(self):
        async def receive():
            if False:
                yield None
        with self.assertRaises(ConnectionError):
            await GeminiLiveSession(Config())._receive(SimpleNamespace(receive=receive))
