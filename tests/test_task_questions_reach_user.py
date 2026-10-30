"""A task's question reaches the user where they are and is answered by voice
(runtime/asking.py): said on the speaker the task was asked on, else pushed
to the phone with a tap that opens its chat, shown on a display through the
satellite's state command, never sent empty, never sent again unchanged.
Fakes and temporary state only: no device, no phone, no network."""

import asyncio
import json
import secrets
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import no_live_desktop  # noqa: F401 -- run alone too: no real notification, push or desktop command
from omarchy_ai.config import Config
from omarchy_ai.core import agenda, conversations, outbox
from omarchy_ai.runtime import asking, questions, service
from omarchy_ai.runtime import runtime as runtime_module
from omarchy_ai.runtime.task import RUNNING, WAITING_USER, Task, TaskStore
from omarchy_ai.voice import satellite as satellite_module

from test_task_runtime import RuntimeHarness, ScriptedJev

SPEAKER = "10.0.0.2"


def waiting_task(store, question="Which screen should I mirror, the computer or the phone?",
                 conversation="c-speaker", answers=None):
    task = Task(id=f"t{secrets.token_hex(3)}", goal="mirror the screen to the TV", workspace="/tmp")
    task.conversation, task.source = conversation, "voice"
    task.question, task.status = question, WAITING_USER
    task.answers = list(answers or [])
    task.budget["asked"] = [{"q": question[:400], "answers": len(task.answers)}]
    store.save(task)
    return task


class Isolated(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = TaskStore(self.root / "tasks")
        outbox.reset_for_tests()
        self.addCleanup(outbox.reset_for_tests)
        asking.reset_for_tests()
        self.addCleanup(asking.reset_for_tests)
        self.push, self.chat = MagicMock(), MagicMock()
        for target, value in (("LEDGER_PATH", self.root / "outbox.json"), ("_push", self.push),
                              ("_chat", self.chat), ("_desktop", MagicMock()), ("_desk_state", lambda: "idle"),
                              ("_phone_connected", lambda: False)):
            patch.object(outbox, target, value).start()
        patch.object(agenda, "INBOX_PATH", self.root / "inbox.jsonl").start()
        patch.object(service, "get_runtime", return_value=SimpleNamespace(store=self.store)).start()
        self.sources = {"c-speaker": ("satellite", SPEAKER), "c-phone": ("phone-text", "")}
        patch.object(conversations, "source", lambda cid: self.sources.get(cid, ("", ""))[0]).start()
        patch.object(conversations, "device", lambda cid: self.sources.get(cid, ("", ""))[1]).start()
        self.timers = []
        patch.object(asking.threading, "Timer",
                     lambda delay, fn, args: SimpleNamespace(daemon=True, start=lambda: self.timers.append((fn, args)))
                     ).start()
        self.addCleanup(patch.stopall)

    def key(self, task):
        return runtime_module.outbox_key(task, runtime_module.notice(task, "waiting_user")[2])


class SpeakerFirstTests(Isolated):
    def test_a_question_asked_on_a_speaker_is_said_there_and_not_pushed(self):
        task = waiting_task(self.store)
        said = []
        with patch.object(satellite_module, "reachable", return_value=True), \
                patch.object(satellite_module, "ask", side_effect=lambda e: said.append(e) or True):
            self.assertTrue(asking.deliver(task, {"id": "e1", "task_id": task.id}))
        [entry] = said
        self.assertEqual((entry["speaker"], entry["conversation"], entry["id"]), (SPEAKER, "c-speaker", "e1"))
        self.assertEqual(entry["question"], "Which screen should I mirror, the computer or the phone?")
        self.assertEqual(entry["question_id"], f"{task.id}.1")
        self.assertTrue(outbox.delivered(self.key(task)), "the runtime's own notice is not sent as well")
        runtime_module.send_question(task, self.key(task))
        self.push.assert_not_called()

    def test_unanswered_on_the_speaker_it_goes_to_the_phone_with_its_chat(self):
        task = waiting_task(self.store)
        with patch.object(satellite_module, "reachable", return_value=True), \
                patch.object(satellite_module, "ask", return_value=True):
            asking.deliver(task)
        [(later, args)] = self.timers
        later(*args)
        title, body = self.push.call_args.args[:2]
        self.assertEqual(title, "Question: Which screen should I mirror, the computer or the phone?")
        self.assertIn("mirror the screen to the TV", body)
        self.chat.assert_called_once_with(title, "other", "c-speaker")  # the tap opens the chat it was asked in
        later(*args)
        self.assertEqual(self.push.call_count, 1, "once")

    def test_answered_meanwhile_nothing_goes_to_the_phone(self):
        task = waiting_task(self.store)
        with patch.object(satellite_module, "reachable", return_value=True), \
                patch.object(satellite_module, "ask", return_value=True):
            asking.deliver(task)
        task.status, task.question = RUNNING, None
        self.store.save(task)
        later, args = self.timers[0]
        later(*args)
        self.push.assert_not_called()

    def test_a_speaker_that_is_not_listening_leaves_it_to_the_phone(self):
        task = waiting_task(self.store)
        with patch.object(satellite_module, "reachable", return_value=False), \
                patch.object(satellite_module, "ask") as ask:
            self.assertFalse(asking.deliver(task))
        ask.assert_not_called()
        self.assertFalse(outbox.delivered(self.key(task)))

    def test_a_task_not_asked_on_a_speaker_goes_the_usual_way(self):
        task = waiting_task(self.store, conversation="c-phone")
        with patch.object(satellite_module, "ask") as ask:
            self.assertFalse(asking.deliver(task))
        ask.assert_not_called()

    def test_a_question_with_no_words_is_not_said_or_sent(self):
        task = waiting_task(self.store, question="SUMMARY: checked the TV.\nFILES CHANGED: none")
        with patch.object(satellite_module, "ask") as ask, self.assertLogs(asking.log, "WARNING"):
            self.assertTrue(asking.deliver(task))
        ask.assert_not_called()


class PhoneNoticeTests(Isolated):
    def notify(self, task):
        runtime_module._notify(SimpleNamespace(store=self.store), task, "waiting_user")

    def test_the_phone_gets_the_question_as_its_title_and_the_chat_link(self):
        task = waiting_task(self.store, conversation="c-phone")
        self.notify(task)
        title = self.push.call_args.args[0]
        self.assertTrue(title.startswith("Question: Which screen"), title)
        self.chat.assert_called_once_with(title, "other", "c-phone")

    def test_the_same_question_again_with_no_answer_is_not_sent_again(self):
        # 2026-10-26: "which screen?" reached the user four times; each step
        # changed the notice's fingerprint.
        task = waiting_task(self.store, conversation="c-phone")
        self.notify(task)
        task.steps.append({"n": 1, "executor": "CODEX", "role": "work", "outcome": "done"})
        self.notify(task)
        self.assertEqual(self.push.call_count, 1)
        task.answers.append("the computer")
        task.steps.append({"n": 2, "executor": "CODEX", "role": "work", "outcome": "done"})
        self.notify(task)
        self.assertEqual(self.push.call_count, 2, "after an answer the same words are a new question")

    def test_an_empty_question_sends_nothing_and_makes_no_card(self):
        task = waiting_task(self.store, question="SUMMARY: nothing to report\nRISKS: none", conversation="c-phone")
        with self.assertLogs(runtime_module.log, "WARNING"):
            self.notify(task)
        self.push.assert_not_called()
        added = []
        with patch.object(conversations, "add_card", side_effect=lambda cid, card: added.append(card)), \
                patch.object(conversations, "_last_task_cards", return_value=[]), \
                self.assertLogs(conversations.log, "WARNING"):
            conversations.task_event(task, "waiting_user")
        self.assertEqual(added, [])


class AnsweredInTheConversationThatHeardItTests(Isolated):
    def test_a_question_said_in_another_conversation_is_answered_there(self):
        task = waiting_task(self.store, conversation="c-phone")
        token = conversations.CURRENT.set("c-speaker")
        self.addCleanup(conversations.CURRENT.reset, token)
        self.assertIsNone(service._in_this_conversation(service.get_runtime(), task.id)[0])
        self.assertIsNone(service._in_this_conversation(service.get_runtime(), None)[0])
        asking.put_to(task.id, "c-speaker")
        self.assertEqual(service._in_this_conversation(service.get_runtime(), task.id), (task.id, None))
        self.assertEqual(service._in_this_conversation(service.get_runtime(), None), (task.id, None))


class DisplayLineTests(Isolated):
    def setUp(self):
        super().setUp()
        self.lines = []
        patch.object(asking, "_tell", self.lines.append).start()

    def test_open_then_answered(self):
        task = waiting_task(self.store)
        asking.on_event(task, "waiting_user")
        asking.on_event(task, "waiting_user")  # the same line is not sent twice
        task.answers.append("the computer")
        task.status, task.question = RUNNING, None
        asking.on_event(task, "progress")
        self.assertEqual(self.lines, [
            {"question": "open", "id": f"{task.id}.1",
             "text": "Which screen should I mirror, the computer or the phone?", "speaker": SPEAKER},
            {"question": "answered", "id": f"{task.id}.1", "speaker": SPEAKER}])

    def test_withdrawn_when_the_task_ends_unanswered(self):
        task = waiting_task(self.store, conversation="c-phone")
        asking.on_event(task, "waiting_user")
        task.status = "cancelled"
        asking.on_event(task, "cancelled")
        self.assertEqual(self.lines[-1], {"question": "withdrawn", "id": f"{task.id}.1", "speaker": ""})

    def test_sync_sends_the_open_ones_again_and_ends_one_answered_elsewhere(self):
        first, second = waiting_task(self.store), waiting_task(self.store, conversation="c-phone")
        asking.sync()
        self.assertEqual(sorted(line["id"] for line in self.lines), sorted([f"{first.id}.1", f"{second.id}.1"]))
        second.answers.append("yes")
        second.status, second.question = RUNNING, None
        self.store.save(second)  # answered in another process
        self.lines.clear()
        asking.sync()
        self.assertIn({"question": "answered", "id": f"{second.id}.1", "speaker": ""}, self.lines)
        self.assertIn(f"{first.id}.1", [line["id"] for line in self.lines if line["question"] == "open"])

    def test_a_cli_task_is_not_shown(self):
        task = waiting_task(self.store)
        task.source = "cli"
        asking.on_event(task, "waiting_user")
        self.assertEqual(self.lines, [])


class AskFromDisplayTests(Isolated):
    def test_only_an_open_question_is_put_on_a_speaker_and_not_twice_at_once(self):
        task = waiting_task(self.store, conversation="c-phone")
        asked = []
        with patch.object(satellite_module, "known", side_effect=lambda h: h in ("", SPEAKER)), \
                patch.object(satellite_module, "reachable", return_value=True), \
                patch.object(satellite_module, "ask", side_effect=lambda e: asked.append(e) or True):
            self.assertEqual(asking.ask_from_display("nope.1")[0], 404)
            self.assertEqual(asking.ask_from_display(f"{task.id}.1", SPEAKER)[0], 202)
            self.assertEqual(asking.ask_from_display(f"{task.id}.1", SPEAKER)[0], 429)
        [entry] = asked
        self.assertEqual((entry["speaker"], entry["task_id"]), (SPEAKER, task.id))
        self.assertEqual(entry["conversation"], "", "a phone chat is not continued on a speaker")
        self.assertIn("Which screen", entry["question"])

    def test_an_unknown_speaker_falls_back_to_the_questions_own_then_the_default(self):
        task = waiting_task(self.store)
        asked = []
        with patch.object(satellite_module, "known", side_effect=lambda h: h in ("", SPEAKER)), \
                patch.object(satellite_module, "reachable", return_value=True), \
                patch.object(satellite_module, "ask", side_effect=lambda e: asked.append(e) or True):
            asking.ask_from_display(f"{task.id}.1", "10.9.9.9")
        self.assertEqual((asked[0]["speaker"], asked[0]["conversation"]), (SPEAKER, "c-speaker"))

    def test_a_speaker_not_listening_is_503_and_may_be_tried_again(self):
        task = waiting_task(self.store)
        with patch.object(satellite_module, "known", return_value=True), \
                patch.object(satellite_module, "reachable", side_effect=[False, True]), \
                patch.object(satellite_module, "ask", return_value=True):
            self.assertEqual(asking.ask_from_display(f"{task.id}.1")[0], 503)
            self.assertEqual(asking.ask_from_display(f"{task.id}.1")[0], 202)


class StillUnansweredTests(RuntimeHarness):
    """A task waiting on a question the user has not answered does not put
    the same question to them again (2026-10-26: "which screen?" x4)."""

    def task(self):
        """Asked "which screen?", not answered, and running again (moved to
        another worker, which drops the question it waited on)."""
        task = Task(id="t-still", goal="x", workspace=str(self.ws))
        task.budget["asked"] = [{"q": "איזה מסך לשקף — הטלפון, המחשב או מכשיר אחר?", "answers": 0}]
        task.status, task.question = RUNNING, None
        self.store.save(task)
        return task

    def test_the_same_question_unanswered_waits_on_the_one_already_asked(self):
        runtime = self.runtime([], ScriptedJev())
        events = []
        runtime.listeners.append(lambda t, e: events.append(e))
        task = self.task()
        again = "I need your input to continue: איזה מסך לשקף — המחשב, הטלפון או מכשיר אחר?"
        self.assertFalse(runtime._worker_question(task, again, "SYSTEM_AGENT"))
        saved = self.store.load(task.id)
        self.assertEqual(saved.status, WAITING_USER)
        self.assertEqual(len(saved.budget["asked"]), 1, "not asked anew")
        self.assertEqual(saved.question, "איזה מסך לשקף — הטלפון, המחשב או מכשיר אחר?")
        self.assertEqual(events, [], "nothing said, carded or pushed again")
        self.assertTrue(any(d["value"] == "STILL_UNANSWERED" for d in saved.decisions))

    def test_after_an_answer_or_for_a_new_question_it_is_asked(self):
        runtime = self.runtime([], ScriptedJev())
        events = []
        runtime.listeners.append(lambda t, e: events.append(e))
        task = self.task()
        runtime._worker_question(task, "What is the two-factor code on your phone?", "SYSTEM_AGENT")
        self.assertEqual(events, ["waiting_user"])
        self.assertEqual(len(self.store.load(task.id).budget["asked"]), 2)


class SatelliteTests(unittest.TestCase):
    def make(self, host="speaker.test", **overrides):
        satellite = satellite_module.Satellite(Config(provider="gemini", jev_fast_path=False, satellite_host=host,
                                                      **overrides))
        satellite._event = lambda *a, **k: None
        self.addCleanup(satellite._pool.shutdown, wait=False)
        return satellite

    def test_reachable_only_while_connected_and_streaming(self):
        satellite = self.make()
        satellite._loop = SimpleNamespace(is_closed=lambda: False)
        with patch.dict(satellite_module._running, {"speaker.test": satellite}, clear=True):
            self.assertFalse(satellite_module.reachable("speaker.test"))  # off
            satellite._phase, satellite._device_heard = "wake", satellite_module.time.monotonic()
            self.assertTrue(satellite_module.reachable("speaker.test"))
            self.assertTrue(satellite_module.reachable(""), "the default speaker")
            self.assertFalse(satellite_module.reachable("other.test"))
            satellite._device_heard -= satellite_module.STREAM_QUIET_SECONDS + 1
            self.assertFalse(satellite_module.reachable("speaker.test"), "deaf: the phone is surer")

    def test_a_question_is_said_in_the_speakers_open_conversation_even_when_attached(self):
        # An attached conversation gets reminders through announce_to_calls;
        # a question is sent to this speaker only, so it says it itself.
        satellite = self.make()
        adapter = SimpleNamespace(_hangup=threading.Event(), announce=MagicMock())
        satellite._adapter, satellite._phase = adapter, "talk"
        satellite._loop = SimpleNamespace(is_closed=lambda: False,
                                          call_soon_threadsafe=lambda fn, *args: fn(*args))
        with patch.dict(satellite_module._running, {"speaker.test": satellite}, clear=True), \
                patch.object(agenda, "attached", return_value=True):
            self.assertTrue(satellite_module.ask({"speaker": "speaker.test", "task_id": "t1"}))
            satellite._announce({"title": "reminder"})  # a reminder: announce_to_calls already said it
        adapter.announce.assert_called_once_with({"speaker": "speaker.test", "task_id": "t1"})

    def test_a_question_line_keeps_its_own_speaker(self):
        owner = self.make(satellite_state_command="true")
        other = self.make("speaker.two", satellite_state_command="true")
        other._hook_owner = owner
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        owner._loop = other._loop = loop
        with patch.dict(satellite_module._running, {"speaker.test": owner, "speaker.two": other}, clear=True):
            satellite_module.tell_question({"question": "open", "id": "t1.1", "text": "Which?", "speaker": "speaker.two"})
            loop.run_until_complete(asyncio.sleep(0))
        self.assertEqual(owner._hook_lines.get_nowait(),
                         {"question": "open", "id": "t1.1", "text": "Which?", "speaker": "speaker.two"})

    def test_a_question_asked_on_this_speaker_continues_its_chat(self):
        satellite = self.make()
        with patch.object(conversations, "exists", return_value=True), \
                patch.object(conversations, "device", side_effect=lambda cid: "speaker.test" if cid == "c1" else "x"), \
                patch.object(conversations, "desktop", return_value="fresh") as fresh:
            self.assertEqual(satellite._conversation_for("c1"), "c1")
            self.assertEqual(satellite._conversation_for("c2"), "fresh")
            self.assertEqual(satellite._conversation_for(""), "fresh")
        fresh.assert_called_with("satellite", "speaker.test")


class FakeWriter:
    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        pass


class DisplayRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.token_path = Path(temp.name) / "ask-token"
        self.token = secrets.token_urlsafe(24)  # generated: no literal secret in the repo
        self.satellite = satellite_module.Satellite(Config(provider="gemini", jev_fast_path=False,
                                                           satellite_host="speaker.test",
                                                           satellite_ask_token_path=str(self.token_path)))
        self.addCleanup(self.satellite._pool.shutdown, wait=False)

    async def fetch(self, method="POST", body=None):
        raw = json.dumps(body).encode() if body is not None else b""
        reader = asyncio.StreamReader()
        reader.feed_data(f"{method} {satellite_module.ASK_PATH} HTTP/1.1\r\nHost: x\r\nContent-Type: text/plain\r\n"
                         f"Content-Length: {len(raw)}\r\n\r\n".encode() + raw)
        reader.feed_eof()
        writer = FakeWriter()
        await asyncio.wait_for(self.satellite._serve_reply(reader, writer), 5)
        head, _, payload = bytes(writer.data).partition(b"\r\n\r\n")
        return int(head.split()[1]), head.decode(), json.loads(payload) if payload else None

    async def test_off_without_a_token_file(self):
        status, _, answer = await self.fetch(body={"token": "x", "id": "t1.1"})
        self.assertEqual((status, answer["ok"]), (403, False))

    async def test_a_wrong_token_is_refused_and_a_right_one_asks(self):
        self.token_path.write_text(self.token + "\n")
        with patch.object(asking, "ask_from_display", return_value=(202, "asking on the speaker")) as ask:
            with self.assertLogs(satellite_module.log, "WARNING"):
                status, _, _ = await self.fetch(body={"token": self.token[:-1], "id": "t1.1"})
            self.assertEqual(status, 403)
            ask.assert_not_called()
            status, head, answer = await self.fetch(body={"token": self.token, "id": "t1.1", "speaker": "speaker.test"})
        self.assertEqual((status, answer), (202, {"ok": True, "message": "asking on the speaker"}))
        ask.assert_called_once_with("t1.1", "speaker.test")
        self.assertIn("Access-Control-Allow-Origin: *", head)

    async def test_preflight_and_other_methods(self):
        self.assertEqual((await self.fetch("OPTIONS"))[0], 204)
        self.assertEqual((await self.fetch("GET"))[0], 405)
        self.token_path.write_text(self.token)
        self.assertEqual((await self.fetch(body=["not", "an", "object"]))[0], 400)


class AnnouncerTests(unittest.TestCase):
    def test_a_task_question_is_asked_with_where_the_reply_goes(self):
        from omarchy_ai.voice.gemini_live import GeminiLiveSession
        sent = []
        task = SimpleNamespace(id="t7", status=WAITING_USER, budget={"asked": [{"q": "Which?", "answers": 0}]})

        class FakeLive:
            async def send_client_content(self, turns, turn_complete):
                sent.append(turns.parts[0].text)

        async def run():
            adapter = GeminiLiveSession(Config(provider="gemini"))
            adapter._transcript = conversations.Transcript("c-speaker", [])
            adapter.announce({"id": "e7", "task_id": "t7", "outcome": "waiting_user", "title": "mirror the screen",
                              "question": "Which screen should I mirror?", "question_id": "t7.1"})
            announcer = asyncio.create_task(adapter._announcer(FakeLive()))
            for _ in range(100):
                if sent:
                    break
                await asyncio.sleep(0.02)
            adapter._hangup.set()
            await announcer

        asking.reset_for_tests()
        self.addCleanup(asking.reset_for_tests)
        with patch("omarchy_ai.runtime.task.TaskStore", return_value=SimpleNamespace(load=lambda tid: task)):
            asyncio.run(run())
        [text] = sent
        self.assertIn("Which screen should I mirror?", text)
        self.assertIn("task_respond with task_id t7 and answer=", text)
        self.assertTrue(asking.was_put_to("t7", "c-speaker"))


if __name__ == "__main__":
    unittest.main()
