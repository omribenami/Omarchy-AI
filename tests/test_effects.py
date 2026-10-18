"""The side-effect journal (core/effects.py) and the places that use it."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import no_live_desktop
from fakes.myapi_server import FakeMyApi, fake_identity
from omarchy_ai.core import approvals, effects
from omarchy_ai.execution import actions
from omarchy_ai.execution.actions import ActionResult
from omarchy_ai.execution.verified_input import InputGuard
from omarchy_ai.myapi import MyApiClient
from omarchy_ai.runtime.executors.base import DONE, Report
from test_task_runtime import RuntimeHarness, ScriptedJev, ScriptExecutor, git_repo

setUpModule, tearDownModule = no_live_desktop.without_session_probe()


def use_journal_in(test, root):
    for name, file in (
        ("journal_path", effects.JOURNAL_NAME),
        ("index_path", effects.INDEX_NAME),
    ):
        patcher = mock.patch.object(effects, name, lambda file=file: root / file)
        patcher.start()
        test.addCleanup(patcher.stop)


class JournalCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        use_journal_in(self, self.root)

    def states(self, key=None):
        return [
            r["state"] for r in effects.records() if key is None or r["idem_key"] == key
        ]


class PerformTests(JournalCase):
    def test_a_confirmed_key_returns_the_stored_result_without_dispatching(self):
        calls = []

        def dispatch(key):
            calls.append(key)
            return ActionResult(True, "sent")

        first = effects.perform("t", "send", {"to": "a"}, dispatch, origin_id="turn:1")
        again = effects.perform("t", "send", {"to": "a"}, dispatch, origin_id="turn:1")
        self.assertEqual(
            (first, again), (ActionResult(True, "sent"), ActionResult(True, "sent"))
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], effects.idem_key("turn:1", "send", {"to": "a"}))
        self.assertEqual(self.states(), ["proposed", "dispatched", "confirmed"])

    def test_another_origin_or_other_arguments_dispatch_again(self):
        calls = []
        dispatch = lambda key: calls.append(key) or ActionResult(True, "ok")
        effects.perform("t", "send", {"to": "a"}, dispatch, origin_id="turn:1")
        effects.perform("t", "send", {"to": "a"}, dispatch, origin_id="turn:2")
        effects.perform("t", "send", {"to": "b"}, dispatch, origin_id="turn:2")
        self.assertEqual(len(set(calls)), 3)

    def test_without_an_origin_nothing_is_deduplicated(self):
        calls = []
        dispatch = lambda key: calls.append(key) or ActionResult(True, "ok")
        effects.perform("t", "send", {"to": "a"}, dispatch)
        effects.perform("t", "send", {"to": "a"}, dispatch)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(effects.records()), 6)

    def test_the_origin_context_is_used(self):
        calls = []
        dispatch = lambda key: calls.append(key) or ActionResult(True, "ok")
        with effects.origin("pending:x"):
            effects.perform("t", "send", {}, dispatch)
            effects.perform("t", "send", {}, dispatch)
        self.assertEqual(calls, [effects.idem_key("pending:x", "send", {})])

    def test_a_failed_key_runs_again(self):
        results = iter([ActionResult(False, "HTTP 500"), ActionResult(True, "sent")])
        calls = []
        dispatch = lambda key: calls.append(key) or next(results)
        self.assertFalse(effects.perform("t", "send", {}, dispatch, origin_id="o").ok)
        self.assertTrue(effects.perform("t", "send", {}, dispatch, origin_id="o").ok)
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            self.states(),
            ["proposed", "dispatched", "failed", "proposed", "dispatched", "confirmed"],
        )

    def test_an_uncertain_key_is_never_dispatched_again(self):
        calls = []

        def dispatch(key):
            calls.append(key)
            raise effects.Uncertain(ActionResult(False, "did not answer within 30s"))

        first = effects.perform("t", "send", {}, dispatch, origin_id="o")
        self.assertEqual(first, ActionResult(False, "did not answer within 30s"))
        again = effects.perform("t", "send", {}, dispatch, origin_id="o")
        self.assertEqual(len(calls), 1)
        self.assertFalse(again.ok)
        self.assertTrue(effects.not_dispatched(again))
        payload = json.loads(again.message)
        self.assertEqual(payload["status"], "uncertain")
        self.assertIn("MAY ALREADY HAVE HAPPENED", payload["message"])
        self.assertEqual(effects.lookup(calls[0]).state, "uncertain")

    def test_an_exception_while_dispatching_leaves_it_uncertain(self):
        def dispatch(key):
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            effects.perform("t", "send", {}, dispatch, origin_id="o")
        self.assertTrue(
            effects.not_dispatched(
                effects.perform("t", "send", {}, dispatch, origin_id="o")
            )
        )

    def test_dispatched_by_a_dead_process_becomes_uncertain(self):
        key = effects.idem_key("o", "send", {})
        with effects._index() as doc:
            effects._entries(doc.data)[key] = {
                "effect_id": "e1",
                "kind": "t",
                "tool": "send",
                "target": "send",
                "args_hash": "",
                "state": "dispatched",
                "ts": 1.0,
                "updated": 9e12,
                "owner": {"pid": 999999999, "start": 1},
                "attempt": 1,
                "result": None,
            }
        calls = []
        result = effects.perform(
            "t", "send", {}, lambda k: calls.append(k), origin_id="o"
        )
        self.assertEqual(calls, [])
        self.assertTrue(effects.not_dispatched(result))
        self.assertEqual(effects.lookup(key).state, "uncertain")

    def test_proposed_by_a_dead_process_never_went_out_and_runs(self):
        key = effects.idem_key("o", "send", {})
        with effects._index() as doc:
            effects._entries(doc.data)[key] = {
                "effect_id": "e1",
                "kind": "t",
                "tool": "send",
                "target": "send",
                "args_hash": "",
                "state": "proposed",
                "ts": 1.0,
                "updated": 9e12,
                "owner": {"pid": 999999999, "start": 1},
                "attempt": 1,
                "result": None,
            }
        result = effects.perform(
            "t", "send", {}, lambda k: ActionResult(True, "sent"), origin_id="o"
        )
        self.assertEqual(result, ActionResult(True, "sent"))
        self.assertEqual(effects.lookup(key).state, "confirmed")

    def test_concurrent_retries_of_one_key_dispatch_once(self):
        calls, gate = [], threading.Event()

        def dispatch(key):
            calls.append(key)
            gate.wait(5)
            return ActionResult(True, "sent once")

        results = []
        threads = [
            threading.Thread(
                target=lambda: results.append(
                    effects.perform("t", "send", {"n": 1}, dispatch, origin_id="o")
                )
            )
            for _ in range(16)
        ]
        for thread in threads:
            thread.start()
        threading.Timer(0.3, gate.set).start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(calls), 1)
        self.assertEqual(results, [ActionResult(True, "sent once")] * 16)

    def test_a_caller_waits_only_so_long_for_another_run(self):
        gate = threading.Event()
        first = threading.Thread(
            target=lambda: effects.perform(
                "t",
                "send",
                {},
                lambda k: (gate.wait(5), ActionResult(True, "x"))[1],
                origin_id="o",
            )
        )
        first.start()
        self.addCleanup(first.join)
        self.addCleanup(gate.set)
        while effects.lookup(effects.idem_key("o", "send", {})) is None:
            pass
        result = effects.perform(
            "t",
            "send",
            {},
            lambda k: self.fail("dispatched twice"),
            origin_id="o",
            wait=0.2,
        )
        self.assertEqual(json.loads(result.message)["status"], "in_progress")

    def test_records_hold_a_hash_of_the_arguments_not_the_arguments(self):
        effects.perform(
            "t",
            "send",
            {"body": "a private message"},
            lambda k: ActionResult(True, "ok"),
            origin_id="o",
            correlation_id="conv-1",
        )
        text = (self.root / effects.JOURNAL_NAME).read_text()
        self.assertNotIn("a private message", text)
        record = effects.records()[0]
        self.assertEqual(
            record["args_hash"], effects.args_hash({"body": "a private message"})
        )
        self.assertEqual(record["correlation_id"], "conv-1")
        for field in ("effect_id", "idem_key", "kind", "target", "state", "ts"):
            self.assertIn(field, record)

    def test_old_records_are_dropped_after_thirty_days(self):
        effects.perform(
            "t", "send", {}, lambda k: ActionResult(True, "ok"), origin_id="old"
        )
        effects.prune()
        self.assertEqual(len(effects.records()), 3)  # younger than 30 days: kept
        effects.prune(time.time() + effects.RETENTION_SECONDS + 60)
        self.assertEqual(effects.records(), [])
        self.assertIsNone(effects.lookup(effects.idem_key("old", "send", {})))


class MyApiWriteTests(JournalCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeMyApi()
        self.addCleanup(self.fake.close)
        patcher = self.fake.patch()
        patcher.start()
        self.addCleanup(patcher.stop)
        identity = mock.patch(
            "omarchy_ai.myapi.client._load_identity", side_effect=fake_identity
        )
        identity.start()
        self.addCleanup(identity.stop)
        connected = mock.patch.object(actions.myapi, "is_connected", return_value=True)
        connected.start()
        self.addCleanup(connected.stop)
        usage = mock.patch.object(actions.myapi_usage, "record")
        usage.start()
        self.addCleanup(usage.stop)

    def send(self, **extra):
        args = {
            "recipient_email": "a@example.com",
            "subject": "Hi",
            "body": "Hello",
            **extra,
        }
        # The user's tap on its confirm card (core/approvals.py).
        with approvals.approved_call("myapi_gmail_send", args, "tap"):
            return actions.run_action("myapi_gmail_send", args)

    def test_the_key_travels_as_the_idempotency_key_header(self):
        with effects.origin("turn:s:1"):
            self.assertTrue(self.send().ok)
        (write,) = self.fake.writes
        arguments = write.body["params"]["arguments"]
        self.assertEqual(
            write.idempotency_key,
            effects.idem_key(
                "turn:s:1",
                "GMAIL_SEND_EMAIL",
                {"service": "gmail", "arguments": arguments},
            ),
        )

    def test_a_retry_in_the_same_turn_is_not_sent_twice(self):
        with effects.origin("turn:s:1"):
            first, second = self.send(), self.send()
        self.assertEqual(first, second)
        self.assertEqual(len(self.fake.writes), 1)
        with effects.origin("turn:s:2"):
            self.send()
        self.assertEqual(len(self.fake.writes), 2)

    def test_a_timeout_is_uncertain_and_the_retry_says_so_instead_of_sending(self):
        self.fake.hang(match="/execute")
        with (
            effects.origin("turn:s:1"),
            mock.patch("omarchy_ai.myapi.client._TIMEOUT", 0.3),
        ):
            first = self.send()
            self.assertFalse(first.ok)
            self.assertIn("MyApi action failed", first.message)
            self.fake.release()
            again = self.send()
        self.assertEqual(len(self.fake.writes), 1)
        self.assertTrue(effects.not_dispatched(again))

    def test_an_answered_error_is_failed_and_may_be_retried(self):
        self.fake.fail(500, match="/execute")
        with effects.origin("turn:s:1"):
            self.assertFalse(self.send().ok)
            self.fake.reset()
            self.assertTrue(self.send().ok)
        self.assertEqual(
            len(self.fake.writes), 1
        )  # reset() cleared the first one's record
        self.assertEqual(
            [r["state"] for r in effects.records() if r["state"] in effects.FINAL],
            ["failed", "confirmed"],
        )

    def test_refused_connections_were_never_sent(self):
        from omarchy_ai.myapi.client import _never_sent
        import socket

        self.assertTrue(_never_sent(ConnectionRefusedError()))
        self.assertTrue(_never_sent(socket.gaierror()))
        self.assertFalse(_never_sent(TimeoutError()))

    def test_myapi_write_journals_the_proxy_call(self):
        args = {
            "service": "gmail",
            "path": "/x",
            "method": "POST",
            "body": {"a": 1},
            "description": "first wording",
        }
        with (
            effects.origin("turn:s:1"),
            approvals.approved_call("myapi_write", args, "tap"),
        ):
            for _ in range(2):
                actions.run_action("myapi_write", args)
        (write,) = self.fake.writes
        self.assertTrue(write.idempotency_key)

    def test_reads_carry_no_key_and_are_not_journaled(self):
        MyApiClient().call_service("gmail", "/gmail/v1/users/me/messages", "GET")
        self.assertIsNone(self.fake.requests[-1].headers.get("Idempotency-Key"))
        self.assertEqual(effects.records(), [])


class FileTests(JournalCase):
    def setUp(self):
        super().setUp()
        cfg = mock.patch.object(
            actions.files,
            "load_config",
            return_value=__import__("types").SimpleNamespace(
                file_access_roots=[str(self.root)]
            ),
        )
        cfg.start()
        self.addCleanup(cfg.stop)

    def test_writing_back_and_forth_in_one_turn_writes_every_time(self):
        path = self.root / "notes.txt"
        with effects.origin("turn:s:1"):
            for content in ("A", "B", "A"):
                result = actions.write_file(
                    {"path": str(path), "content": content, "overwrite": True}
                )
                self.assertTrue(result.ok, result.message)
        self.assertEqual(path.read_text(), "A")
        self.assertEqual(sum(r["state"] == "confirmed" for r in effects.records()), 3)


class TurnOriginTests(unittest.TestCase):
    def test_calls_between_two_user_turns_share_an_origin(self):
        guard, seen = InputGuard(), []
        execute = lambda name, args: (
            seen.append(effects.ORIGIN.get()) or ActionResult(True, "")
        )
        guard.run(execute, "write_file", {})
        guard.run(execute, "write_file", {})
        guard.heard_user()
        guard.run(execute, "write_file", {})
        self.assertEqual(seen[0], seen[1])
        self.assertNotEqual(seen[1], seen[2])
        self.assertNotEqual(InputGuard().turn_origin, guard.turn_origin)
        self.assertEqual(effects.ORIGIN.get(), "")


class TaskCommandTests(RuntimeHarness):
    def setUp(self):
        super().setUp()
        use_journal_in(self, self.root / "state")

    def test_elevated_commands_are_journaled_and_a_deliberate_repeat_runs(self):
        git_repo(self.ws)
        exits = []

        def work(a, ctx):
            for _ in range(2):
                exits.append(
                    ctx.run_command("git clean -n", a.workspace, 10)["exit_code"]
                )
            ctx.run_command("true", a.workspace, 10)  # NORMAL: not journaled
            return Report(DONE, claim="listed")

        runtime = self.runtime(
            [ScriptExecutor("SYSTEM_AGENT", work)],
            ScriptedJev(),
            auto_approve="ELEVATED",
        )
        task = runtime.start("clean up", str(self.ws), background=False)
        self.assertEqual(exits, [0, 0])
        done = [r for r in effects.records() if r["state"] == "confirmed"]
        self.assertEqual(
            [(r["kind"], r["target"]) for r in done], [("command", "git clean -n")] * 2
        )
        self.assertEqual({r["correlation_id"] for r in done}, {task.id})
        self.assertEqual(len({r["idem_key"] for r in done}), 2)


if __name__ == "__main__":
    unittest.main()
