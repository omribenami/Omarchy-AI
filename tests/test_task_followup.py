"""A voice task outlives its conversation and is reported in the next one,
and the user can ask which model is doing it (real gaps, 2026-09-26)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from omarchy_ai.core import agenda
from omarchy_ai.runtime import service
from omarchy_ai.runtime.executors.base import DONE, Report
from omarchy_ai.runtime.executors.coding_agents import ClaudeCode
from omarchy_ai.runtime.task import Task

from test_task_runtime import FakeModel, RuntimeHarness, ScriptedJev, ScriptExecutor


class FollowUpTests(RuntimeHarness):
    def test_only_an_agent_named_in_the_goal_is_pinned(self):
        self.assertEqual(service.named_agent("Use Codex to fix this")[0], "codex")
        self.assertEqual(service.named_agent("Use the browser to finish this")[0], "")

    def setUp(self):
        super().setUp()
        patch.object(agenda, "INBOX_PATH", self.root / "inbox.jsonl").start()
        self.addCleanup(patch.stopall)
        agenda._briefed.clear()

    def run_task(self, source, session=None):
        agent = ScriptExecutor("SYSTEM_AGENT", lambda a, ctx: (ctx.run_command("true", a.workspace),
                                                               Report(DONE, claim="found the cause"))[1])
        agent.model = FakeModel([])
        runtime = self.runtime([agent], ScriptedJev())
        loop = MagicMock()
        loop.call_soon_threadsafe.side_effect = lambda fn, *args: fn(*args)
        with patch.object(service, "get_runtime", return_value=runtime):
            service.announce_to(lambda: session, loop)
        return runtime.start("why did my system freeze", str(self.ws), source=source, background=False)

    def test_result_waits_for_the_next_conversation_after_hanging_up(self):
        task = self.run_task("voice", session=None)
        [item] = agenda.briefing()
        self.assertEqual((item["kind"], item["task_id"], item["outcome"]), ("task", task.id, "certified"))
        with patch("omarchy_ai.core.agenda.briefing", return_value=[item]):
            from omarchy_ai.config import load_config
            from omarchy_ai.voice.live import build_session_config
            prompt = build_session_config(load_config())["instructions"]
        self.assertIn("BACKGROUND TASKS", prompt)
        self.assertIn(f"task_id={task.id}", prompt)
        self.assertIn("whether they want a summary", prompt)
        agenda.mark_briefed()
        self.assertEqual(agenda.briefing(), [], "heard once, not repeated")

    def test_open_conversation_hears_the_same_entry_it_can_acknowledge(self):
        session = MagicMock()
        self.run_task("voice", session=session)
        entry = session.announce.call_args.args[0]
        self.assertEqual(entry["kind"], "task")
        agenda.mark_delivered([entry["id"]])
        self.assertEqual(agenda.briefing(), [])

    def test_task_result_reaches_an_open_phone_call(self):
        call, loop = MagicMock(), MagicMock()
        loop.is_closed.return_value = False
        loop.call_soon_threadsafe.side_effect = lambda fn, *a: fn(*a)
        agenda.attach_call(call, loop)
        self.addCleanup(agenda.detach_call, call)
        task = self.run_task("voice", session=None)
        entry = call.announce.call_args.args[0]
        self.assertEqual((entry["task_id"], entry["outcome"]), (task.id, "certified"))
        self.assertEqual(len(agenda.briefing()), 1, "kept until the user answers on the call")

    def test_cli_tasks_are_not_queued(self):
        self.run_task("cli", session=None)
        self.assertEqual(agenda.briefing(), [])

    def test_newer_event_replaces_unheard_question_and_duplicates_are_dropped(self):
        agenda.task_result("t1", "g", "waiting_user", "question: which disk?")
        agenda.task_result("t1", "g", "certified", "done")
        self.assertIsNone(agenda.task_result("t1", "g", "certified", "done"))
        self.assertEqual([e["outcome"] for e in agenda.briefing()], ["certified"])

    def test_interrupted_voice_task_is_queued_once_at_startup(self):
        self.store.save(Task(id="cut", goal="g", workspace=str(self.ws), status="interrupted", source="voice"))
        runtime = self.runtime([], ScriptedJev())
        with patch.object(service, "get_runtime", return_value=runtime):
            service.announce_to(lambda: None, MagicMock())
            service.announce_to(lambda: None, MagicMock())
        self.assertEqual([(e["task_id"], e["outcome"]) for e in agenda.briefing()], [("cut", "interrupted")])

    def test_status_names_the_model_of_each_step(self):
        task = self.run_task("voice")
        summary = self.store.load(task.id).summary()
        self.assertEqual(summary["last_step"]["model"], "fake")
        self.assertEqual(summary["models"], {"SYSTEM_AGENT": "fake"})
        self.assertIn("Jev", summary["decisions_by"])


class PhoneCallTests(unittest.TestCase):
    """A phone call runs on its own thread and loop; results must reach it."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patch.object(agenda, "INBOX_PATH", Path(temp.name) / "inbox.jsonl").start()
        self.addCleanup(patch.stopall)
        agenda._calls.clear()
        self.addCleanup(agenda._calls.clear)

    def test_task_result_is_said_on_an_open_phone_call(self):
        import asyncio
        import threading
        from omarchy_ai.config import Config
        from omarchy_ai.voice.gemini_live import GeminiLiveSession
        sent, ready = [], threading.Event()

        class FakeLive:
            async def send_client_content(self, turns, turn_complete):
                sent.append(turns.parts[0].text)

        async def call():
            adapter = GeminiLiveSession(Config(provider="gemini"))
            agenda.attach_call(adapter, asyncio.get_running_loop())
            ready.set()
            announcer = asyncio.create_task(adapter._announcer(FakeLive()))
            for _ in range(100):
                if sent:
                    break
                await asyncio.sleep(0.05)
            adapter._hangup.set()
            await announcer
            agenda.detach_call(adapter)

        thread = threading.Thread(target=asyncio.run, args=(call(),))
        thread.start()
        ready.wait(5)
        entry = agenda.task_result("t9", "why did it freeze", "certified", "Task t9 finished and verified.")
        self.assertTrue(agenda.announce_to_calls(entry))
        thread.join(10)
        [text] = sent
        self.assertIn("why did it freeze", text)
        self.assertIn("task_id t9", text)
        self.assertIn("summary", text)
        self.assertEqual(agenda._calls, {})
        self.assertFalse(agenda.announce_to_calls(entry), "no call open any more")

    def test_she_does_not_announce_over_queued_phone_audio(self):
        from omarchy_ai.config import Config
        from omarchy_ai.voice.gemini_live import GeminiLiveSession
        adapter = GeminiLiveSession(Config(provider="gemini"))
        self.assertTrue(adapter._idle())
        adapter._audio.put_nowait((0, b"\0" * 960))
        self.assertFalse(adapter._idle())

    def test_daemon_does_not_wake_the_desktop_while_the_user_is_on_the_phone(self):
        import threading
        from omarchy_ai.core.daemon import OmaDaemon
        from omarchy_ai.config import Config
        d = OmaDaemon.__new__(OmaDaemon)
        d.config, d._state, d._session = Config(provider="gemini"), "listening", None
        d._pending_announcements, d._manual, d._listen_stop = [], False, threading.Event()
        call, loop = MagicMock(), MagicMock()
        loop.is_closed.return_value = False
        loop.call_soon_threadsafe.side_effect = lambda fn, *a: fn(*a)
        agenda.attach_call(call, loop)
        d._on_agenda_result({"id": "r1", "title": "Build finished"})
        call.announce.assert_called_once()
        self.assertFalse(d._listen_stop.is_set())
        self.assertEqual(d._pending_announcements, [])


class ClaudeModelTests(unittest.TestCase):
    def test_claude_code_reports_the_models_it_used(self):
        out = json.dumps({"result": "ok", "modelUsage": {"claude-opus-5-5": {}}})
        with tempfile.TemporaryDirectory() as tmp:
            _, meta = ClaudeCode().parse(out, Path(tmp))
        self.assertEqual(meta["models"], ["claude-opus-5-5"])


if __name__ == "__main__":
    unittest.main()


class TaskQuestionGuidanceTests(unittest.TestCase):
    # 2026-09-27 18:55: a task waiting on a question was read out as an
    # approval and the user's replies were never passed to it.
    def test_status_of_a_task_with_a_question_says_to_relay_the_reply(self):
        from unittest.mock import MagicMock, patch
        from omarchy_ai.runtime import service
        runtime = MagicMock()
        runtime.status.return_value = {"id": "t1", "status": "waiting_user", "question": "add the key?",
                                       "pending_approval": None}
        with patch.object(service, "get_runtime", return_value=runtime):
            summary = json.loads(service.task_status({}).message)
        self.assertIn("NOT waiting for approval", summary["what_to_do"])
        self.assertIn("task_respond with task_id t1 and answer=", summary["what_to_do"])
