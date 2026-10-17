"""DoD-6 for pending actions: nothing is stuck or re-run after a restart.

A child process presses Send on a prepared email and is killed (os._exit) at
a point on the send path; a new child runs what the daemon runs at startup
(pending_actions.recover) against the same state and the fake MyApi. The
action must land in the right state, in well under 60 s, and nothing may be
sent again unless the user deliberately resends it.
"""

import unittest

from chaos.harness import ChaosHarness
from fakes.myapi_server import FakeMyApi

T = "chaos.recovery_targets:"
# crashpoint -> (writes the fake received, status after recovery, outcome)
EXPECTED = {
    "pending.after_claim": (0, "done", "failed"),
    "effects.before_dispatch": (0, "done", "failed"),
    "effects.after_dispatch_before_record": (1, "uncertain", "uncertain"),
    "effects.after_record": (1, "done", "done"),
}


class PendingRecoveryChaosTests(unittest.TestCase):
    def setUp(self):
        self.h = ChaosHarness()
        self.addCleanup(self.h.cleanup)
        self.fake = FakeMyApi()
        self.addCleanup(self.fake.close)

    def child(self, target, *args, **kwargs):
        run = self.h.run(T + target, *args, myapi_url=self.fake.url, **kwargs)
        self.assertTrue(run.ok, run.stderr[-2000:])
        return run.result

    def crash(self, target, point, *args):
        run = self.h.run(
            T + target, *args, crashpoints=f"{point}=exit", myapi_url=self.fake.url
        )
        self.assertEqual(run.crashed_at, point, run.stderr[-2000:])

    def killed_mid_send(self, point):
        made = self.child("prepare")
        aid, cid = made["id"], made["conversation"]
        self.crash("press", point, aid, cid)
        return aid, cid

    def test_a_send_killed_anywhere_is_settled_at_startup_and_never_rerun(self):
        for point, (writes, status, outcome) in EXPECTED.items():
            with self.subTest(point=point):
                self.fake.reset()
                aid, cid = self.killed_mid_send(point)
                self.assertEqual(
                    self.h.state("pending_actions.json")[aid]["status"], "running"
                )
                after = self.child("startup", aid, cid)
                self.assertLess(after["seconds"], 60)
                self.assertEqual(after["changed"], [aid])
                action = after["action"]
                self.assertEqual(
                    (action["status"], action["outcome"]), (status, outcome)
                )
                self.assertEqual(len(self.fake.writes), writes, point)
                (told,) = after["notified"]
                self.assertEqual(told["conversation"], cid)
                card = after["cards"][-1]
                self.assertEqual(card["task_id"], f"action:{aid}")
                if status == "uncertain":
                    self.assertEqual(card["kind"], "uncertain")
                    self.assertEqual(card["choices"], ["check", "resend", "drop"])
                    self.assertTrue(told["title"].startswith("May have sent"))
                else:
                    self.assertEqual(card["kind"], "result")
                    self.assertEqual(
                        card["event"], "certified" if outcome == "done" else "failed"
                    )
                if outcome == "done":
                    self.assertTrue(action["result"]["ok"])
                # Another restart and another Send change nothing.
                again = self.child("startup", aid, cid)
                self.assertEqual(again["changed"], [])
                pressed = self.child("press", aid, cid)
                self.assertFalse(pressed["ok"])
                self.assertEqual(pressed["action"]["status"], status)
                self.assertEqual(len(self.fake.writes), writes, point)

    def test_the_kits_claim_then_die_is_no_longer_stuck(self):
        crashed, after = self.h.crash_then_restart(
            "chaos.targets:decide_action",
            "chaos.after_claim",
            restart="chaos.targets:pending_snapshot",
        )
        (aid,) = after.result
        self.assertEqual(after.result[aid]["status"], "running")
        recovered = self.child("startup", aid, "")
        self.assertEqual(
            (recovered["action"]["status"], recovered["action"]["outcome"]),
            ("done", "failed"),
        )
        self.assertEqual(self.fake.writes, [])

    def test_resend_is_a_deliberate_second_send_and_can_itself_be_recovered(self):
        aid, cid = self.killed_mid_send("effects.after_dispatch_before_record")
        self.child("startup", aid, cid)
        self.assertEqual(len(self.fake.writes), 1)
        # The resend dies the same way: recovered again, from its own record.
        self.crash("answer", "effects.after_dispatch_before_record", aid, cid, "resend")
        self.assertEqual(len(self.fake.writes), 2)
        first, second = (w.idempotency_key for w in self.fake.writes)
        self.assertTrue(first and second and first != second)
        after = self.child("startup", aid, cid)
        self.assertEqual(after["action"]["status"], "uncertain")
        self.assertEqual(len(self.fake.writes), 2)
        resent = self.child("answer", aid, cid, "resend")
        self.assertTrue(resent["ok"], resent["message"])
        self.assertEqual(
            (resent["action"]["status"], resent["action"]["outcome"]), ("done", "done")
        )
        self.assertEqual(len(self.fake.writes), 3)
        self.assertEqual(len({w.idempotency_key for w in self.fake.writes}), 3)
        # Settled: no answer sends it again.
        late = self.child("answer", aid, cid, "resend")
        self.assertFalse(late["ok"])
        self.assertEqual(len(self.fake.writes), 3)

    def test_drop_closes_it_without_sending(self):
        aid, cid = self.killed_mid_send("effects.after_dispatch_before_record")
        self.child("startup", aid, cid)
        dropped = self.child("answer", aid, cid, "drop")
        self.assertTrue(dropped["ok"])
        self.assertEqual(
            (dropped["action"]["status"], dropped["action"]["outcome"]),
            ("done", "dropped"),
        )
        self.assertEqual(dropped["cards"][-1]["event"], "cancelled")
        self.assertEqual(len(self.fake.writes), 1)

    def test_expired_waiting_actions_are_cancelled_and_notified(self):
        for kwargs in ({"expires_in": -1}, {"legacy_age": 8 * 86400}):
            with self.subTest(**kwargs):
                made = self.child("prepare", **kwargs)
                aid, cid = made["id"], made["conversation"]
                after = self.child("startup", aid, cid)
                self.assertEqual(
                    (after["action"]["status"], after["action"]["outcome"]),
                    ("done", "cancelled"),
                )
                (told,) = after["notified"]
                self.assertTrue(told["title"].startswith("Expired"), told)
                self.assertIn("expired", after["cards"][-1]["text"])
                pressed = self.child("press", aid, cid)
                self.assertFalse(pressed["ok"])
        self.assertEqual(self.fake.writes, [])

    def test_a_fresh_waiting_action_is_left_alone(self):
        made = self.child("prepare")
        after = self.child("startup", made["id"], made["conversation"])
        self.assertEqual(after["changed"], [])
        self.assertEqual(after["action"]["status"], "waiting")
        self.assertGreater(after["action"]["expires_at"], after["action"]["created"])
        self.assertEqual(after["notified"], [])


if __name__ == "__main__":
    unittest.main()
