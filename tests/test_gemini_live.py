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
        by_name = {d.name: d.behavior for d in declarations}
        # desktop_task/browser_task run 25-90s (see their tool descriptions)
        # and must be NON_BLOCKING so the server keeps generating audio while
        # they run instead of freezing the whole session on them -- see
        # gemini_live.NON_BLOCKING_ACTIONS. Every other tool stays BLOCKING:
        # those are fast, and several (e.g. focus_window before type_text)
        # depend on the model actually seeing the prior result before
        # deciding what to call next.
        self.assertEqual(by_name['desktop_task'], 'NON_BLOCKING')
        self.assertEqual(by_name['browser_task'], 'NON_BLOCKING')
        # run_mission narrates through the session while it acts.
        self.assertEqual(by_name['run_mission'], 'NON_BLOCKING')
        # Slow tools are background "sub-agents" so the user can always keep
        # talking (measured up to 11.2s for myapi_call while she sat frozen).
        from omarchy_ai.voice.gemini_live import NON_BLOCKING_ACTIONS
        for slow in ('describe_screen', 'start_casting', 'list_commands', 'check_assistant_updates'):
            self.assertEqual(by_name[slow], 'NON_BLOCKING', slow)
        # Fast chained steps still wait: the next call depends on the result.
        for chained in ('focus_window', 'type_text', 'press_key', 'list_windows'):
            self.assertEqual(by_name[chained], 'BLOCKING', chained)
        self.assertTrue(all(
            behavior == 'BLOCKING' for name, behavior in by_name.items()
            if name not in NON_BLOCKING_ACTIONS
        ))

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

    async def test_non_blocking_action_does_not_serialize_behind_a_fast_call(self):
        # The actual bug this project reported: desktop_task/browser_task are
        # slow (25-90s) and used to be dispatched exactly like every other
        # tool call -- awaited inline in the same queue-processing loop, so a
        # fast action requested right after one queued up behind it instead
        # of running immediately. NON_BLOCKING dispatch (see _tools) must let
        # the fast call finish independently of the slow one still running.
        adapter = GeminiLiveSession(Config())
        entered, release = threading.Event(), threading.Event()

        def dispatch(name, args):
            if name == "desktop_task":
                entered.set()
                release.wait(2)
                return ActionResult(True, "workspace switched")
            return ActionResult(True, "volume up")

        session = SimpleNamespace(send_tool_response=AsyncMock())
        adapter._calls.put_nowait(types.FunctionCall(id="bg", name="desktop_task", args={"goal": "next workspace"}))
        adapter._calls.put_nowait(types.FunctionCall(id="fast", name="volume_up", args={}))
        with patch('omarchy_ai.voice.gemini_live.run_action', side_effect=dispatch), \
                patch('omarchy_ai.voice.gemini_live.LiveSession._current_window', return_value={}):
            worker = asyncio.create_task(adapter._tools(session))
            try:
                for _ in range(100):
                    if entered.is_set() and session.send_tool_response.await_count:
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set())
                # The fast call already got a response while desktop_task is
                # still blocked on `release` -- proof it wasn't stuck in line.
                session.send_tool_response.assert_awaited_once()
                fast_reply = session.send_tool_response.call_args.kwargs['function_responses']
                self.assertEqual(fast_reply.id, 'fast')
                release.set()
                for _ in range(100):
                    if session.send_tool_response.await_count > 1:
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(session.send_tool_response.await_count, 2)
                bg_reply = session.send_tool_response.call_args.kwargs['function_responses']
                self.assertEqual(bg_reply.id, 'bg')
                # WHEN_IDLE scheduling: fold the result in once the model has
                # nothing else to say, not by cutting off other speech.
                self.assertEqual(bg_reply.scheduling, types.FunctionResponseScheduling.WHEN_IDLE)
                self.assertIsNone(fast_reply.scheduling)
            finally:
                release.set()
                worker.cancel()
                for task in list(adapter._bg_tasks):
                    task.cancel()
                await asyncio.gather(worker, *adapter._bg_tasks, return_exceptions=True)

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


class OverlayStateTests(unittest.TestCase):
    """The ASCII overlay's thinking/listening/speaking derivation."""

    def session(self):
        from omarchy_ai.config import Config
        from omarchy_ai.voice.gemini_live import GeminiLiveSession
        with patch("omarchy_ai.voice.gemini_live.EchoCancellation"):
            return GeminiLiveSession(Config())

    def test_a_running_tool_is_thinking(self):
        s = self.session()
        self.assertEqual(s._display_state(100.0), "listening")
        s._running_blocking = 1          # e.g. a MyApi call
        self.assertEqual(s._display_state(100.0), "thinking")

    def test_user_mid_sentence_stays_listening_then_thinking_until_the_reply(self):
        s = self.session()
        s._last_user_speech = s._awaiting_since = 100.0
        self.assertEqual(s._display_state(100.3), "listening")
        self.assertEqual(s._display_state(101.0), "thinking")
        s._state, s._playback_until = "speaking", 103.0
        self.assertEqual(s._display_state(101.5), "speaking")
        # Once the queued speech has played out it is no longer speaking.
        self.assertNotEqual(s._display_state(103.5), "speaking")

    def test_play_audio_keeps_a_cushion_instead_of_lock_step_sleeping(self):
        import asyncio
        from omarchy_ai.voice import gemini_live
        s = self.session()
        writes, sleeps = [], []
        class Stdin:
            def write(self, raw): writes.append(len(raw))
            async def drain(self): pass
        class Speaker:
            returncode = None
            stdin = Stdin()
        async def run():
            s._speaker, s._speaker_generation = Speaker(), s._generation
            for _ in range(3):   # three 0.2 s chunks arriving back to back
                s._audio.put_nowait((s._generation, b"\x00\x00" * 4800))
            task = asyncio.create_task(s._play_audio())
            for _ in range(20):
                await real_sleep(0.005)
            task.cancel()
        real_sleep = asyncio.sleep
        async def fake_sleep(seconds):
            if seconds > 0.01:
                sleeps.append(round(seconds, 2))
            await real_sleep(0)
        with patch.object(gemini_live.asyncio, "sleep", fake_sleep):
            asyncio.run(run())
        self.assertEqual(len(writes), 3)
        # Old code slept 0.2 s after EVERY chunk, draining the pipe to empty
        # right when pw-play read it. Now nothing waits until more than the
        # 0.3 s cushion is queued, and then only for the excess (the fake
        # sleep does not advance the clock, hence 0.1 then 0.3).
        self.assertEqual(sleeps, [0.1, 0.3])

    def test_a_reply_that_never_comes_does_not_stick_on_thinking(self):
        s = self.session()
        s._last_user_speech = s._awaiting_since = 100.0
        self.assertEqual(s._display_state(100.0 + s.REPLY_WAIT_SECONDS + 1), "listening")

    def test_background_task_shows_busy_between_turns(self):
        s = self.session()
        s._running_background = 1        # browser_task / desktop_task
        self.assertEqual(s._display_state(100.0), "thinking")


class JevFastPathTests(unittest.TestCase):
    """Simple commands run from the transcript, and Gemini cannot redo or contradict them."""

    def session(self):
        from omarchy_ai.config import Config
        from omarchy_ai.voice.gemini_live import GeminiLiveSession
        with patch("omarchy_ai.voice.gemini_live.EchoCancellation"):
            return GeminiLiveSession(Config())

    def call(self, name, args):
        return type("Call", (), {"name": name, "args": args, "id": "c1"})()

    def test_utterance_is_judged_once_after_the_pause_and_executed(self):
        import asyncio
        from omarchy_ai.execution.actions import ActionResult
        s = self.session()
        s._utterance = ["Can you switch to ", "workspace 4?"]
        s._last_user_speech = 0.0   # long ago: the pause has passed
        with patch("omarchy_ai.voice.jev_fast.decide", return_value=("workspace_switch", {"number": 4}, {})) as decide, \
                patch("omarchy_ai.voice.jev_fast.execute", return_value=ActionResult(True, "verified: now on workspace 4")) as execute:
            async def run():
                task = asyncio.create_task(s._fast_path())
                await asyncio.sleep(0.25)
                task.cancel()
            asyncio.run(run())
        decide.assert_called_once_with("Can you switch to workspace 4?")
        execute.assert_called_once_with("workspace_switch", {"number": 4})
        self.assertEqual(s._jev_done[:2], ("workspace_switch", {"number": 4}))

    def test_jev_does_not_repeat_what_gemini_already_did(self):
        import asyncio, time
        s = self.session()
        s._utterance, s._last_user_speech = ["move this terminal to workspace 4"], 0.0
        s._gemini_inflight = [("move_window_to_workspace", {"number": 4}, time.monotonic())]
        with patch("omarchy_ai.voice.jev_fast.decide", return_value=("move_window_to_workspace", {"number": 4}, {})), \
                patch("omarchy_ai.voice.jev_fast.execute") as execute:
            async def run():
                task = asyncio.create_task(s._fast_path())
                await asyncio.sleep(0.25)
                task.cancel()
            asyncio.run(run())
        execute.assert_not_called()

    def test_gemini_repeating_the_same_command_is_not_executed_twice(self):
        import time
        s = self.session()
        s._jev_done = ("workspace_switch", {"number": 4}, time.monotonic(), "verified: now on workspace 4", "switch to 4")
        answer = s._jev_already(self.call("workspace_switch", {"number": 4}))
        self.assertTrue(answer["ok"])
        self.assertIn("Already done", answer["message"])

    def test_gemini_contradicting_jev_is_refused(self):
        # Regression: user said 4, Gemini switched to 5.
        import time
        s = self.session()
        s._jev_done = ("workspace_switch", {"number": 4}, time.monotonic(), "verified: now on workspace 4", "Can you switch to workspace 4?")
        answer = s._jev_already(self.call("workspace_switch", {"number": 5}))
        self.assertFalse(answer["ok"])
        self.assertIn("workspace 4", answer["message"])

    def test_dedupe_expires_and_ignores_other_tools(self):
        import time
        s = self.session()
        s._jev_done = ("workspace_switch", {"number": 4}, time.monotonic() - 60, "m", "t")
        self.assertIsNone(s._jev_already(self.call("workspace_switch", {"number": 5})))
        s._jev_done = ("workspace_switch", {"number": 4}, time.monotonic(), "m", "t")
        self.assertIsNone(s._jev_already(self.call("volume_up", {})))


class MissionTests(unittest.TestCase):
    """The commercial_prompt.md failure: narration dropped, wrong terminal, substituted TV."""

    STEPS = {"workspace": 5, "steps": [
        {"say": "Hello, I am Omarchy.", "action": "say"},
        {"say": "Now I will open the browser.", "action": "browser_task", "args": {"url": "https://www.google.com", "goal": "open the first result"}},
        {"say": "I will open a terminal and list files.", "action": "terminal_run", "args": {"command": "ls"}},
        {"say": "Finally I will mirror my screen.", "action": "start_casting", "args": {"target": "HY300 Pro"}},
    ]}

    def session(self):
        from omarchy_ai.config import Config
        from omarchy_ai.voice.gemini_live import GeminiLiveSession
        with patch("omarchy_ai.voice.gemini_live.EchoCancellation"):
            return GeminiLiveSession(Config())

    def test_validation(self):
        from omarchy_ai.execution import missions
        steps, workspace = missions.validate(self.STEPS)
        self.assertEqual((len(steps), workspace), (4, 5))
        for bad in ({"steps": []}, {"steps": [{"action": "rm_rf", "say": "x"}]},
                    {"steps": [{"action": "terminal_run", "say": "x", "args": {}}]}):
            with self.assertRaises(ValueError):
                missions.validate(bad)

    def run_mission(self, results):
        import asyncio
        from omarchy_ai.execution.actions import ActionResult
        s = self.session()
        narrated, ran = [], []
        async def narrate(session, text, i, n):
            narrated.append(text)
            s._turn_done.set()     # the line "finished"
        s._narrate = narrate
        def run_step(step):
            ran.append(step["action"])
            return results.get(step["action"], ActionResult(True, "ok"))
        with patch("omarchy_ai.execution.missions.run_step", side_effect=run_step), \
                patch("omarchy_ai.execution.missions.ensure_workspace", return_value=ActionResult(True, "on ws5")) as ws:
            result = asyncio.run(s._run_mission(object(), self.STEPS))
        return result, narrated, ran, ws

    def test_every_line_is_narrated_with_its_step_in_order(self):
        result, narrated, ran, ws = self.run_mission({})
        self.assertTrue(result.ok)
        self.assertEqual(narrated[0], "Hello, I am Omarchy.")
        self.assertEqual(ran, ["say", "browser_task", "terminal_run", "start_casting"])
        self.assertEqual(ws.call_count, 4)   # workspace rule enforced before every step

    def test_a_missing_projector_stops_the_mission_instead_of_a_substitute(self):
        from omarchy_ai.execution.actions import ActionResult
        result, narrated, ran, _ = self.run_mission(
            {"start_casting": ActionResult(False, "no TV named 'HY300 Pro' found")})
        self.assertFalse(result.ok)
        self.assertIn("step 4", result.message)
        self.assertIn("Do NOT substitute", result.message)

    def test_terminal_run_types_into_the_terminal_it_opened(self):
        from omarchy_ai.execution import missions
        from omarchy_ai.execution.actions import ActionResult
        calls = []
        def fake(name, args):
            calls.append((name, args))
            if name == "open_terminal":
                return ActionResult(True, "opened Omarchy AI 4fd86de1; use that exact title when focusing it")
            return ActionResult(True, "file_a file_b" if name == "read_tile_log" else "ok")
        with patch.object(missions, "run_action", side_effect=fake), patch.object(missions.time, "sleep"):
            result = missions.terminal_run("ls")
        self.assertTrue(result.ok)
        self.assertIn(("focus_window", {"target": "Omarchy AI 4fd86de1"}), calls)
        self.assertEqual([c[0] for c in calls], ["open_terminal", "focus_window", "type_text", "press_key", "read_tile_log"])


class MissionRedirectTests(unittest.TestCase):
    def session(self):
        from omarchy_ai.config import Config
        from omarchy_ai.voice.gemini_live import GeminiLiveSession
        with patch("omarchy_ai.voice.gemini_live.EchoCancellation"):
            return GeminiLiveSession(Config())

    def call(self, name):
        return type("Call", (), {"name": name, "args": {}, "id": "c"})()

    def test_first_action_after_reading_a_script_is_redirected_once(self):
        import time
        s = self.session()
        s._script_read_at = time.monotonic()
        self.assertIsNone(s._mission_redirect(self.call("list_windows")))      # read-only is fine
        blocked = s._mission_redirect(self.call("workspace_switch"))
        self.assertFalse(blocked.ok)
        self.assertIn("run_mission", blocked.message)
        self.assertIsNone(s._mission_redirect(self.call("workspace_switch")))  # only once

    def test_no_redirect_without_a_script_or_for_run_mission(self):
        import time
        s = self.session()
        self.assertIsNone(s._mission_redirect(self.call("workspace_switch")))
        s._script_read_at = time.monotonic()
        self.assertIsNone(s._mission_redirect(self.call("run_mission")))
        self.assertIsNone(s._mission_redirect(self.call("workspace_switch")))


class HeartbeatAnnouncementTests(unittest.TestCase):
    """Jev's heartbeat result wakes her; delivered only if the user answers."""

    def session(self, announcements=None):
        from omarchy_ai.config import Config
        from omarchy_ai.voice.gemini_live import GeminiLiveSession
        with patch("omarchy_ai.voice.gemini_live.EchoCancellation"):
            return GeminiLiveSession(Config(), announcements=announcements)

    def test_result_is_said_and_an_unanswered_self_started_call_hangs_up(self):
        import asyncio
        s = self.session([{"id": "r1", "title": "Notify when Claude is done", "detail": "Watch fired"}])
        sent = []
        class FakeSession:
            async def send_client_content(self, turns, turn_complete):
                sent.append(turns.parts[0].text)
        s.PROACTIVE_SILENCE_SECONDS = 0.3
        async def run():
            await asyncio.wait_for(s._announcer(FakeSession()), 5)
        asyncio.run(run())
        self.assertTrue(s.proactive)
        self.assertIn("Notify when Claude is done", sent[0])
        self.assertIn("started this conversation yourself", sent[0])
        self.assertTrue(s._hangup.is_set())
        self.assertEqual(s.acknowledged_ids(), [])     # nobody answered: stays pending

    def test_an_answer_after_her_speech_acknowledges(self):
        import time
        s = self.session()
        s._announced = [("r1", time.monotonic() - 5)]
        s.user_spoke_at = time.monotonic()
        self.assertEqual(s.acknowledged_ids(), ["r1"])


class AgendaDeliveryTests(unittest.TestCase):
    def test_listeners_get_every_new_result_and_forget_keeps_it_pending(self):
        import tempfile
        from pathlib import Path
        from omarchy_ai.core import agenda
        with tempfile.TemporaryDirectory() as d, patch.object(agenda, "INBOX_PATH", Path(d) / "inbox.jsonl"):
            got = []
            agenda.listeners.append(got.append)
            try:
                agenda._inbox_add({"id": "t-1", "title": "Build watch", "kind": "watch"}, "condition_met", "done")
            finally:
                agenda.listeners.remove(got.append)
            self.assertEqual(got[0]["title"], "Build watch")
            self.assertEqual(len(agenda.briefing()), 1)
            agenda.forget_briefed()                        # user never spoke
            self.assertEqual(len(agenda.briefing()), 1)    # still pending next time
            agenda.mark_delivered([got[0]["id"]])
            agenda.forget_briefed()
            self.assertEqual(agenda.briefing(), [])


class EchoGateTests(unittest.TestCase):
    """She must be able to finish her sentence (13:45 session: cut off 4x by her own echo)."""

    def session(self):
        from omarchy_ai.config import Config
        from omarchy_ai.voice.gemini_live import GeminiLiveSession
        with patch("omarchy_ai.voice.gemini_live.EchoCancellation"):
            return GeminiLiveSession(Config())

    def frame(self, n):
        return bytes([n % 256]) * 640

    def test_her_echo_never_reaches_gemini_while_she_speaks(self):
        s = self.session()
        s._playback_until = 100.0
        for i, rms in enumerate([1764.9, 2385.9, 3163.7, 4556.3] * 10):   # the logged echo levels
            sent = s._gate(self.frame(i), rms, 99.0)
            self.assertEqual(sent, [bytes(640)], "echo must be replaced by silence")
        self.assertEqual(s._barge_count, 0)

    def test_the_user_can_still_barge_in_and_the_onset_is_kept(self):
        s = self.session()
        s._playback_until = 100.0
        for i in range(20):
            s._gate(self.frame(i), 3000.0, 99.0)             # her echo, learned as the baseline
        out = []
        for i in range(s.BARGE_FRAMES):
            out = s._gate(self.frame(50 + i), 8952.0, 99.0)  # a logged real interruption level
        self.assertEqual(out, [self.frame(50 + i) for i in range(s.BARGE_FRAMES)])
        self.assertEqual(s._barge_count, 1)
        self.assertEqual(s._gate(self.frame(99), 1000.0, 99.0), [self.frame(99)])  # stays open

    def test_a_short_loud_blip_is_not_a_barge_in(self):
        s = self.session()
        s._playback_until = 100.0
        for i in range(3):
            s._gate(self.frame(i), 9000.0, 99.0)
        self.assertEqual(s._gate(self.frame(9), 2000.0, 99.0), [bytes(640)])
        self.assertEqual(s._barge_count, 0)

    def test_mic_passes_untouched_when_she_is_silent(self):
        s = self.session()
        s._playback_until = 10.0
        self.assertEqual(s._gate(self.frame(1), 500.0, 20.0), [self.frame(1)])


class MissionVisibilityTests(unittest.TestCase):
    def test_mission_browser_steps_are_always_shown(self):
        from omarchy_ai.execution import missions
        from omarchy_ai.execution.actions import ActionResult
        with patch.object(missions, "run_action", return_value=ActionResult(True, "ok")) as run:
            missions.run_step({"action": "browser_task", "say": "", "args": {"url": "https://www.google.com", "goal": "x"}})
        self.assertEqual(run.call_args.args[1]["show"], "yes")
