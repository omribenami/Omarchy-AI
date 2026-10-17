"""Tool-call dispatch for the desktop live session (gpt-live-1).

How one function call from the model is run and answered. Calls are ordered
by the resource their tool holds (execution/tools.py RESOURCES, through
execution/runner.Resources): mutating calls on one resource run one at a
time in the order received, as every call once did behind a single lock;
read-only calls run side by side and never overtake a mutation received
before them. Each call runs through InputGuard on the ToolRunner, with its
tool's deadline: a call that hangs is answered "timed out after N s; nothing
was confirmed" instead of holding the session. Its outcome goes into the
per-window action log (get_recent_actions) and back to the model as a
function_call_output (_send_function_result).

Split out of voice/live.py; LiveSession inherits ToolDispatchMixin and still
owns the state these methods use (_resources, _hangup, _action_log,
_input_guard, _dc, _demo_waiting_for_action, the watchdog flags).
"""
from __future__ import annotations

import asyncio
import json
import logging

from ..core import logs
from ..execution import runner
from ..execution.actions import run_action
from . import watchdog

log = logging.getLogger("omarchy_ai.voice.live")


class ToolDispatchMixin:
    """LiveSession's tool-call execution path."""

    async def _run_tool_call(self, call_id: str, name: str, args: dict) -> None:
        if self._resources is None:
            self._resources = runner.Resources()
        tool = runner.spec(name)
        async with self._resources.hold(tool.resource, exclusive=not tool.reads):
            if self._hangup.is_set():
                return
            # Its log lines, and its tool's (worker included), carry these (core/logs.py).
            with logs.bind(session_id=getattr(self, "_audit_session", None),
                           turn_id=getattr(self, "_turn_id", None), tool_call_id=call_id):
                await self._run_tool_call_ordered(call_id, name, args)

    async def _run_tool_call_ordered(self, call_id: str, name: str, args: dict) -> None:
        if self._demo_waiting_for_action.is_set():
            self._demo_waiting_for_action.clear()
            log.info("demo: first real action %s; enabling assistant audio", name)
        loop = asyncio.get_event_loop()
        window = await loop.run_in_executor(None, self._current_window)
        log.info("tool call: %s(%s)", name, args)
        # "thinking/calling a tool" per the watchdog overlay's state
        # vocabulary — reuses this call's own name/args, no new tracking.
        self._set_watchdog_state("thinking")
        if self._watchdog_on:
            watchdog.tool_call(name, args)
        result = await runner.default_runner().arun(
            name, args, execute=run_action, wrap=self._input_guard.run, owner=self, tag=call_id)
        log.info("tool result: ok=%s message=%r", result.ok, result.message)
        if self._watchdog_on:
            watchdog.tool_result(name, args, result.ok, result.message)
        if name == "close_window" and result.ok:
            # That window no longer exists — drop its whole history rather
            # than let get_recent_actions keep recalling a closed tile
            # (user request: no reason to retain it).
            self._action_log = [
                e for e in self._action_log if e.get("window") != window
            ]
        else:
            self._action_log.append(
                {"action": name, "args": args, "ok": result.ok, "message": result.message, "window": window}
            )
        self._send_function_result(call_id, result.ok, result.message)

    @staticmethod
    def _current_window() -> dict:
        try:
            r = run_action("list_windows", {})
            if r.ok:
                for w in json.loads(r.message):
                    if w.get("focused"):
                        return {"app": w.get("app"), "title": w.get("title")}
        except Exception:  # noqa: BLE001
            log.debug("failed to resolve current window", exc_info=True)
        return {}

    def _get_recent_actions(self, target: str | None) -> str:
        entries = self._action_log
        if target:
            needle = target.lower()
            entries = [
                e for e in entries
                if needle in (e.get("window", {}).get("app") or "").lower()
                or needle in (e.get("window", {}).get("title") or "").lower()
            ]
        if not entries:
            return json.dumps([])
        return json.dumps(entries[-20:])

    def _send_function_result(self, call_id: str, ok: bool, message: str) -> None:
        if self._dc is None:
            return
        # "conversation.item.create" (the older Realtime API's convention)
        # is rejected on this API — confirmed live, the error response
        # listed the real supported event types, "response.item.create"
        # among them. Same item shape, corrected event name.
        payload = {
            "type": "response.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps({"ok": ok, "message": message}),
            },
        }
        try:
            self._dc.send(json.dumps(payload))
            # Submitting the result doesn't make the model continue on its
            # own — confirmed live: no error, but also no reaction at all
            # for 6+ seconds until this was added. Same "explicit trigger
            # needed" pattern as the very first response.
            self._dc.send(json.dumps({"type": "response.create"}))
        except Exception:  # noqa: BLE001
            log.debug("failed to send function_call_output", exc_info=True)
