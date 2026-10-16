"""Self-tests of the fault-injection kit: crashpoints, the restart harness, the fake MyApi."""
import json
import tempfile
import unittest
import urllib.request
import uuid
from pathlib import Path
from unittest import mock

from chaos.harness import ChaosHarness
from fakes.myapi_server import FakeMyApi, fake_identity
from omarchy_ai.core import crashpoints
from omarchy_ai.myapi import MyApiClient, MyApiError


class CrashpointTest(unittest.TestCase):
    def tearDown(self):
        crashpoints.arm("")

    def test_unarmed_point_does_nothing(self):
        crashpoints.crashpoint("chaos.anything")
        crashpoints.arm("chaos.other=raise")
        crashpoints.crashpoint("chaos.anything")

    def test_raise_mode_is_not_swallowed_by_except_exception(self):
        crashpoints.arm({"chaos.here": "raise"})
        with self.assertRaises(crashpoints.CrashpointHit) as caught:
            try:
                crashpoints.crashpoint("chaos.here")
            except Exception:  # noqa: BLE001 -- what tool wrappers do
                self.fail("CrashpointHit was caught as an Exception")
        self.assertEqual(caught.exception.name, "chaos.here")

    def test_spec_parsing(self):
        self.assertEqual(crashpoints.parse(" a=raise, b ,c=exit,"), {"a": "raise", "b": "exit", "c": "exit"})
        for bad in ("a=explode", "=raise"):
            with self.assertRaises(ValueError):
                crashpoints.parse(bad)


class HarnessTest(unittest.TestCase):
    def setUp(self):
        self.h = ChaosHarness()
        self.addCleanup(self.h.cleanup)

    def test_child_dirs_are_inside_the_temp_root(self):
        run = self.h.run("chaos.targets:where")
        self.assertTrue(run.ok, run.stderr)
        for name, path in run.result.items():
            self.assertTrue(Path(path).is_relative_to(self.h.root), (name, path))

    def test_child_refuses_to_run_with_state_outside_the_root(self):
        elsewhere = Path(tempfile.mkdtemp(prefix="omarchy-chaos-elsewhere-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(elsewhere, ignore_errors=True))
        env = self.h.env
        with mock.patch.object(self.h, "env", lambda *a: {**env(*a), "XDG_STATE_HOME": str(elsewhere)}):
            with self.assertRaises(RuntimeError):
                self.h.run("chaos.targets:decide_action")
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_exit_between_claim_and_finish_leaves_the_action_running(self):
        crashed, after = self.h.crash_then_restart("chaos.targets:decide_action", "chaos.after_claim",
                                                   restart="chaos.targets:pending_snapshot")
        self.assertEqual(crashed.crashed_at, "chaos.after_claim")
        self.assertIsNone(crashed.result)
        self.assertTrue(after.ok, after.stderr)
        (action,) = after.result.values()
        self.assertEqual(action["status"], "running")
        self.assertEqual(self.h.state("pending_actions.json"), after.result)

    def test_raise_mode_runs_finally_blocks_before_dying(self):
        crashed, after = self.h.crash_then_restart("chaos.targets:decide_action", "chaos.before_finish",
                                                   mode="raise", restart="chaos.targets:pending_snapshot")
        self.assertEqual(crashed.crashed_at, "chaos.before_finish")
        (action,) = after.result.values()
        self.assertEqual(action["status"], "done")

    def test_unarmed_run_completes(self):
        run = self.h.run("chaos.targets:decide_action")
        self.assertTrue(run.ok, run.stderr)
        self.assertIsNone(run.crashed_at)
        self.assertEqual(self.h.state("pending_actions.json")[run.result]["status"], "done")

    def test_crash_that_never_fires_fails_loudly(self):
        with self.assertRaises(AssertionError):
            self.h.crash_then_restart("chaos.targets:decide_action", "chaos.not_on_this_path")

    def test_restart_sees_agenda_job_saved_before_the_crash(self):
        crashed, after = self.h.crash_then_restart("chaos.targets:add_reminder", "chaos.after_agenda_create",
                                                   args=("stretch",), restart="chaos.targets:agenda_snapshot")
        self.assertEqual(crashed.crashed_at, "chaos.after_agenda_create")
        self.assertEqual([j["title"] for j in after.result], ["stretch"])

    def test_child_reaches_the_fake_myapi_and_its_write_survives_the_crash(self):
        with FakeMyApi() as fake:
            crashed = self.h.run("chaos.targets:myapi_send", "hello", crashpoints="chaos.after_dispatch",
                                 myapi_url=fake.url)
            self.assertEqual(crashed.crashed_at, "chaos.after_dispatch", crashed.stderr)
            (write,) = fake.writes
            self.assertEqual(write.service, "gmail")
            self.assertEqual(write.body["body"], {"subject": "hello"})
        self.assertTrue((self.h.config_dir / "myapi" / "identity.json").exists())


class FakeMyApiTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMyApi()
        self.addCleanup(self.fake.close)
        patcher = self.fake.patch()
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = MyApiClient()
        self.client._identity = fake_identity()

    def test_listens_on_loopback_only(self):
        self.assertTrue(self.fake.url.startswith("http://127.0.0.1:"))

    def test_records_writes_with_their_idempotency_key(self):
        key = uuid.uuid4().hex
        request = urllib.request.Request(
            self.fake.url + "/services/gmail/execute", method="POST",
            data=json.dumps({"method": "GMAIL_SEND_EMAIL", "params": {"arguments": {}}}).encode(),
            headers={"Content-Type": "application/json", "Idempotency-Key": key})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
        (write,) = self.fake.writes
        self.assertEqual((write.idempotency_key, write.service, write.path), (key, "gmail", "/services/gmail/execute"))

    def test_reads_are_recorded_but_are_not_writes(self):
        self.client.call_service("gmail", "/gmail/v1/users/me/messages", "GET", query={"q": "x"})
        self.client.call_service("gmail", "/gmail/v1/users/me/messages/send", "POST", body={"raw": "y"})
        self.assertEqual(len(self.fake.requests), 2)
        (write,) = self.fake.writes
        self.assertEqual(write.body["method"], "POST")
        self.assertIsNone(write.idempotency_key)

    def test_fail(self):
        self.fake.fail(503, match="/proxy")
        with self.assertRaises(MyApiError):
            self.client.call_service("gmail", "/x", "POST", body={})
        self.fake.fail(401)
        with self.assertRaises(MyApiError) as caught:
            self.client.list_services()
        self.assertTrue(caught.exception.reconnect_needed)

    def test_hang_until_released(self):
        self.fake.hang(match="/execute")
        with mock.patch("omarchy_ai.myapi.client._TIMEOUT", 0.5), self.assertRaises(MyApiError):
            self.client.request("POST", "/services/gmail/execute", body={"method": "GMAIL_SEND_EMAIL"})
        self.assertEqual(len(self.fake.writes), 1)  # received, though never answered
        self.fake.release()
        self.assertEqual(self.client.request("POST", "/services/gmail/execute", body={}), {"ok": True, "data": {}})

    def test_succeed_with_a_canned_answer(self):
        self.fake.succeed({"data": [{"id": "notion", "status": "connected"}]}, match="/services")
        self.assertEqual(self.client.request("GET", "/services")["data"][0]["id"], "notion")


if __name__ == "__main__":
    unittest.main()
