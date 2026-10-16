"""Tool-call dispatch for the desktop live session (gpt-live-1).

How one function call from the model is run and answered: calls run one at a
time, in the order received, behind an asyncio.Lock; each runs off the event
loop through run_in_executor and InputGuard; its outcome goes into the
per-window action log (get_recent_actions) and back to the model as a
function_call_output (_send_function_result).

Split out of voice/live.py; LiveSession inherits ToolDispatchMixin and still
owns the state these methods use (_tool_lock, _hangup, _action_log,
_input_guard, _dc, _demo_waiting_for_action, the watchdog flags).
"""
from __future__ import annotations

import asyncio
import json
import logging

from ..execution.actions import run_action
from . import watchdog

log = logging.getLogger("omarchy_ai.voice.live")


class ToolDispatchMixin:
    """LiveSession's tool-call execution path."""

    async def _run_tool_call(self, call_id: str, name: str, args: dict) -> None:
        if self._tool_lock is None:
            self._tool_lock = asyncio.Lock()
        async with self._tool_lock:
            if self._hangup.is_set():
                return
            await self._run_tool_call_serialized(call_id, name, args)

    async def _run_tool_call_serialized(self, call_id: str, name: str, args: dict) -> None:
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
        result = await loop.run_in_executor(None, self._input_guard.run, run_action, name, args)
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
