import asyncio
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from google.genai import types
from omarchy_ai.config import Config
from omarchy_ai.execution.actions import ActionResult
from omarchy_ai.voice.gemini_live import GeminiLiveSession, build_live_config


class GeminiTests(unittest.IsolatedAsyncioTestCase):
    def test_config_waits_for_action_results_and_preserves_shared_context(self):
        with patch('omarchy_ai.voice.live.load_preferences', return_value=['Type English']), patch('omarchy_ai.voice.live.load_recent_context', return_value='old task'):
            config = types.LiveConnectConfig(**build_live_config(Config()))
        self.assertIn('Type English', config.system_instruction)
        self.assertIn('NOT pending tasks', config.system_instruction)
        declarations = config.tools[0].function_declarations
        self.assertIn('end_conversation', [d.name for d in declarations])
        self.assertTrue(all(d.behavior == 'BLOCKING' for d in declarations))

    def test_noise_resistant_vad_still_allows_user_interruptions(self):
        config = types.LiveConnectConfig(**build_live_config(Config()))
        realtime = config.realtime_input_config
        self.assertEqual(realtime.activity_handling, types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS)
        vad = realtime.automatic_activity_detection
        self.assertFalse(vad.disabled)
        self.assertEqual(vad.start_of_speech_sensitivity, types.StartSensitivity.START_SENSITIVITY_LOW)
        self.assertEqual(vad.end_of_speech_sensitivity, types.EndSensitivity.END_SENSITIVITY_LOW)
        self.assertEqual(vad.prefix_padding_ms, 300)
        self.assertEqual(vad.silence_duration_ms, 600)

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
        adapter._mic_levels.append((time.monotonic(), 30.0, 80))
        with self.assertLogs('omarchy_ai.voice.gemini', level='WARNING') as logs:
            await adapter._receive(SimpleNamespace(receive=receive))
        self.assertTrue(adapter._audio.empty())
        self.assertEqual(adapter._generation, 1)
        self.assertEqual(adapter._interruptions, 1)
        self.assertIn('mic_rms_500ms=30.0', logs.output[0])
        self.assertIn('queued_chunks=1', logs.output[0])

    async def test_microphone_metrics_preserve_input_even_while_speaking(self):
        adapter = GeminiLiveSession(Config())
        adapter._state = 'speaking'
        reader = asyncio.StreamReader()
        pcm = b'\x64\x00' * 320
        reader.feed_data(pcm)
        reader.feed_eof()
        session = SimpleNamespace(send_realtime_input=AsyncMock())
        with self.assertRaisesRegex(RuntimeError, 'Microphone'):
            await adapter._send_audio(session, SimpleNamespace(stdout=reader))
        self.assertEqual(session.send_realtime_input.call_args.kwargs['audio'].data, pcm)
        self.assertEqual(adapter._mic_levels[-1][1:], (100.0, 100))

    async def test_interrupted_queued_action_is_skipped_but_acknowledged(self):
        adapter = GeminiLiveSession(Config())
        async def receive():
            yield types.LiveServerMessage(tool_call=types.LiveServerToolCall(function_calls=[
                types.FunctionCall(id='queued', name='open_terminal', args={})]))
            yield types.LiveServerMessage(server_content=types.LiveServerContent(interrupted=True))
            adapter._hangup.set()
        await adapter._receive(SimpleNamespace(receive=receive))
        adapter._calls.put_nowait(types.FunctionCall(id='end', name='end_conversation', args={}))
        session = SimpleNamespace(send_tool_response=AsyncMock())
        with patch('omarchy_ai.voice.gemini_live.run_action') as action:
            await adapter._tools(session)
        action.assert_not_called()
        reply = session.send_tool_response.call_args_list[0].kwargs['function_responses']
        self.assertEqual(reply.id, 'queued')
        self.assertFalse(reply.response['ok'])
        self.assertIn('Not executed', reply.response['message'])
        self.assertNotIn('queued', adapter._pending_calls)

    async def test_interruption_preserves_running_tool_result(self):
        adapter = GeminiLiveSession(Config())
        entered, release = threading.Event(), threading.Event()
        def action(*_):
            entered.set(); release.wait(2)
            return ActionResult(True, 'Actual completed result')
        session = SimpleNamespace(send_tool_response=AsyncMock())
        adapter._calls.put_nowait(types.FunctionCall(id='running', name='list_windows', args={}))
        async def receive():
            yield types.LiveServerMessage(server_content=types.LiveServerContent(interrupted=True))
            adapter._hangup.set()
        with patch('omarchy_ai.voice.gemini_live.run_action', side_effect=action), patch('omarchy_ai.voice.gemini_live.LiveSession._current_window', return_value={}):
            worker = asyncio.create_task(adapter._tools(session))
            try:
                for _ in range(100):
                    if entered.is_set(): break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set())
                await adapter._receive(SimpleNamespace(receive=receive))
                self.assertNotIn('running', adapter._cancelled)
                release.set()
                for _ in range(100):
                    if session.send_tool_response.await_count: break
                    await asyncio.sleep(.01)
                session.send_tool_response.assert_awaited_once()
                reply = session.send_tool_response.call_args.kwargs['function_responses']
                self.assertEqual(reply.id, 'running')
                self.assertEqual(reply.response, {'ok': True, 'message': 'Actual completed result'})
            finally:
                release.set(); worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    async def test_explicit_server_tool_cancellation_does_not_get_response(self):
        adapter = GeminiLiveSession(Config())
        async def receive():
            yield types.LiveServerMessage(tool_call=types.LiveServerToolCall(function_calls=[
                types.FunctionCall(id='cancelled', name='open_terminal', args={})]))
            yield types.LiveServerMessage(tool_call_cancellation=types.LiveServerToolCallCancellation(ids=['cancelled']))
            adapter._hangup.set()
        await adapter._receive(SimpleNamespace(receive=receive))
        adapter._calls.put_nowait(types.FunctionCall(id='end', name='end_conversation', args={}))
        session = SimpleNamespace(send_tool_response=AsyncMock())
        with patch('omarchy_ai.voice.gemini_live.run_action') as action:
            await adapter._tools(session)
        action.assert_not_called()
        self.assertEqual([c.kwargs['function_responses'].id for c in session.send_tool_response.call_args_list], ['end'])

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

    async def test_failed_focus_blocks_queued_input_and_records_reason(self):
        adapter = GeminiLiveSession(Config())
        session = SimpleNamespace(send_tool_response=AsyncMock())
        for number, name in enumerate(['focus_window', 'type_text', 'end_conversation']):
            adapter._calls.put_nowait(types.FunctionCall(id=str(number), name=name, args={}))
        with patch('omarchy_ai.voice.gemini_live.run_action', return_value=ActionResult(False, 'target missing')) as execute, patch('omarchy_ai.voice.gemini_live.LiveSession._current_window', return_value={}):
            await adapter._tools(session)
        self.assertEqual(execute.call_count, 1)
        responses = [c.kwargs['function_responses'].response for c in session.send_tool_response.await_args_list]
        self.assertFalse(responses[0]['ok'])
        self.assertFalse(responses[1]['ok'])
        self.assertIn('Input NOT sent', responses[1]['message'])
        self.assertIn('Input NOT sent', adapter._action_log[-1]['message'])
        self.assertEqual(adapter._action_log[-1]['call_id'], '1')
