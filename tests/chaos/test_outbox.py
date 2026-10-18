"""DoD-7: a restart during delivery never delivers a result twice.

A child reports a job's result and is killed (os._exit) right after the
outbox claimed it, before any channel showed it; a new child reports the
same result again (a retried run). Nothing is shown twice, and the result
is still in the inbox for the next conversation, so it is not lost.
"""

import unittest

from chaos.harness import ChaosHarness

T = "chaos.outbox_targets:"


class OutboxRestartTests(unittest.TestCase):
    def setUp(self):
        self.h = ChaosHarness()
        self.addCleanup(self.h.cleanup)

    def test_killed_mid_delivery_then_retried_shows_nothing_twice(self):
        crashed, after = self.h.crash_then_restart(T + "report", "outbox.after_claim")
        self.assertEqual(crashed.crashed_at, "outbox.after_claim")
        self.assertTrue(after.ok, after.stderr[-2000:])
        self.assertEqual(
            after.result["delivered"], [], "claimed before the crash: never shown again"
        )
        self.assertIn(
            "succeeded", after.result["inbox"], "kept for the next conversation"
        )

    def test_a_delivered_result_is_not_repeated_after_a_restart(self):
        first = self.h.run(T + "report")
        self.assertTrue(first.ok, first.stderr[-2000:])
        again = self.h.run(T + "report")
        self.assertEqual(again.result["delivered"], first.result["delivered"])
        self.assertEqual(
            [d["channel"] for d in again.result["delivered"]], ["desktop", "push"]
        )


if __name__ == "__main__":
    unittest.main()
