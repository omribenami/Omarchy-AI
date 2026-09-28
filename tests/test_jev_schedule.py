import unittest
from datetime import datetime
from unittest.mock import patch

from omarchy_ai.core import jev, schedule


def gateway(answers, confidence=None):
    """The real Gateway response shape (captured 2026-09-22)."""
    response = {"answers": answers, "usage": {"inputTokens": 1, "outputTokens": 1}}
    if confidence is not None:
        response["providerMetadata"] = {"typesafe": {"confidence": confidence}}
    return response


class JevParseTests(unittest.TestCase):
    def setUp(self):
        self.qs = {"b": jev.boolean("Is it recurring?"),
                   "c": jev.choice("Which?", {"none": "x", "daily": "y", "weekdays": "z"}),
                   "s": jev.score("Urgency?", ["low", "mid", "high"])}

    def test_all_three_gateway_answer_types_and_separate_confidence(self):
        parsed = jev.parse(gateway({
            "b": {"type": "boolean", "probability": 0.99},
            "c": {"type": "choice", "choice": "weekdays", "probabilities": {"none": 0, "daily": 0, "weekdays": 1}},
            "s": {"type": "score", "score": 0.41, "probabilities": {"0": 0.59, "1": 0.4, "2": 0.01}},
        }, confidence={"c": 1, "s": 0.7}), self.qs)
        self.assertEqual(parsed["b"]["p"], 0.99)
        self.assertEqual(parsed["c"]["choice"], "weekdays")
        self.assertEqual(parsed["c"]["confidence"], 1.0)
        self.assertEqual(parsed["s"]["score"], 0.41)
        self.assertEqual(parsed["s"]["confidence"], 0.7)

    def test_missing_confidence_stays_absent(self):
        parsed = jev.parse(gateway({
            "b": {"type": "boolean", "probability": 0.2},
            "c": {"type": "choice", "choice": "none", "probabilities": {"none": 1, "daily": 0, "weekdays": 0}},
            "s": {"type": "score", "score": 0, "probabilities": {"0": 1, "1": 0, "2": 0}},
        }), self.qs)
        self.assertIsNone(parsed["c"]["confidence"])

    def test_rejects_unknown_option_mistyped_answer_and_non_max_choice(self):
        good_b = {"type": "boolean", "probability": 0.5}
        good_s = {"type": "score", "score": 0, "probabilities": {"0": 1, "1": 0, "2": 0}}
        for bad_c in ({"type": "choice", "choice": "weekly", "probabilities": {"none": 1, "daily": 0, "weekdays": 0}},
                      {"type": "choice", "choice": "none", "probabilities": {"none": 0.2, "daily": 0.8, "weekdays": 0}},
                      {"type": "boolean", "probability": 1}):
            with self.assertRaises(jev.JevError):
                jev.parse(gateway({"b": good_b, "c": bad_c, "s": good_s}), self.qs)
        with self.assertRaises(jev.JevError):
            jev.parse(gateway({"b": {"type": "boolean", "probability": 1.5}}), {"b": self.qs["b"]})

    def test_transport_failure_becomes_jev_error(self):
        def broken(state, questions, timeout):
            raise OSError("offline")
        with self.assertRaises(jev.JevError):
            jev.Jev(broken).ask({"x": 1}, {"b": self.qs["b"]})

    def test_transient_gateway_errors_are_retried_with_backoff(self):
        attempts = []
        def flaky(state, questions, timeout):
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("Gateway request failed (HTTP 503): Service temporarily unavailable")
            return gateway({"b": {"type": "boolean", "probability": 0.9}})
        with patch.object(jev.time, "sleep") as sleep:
            self.assertEqual(jev.Jev(flaky).ask({}, {"b": self.qs["b"]})["b"]["p"], 0.9)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_permanent_gateway_errors_are_not_retried(self):
        attempts = []
        def bad(state, questions, timeout):
            attempts.append(1)
            raise RuntimeError("Gateway request failed (HTTP 400): Invalid discriminator value")
        with self.assertRaises(jev.JevError):
            jev.Jev(bad).ask({}, {"b": self.qs["b"]})
        self.assertEqual(len(attempts), 1)


class BreakerTests(unittest.TestCase):
    """2026-09-28: Gateway 503s/timeouts added ~10s to every voice turn."""
    def setUp(self):
        jev.BREAKER = jev._Breaker()
        self.qs = {"b": jev.boolean("?")}
        self.attempts = []

    def down(self, state, questions, timeout):
        self.attempts.append(1)
        raise RuntimeError("Gateway request timed out")

    def ok(self, state, questions, timeout):
        self.attempts.append(1)
        return gateway({"b": {"type": "boolean", "probability": 0.9}})

    def fail(self, fn=None, **kwargs):
        with self.assertRaises(jev.JevError) as caught:
            jev.Jev(fn or self.down).ask({}, self.qs, retries=0, **kwargs)
        return str(caught.exception)

    def test_opens_after_threshold_and_skips_fail_fast_callers(self):
        for _ in range(jev._Breaker.THRESHOLD):
            self.fail(fail_fast=True)
        self.assertIn("circuit open", self.fail(fail_fast=True))
        self.assertEqual(len(self.attempts), jev._Breaker.THRESHOLD)

    def test_background_callers_still_try_while_open(self):
        for _ in range(jev._Breaker.THRESHOLD):
            self.fail()
        self.fail()
        self.assertEqual(len(self.attempts), jev._Breaker.THRESHOLD + 1)

    def test_probe_after_cooldown_closes_it(self):
        for _ in range(jev._Breaker.THRESHOLD):
            self.fail()
        with patch.object(jev.time, "monotonic", return_value=jev.time.monotonic() + jev._Breaker.COOLDOWN + 1):
            self.assertEqual(jev.Jev(self.ok).ask({}, self.qs, fail_fast=True)["b"]["p"], 0.9)
        self.assertEqual(jev.BREAKER.open_for(), 0)
        self.fail(fail_fast=True)
        self.assertEqual(jev.BREAKER.open_for(), 0)  # one failure does not reopen it

    def test_permanent_errors_and_successes_do_not_trip_it(self):
        def bad(state, questions, timeout):
            raise RuntimeError("Gateway request failed (HTTP 400): bad")
        for _ in range(jev._Breaker.THRESHOLD + 2):
            self.fail(bad)
        self.assertEqual(jev.BREAKER.open_for(), 0)
        for _ in range(jev._Breaker.THRESHOLD - 1):
            self.fail()
        jev.Jev(self.ok).ask({}, self.qs)
        self.fail()
        self.assertEqual(jev.BREAKER.open_for(), 0)

class CronTests(unittest.TestCase):
    now = datetime(2026, 9, 22, 23, 30)  # a Tuesday

    def next(self, expression):
        return schedule.Cron(expression).next_after(self.now)

    def test_common_expressions(self):
        self.assertEqual(self.next("0 9 * * 1-5"), datetime(2026, 9, 23, 9, 0))
        self.assertEqual(self.next("*/15 * * * *"), datetime(2026, 9, 22, 23, 45))
        self.assertEqual(self.next("30 8 1 * *"), datetime(2026, 10, 1, 8, 30))
        self.assertEqual(self.next("@hourly"), datetime(2026, 9, 23, 0, 0))
        self.assertEqual(self.next("0 9 * * sun"), datetime(2026, 9, 27, 9, 0))
        self.assertEqual(self.next("0 9 * * 7"), datetime(2026, 9, 27, 9, 0))

    def test_restricted_day_fields_match_either_like_vixie_cron(self):
        self.assertEqual(self.next("0 12 13 * 5"), datetime(2026, 9, 25, 12, 0))

    def test_leap_day_and_strictly_after(self):
        self.assertEqual(self.next("0 0 29 2 *"), datetime(2028, 2, 29, 0, 0))
        self.assertEqual(schedule.Cron("30 23 * * *").next_after(self.now), datetime(2026, 9, 23, 23, 30))

    def test_invalid_expressions(self):
        for bad in ("* * * *", "60 * * * *", "* 24 * * *", "5-1 * * * *", "*/0 * * * *", "a * * * *"):
            with self.assertRaises(ValueError):
                schedule.Cron(bad)

    def test_next_run_and_parse_at(self):
        now = self.now.timestamp()
        self.assertIsNone(schedule.next_run({"at": now - 1}, now))
        self.assertEqual(schedule.next_run({"every_minutes": 5}, now), now + 300)
        self.assertEqual(schedule.parse_at("09:00", now), datetime(2026, 9, 23, 9, 0).timestamp())
        self.assertEqual(schedule.parse_at("23:45", now), datetime(2026, 9, 22, 23, 45).timestamp())
        self.assertEqual(schedule.parse_at("2026-10-01T08:00", now), datetime(2026, 10, 1, 8, 0).timestamp())
        with self.assertRaises(ValueError):
            schedule.parse_at("tomorrow morning", now)


if __name__ == "__main__":
    unittest.main()
