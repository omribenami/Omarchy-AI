"""Who a request comes from (core/identities.py) decides MyApi access and
where a reminder goes: an attached phone is certain, a voice is a match,
typed at the computer is its owner, a background job acts as who asked."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from omarchy_ai.config import Config
from omarchy_ai.core import agenda, identities, outbox
from omarchy_ai.core.identities import Identity
from omarchy_ai.execution import actions, myapi_agent, provenance
from omarchy_ai.execution.actions import ActionResult
from omarchy_ai.phone import server
from omarchy_ai.voice import speaker_id
from omarchy_ai.voice.gemini_live import GeminiLiveSession


class Household(unittest.TestCase):
    """person-a owns MyApi and has a phone; person-b has a phone; person-c has none."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for patcher in (
            patch.object(identities, "PATH", self.root / "identities.json"),
            patch.object(server, "_CERT_DIR", self.root / "phone_bridge"),
            patch.object(server, "_SESSIONS_PATH", self.root / "phone_bridge" / "paired_sessions.json"),
            patch.object(speaker_id, "PEOPLE_PATH", Path("/nonexistent/people.yaml")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        for key, name in (("person-a", "Person A"), ("person-b", "Person B"), ("person-c", "Person C")):
            identities.add(key, name)
        identities.attach_phone("person-a", "device-a")
        identities.attach_phone("person-b", "device-b")
        identities.set_owner("person-a")

    def as_(self, identity):
        token = identities.set_current(identity)
        self.addCleanup(identities.reset_current, token)


class MyApiGateTests(Household):
    def run_call(self):
        ran = []
        with patch.dict(actions.ACTIONS, {"myapi_call": lambda args: ran.append(args) or ActionResult(True, "ok")}):
            result = actions.run_action("myapi_call", {"service": "x"})
        return result, ran

    def test_only_the_owner_may_use_myapi(self):
        for identity in (Identity(), Identity("person-b", "phone"), Identity("person-c", "voice")):
            with self.subTest(identity=identity):
                token = identities.set_current(identity)
                try:
                    result, ran = self.run_call()
                finally:
                    identities.reset_current(token)
                self.assertFalse(result.ok)
                self.assertIn("MyApi is private to Person A.", result.message)
                self.assertEqual(ran, [])
        self.as_(Identity("person-a", "voice"))
        result, ran = self.run_call()
        self.assertTrue(result.ok)
        self.assertEqual(ran, [{"service": "x"}])

    def test_the_myapi_agent_refuses_too(self):
        self.as_(Identity("person-b", "phone"))
        with patch("omarchy_ai.myapi.is_connected", return_value=True), patch.object(myapi_agent, "_run") as run:
            result = myapi_agent.run({"request": "read my mail"})
        self.assertFalse(result.ok)
        self.assertIn("private to Person A", result.message)
        run.assert_not_called()

    def test_without_an_owner_nothing_changes(self):
        identities.set_owner(None)
        result, ran = self.run_call()  # nobody known
        self.assertTrue(result.ok)
        self.assertEqual(len(ran), 1)

    def test_other_tools_are_not_gated(self):
        with patch.dict(actions.ACTIONS, {"get_time": lambda args: ActionResult(True, "noon")}):
            self.assertTrue(actions.run_action("get_time", {}).ok)


class PhoneIdentityTests(Household):
    def pair(self, **record):
        server._write_json_0600(server._SESSIONS_PATH, [{"id": "session-1", **record}])
        return f"{server._SESSION_COOKIE}=session-1"

    def test_a_session_is_its_person_or_the_person_its_device_is_attached_to(self):
        self.assertEqual(server.identity_for_cookie(self.pair(person="person-b")), Identity("person-b", "phone"))
        self.assertEqual(server.identity_for_cookie(self.pair(device="device-a")), Identity("person-a", "phone"))
        self.assertEqual(server.identity_for_cookie(self.pair(device="device-unknown")), Identity())
        self.assertEqual(server.identity_for_cookie(""), Identity())

    def handler(self, cookie, body):
        h = server._Handler.__new__(server._Handler)
        h.headers = {"Cookie": cookie}
        h._read_json_body = lambda: body
        h.sent = []
        h._send_json = lambda status, payload: h.sent.append(payload)
        return h

    def test_a_phone_tool_call_runs_as_the_phones_person(self):
        seen = []
        h = self.handler(self.pair(device="device-b"), {"name": "myapi_call", "arguments": {}})
        with patch.object(server, "run_action", side_effect=lambda n, a: seen.append(identities.current())
                          or ActionResult(True, "ok")):
            h._handle_tool()
        self.assertEqual(seen, [Identity("person-b", "phone")])
        self.assertEqual(identities.current(), Identity(), "reset after the call")

    def test_the_owners_phone_may_use_myapi_and_anothers_may_not(self):
        with patch.dict(actions.ACTIONS, {"myapi_call": lambda args: ActionResult(True, "sent")}):
            owner = self.handler(self.pair(device="device-a"), {"name": "myapi_call", "arguments": {}})
            owner._handle_tool()
            other = self.handler(self.pair(person="person-b"), {"name": "myapi_call", "arguments": {}})
            other._handle_tool()
        self.assertEqual(owner.sent, [{"ok": True, "message": provenance.fence("sent", "external:myapi")}])
        self.assertFalse(other.sent[0]["ok"])

    def test_the_openai_phone_prompt_names_a_known_person(self):
        config = SimpleNamespace(provider="openai")
        captured = {}

        def fake_build(cfg):
            return {"instructions": "BASE", "delegation": {"responses": {"tools": []}}}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"transport": {"sdp": "answer"}}'

        def fake_open(req, timeout):
            import json
            captured.update(json.loads(req.data))
            return Response()
        with patch.object(server, "build_session_config", fake_build), \
                patch.object(server, "_read_api_key", return_value="k"), \
                patch("urllib.request.urlopen", fake_open):
            server._relay_offer(config, "offer", "", Identity("person-b", "phone"))
        self.assertIn("Person B, on their own phone (certain)", captured["session"]["instructions"])


class VoiceIdentityTests(Household):
    def session(self, **config):
        s = GeminiLiveSession(Config(speaker_id_enabled=True, **config))
        return s

    def test_identity_follows_the_recognized_voice_and_a_known_phone_wins(self):
        s = self.session()
        self.assertEqual(s.identity(), Identity())
        s._voices.speaker = "person-a"
        self.assertEqual(s.identity(), Identity("person-a", "voice"))
        s._voices.speaker = "person-b"
        self.assertEqual(s.identity(), Identity("person-b", "voice"))
        s.known_identity = Identity("person-c", "phone")
        self.assertEqual(s.identity(), Identity("person-c", "phone"))

    async def run_in_tool_thread(self, s):
        """What a tool handler thread sees, set the way _run_call sets it."""
        async def call():
            s._as_speaker()
            return await asyncio.to_thread(identities.current)
        return await asyncio.create_task(call())

    def test_a_tool_thread_sees_the_speaker_at_call_time(self):
        s = self.session()
        s._voices.speaker = "person-a"
        self.assertEqual(asyncio.run(self.run_in_tool_thread(s)), Identity("person-a", "voice"))
        s._voices.speaker = "person-b"  # the speaker changed mid-session
        self.assertEqual(asyncio.run(self.run_in_tool_thread(s)), Identity("person-b", "voice"))

    async def watch(self, s, answers, seconds=1.2):
        session = SimpleNamespace(send_client_content=AsyncMock())
        identifier = SimpleNamespace(ready=lambda: True, identify=lambda samples: answers.pop(0) if answers
                                     else (None, {"person-a": 0.3}))
        with patch.object(speaker_id, "Identifier", return_value=identifier):
            worker = asyncio.create_task(s._speaker_watch(session))
            await asyncio.sleep(seconds)
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        return session.send_client_content

    def stub_voices(self, s, speaker=None):
        s._voices = SimpleNamespace(speaker=speaker, due=lambda: b"\0", heard=0, loud=0, kept=0)

    def test_a_clear_stranger_resets_the_speaker_and_an_unsure_stretch_does_not(self):
        s = self.session()
        self.stub_voices(s, "person-a")
        asyncio.run(self.watch(s, [(None, {"person-a": 0.33, "person-b": 0.1})] * 3))
        self.assertEqual(s.identity(), Identity("person-a", "voice"))
        sent = asyncio.run(self.watch(s, [(None, {"person-a": 0.05, "person-b": 0.02})] * 2))
        self.assertEqual(s.identity(), Identity())
        self.assertIn("not recognized", sent.call_args.kwargs["turns"]["parts"][0]["text"])

    def test_a_phone_session_with_a_person_is_said_at_once_and_never_voice_matched(self):
        s = self.session()
        s.known_identity = Identity("person-b", "phone")
        sent = asyncio.run(self.watch(s, [("person-a", {"person-a": 0.9})], seconds=0.4))
        sent.assert_awaited_once()
        self.assertIn("Person B, on their own phone (certain)", sent.call_args.kwargs["turns"]["parts"][0]["text"])
        self.assertEqual(s.identity(), Identity("person-b", "phone"))


class TypedChatIdentityTests(Household):
    def test_typed_chat_is_the_owner(self):
        from omarchy_ai.voice import text_chat
        seen = {}

        def connect(cfg, live_config):
            seen["identity"] = chat._adapter.identity()
            seen["prompt"] = live_config["system_instruction"]
            raise ConnectionError("stop here")
        chat = text_chat.TextChat(Config(provider="gemini"), push=lambda state: None, connect=connect)
        with patch.object(agenda, "INBOX_PATH", self.root / "inbox.jsonl"), \
                self.assertLogs("omarchy_ai.voice.text_chat", "ERROR"):
            asyncio.run(chat._run(asyncio.Queue()))
        self.assertEqual(seen["identity"], Identity("person-a", "typed"))
        self.assertIn("WHO IS TYPING: The person talking to you is Person A", seen["prompt"])


class ReminderRoutingTests(Household):
    def setUp(self):
        super().setUp()
        for patcher in (
            patch.object(agenda, "JOBS_PATH", self.root / "agenda.json"),
            patch.object(agenda, "INBOX_PATH", self.root / "inbox.jsonl"),
            patch.object(agenda, "_refresh_routines_hud"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.send = patch.object(outbox, "_push").start()
        self.addCleanup(patch.stopall)

    def fire(self, identity):
        token = identities.set_current(identity)
        try:
            job = agenda.create({"title": "Take the bread out", "kind": "remind", "in_minutes": 5})
        finally:
            identities.reset_current(token)
        heard = []
        with patch.object(agenda, "listeners", [heard.append]):
            agenda.run_job(job, now=time.time() + 400)
        return job, heard[0] if heard else None

    def test_a_reminder_goes_to_the_phones_of_who_asked(self):
        job, entry = self.fire(Identity("person-b", "voice"))
        self.assertEqual((job["requester"], job["requester_how"]), ("person-b", "voice"))
        self.assertEqual(self.send.call_args.args[3], ["device-b"])
        self.assertIn("Reminder for Person B", entry["detail"])

    def test_unknown_or_phoneless_requesters_reach_every_phone(self):
        for identity in (Identity(), Identity("person-c", "voice")):
            with self.subTest(identity=identity):
                job, entry = self.fire(identity)
                self.assertIsNone(self.send.call_args.args[3])
        self.assertNotIn("requester", self.fire(Identity())[0])

    def test_a_scheduled_job_runs_as_who_asked(self):
        seen = []
        with patch.dict(agenda.RUNNERS, {"remind": lambda job, now, jev: seen.append(identities.current())}):
            self.fire(Identity("person-a", "phone"))
        self.assertEqual(seen, [Identity("person-a", "phone")])
        self.assertEqual(identities.current(), Identity())


class BackgroundTaskIdentityTests(Household):
    def test_a_task_runs_as_the_person_who_started_it(self):
        from omarchy_ai.runtime.runtime import TaskRuntime
        from omarchy_ai.runtime.task import TaskStore
        rt = TaskRuntime.__new__(TaskRuntime)
        rt.store, rt.executors, rt.default_auto_approve = TaskStore(self.root / "tasks"), {}, "NORMAL"
        rt._refresh_task_hud = lambda: None
        rt._launch = lambda task, background: None
        token = identities.set_current(Identity("person-a", "phone"))
        try:
            task = rt.start("check my mail", str(self.root))
        finally:
            identities.reset_current(token)
        self.assertEqual((task.person, task.person_how), ("person-a", "phone"))
        self.assertEqual(rt.store.load(task.id).person, "person-a")
        seen = []
        rt._loop = lambda t: seen.append(identities.current())
        rt._drive(task.id)  # on another thread it starts from nobody; the task supplies who
        self.assertEqual(seen, [Identity("person-a", "phone")])
        self.assertEqual(identities.current(), Identity())


if __name__ == "__main__":
    unittest.main()
