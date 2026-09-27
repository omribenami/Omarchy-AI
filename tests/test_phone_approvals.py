import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from omarchy_ai.execution import approval_pin
from omarchy_ai.phone import server
from omarchy_ai.runtime import service


class ApprovalPinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "approval_pin.json"
        patcher = patch.object(approval_pin, "PIN_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_stores_only_a_hash_with_owner_only_permissions(self):
        approval_pin.store("4821")
        self.assertNotIn("4821", self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(approval_pin.verify("4821"), (True, "ok"))
        self.assertFalse(approval_pin.verify("1111")[0])

    def test_rejects_short_pin_and_unset_state(self):
        with self.assertRaises(ValueError):
            approval_pin.store("12")
        ok, reason = approval_pin.verify("1234")
        self.assertFalse(ok)
        self.assertIn("no approval PIN", reason)

    def test_locks_out_after_repeated_failures_even_for_the_right_pin(self):
        approval_pin.store("4821")
        for _ in range(approval_pin.MAX_FAILURES):
            self.assertFalse(approval_pin.verify("0000")[0])
        ok, reason = approval_pin.verify("4821")
        self.assertFalse(ok)
        self.assertIn("try again", reason)
        self.assertTrue(approval_pin.status()["locked_until"])

    def test_success_resets_the_failure_count(self):
        approval_pin.store("4821")
        for _ in range(approval_pin.MAX_FAILURES - 1):
            approval_pin.verify("0000")
        approval_pin.verify("4821")
        for _ in range(approval_pin.MAX_FAILURES - 1):
            approval_pin.verify("0000")
        self.assertEqual(approval_pin.verify("4821"), (True, "ok"))


def _task(status="waiting_approval", fingerprint="fp1"):
    return SimpleNamespace(id="t1", status=status, goal="push the README", workspace="",
                           pending_approval={"fingerprint": fingerprint, "kind": "command",
                                             "subject": "git push", "risk": "HIGH", "reasons": ["force"]})


class RespondApprovalTests(unittest.TestCase):
    def setUp(self):
        self.runtime = MagicMock()
        self.runtime.store.load.return_value = _task()
        self.runtime.respond.return_value = {"ok": True, "message": "resumed task t1"}
        patcher = patch.object(service, "get_runtime", return_value=self.runtime)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_wrong_pin_never_reaches_the_runtime(self):
        with patch.object(approval_pin, "verify", return_value=(False, "wrong PIN (4 tries left)")):
            status, body = server.respond_approval({"task_id": "t1", "fingerprint": "fp1", "approve": True, "pin": "0"})
        self.assertEqual(status, 403)
        self.assertIn("wrong PIN", body["message"])
        self.runtime.respond.assert_not_called()

    def test_right_pin_approves_on_the_phone_channel(self):
        with patch.object(approval_pin, "verify", return_value=(True, "ok")) as verify:
            status, body = server.respond_approval({"task_id": "t1", "fingerprint": "fp1", "approve": True, "pin": "4821"})
        verify.assert_called_once_with("4821")
        self.assertEqual(status, 200)
        self.runtime.respond.assert_called_once_with("t1", approve=True, channel="phone")

    def test_deny_needs_no_pin(self):
        with patch.object(approval_pin, "verify") as verify:
            status, _ = server.respond_approval({"task_id": "t1", "fingerprint": "fp1", "approve": False})
        verify.assert_not_called()
        self.assertEqual(status, 200)
        self.runtime.respond.assert_called_once_with("t1", approve=False, channel="phone")

    def test_a_changed_request_is_not_approved(self):
        # The phone showed fp1; the task now waits on a different request.
        self.runtime.store.load.return_value = _task(fingerprint="fp2")
        with patch.object(approval_pin, "verify", return_value=(True, "ok")):
            status, _ = server.respond_approval({"task_id": "t1", "fingerprint": "fp1", "approve": True, "pin": "4821"})
        self.assertEqual(status, 409)
        self.runtime.respond.assert_not_called()

    def test_malformed_body(self):
        status, _ = server.respond_approval({"task_id": "t1", "approve": "yes"})
        self.assertEqual(status, 400)

    def test_lists_only_tasks_waiting_for_approval(self):
        self.runtime.store.list.return_value = [_task(), _task(status="running")]
        with patch.object(approval_pin, "status", return_value={"set": True, "locked_until": 0}):
            data = server.pending_approvals()
        self.assertEqual(len(data["approvals"]), 1)
        self.assertEqual(data["approvals"][0]["subject"], "git push")
        self.assertTrue(data["pin"]["set"])


if __name__ == "__main__":
    unittest.main()
