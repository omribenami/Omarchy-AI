"""Tool calls with deadlines and cancellation (execution/runner.py), DoD-2:
a hung tool is stopped within its deadline + 2 s and the session stays
responsive."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fakes.effects_journal import isolate_journal
from omarchy_ai.core import cancel, effects
from omarchy_ai.execution import actions, runner, tools
from omarchy_ai.execution.actions import ActionResult
from omarchy_ai.execution.catalog import USE_TOOL

setUpModule, tearDownModule = isolate_journal()

TESTS = Path(__file__).resolve().parent
SLACK = 1.0  # scheduling slack on a loaded CI machine
cancelled_tools: list[str] = []


def record_cancel() -> None:
    cancelled_tools.append("hang")


def wait_until(predicate, timeout: float) -> bool:
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:  # a zombie child of this process counts as gone
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] == "Z"
    except OSError:
        return True


class Specs(unittest.TestCase):
    def test_every_tool_declares_a_deadline_and_a_resource(self):
        self.assertEqual(set(tools.DEADLINES), set(tools.EFFECT_CLASSES))
        self.assertEqual(set(tools.RESOURCES), set(tools.EFFECT_CLASSES))
        self.assertIn(USE_TOOL["name"], tools.DEADLINES)
        for name, seconds in tools.DEADLINES.items():
            self.assertGreaterEqual(seconds, 30.0, name)

    def test_worker_tools_are_reads_the_actions_table_has(self):
        for name in tools.WORKER_TOOLS:
            self.assertEqual(tools.effect_class(name), tools.READ, name)
            self.assertIn(name, actions.ACTIONS)

    def test_deadlines_cover_what_tools_take_today(self):
        self.assertGreater(
            tools.deadline("browser_task"), 90 + 12
        )  # task budget + browser start
        self.assertGreater(tools.deadline("myapi_call"), 30)  # one MyApi request
        self.assertGreaterEqual(tools.deadline("install_receiver_on_tv"), 300 + 120)
        self.assertEqual(tools.deadline("not_a_tool"), tools.DEFAULT_DEADLINE)
        self.assertEqual(tools.resource("not_a_tool"), tools.DESKTOP)

    def test_spec(self):
        spec = runner.spec("myapi_gmail_search")
        self.assertEqual(
            (spec.effect_class, spec.resource, spec.worker, spec.reads),
            (tools.READ, tools.MYAPI, True, True),
        )
        self.assertFalse(runner.spec("type_text").reads)


class InProcess(unittest.TestCase):
    def setUp(self):
        self.runner = runner.ToolRunner()

    def test_a_result_passes_through_unchanged(self):
        result = self.runner.run(
            "list_windows", {}, execute=lambda n, a: ActionResult(True, "[]")
        )
        self.assertEqual(
            (result.ok, result.message, result.error_class), (True, "[]", None)
        )
        self.assertGreater(result.duration, 0)
        failed = self.runner.run(
            "list_windows", {}, execute=lambda n, a: ActionResult(False, "nope")
        )
        self.assertEqual(
            (failed.ok, failed.message, failed.error_class), (False, "nope", "failed")
        )

    def test_the_default_execute_is_run_action(self):
        with mock.patch.object(
            actions, "run_action", return_value=ActionResult(True, "ran")
        ) as run_action:
            result = self.runner.run("volume_up", {"step": 5})
        run_action.assert_called_once_with("volume_up", {"step": 5})
        self.assertEqual(result.message, "ran")

    def test_wrap_gets_the_execute_function(self):
        seen = []

        def wrap(execute, name, args):
            seen.append(name)
            return execute(name, args)

        result = self.runner.run(
            "list_windows", {}, execute=lambda n, a: ActionResult(True, n), wrap=wrap
        )
        self.assertEqual((seen, result.message), (["list_windows"], "list_windows"))

    def test_a_crash_is_a_result(self):
        def boom(name, args):
            raise RuntimeError("bad")

        result = self.runner.run("list_windows", {}, execute=boom)
        self.assertEqual(
            (result.ok, result.message, result.error_class),
            (False, "list_windows crashed", "crashed"),
        )

    def test_a_hung_adapter_is_answered_at_its_deadline(self):
        release = threading.Event()
        self.addCleanup(release.set)
        started = time.monotonic()
        result = self.runner.run(
            "list_windows", {}, execute=lambda n, a: release.wait(), deadline=0.3
        )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.3 + runner.KILL_GRACE)
        self.assertEqual(result.error_class, "timeout")
        self.assertTrue(result.retriable)  # a read
        self.assertEqual(
            result.message, "list_windows timed out after 0.3 s; nothing was confirmed."
        )
        self.assertEqual(self.runner.running(), [])

    def test_a_mutating_timeout_says_it_may_have_happened(self):
        release = threading.Event()
        self.addCleanup(release.set)
        result = self.runner.run(
            "type_text",
            {"text": "x"},
            execute=lambda n, a: release.wait(),
            deadline=0.2,
        )
        self.assertFalse(result.retriable)
        self.assertIn("nothing was confirmed", result.message)
        self.assertIn("may have partly happened", result.message)

    def test_a_cooperative_adapter_sees_the_token(self):
        stopped = threading.Event()

        def adapter(name, args):
            token = cancel.current()
            assert token is not None
            token.wait(10)
            stopped.set()
            cancel.checkpoint()  # raises: the call was cancelled
            return ActionResult(True, "late")

        result = self.runner.run("list_windows", {}, execute=adapter, deadline=0.2)
        self.assertEqual(result.error_class, "timeout")
        self.assertTrue(stopped.wait(2))

    def test_a_late_result_is_dropped(self):
        release = threading.Event()
        done = threading.Event()

        def adapter(name, args):
            release.wait()
            done.set()
            return ActionResult(True, "late")

        result = self.runner.run("list_windows", {}, execute=adapter, deadline=0.1)
        release.set()
        self.assertTrue(done.wait(2))
        self.assertEqual(result.error_class, "timeout")

    def test_cancel_by_owner_runs_the_tools_canceller(self):
        cancelled_tools.clear()
        release = threading.Event()
        self.addCleanup(release.set)
        owner, other = object(), object()
        results = {}
        with mock.patch.dict(
            runner.CANCELLERS, {"browser_task": f"{__name__}:record_cancel"}
        ):
            for who in (owner, other):
                threading.Thread(
                    target=lambda who=who: results.__setitem__(
                        who,
                        self.runner.run(
                            "browser_task",
                            {},
                            execute=lambda n, a: release.wait(),
                            owner=who,
                        ),
                    )
                ).start()
            self.assertTrue(wait_until(lambda: len(self.runner.running()) == 2, 2))
            self.assertEqual(self.runner.cancel(owner=owner, reason="hangup"), 1)
            self.assertTrue(wait_until(lambda: owner in results, 2))
        self.assertEqual(results[owner].error_class, "cancelled")
        self.assertEqual(
            results[owner].message,
            "browser_task was stopped (hangup); nothing was confirmed. "
            "It may have partly happened: check before doing it again.",
        )
        self.assertEqual(cancelled_tools, ["hang"])
        self.assertEqual(self.runner.running(other), ["browser_task"])
        self.assertEqual(self.runner.cancel(names={"browser_task"}), 1)
        self.assertTrue(wait_until(lambda: other in results, 2))


class Effects(unittest.TestCase):
    """A cancelled mutating call never ends `confirmed` in the journal."""

    def setUp(self):
        self.runner = runner.ToolRunner()

    def write(self, gate: threading.Event, dispatched: list):
        def execute(name, args):
            def dispatch(key):
                dispatched.append(key)
                gate.wait()
                return ActionResult(True, "sent")

            return effects.perform(
                "myapi_write", name, args, dispatch, origin_id="turn:t"
            )

        return execute

    def test_a_call_stopped_while_dispatched_is_uncertain(self):
        gate, dispatched = threading.Event(), []
        self.addCleanup(gate.set)
        result = self.runner.run(
            "myapi_write",
            {"to": "a"},
            execute=self.write(gate, dispatched),
            deadline=0.3,
        )
        self.assertEqual(result.error_class, "timeout")
        key = effects.idem_key("turn:t", "myapi_write", {"to": "a"})
        self.assertEqual(effects.lookup(key).state, effects.UNCERTAIN)
        self.assertEqual(result.effect_id, effects.lookup(key).effect_id)
        gate.set()  # the abandoned dispatch now returns: still not confirmed
        self.assertTrue(
            wait_until(
                lambda: (
                    len([r for r in effects.records() if r["idem_key"] == key]) >= 4
                ),
                2,
            )
        )
        self.assertEqual(effects.lookup(key).state, effects.UNCERTAIN)
        self.assertNotIn(
            effects.CONFIRMED,
            [r["state"] for r in effects.records() if r["idem_key"] == key],
        )

    def test_a_call_stopped_before_dispatch_is_never_sent(self):
        gate, dispatched, before = threading.Event(), [], threading.Event()
        self.addCleanup(before.set)
        write = self.write(gate, dispatched)

        def execute(name, args):
            before.wait()
            return write(name, args)

        result = self.runner.run(
            "myapi_write", {"to": "b"}, execute=execute, deadline=0.2
        )
        self.assertEqual(result.error_class, "timeout")
        before.set()
        key = effects.idem_key("turn:t", "myapi_write", {"to": "b"})
        self.assertTrue(
            wait_until(
                lambda: (
                    effects.lookup(key) is not None
                    and effects.lookup(key).state == effects.FAILED
                ),
                2,
            )
        )
        self.assertEqual(dispatched, [])

    def test_a_confirmed_call_that_finishes_in_time_stays_confirmed(self):
        gate, dispatched = threading.Event(), []
        gate.set()
        result = self.runner.run(
            "myapi_write", {"to": "c"}, execute=self.write(gate, dispatched)
        )
        self.assertTrue(result.ok)
        key = effects.idem_key("turn:t", "myapi_write", {"to": "c"})
        self.assertEqual(effects.lookup(key).state, effects.CONFIRMED)
        self.assertEqual(result.effect_id, effects.lookup(key).effect_id)


class Async(unittest.IsolatedAsyncioTestCase):
    async def test_a_second_read_returns_while_the_first_hangs(self):
        """DoD-2: the session stays responsive while one tool hangs."""
        tool_runner, resources = runner.ToolRunner(), runner.Resources()
        release = threading.Event()
        self.addCleanup(release.set)

        def execute(name, args):
            if args.get("hang"):
                release.wait()
            return ActionResult(True, "fresh")

        async def call(args, deadline):
            async with resources.hold("desktop", exclusive=False):
                return await tool_runner.arun(
                    "read_tile_log", args, execute=execute, deadline=deadline
                )

        started = time.monotonic()
        hung = asyncio.create_task(call({"hang": True}, 1.0))
        await asyncio.sleep(0.05)
        second = await call({}, 5)
        self.assertEqual(second.message, "fresh")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(hung.done())
        first = await hung
        self.assertEqual(first.error_class, "timeout")
        self.assertLess(time.monotonic() - started, 1.0 + runner.KILL_GRACE)

    async def test_cancelling_the_awaiting_task_cancels_the_call(self):
        tool_runner = runner.ToolRunner()
        tokens = []

        def execute(name, args):
            token = cancel.current()
            tokens.append(token)
            token.wait(10)
            return ActionResult(True, "late")

        task = asyncio.create_task(tool_runner.arun("type_text", {}, execute=execute))
        await asyncio.sleep(0.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(tokens[0].cancelled)
        self.assertEqual(tokens[0].reason, "cancelled")


class Ordering(unittest.IsolatedAsyncioTestCase):
    async def test_mutations_keep_their_order_and_reads_do_not_overtake_them(self):
        resources, log = runner.Resources(), []

        async def call(name, exclusive, seconds, resource="desktop"):
            async with resources.hold(resource, exclusive=exclusive):
                log.append(f"{name}+")
                await asyncio.sleep(seconds)
                log.append(f"{name}-")

        await asyncio.gather(
            call("type", True, 0.05),
            call("enter", True, 0.01),
            call("read1", False, 0.05),
            call("read2", False, 0.01),
            call("myapi", True, 0.0, resource="myapi"),
            call("focus", True, 0.0),
        )
        self.assertLess(log.index("type-"), log.index("enter+"))
        self.assertLess(log.index("enter-"), log.index("read1+"))
        self.assertLess(log.index("read2+"), log.index("read1-"))  # reads side by side
        self.assertLess(
            log.index("read1-"), log.index("focus+")
        )  # a write waits for earlier reads
        self.assertLess(
            log.index("myapi-"), log.index("type-")
        )  # another resource does not wait

    async def test_a_cancelled_waiter_keeps_the_chain(self):
        resources, log = runner.Resources(), []
        gate = asyncio.Event()

        async def first():
            async with resources.hold("desktop", exclusive=True):
                await gate.wait()
                log.append("first")

        async def after():
            async with resources.hold("desktop", exclusive=True):
                log.append("third")

        one = asyncio.create_task(first())
        await asyncio.sleep(0)
        middle = asyncio.create_task(after())
        await asyncio.sleep(0)
        third = asyncio.create_task(after())
        await asyncio.sleep(0.01)
        middle.cancel()
        await asyncio.sleep(0.01)
        self.assertEqual(log, [])  # the third still waits for the first
        gate.set()
        await asyncio.gather(one, third)
        self.assertEqual(log, ["first", "third"])


class Worker(unittest.TestCase):
    """The killable worker process (execution/worker_main.py)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="omarchy-worker-"))
        paths = [str(TESTS), os.environ.get("PYTHONPATH", "")]
        env = mock.patch.dict(
            os.environ, {"PYTHONPATH": os.pathsep.join(p for p in paths if p)}
        )
        env.start()
        self.addCleanup(env.stop)
        names = mock.patch.object(
            tools,
            "WORKER_TOOLS",
            frozenset({"echo", "fail", "boom", "die", "hang", "stubborn"}),
        )
        names.start()
        self.addCleanup(names.stop)
        self.pool = runner.WorkerPool(
            [
                sys.executable,
                "-m",
                "omarchy_ai.execution.worker_main",
                "--actions",
                "fakes.worker_actions:ACTIONS",
            ]
        )
        self.addCleanup(self.pool.close)
        self.runner = runner.ToolRunner(self.pool)

    def pids(self) -> list[int]:
        path = self.tmp / "pids"
        self.assertTrue(wait_until(path.exists, 10), "the fake adapter never started")
        return json.loads(path.read_text())

    def test_a_call_runs_in_a_reused_worker(self):
        first = self.runner.run("echo", {"a": 1}, deadline=20)
        second = self.runner.run("echo", {"a": 2}, deadline=20)
        self.assertTrue(first.ok, first.message)
        one, two = json.loads(first.message), json.loads(second.message)
        self.assertEqual(one["args"], {"a": 1})
        self.assertNotEqual(one["pid"], os.getpid())
        self.assertEqual(one["pid"], two["pid"])

    def test_results_failures_and_crashes(self):
        self.assertEqual(
            self.runner.run("fail", {}, deadline=20).message, "no such thing"
        )
        crashed = self.runner.run("boom", {}, deadline=20)
        self.assertEqual((crashed.ok, crashed.message), (False, "boom crashed"))
        lost = self.runner.run("die", {}, deadline=20)
        self.assertEqual(lost.error_class, "worker_lost")
        self.assertTrue(self.runner.run("echo", {}, deadline=20).ok)  # a new worker

    def test_a_hung_worker_is_killed_within_its_deadline_plus_two_seconds(self):
        """DoD-2: the call is answered at its deadline; the worker and what
        it started are gone within deadline + 2 s."""
        self.runner.run(
            "echo", {}, deadline=20
        )  # a warm worker: no start-up in the deadline
        deadline = 1.0
        started = time.monotonic()
        result = self.runner.run(
            "hang", {"pids": str(self.tmp / "pids")}, deadline=deadline
        )
        answered = time.monotonic() - started
        self.assertEqual(result.error_class, "timeout")
        self.assertLess(answered, deadline + SLACK)
        worker, child = self.pids()
        self.assertTrue(
            wait_until(
                lambda: gone(worker) and gone(child),
                deadline + runner.KILL_GRACE + SLACK - answered,
            )
        )
        self.assertLess(
            time.monotonic() - started, deadline + runner.KILL_GRACE + SLACK
        )
        self.assertTrue(self.runner.run("echo", {}, deadline=20).ok)

    def test_sigkill_follows_sigterm_after_the_grace(self):
        self.runner.run("echo", {}, deadline=20)
        started = time.monotonic()
        result = self.runner.run(
            "stubborn", {"pids": str(self.tmp / "pids")}, deadline=1.0
        )
        self.assertEqual(result.error_class, "timeout")
        worker, child = self.pids()
        self.assertTrue(
            wait_until(
                lambda: gone(worker) and gone(child), 1.0 + runner.KILL_GRACE + SLACK
            )
        )
        self.assertGreaterEqual(
            time.monotonic() - started, 1.0 + runner.KILL_GRACE - 0.1
        )

    def test_cancel_kills_the_worker(self):
        results = []
        thread = threading.Thread(
            target=lambda: results.append(
                self.runner.run(
                    "hang", {"pids": str(self.tmp / "pids")}, deadline=60, owner=self
                )
            )
        )
        thread.start()
        worker, child = self.pids()
        self.assertEqual(self.runner.cancel(owner=self, reason="stop"), 1)
        thread.join(5)
        self.assertEqual(results[0].error_class, "cancelled")
        self.assertTrue(
            wait_until(lambda: gone(worker) and gone(child), runner.KILL_GRACE + SLACK)
        )

    def test_the_myapi_owner_check_stays_in_the_daemon(self):
        with (
            mock.patch.object(tools, "WORKER_TOOLS", frozenset({"myapi_call"})),
            mock.patch(
                "omarchy_ai.core.identities.myapi_refusal", return_value="private"
            ),
        ):
            result = self.runner.run("myapi_call", {}, deadline=20)
        self.assertEqual((result.ok, result.message), (False, "private"))
        self.assertEqual(self.pool._busy, set())


if __name__ == "__main__":
    unittest.main()
