"""Live-session tool dispatch on the ToolRunner (voice/tool_dispatch.py,
voice/gemini_live.py): reads run side by side, mutations stay ordered per
resource, every call has a deadline, and a hangup stops what is running."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, patch

import no_live_desktop  # noqa: F401 -- never a real desktop command
from fakes.effects_journal import isolate_journal
from google.genai import types
from omarchy_ai.config import Config
from omarchy_ai.core import effects
from omarchy_ai.execution import runner, tools
from omarchy_ai.execution.actions import ActionResult
from omarchy_ai.execution.verified_input import InputGuard
from omarchy_ai.voice.gemini_live import GeminiLiveSession
from omarchy_ai.voice.tool_dispatch import ToolDispatchMixin

setUpModule, tearDownModule = isolate_journal()


class FakeChannel:
    def __init__(self) -> None:
        self.outputs: list[tuple[str, dict]] = []

    def send(self, text: str) -> None:
        event = json.loads(text)
        if event["type"] == "response.item.create":
            item = event["item"]
            self.outputs.append((item["call_id"], json.loads(item["output"])))


class Session(ToolDispatchMixin):
    """The state LiveSession keeps for its dispatch, without audio or WebRTC."""

    def __init__(self) -> None:
        self._resources = None
        self._hangup = asyncio.Event()
        self._action_log: list = []
        self._input_guard = InputGuard()
        self._dc = FakeChannel()
        self._demo_waiting_for_action = threading.Event()
        self._watchdog_on = False

    def _set_watchdog_state(self, label: str) -> None:
        pass

    def output(self, call_id: str) -> dict | None:
        return next((o for c, o in self._dc.outputs if c == call_id), None)


class LiveDispatch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.order: list[str] = []
        for target, value in (
            ("omarchy_ai.voice.tool_dispatch.run_action", self.action),
            (
                "omarchy_ai.voice.tool_dispatch.ToolDispatchMixin._current_window",
                lambda: {},
            ),
        ):
            patcher = patch(target, side_effect=value) if callable(value) else None
            patcher.start()
            self.addCleanup(patcher.stop)
        self.session = Session()
        self.addCleanup(runner.default_runner().cancel, owner=self.session)

    def action(self, name, args):
        self.order.append(f"{name}+")
        if args.get("hang"):
            self.release.wait(10)
        elif args.get("seconds"):
            time.sleep(args["seconds"])
        self.order.append(f"{name}-")
        return ActionResult(True, f"{name} done")

    def call(self, call_id, name, args=None):
        return asyncio.create_task(
            self.session._run_tool_call(call_id, name, args or {})
        )

    async def wait_for(self, call_id, timeout=3.0):
        until = time.monotonic() + timeout
        while self.session.output(call_id) is None and time.monotonic() < until:
            await asyncio.sleep(0.01)
        return self.session.output(call_id)

    async def test_a_second_read_returns_while_the_first_hangs(self):
        hung = self.call("1", "read_tile_log", {"hang": True})
        await asyncio.sleep(0.05)
        second = self.call("2", "list_windows")
        self.assertEqual(
            await self.wait_for("2"), {"ok": True, "message": "list_windows done"}
        )
        self.assertIsNone(self.session.output("1"))
        self.release.set()
        await asyncio.gather(hung, second)
        self.assertEqual(self.session.output("1")["message"], "read_tile_log done")

    async def test_a_hung_call_is_answered_at_its_deadline(self):
        with mock.patch.dict(tools.DEADLINES, {"volume_up": 0.3}):
            started = time.monotonic()
            await self.call("1", "volume_up", {"hang": True})
        self.assertLess(time.monotonic() - started, 0.3 + runner.KILL_GRACE)
        self.assertEqual(
            self.session.output("1"),
            {
                "ok": False,
                "message": "volume_up timed out after 0.3 s; nothing was confirmed. "
                "It may have partly happened: check before doing it again.",
            },
        )
        # The session goes on: the next mutation on that resource runs.
        await self.call("2", "volume_down")
        self.assertEqual(self.session.output("2")["message"], "volume_down done")

    async def test_mutations_on_one_resource_keep_their_order(self):
        calls = [
            self.call("1", "workspace_switch", {"seconds": 0.05}),
            self.call("2", "volume_up"),
            self.call("3", "read_tile_log"),
            self.call("4", "myapi_gmail_search", {"seconds": 0.0}),
        ]
        await asyncio.gather(*calls)
        self.assertLess(
            self.order.index("workspace_switch-"), self.order.index("volume_up+")
        )
        self.assertLess(
            self.order.index("volume_up-"), self.order.index("read_tile_log+")
        )
        # MyApi is another resource: it did not wait for the desktop.
        self.assertLess(
            self.order.index("myapi_gmail_search-"),
            self.order.index("workspace_switch-"),
        )

    async def test_a_hangup_stops_a_mutating_call_and_its_effect_is_uncertain(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        dispatched = threading.Event()

        def write(name, args):
            def dispatch(key):
                dispatched.set()
                gate.wait(10)
                return ActionResult(True, "sent")

            return effects.perform("myapi_write", name, args, dispatch)

        with patch("omarchy_ai.voice.tool_dispatch.run_action", side_effect=write):
            self.session._input_guard.approve_external_write("myapi_write", {"to": "a"})
            task = self.call("1", "myapi_write", {"to": "a"})
            self.assertTrue(await asyncio.to_thread(dispatched.wait, 5))
            self.assertEqual(
                runner.default_runner().cancel(owner=self.session, reason="hangup"), 1
            )
            await task
        output = self.session.output("1")
        self.assertFalse(output["ok"])
        self.assertIn("was stopped (hangup); nothing was confirmed", output["message"])
        key = effects.idem_key(
            self.session._input_guard.turn_origin, "myapi_write", {"to": "a"}
        )
        self.assertEqual(effects.lookup(key).state, effects.UNCERTAIN)
        gate.set()  # the abandoned write answers late: still not confirmed
        await asyncio.sleep(0.2)
        self.assertEqual(effects.lookup(key).state, effects.UNCERTAIN)


class GeminiDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_a_hung_call_returns_a_timeout_to_the_model(self):
        adapter = GeminiLiveSession(Config())
        release = threading.Event()
        self.addCleanup(release.set)
        session = SimpleNamespace(send_tool_response=AsyncMock())
        call = types.FunctionCall(id="1", name="list_windows", args={})
        with (
            patch(
                "omarchy_ai.voice.gemini_live.run_action",
                side_effect=lambda *a: release.wait(10),
            ),
            patch(
                "omarchy_ai.voice.gemini_live.LiveSession._current_window",
                return_value={},
            ),
            mock.patch.dict(tools.DEADLINES, {"list_windows": 0.3}),
        ):
            started = time.monotonic()
            await adapter._run_call(session, call)
        self.assertLess(time.monotonic() - started, 0.3 + runner.KILL_GRACE)
        reply = session.send_tool_response.call_args.kwargs["function_responses"]
        self.assertEqual(
            reply.response,
            {
                "ok": False,
                "message": "list_windows timed out after 0.3 s; nothing was confirmed.",
            },
        )

    async def test_the_conversation_cancelling_a_call_stops_it(self):
        adapter = GeminiLiveSession(Config())
        release = threading.Event()
        self.addCleanup(release.set)
        session = SimpleNamespace(send_tool_response=AsyncMock())
        call = types.FunctionCall(id="bg", name="list_windows", args={})
        with (
            patch(
                "omarchy_ai.voice.gemini_live.run_action",
                side_effect=lambda *a: release.wait(10),
            ),
            patch(
                "omarchy_ai.voice.gemini_live.LiveSession._current_window",
                return_value={},
            ),
        ):
            task = asyncio.create_task(adapter._run_call(session, call))
            for _ in range(100):
                if runner.default_runner().running(adapter):
                    break
                await asyncio.sleep(0.01)
            cancelled = runner.default_runner().cancel(
                owner=adapter, tags={"bg"}, reason="cancelled"
            )
            await asyncio.wait_for(task, 3)
        self.assertGreaterEqual(cancelled, 1)
        self.assertEqual(runner.default_runner().running(adapter), [])


if __name__ == "__main__":
    unittest.main()
