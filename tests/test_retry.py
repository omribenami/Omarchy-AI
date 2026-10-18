"""core/retry.py: one retry policy, and the sites that use it."""

import io
import json
import socket
import threading
import time
import unittest
import urllib.error
from email.message import Message
from unittest import mock

from omarchy_ai.core import cancel, retry
from omarchy_ai.execution import tools
from omarchy_ai.myapi.client import MyApiError

KINDS = (
    retry.NOT_SENT,
    retry.RATE_LIMITED,
    retry.TRANSPORT,
    retry.TIMEOUT,
    retry.SERVER,
    retry.FAILED,
    retry.PERMANENT,
    retry.UNCERTAIN,
    retry.CANCELLED,
)
# kind -> (read, reversible, irreversible, irreversible + idempotency key)
TABLE = {
    retry.NOT_SENT: (True, True, False, True),
    retry.RATE_LIMITED: (True, True, False, True),
    retry.TRANSPORT: (True, True, False, True),
    retry.TIMEOUT: (True, True, False, True),
    retry.SERVER: (True, True, False, False),
    retry.FAILED: (True, True, False, False),
    retry.PERMANENT: (False, False, False, False),
    retry.UNCERTAIN: (False, False, False, False),
    retry.CANCELLED: (False, False, False, False),
}
NO_JITTER = retry.Policy(attempts=3, base=1.0, factor=2.0, jitter=0.0)


def http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError(
        "https://example.invalid", code, "x", headers, io.BytesIO(b"")
    )


class PolicyTableTests(unittest.TestCase):
    def test_effect_classes_match_the_tool_registry(self):
        self.assertEqual(
            (retry.READ, retry.REVERSIBLE, retry.IRREVERSIBLE),
            (tools.READ, tools.REVERSIBLE, tools.IRREVERSIBLE),
        )

    def test_the_decision_table(self):
        self.assertEqual(set(TABLE), set(KINDS))
        for kind, (read, reversible, irreversible, keyed) in TABLE.items():
            with self.subTest(kind=kind):
                self.assertEqual(retry.should_retry(kind, retry.READ), read)
                self.assertEqual(retry.should_retry(kind, retry.REVERSIBLE), reversible)
                self.assertEqual(
                    retry.should_retry(kind, retry.IRREVERSIBLE), irreversible
                )
                self.assertEqual(
                    retry.should_retry(kind, retry.IRREVERSIBLE, idempotent=True), keyed
                )

    def test_an_unknown_effect_class_is_irreversible(self):
        for kind in KINDS:
            self.assertFalse(retry.should_retry(kind, "something-new"))

    def test_a_key_adds_nothing_for_a_read(self):
        for kind in KINDS:
            self.assertEqual(
                retry.should_retry(kind, retry.READ, idempotent=True),
                retry.should_retry(kind, retry.READ),
            )


class ClassifyTests(unittest.TestCase):
    def test_http_status(self):
        self.assertEqual(retry.classify(http_error(429)).kind, retry.RATE_LIMITED)
        self.assertEqual(retry.classify(http_error(503)).kind, retry.SERVER)
        self.assertEqual(retry.classify(http_error(400)).kind, retry.PERMANENT)
        self.assertEqual(retry.classify(http_error(401)).kind, retry.PERMANENT)

    def test_retry_after_is_read_from_the_response(self):
        self.assertEqual(retry.classify(http_error(429, 7)).retry_after, 7.0)
        when = "Wed, 21 Oct 2015 07:28:00 GMT"
        later = retry.retry_after_seconds(when, now=1445412480.0 - 30)
        self.assertAlmostEqual(later, 30.0)
        self.assertIsNone(retry.retry_after_seconds("soon"))

    def test_connection_errors(self):
        refused = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
        self.assertEqual(retry.classify(refused).kind, retry.NOT_SENT)
        self.assertEqual(
            retry.classify(socket.gaierror("no name")).kind, retry.NOT_SENT
        )
        self.assertEqual(
            retry.classify(ConnectionResetError("reset")).kind, retry.TRANSPORT
        )
        self.assertEqual(retry.classify(TimeoutError()).kind, retry.TIMEOUT)

    def test_myapi_errors(self):
        self.assertEqual(
            retry.classify(MyApiError("busy", status=429, retry_after=3)).retry_after, 3
        )
        self.assertEqual(retry.classify(MyApiError("x", status=502)).kind, retry.SERVER)
        self.assertEqual(
            retry.classify(MyApiError("x", status=403, reconnect_needed=True)).kind,
            retry.PERMANENT,
        )
        self.assertEqual(
            retry.classify(MyApiError("could not reach", transport=True)).kind,
            retry.NOT_SENT,
        )
        self.assertEqual(
            retry.classify(
                MyApiError("did not answer within 30s", transport=True, maybe_sent=True)
            ).kind,
            retry.TIMEOUT,
        )
        # The provider answered "ok": false -- the service said no.
        self.assertEqual(retry.classify(MyApiError("no such label")).kind, retry.FAILED)

    def test_messages(self):
        for text, kind in (
            (
                "Gateway request failed (HTTP 503): Service temporarily unavailable",
                retry.SERVER,
            ),
            ("Gateway request failed (HTTP 429): slow down", retry.RATE_LIMITED),
            ("Gateway request timed out", retry.TIMEOUT),
            ("Gateway is unavailable: [Errno 111] Connection refused", retry.SERVER),
            (
                "Gateway request failed (HTTP 400): Invalid discriminator value",
                retry.PERMANENT,
            ),
            ("could not reach myapiai.com: timed out", retry.TIMEOUT),
            ("Sorry, I am not aware of any device called fan", retry.FAILED),
        ):
            with self.subTest(text=text):
                self.assertEqual(retry.classify(text).kind, kind)

    def test_cancel_and_not_dispatched(self):
        self.assertEqual(retry.classify(cancel.Cancelled("stop")).kind, retry.CANCELLED)
        result = mock.Mock(ok=False, message="", error_class="uncertain")
        self.assertEqual(retry.classify(result).kind, retry.UNCERTAIN)
        result = mock.Mock(ok=False, message="", error_class="timeout")
        self.assertEqual(retry.classify(result).kind, retry.TIMEOUT)


class BackoffTests(unittest.TestCase):
    def test_exponential_and_capped(self):
        policy = retry.Policy(base=1.0, factor=2.0, cap=5.0, jitter=0.0)
        self.assertEqual([policy.delay(n) for n in range(5)], [1.0, 2.0, 4.0, 5.0, 5.0])

    def test_jitter_stays_within_its_fraction(self):
        policy = retry.Policy(base=10.0, factor=1.0, jitter=0.2)
        delays = [policy.delay(0) for _ in range(200)]
        self.assertTrue(all(8.0 <= d <= 12.0 for d in delays))
        self.assertGreater(len(set(delays)), 1)

    def test_a_longer_retry_after_is_honored(self):
        failure = retry.Failure(retry.RATE_LIMITED, retry_after=9.0)
        self.assertEqual(NO_JITTER.delay(0, failure), 9.0)
        self.assertEqual(
            NO_JITTER.delay(0, retry.Failure(retry.RATE_LIMITED, 0.1)), 1.0
        )

    def test_a_retry_after_past_the_limit_is_not_waited_for(self):
        failure = retry.Failure(retry.RATE_LIMITED, retry_after=3600.0)
        self.assertIsNone(NO_JITTER.plan(0, failure))


class RunTests(unittest.TestCase):
    def setUp(self):
        self.calls = 0
        self.sleep = mock.patch.object(retry.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def flaky(self, failures, error=lambda: ConnectionResetError("reset")):
        def call():
            self.calls += 1
            if self.calls <= failures:
                raise error()
            return "ok"

        return call

    def test_a_transient_read_failure_is_retried_until_it_works(self):
        result = retry.run(self.flaky(2), effect_class=retry.READ, policy=NO_JITTER)
        self.assertEqual((result, self.calls), ("ok", 3))
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1.0, 2.0])

    def test_attempts_bound_the_retries_and_the_last_error_is_raised(self):
        with self.assertRaises(ConnectionResetError):
            retry.run(self.flaky(10), effect_class=retry.READ, policy=NO_JITTER)
        self.assertEqual(self.calls, 3)

    def test_a_permanent_failure_is_not_retried(self):
        with self.assertRaises(urllib.error.HTTPError):
            retry.run(self.flaky(1, lambda: http_error(400)), effect_class=retry.READ)
        self.assertEqual(self.calls, 1)

    def test_an_irreversible_call_is_never_retried(self):
        for error in (
            lambda: ConnectionResetError("reset"),
            lambda: http_error(503),
            lambda: http_error(429),
            lambda: TimeoutError(),
        ):
            self.calls = 0
            with self.subTest(error=error), self.assertRaises(Exception):
                retry.run(self.flaky(1, error), effect_class=retry.IRREVERSIBLE)
            self.assertEqual(self.calls, 1)
        self.sleep.assert_not_called()

    def test_an_irreversible_call_with_a_key_is_retried_only_where_it_cannot_resend(
        self,
    ):
        result = retry.run(
            self.flaky(1, TimeoutError),
            effect_class=retry.IRREVERSIBLE,
            idempotent=True,
            policy=NO_JITTER,
        )
        self.assertEqual((result, self.calls), ("ok", 2))
        self.calls = 0
        with self.assertRaises(urllib.error.HTTPError):
            retry.run(
                self.flaky(1, lambda: http_error(503)),
                effect_class=retry.IRREVERSIBLE,
                idempotent=True,
            )
        self.assertEqual(self.calls, 1)

    def test_a_failed_result_is_retried_through_check(self):
        results = iter(["HTTP 503", "fine"])
        result = retry.run(
            lambda: next(results),
            effect_class=retry.REVERSIBLE,
            policy=NO_JITTER,
            check=lambda r: None if r == "fine" else retry.classify_text(r),
        )
        self.assertEqual(result, "fine")

    def test_no_retry_that_would_pass_the_deadline(self):
        deadline = time.monotonic() + 1.5  # room for the 1 s wait, not the 2 s one
        with self.assertRaises(ConnectionResetError):
            retry.run(
                self.flaky(10),
                effect_class=retry.READ,
                policy=NO_JITTER,
                deadline=deadline,
            )
        self.assertEqual(self.calls, 2)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1.0])

    def test_a_retry_after_past_the_deadline_is_not_waited_for(self):
        def busy():
            self.calls += 1
            raise http_error(429, 30)

        with self.assertRaises(urllib.error.HTTPError):
            retry.run(busy, effect_class=retry.READ, deadline=time.monotonic() + 10)
        self.assertEqual(self.calls, 1)
        self.sleep.assert_not_called()

    def test_a_cancelled_call_is_not_retried(self):
        token = cancel.CancelToken()
        token.cancel("stop")
        with self.assertRaises(ConnectionResetError):
            retry.run(self.flaky(1), effect_class=retry.READ, token=token)
        self.assertEqual(self.calls, 1)

    def test_the_current_token_is_used_by_default(self):
        token = cancel.CancelToken()
        token.cancel("stop")
        reset = cancel.CURRENT.set(token)
        self.addCleanup(cancel.CURRENT.reset, reset)
        with self.assertRaises(ConnectionResetError):
            retry.run(self.flaky(1), effect_class=retry.READ)
        self.assertEqual(self.calls, 1)

    def test_a_cancel_ends_the_wait_at_once(self):
        mock.patch.stopall()  # real waiting
        token = cancel.CancelToken()
        threading.Timer(0.1, token.cancel, args=("stop",)).start()
        started = time.monotonic()
        with self.assertRaises(ConnectionResetError):
            retry.run(
                self.flaky(10),
                effect_class=retry.READ,
                policy=retry.Policy(base=30.0, jitter=0.0),
                token=token,
            )
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(self.calls, 1)


class SiteTests(unittest.TestCase):
    """The sites that used to retry on their own."""

    def test_agenda_keeps_its_5_then_20_second_timing(self):
        from omarchy_ai.core import agenda

        policy = agenda.STEP_RETRY
        self.assertEqual(policy.attempts, 3)
        for retry_number, expected in ((0, 5.0), (1, 20.0)):
            for _ in range(50):
                self.assertAlmostEqual(
                    policy.delay(retry_number), expected, delta=expected * 0.1
                )

    def test_agenda_waits_those_delays_between_attempts(self):
        from omarchy_ai.core import agenda, effects
        from omarchy_ai.execution.actions import ActionResult

        step = {"tool": "volume_set", "args": {"percent": 20}}
        with (
            mock.patch.object(retry.time, "sleep") as sleep,
            mock.patch.object(
                effects, "perform", return_value=ActionResult(False, "HTTP 503")
            ) as perform,
        ):
            result, why, attempts = agenda._call(step, "agenda:test:1:1", "test")
        self.assertEqual((attempts, perform.call_count), (3, 3))
        self.assertEqual(why, "HTTP 503")
        waits = [c.args[0] for c in sleep.call_args_list]
        self.assertEqual(len(waits), 2)
        self.assertAlmostEqual(waits[0], 5.0, delta=0.5)
        self.assertAlmostEqual(waits[1], 20.0, delta=2.0)

    def test_agenda_never_retries_an_irreversible_step(self):
        from omarchy_ai.core import agenda, effects
        from omarchy_ai.execution.actions import ActionResult

        step = {"tool": "nightlight_toggle", "args": {}}
        with (
            mock.patch.object(retry.time, "sleep") as sleep,
            mock.patch.object(
                effects, "perform", return_value=ActionResult(False, "HTTP 503")
            ) as perform,
        ):
            _, _, attempts = agenda._call(step, "agenda:test:1:1", "test")
        self.assertEqual((attempts, perform.call_count), (1, 1))
        sleep.assert_not_called()

    def test_llm_retries_a_transient_api_failure_within_its_deadline(self):
        from omarchy_ai.runtime import llm

        replies = [
            RuntimeError("Gateway request failed (HTTP 503): unavailable"),
            {"choices": [{"message": {"content": '{"ok": true}'}}]},
        ]

        def transport(payload, timeout):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        wm = llm.WorkerModel("test/model", transport)
        with mock.patch.object(retry.time, "sleep") as sleep:
            self.assertEqual(
                wm._api_complete("s", "u", timeout=30, retries=2), {"ok": True}
            )
        self.assertEqual(sleep.call_count, 1)
        self.assertAlmostEqual(sleep.call_args.args[0], 0.8, delta=0.8 * 0.2)

    def test_llm_does_not_retry_past_its_deadline(self):
        from omarchy_ai.runtime import llm

        calls = []

        def transport(payload, timeout):
            calls.append(timeout)
            raise RuntimeError("Gateway request timed out")

        wm = llm.WorkerModel("test/model", transport)
        with (
            mock.patch.object(retry.time, "sleep") as sleep,
            self.assertRaises(llm.WorkerModelError) as caught,
        ):
            wm._api_complete("s", "u", timeout=0.5, retries=2)
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()
        self.assertIn("timed out", str(caught.exception))

    def test_a_myapi_read_is_retried_and_honors_retry_after(self):
        from omarchy_ai.execution import myapi_agent
        from omarchy_ai.execution.actions import myapi_tools

        replies = [
            MyApiError("slow down", status=429, retry_after=4.0),
            {"messages": []},
        ]

        def request(service, method, args):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        with (
            mock.patch.object(
                myapi_tools, "myapi_execute_request", side_effect=request
            ),
            mock.patch.object(retry.time, "sleep") as sleep,
        ):
            result = myapi_agent._read("gmail", "GMAIL_LIST_LABELS", {})
        self.assertTrue(result.ok)
        self.assertEqual(json.loads(result.message), {"messages": []})
        self.assertEqual(sleep.call_args.args[0], 4.0)

    def test_a_myapi_read_client_error_is_not_retried(self):
        from omarchy_ai.execution import myapi_agent
        from omarchy_ai.execution.actions import myapi_tools

        with (
            mock.patch.object(
                myapi_tools,
                "myapi_execute_request",
                side_effect=MyApiError("bad argument", status=400),
            ) as request,
            mock.patch.object(retry.time, "sleep") as sleep,
        ):
            result = myapi_agent._read("gmail", "GMAIL_LIST_LABELS", {})
        self.assertFalse(result.ok)
        self.assertIn("bad argument", result.message)
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class LiveCloseTests(unittest.TestCase):
    """classify_live: which ends of a Gemini Live session reconnect once."""

    def api_error(self, code, message):
        from google.genai import errors

        return errors.APIError(code, {"error": {"message": message}})

    def test_transient_closes(self):
        # The journal's own error (2026-10-17 08:09 and 09:50), and its cause.
        from websockets.exceptions import ConnectionClosedError
        from websockets.frames import Close

        closed = ConnectionClosedError(
            Close(1011, "Internal error encountered."), Close(1011, "x"), True
        )
        for error in (
            self.api_error(1011, "Internal error encountered."),
            self.api_error(1006, "Abnormal closure."),
            self.api_error(1001, "Going away."),
            closed,
            ConnectionResetError("connection reset by peer"),
            ConnectionError("Gemini receive stream closed without a turn"),
        ):
            failure = retry.classify_live(error)
            self.assertTrue(retry.live_transient(failure), (error, failure))
        self.assertEqual(retry.close_code(closed), 1011)
        self.assertEqual(
            retry.classify_live(self.api_error(1011, "x")).detail, "1011 internal error"
        )

    def test_not_transient(self):
        for error, kind in (
            (
                self.api_error(1011, "Resource has been exhausted (e.g. check quota)."),
                retry.RATE_LIMITED,
            ),
            (self.api_error(1008, "Policy violation."), retry.PERMANENT),
            (
                self.api_error(1007, "Request contains an invalid argument."),
                retry.PERMANENT,
            ),
            (ConnectionError("HTTP 403 forbidden"), retry.PERMANENT),
            (RuntimeError("API key not valid"), retry.PERMANENT),
            (ValueError("bug"), retry.FAILED),
        ):
            failure = retry.classify_live(error)
            self.assertEqual(failure.kind, kind, error)
            self.assertFalse(retry.live_transient(failure), error)
