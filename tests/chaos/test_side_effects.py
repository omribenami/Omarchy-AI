"""DoD-1: a crash anywhere on the send path never makes a second send.

Each test kills a child process (os._exit, no cleanup) at one of the
journal's crashpoints while it sends through the fake MyApi, restarts it
against the same state, and counts what the fake server received.
"""

import json
import os
import random
import threading
import unittest

from chaos.harness import ChaosHarness
from fakes.myapi_server import FakeMyApi

CRASHPOINTS = (
    "effects.before_dispatch",
    "effects.after_dispatch_before_record",
    "effects.after_record",
)
T = "chaos.side_effect_targets:"


class SideEffectCrashTests(unittest.TestCase):
    def setUp(self):
        self.h = ChaosHarness()
        self.addCleanup(self.h.cleanup)
        self.fake = FakeMyApi()
        self.addCleanup(self.fake.close)

    def run_child(self, target, *args, **kwargs):
        run = self.h.run(T + target, *args, myapi_url=self.fake.url, **kwargs)
        self.assertTrue(run.ok, run.stderr[-2000:])
        return run.result

    def crash(self, target, point, *args):
        run = self.h.run(
            T + target, *args, crashpoints=f"{point}=exit", myapi_url=self.fake.url
        )
        self.assertEqual(run.crashed_at, point, run.stderr[-2000:])

    def test_a_retried_send_after_a_crash_is_received_exactly_once(self):
        for point in CRASHPOINTS:
            with self.subTest(point=point):
                self.fake.reset()
                origin = f"turn:chaos:{point}"
                self.crash("send", point, origin)
                sent_before_restart = len(self.fake.writes)
                self.assertEqual(
                    sent_before_restart, 0 if point == "effects.before_dispatch" else 1
                )
                after = self.run_child("send", origin)
                self.assertEqual(len(self.fake.writes), 1, point)
                if point == "effects.after_dispatch_before_record":
                    self.assertFalse(after["ok"])
                    self.assertEqual(
                        json.loads(after["message"])["status"], "uncertain"
                    )
                else:
                    self.assertTrue(after["ok"], after["message"])
                # Retrying once more changes nothing either.
                self.run_child("send", origin)
                self.assertEqual(len(self.fake.writes), 1, point)
                (key,) = {w.idempotency_key for w in self.fake.writes}
                self.assertTrue(key)

    def test_a_routine_that_dies_mid_send_is_never_sent_again(self):
        for point in CRASHPOINTS:
            with self.subTest(point=point):
                self.fake.reset()
                self.run_child("schedule_send", point)
                self.crash("heartbeat", point)
                received = len(self.fake.writes)
                self.assertEqual(
                    received, 0 if point == "effects.before_dispatch" else 1
                )
                for minutes in (
                    2,
                    60,
                    24 * 60,
                ):  # later heartbeats, past the stale-run report
                    after = self.run_child("heartbeat", minutes)
                    self.assertEqual(after["started"], [])
                self.assertEqual(len(self.fake.writes), received, point)

    def test_an_approved_action_that_dies_mid_send_is_never_sent_again(self):
        for point in CRASHPOINTS:
            with self.subTest(point=point):
                self.fake.reset()
                aid = self.run_child("prepare_send")
                self.crash("press_send", point, aid)
                received = len(self.fake.writes)
                pressed_again = self.run_child("press_send", aid)
                self.assertFalse(pressed_again["ok"])  # claimed: never decided twice
                resent = self.run_child("resend_pending", aid)
                expected = 1 if received == 0 else received
                self.assertEqual(len(self.fake.writes), expected, point)
                if point == "effects.after_dispatch_before_record":
                    self.assertEqual(
                        json.loads(resent["message"])["status"], "uncertain"
                    )

    def test_randomized_crashes_and_retries_never_send_twice(self):
        # OMARCHY_CHAOS_RUNS=1000 is the full DoD-1 run; CI does a few.
        runs = int(os.environ.get("OMARCHY_CHAOS_RUNS") or 9)
        rng = random.Random(runs)
        for n in range(runs):
            self.fake.reset()
            point = rng.choice(CRASHPOINTS)
            origin = f"turn:random:{n}"
            self.crash("send", point, origin)
            for _ in range(rng.randint(1, 3)):
                self.run_child("send", origin)
            self.assertEqual(len(self.fake.writes), 1, (n, point))

    def test_concurrent_processes_retrying_one_key_make_one_write(self):
        self.fake.hang(match="/execute")
        runs = []
        threads = [
            threading.Thread(
                target=lambda: runs.append(
                    self.h.run(T + "send", "turn:chaos:race", myapi_url=self.fake.url)
                )
            )
            for _ in range(6)
        ]
        for thread in threads:
            thread.start()
        threading.Timer(3.0, self.fake.release).start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(len(runs), 6)
        for run in runs:
            self.assertTrue(run.ok, run.stderr[-2000:])
            self.assertTrue(run.result["ok"], run.result["message"])
        self.assertEqual(len(self.fake.writes), 1)
        self.assertEqual(len({run.result["message"] for run in runs}), 1)


if __name__ == "__main__":
    unittest.main()
