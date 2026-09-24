"""Full-duplex Gemini Live audio and desktop actions."""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
import uuid
import time
from pathlib import Path
import numpy as np
from ..core import updates
from ..core.history import append_session
from ..execution.actions import run_action, ActionResult
from ..execution.verified_input import InputGuard
from . import status_icon, watchdog
from .live import build_session_config, LiveSession
from .echo_cancel import EchoCancellation

log = logging.getLogger("omarchy_ai.voice.gemini")


# desktop_task and browser_task alone can run 25-90s (see their tool
# descriptions in execution/tools.py). Declaring every tool "behavior":
# "BLOCKING" (as this did before) is a Gemini Live API setting, not a model
# limitation: BLOCKING tells the server to pause ALL audio generation for
# this session until the function response arrives, so the assistant went
# fully silent for the entire action instead of talking/listening while it
# ran. Confirmed live via journalctl: real sessions logged multi-second
# "Gemini action started" -> "...finished" gaps with zero audio in between,
# and the instructions text already had to hack around it by telling the
# model to blurt "on it" right at the call site. Gemini Live's async
# function calling ("behavior": "NON_BLOCKING") exists specifically for
# this: the model keeps generating/listening immediately after the call,
# and the eventual FunctionResponse.scheduling (WHEN_IDLE here) controls
# how its result gets folded back in without cutting off other speech. See
# ai.google.dev/gemini-api/docs/live-guide#async-function-calling. Fast
# actions stay BLOCKING on purpose: for a one-shot volume/window toggle,
# waiting a fraction of a second for confirmation before continuing is the
# right, unsurprising behavior, and the model's own next tool call already
# depends on that confirmation for many of them.
# run_mission too: it narrates through this same session while it acts, so
# Gemini must keep generating audio while the call is open.
#
# The rest are "sub-agents": anything that can take seconds runs in the
# background so the user can always keep talking. A BLOCKING call makes
# Gemini stop talking AND listening until it returns; measured over two days
# of sessions (2026-09-23): myapi_call up to 11.2s, list_cast_targets 4.5s,
# myapi_service_methods 3.2s median, describe_screen 2.8s, open_browser
# 2.6s, start_casting 2.5s, list_commands 1.7s (Jev ranking), update check
# ~0.9s. Fast chained steps (focus_window -> type_text) stay BLOCKING: the
# next call depends on seeing the previous result.
NON_BLOCKING_ACTIONS = {
    "desktop_task", "browser_task", "run_mission",
    "describe_screen", "open_browser", "start_casting", "stop_casting", "list_cast_targets",
    "install_receiver_on_tv", "list_commands", "find_skill",
    "check_assistant_updates", "update_assistant", "get_release_notes", "report_issue",
    "myapi_list_services", "myapi_service_methods", "myapi_call",
    "myapi_gmail_search_attachments", "myapi_gmail_download_attachment",
}

# Output buffering for pw-play (see _play_audio). 40ms plus a lock-step
# writer produced audible gaps/clicks on this 2-core machine under load.
PLAYBACK_LATENCY = "100ms"
PLAYBACK_LEAD = 0.3


def build_live_config(config):
    shared = build_session_config(config)
    tools = [{"name": t["name"], "description": t["description"],
              "parameters_json_schema": t["parameters"],
              "behavior": "NON_BLOCKING" if t["name"] in NON_BLOCKING_ACTIONS else "BLOCKING"}
             for t in shared["delegation"]["responses"]["tools"]]
    return {"response_modalities": ["AUDIO"], "system_instruction": shared["instructions"],
            "input_audio_transcription": {}, "output_audio_transcription": {},
            # Reduce false speech starts from residual echo/noise while keeping
            # real barge-in and tolerating natural pauses in user speech.
            "realtime_input_config": {
                "automatic_activity_detection": {
                    "disabled": False,
                    "start_of_speech_sensitivity": "START_SENSITIVITY_LOW",
                    "end_of_speech_sensitivity": "END_SENSITIVITY_LOW",
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 600,
                },
                "activity_handling": "START_OF_ACTIVITY_INTERRUPTS",
            },
            "tools": [{"function_declarations": tools}]}


async def announce_update(session):
    notice = updates.wake_notice()
    if notice:
        await session.send_client_content(turns={"role": "user", "parts": [{"text":
            "[Automatic wake notice] Briefly announce this verified notice, including any offer "
            "to go over what's new (use get_release_notes if the user accepts), then listen: "
            + notice + " Do not install anything without an explicit user update request."}]}, turn_complete=True)


class GeminiLiveSession:
    # A conversation she started herself to report a heartbeat result ends
    # this long after she finished speaking if the user never answers; the
    # result then stays pending for the next conversation.
    PROACTIVE_SILENCE_SECONDS = 25

    def __init__(self, config, announcements=None):
        self.config = config
        self._announcements = asyncio.Queue()
        for entry in announcements or []:
            self._announcements.put_nowait(entry)
        self.proactive = bool(announcements)
        self._announced = []        # (inbox id, monotonic time said)
        self.user_spoke_at = 0.0
        self._hangup = asyncio.Event()
        self._transcript = []
        self._audio = asyncio.Queue(maxsize=250)
        self._calls = asyncio.Queue(maxsize=64)
        self._seen = set()
        self._cancelled = set()
        self._pending_calls = set()
        self._interrupted_calls = set()
        self._state = "listening"
        self._level = 0.0
        # Overlay inputs (see _display_state). Only run() owns the desktop
        # overlay; the phone bridge reuses _receive/_tools and must not
        # paint the desktop HUD, so this stays False there.
        self._overlay = False
        self._running_blocking = 0
        self._running_background = 0
        self._last_user_speech = 0.0
        self._awaiting_since = 0.0  # 0: no reply pending
        self._playback_until = 0.0  # monotonic time the queued speech finishes
        self._echo_levels = deque(maxlen=150)   # mic RMS while she speaks (~3s)
        self._barge_run = deque(maxlen=5)
        self._barge_open = False
        self._gated_frames = 0
        self._barge_count = 0
        self._splice_armed = False   # user words transcribed, no reply yet
        self._splice_until = 0.0
        self._loud_run = 0
        self._last_loud_at = 0.0
        self._spliced_at = 0.0
        self._splices = 0
        # Jev fast path (voice/jev_fast.py): the current utterance, whether
        # Jev has judged it, and what Jev last did (dedupe against Gemini).
        self._turn_done = asyncio.Event()  # set on every Gemini turn_complete
        self._utterance: list[str] = []
        self._utterance_checked = False
        self._jev_done = None  # (tool, args, monotonic time, message, text)
        self._gemini_inflight = []  # (tool, args, monotonic start) of Gemini's recent calls
        self._script_read_at = 0.0  # when read_file last returned a multi-step script
        self._generation = 0
        self._speaker = None
        self._speaker_generation = -1
        self._action_log = []
        self._input_guard = InputGuard()
        # desktop_task/browser_task run as detached tasks (NON_BLOCKING, see
        # build_live_config) instead of being awaited inline in _tools, so a
        # slow one can't hold up a fast action called right after it or the
        # conversation loop. Tracked here so run()'s shutdown can actually
        # cancel/await them instead of leaking a bare create_task().
        self._bg_tasks: set[asyncio.Task] = set()
        self._audit_session = uuid.uuid4().hex
        self.on_connected = None
        self.on_message = None
        self._echo = EchoCancellation()
        # Numeric diagnostics only: no microphone recording or speech text in
        # interruption logs. A short window captures noise just before barge-in.
        self._mic_levels = deque(maxlen=50)
        self._last_playback_at = None
        self._interruptions = 0
        self._received_audio_bytes = 0
        self._submitted_audio_bytes = 0

    async def _receive(self, session):
        while not self._hangup.is_set():
            received = False
            async for message in session.receive():
                received = True
                if self.on_message:
                    self.on_message(message)
                if message.tool_call_cancellation:
                    self._cancelled.update(message.tool_call_cancellation.ids or [])
                    from ..execution.browser_jev import cancel_browser_tasks
                    cancel_browser_tasks()
                if message.tool_call:
                    self._replied()
                    for call in message.tool_call.function_calls or []:
                        if call.name == "end_conversation":
                            from ..execution.browser_jev import cancel_browser_tasks
                            cancel_browser_tasks()
                            self._hangup.set()
                            return
                        if call.id not in self._seen:
                            self._seen.add(call.id)
                            self._pending_calls.add(call.id)
                            self._calls.put_nowait(call)
                server = message.server_content
                if not server:
                    continue
                for role, transcription in (("user", server.input_transcription), ("assistant", server.output_transcription)):
                    if transcription and transcription.text:
                        self._transcript.append({"role": role, "text": transcription.text})
                        if role == "user":
                            self._last_user_speech = time.monotonic()
                            # Her own voice leaking back is transcribed as the
                            # user ("Sure." right after she said it, 2026-09-23).
                            # Only speech after her audio ended is an answer.
                            if self._last_user_speech > self._playback_until + 0.3:
                                self.user_spoke_at = self._last_user_speech
                            self._awaiting_since = self._last_user_speech
                            if self._utterance_checked:
                                self._utterance, self._utterance_checked = [], False
                            self._utterance.append(transcription.text)
                            self._splice_armed = True
                if server.interrupted or server.turn_complete:
                    self._replied()
                if server.interrupted:
                    self._interruptions += 1
                    now = time.monotonic()
                    levels = [(rms, peak) for ts, rms, peak in self._mic_levels if now - ts <= .5]
                    log.warning(
                        "Gemini interrupted: session=%s count=%d state=%s queued_chunks=%d "
                        "mic_rms_500ms=%.1f mic_peak_500ms=%d playback_age_ms=%s "
                        "received_audio_bytes=%d submitted_audio_bytes=%d",
                        self._audit_session, self._interruptions, self._state, self._audio.qsize(),
                        max((rms for rms, _ in levels), default=0),
                        max((peak for _, peak in levels), default=0),
                        round((now - self._last_playback_at) * 1000) if self._last_playback_at is not None else None,
                        self._received_audio_bytes, self._submitted_audio_bytes,
                    )
                    from ..execution.desktop_jev import cancel_desktop_tasks
                    cancel_desktop_tasks()
                    # Speech interruption does not cancel a tool response in
                    # the protocol. Suppressing it can strand a BLOCKING call.
                    # Skip queued old work, but acknowledge it unless Gemini
                    # explicitly supplied that ID in tool_call_cancellation.
                    self._interrupted_calls.update(self._pending_calls)
                    self._generation += 1
                    while not self._audio.empty():
                        self._audio.get_nowait()
                    if self._speaker and self._speaker.returncode is None:
                        try:
                            self._speaker.terminate()
                        except ProcessLookupError:
                            pass
                    self._state, self._level = "listening", 0.0
                    self._playback_until = 0.0
                if server.model_turn:
                    for part in server.model_turn.parts or []:
                        if part.inline_data and part.inline_data.data:
                            self._replied()
                            self._awaiting_since = 0.0
                            self._received_audio_bytes += len(part.inline_data.data)
                            self._audio.put_nowait((self._generation, part.inline_data.data))
                if server.turn_complete or server.interrupted:
                    self._awaiting_since = 0.0
                if server.turn_complete:
                    self._turn_done.set()
                    log.info("Gemini turn complete: session=%s interrupted=%s queued_chunks=%d",
                             self._audit_session, bool(server.interrupted), self._audio.qsize())
            if not received:
                raise ConnectionError("Gemini receive stream closed without a turn")

    async def _tools(self, session):
        from google.genai import types
        while True:
            call = await self._calls.get()
            if call.id in self._cancelled:
                self._pending_calls.discard(call.id)
                continue
            if call.id in self._interrupted_calls:
                await session.send_tool_response(function_responses=types.FunctionResponse(
                    id=call.id, name=call.name, response={"ok": False,
                    "message": "Not executed: user interrupted before this action started. Wait for the current request."}))
                self._pending_calls.discard(call.id)
                continue
            if call.name == "end_conversation":
                await session.send_tool_response(function_responses=types.FunctionResponse(
                    id=call.id, name=call.name, response={"ok": True}))
                self._hangup.set()
                return
            self._pending_calls.add(call.id)
            if call.name in NON_BLOCKING_ACTIONS:
                # Declared NON_BLOCKING in build_live_config: the server does
                # not wait for this response before letting the model keep
                # talking, so this loop shouldn't wait either -- dispatch and
                # immediately go back to pulling the next call (a fast action
                # the model asks for in the meantime, or a second background
                # task) instead of serializing behind a 25-90s action.
                task = asyncio.create_task(self._run_call(session, call))
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)
            else:
                await self._run_call(session, call)

    # Gemini's own call for the same command, arriving after Jev acted.
    JEV_DEDUPE_SECONDS = 6

    async def _fast_path(self):
        """Judge each finished utterance with Jev; run a clear simple command
        at once. Gemini still hears everything and answers as usual."""
        from . import jev_fast
        while True:
            await asyncio.sleep(0.1)
            if not self._utterance or self._utterance_checked:
                continue
            if time.monotonic() - self._last_user_speech < self.USER_PAUSE_SECONDS:
                continue
            self._utterance_checked = True
            text = " ".join("".join(self._utterance).split())
            started = time.monotonic()
            decision = await asyncio.to_thread(jev_fast.decide, text)
            if decision is None:
                log.info("Jev fast path: no fast command in %r (%.0fms)", text[:120], (time.monotonic() - started) * 1000)
                continue
            tool, args, evidence = decision
            # Gemini sometimes gets there first (real: 00:40:40.57 Gemini vs
            # 00:40:41.14 Jev, and the window move ran twice). Never repeat it.
            recent = [c for c in self._gemini_inflight if time.monotonic() - c[2] < self.JEV_DEDUPE_SECONDS]
            if any(name == tool and a == args for name, a, _ in recent):
                log.info("Jev fast path: %r -> %s %s already requested by Gemini; not repeated", text[:120], tool, args)
                continue
            if self._overlay:
                await asyncio.to_thread(watchdog.tool_call, tool, args)
            result = await asyncio.to_thread(jev_fast.execute, tool, args)
            self._jev_done = (tool, args, time.monotonic(), result.message, text) if result.ok else None
            log.info("Jev fast path: %r -> %s %s ok=%s %r evidence=%s total=%.0fms", text[:120], tool, args,
                     result.ok, result.message[:160], evidence, (time.monotonic() - started) * 1000)
            if self._overlay:
                await asyncio.to_thread(watchdog.tool_result, tool, args, result.ok, result.message)
            self._action_log.append({"action": tool, "args": args, "ok": result.ok, "message": result.message,
                                     "call_id": "jev-fast", "ts": time.time(), "window": None})

    def _jev_already(self, call):
        """A response for Gemini's call if Jev just handled this request."""
        done = self._jev_done
        if not done or time.monotonic() - done[2] > self.JEV_DEDUPE_SECONDS:
            return None
        tool, args, _, message, text = done
        if call.name != tool:
            return None
        if dict(call.args or {}) == args:
            return {"ok": True, "message": f"Already done by the Jev fast path: {message}. Just confirm it."}
        # Same tool, different arguments: the 2026-09-23 "asked for 4, got 5"
        # flip-flop. Jev followed the user's transcribed words.
        return {"ok": False, "message": (f"Not executed: the Jev fast path already did {tool} {args} for the user's "
                                         f"words {text!r} ({message}). If the user wants something else, ask them.")}

    def announce(self, entry: dict) -> None:
        """A heartbeat result arrived while this conversation is open."""
        self._announcements.put_nowait(entry)

    def acknowledged_ids(self) -> list[str]:
        """Results the user heard AND answered (spoke after them)."""
        return [i for i, said in self._announced if self.user_spoke_at > said]

    def _idle(self) -> bool:
        now = time.monotonic()
        return (self._display_state(now) == "listening" and now - self._last_user_speech > 1.5
                and not self._running_blocking)

    async def _announcer(self, session):
        """Say heartbeat results (Jev's watches, reminders, finished tasks)
        without talking over anyone; end a self-started conversation that
        nobody answers."""
        from google.genai import types
        while not self._hangup.is_set():
            try:
                entry = await asyncio.wait_for(self._announcements.get(), 0.5)
            except asyncio.TimeoutError:
                entry = None
            if entry is not None:
                while not self._idle():
                    await asyncio.sleep(0.2)
                what = f"{entry.get('title')}: {entry.get('detail', '')[:500]}"
                opener = ("You started this conversation yourself because a scheduled check finished. "
                          if self.proactive and not self._announced else "")
                await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=(
                    f"[Heartbeat result, automatic] {opener}Tell the user now, briefly, in their language: {what}. "
                    "Then ask if they got it. If they do not answer, say nothing more."))]), turn_complete=True)
                self._announced.append((entry.get("id"), time.monotonic()))
                log.info("Heartbeat result announced: session=%s id=%s title=%r", self._audit_session,
                         entry.get("id"), entry.get("title"))
                continue
            if self.proactive and self._announced and self.user_spoke_at <= self._announced[0][1]:
                quiet_since = max(self._announced[-1][1], self._playback_until)
                if time.monotonic() - quiet_since > self.PROACTIVE_SILENCE_SECONDS:
                    log.info("Heartbeat announcement unanswered for %ds; ending and keeping it pending",
                             self.PROACTIVE_SILENCE_SECONDS)
                    self._hangup.set()
                    return

    # Read-only calls that may legitimately come between reading a script
    # and calling run_mission.
    _MISSION_NEUTRAL = {"read_file", "list_files", "list_windows", "list_cast_targets", "get_recent_actions",
                        "search_os_knowledge", "find_skill", "run_mission", "get_update_status"}

    def _mission_redirect(self, call):
        """Real Gemini, 1 run in 5 even with the read_file note: it read
        commercial_prompt.md and started the steps one tool at a time again.
        Block that first action once and point it to run_mission."""
        if call.name == "run_mission":
            self._script_read_at = 0.0
            return None
        if not self._script_read_at or time.monotonic() - self._script_read_at > 60 or call.name in self._MISSION_NEUTRAL:
            return None
        self._script_read_at = 0.0  # once: the user may really want a single step
        return ActionResult(False, "Not executed: you just read a multi-step script. Perform it with run_mission "
                                   "(one call, all steps with say + action, its exact targets and workspace), "
                                   "not step by step. If the user only wants this single step, call it again.")

    NARRATION_WAIT_SECONDS = 20

    async def _narrate(self, session, text: str, index: int, total: int) -> None:
        from google.genai import types
        self._turn_done.clear()
        await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=(
            f"[Mission narration, step {index} of {total}] Say this to the user now, in your own voice, in the "
            f"user's language, briefly and naturally. Call no tools; the step's action is already running: {text}"))]),
            turn_complete=True)

    async def _run_mission(self, session, args: dict) -> ActionResult:
        """Say each step's line while its action runs; verify; stop on failure."""
        from ..execution import missions

        def planner(system, user):
            from .omarchy import GatewayClient
            return GatewayClient(self.config).complete_json(system, user)
        try:
            steps, workspace, notes = await asyncio.to_thread(
                missions.validate, args, getattr(self, "_script_text", None), planner)
        except (ValueError, TypeError) as exc:
            return ActionResult(False, f"Mission NOT started: {exc}")
        if notes:
            log.info("Mission plan repaired: %s", "; ".join(notes))
        done = []
        for i, step in enumerate(steps, 1):
            if self._hangup.is_set():
                break
            if workspace is not None:
                where = await asyncio.to_thread(missions.ensure_workspace, workspace)
                if not where.ok:
                    return ActionResult(False, f"Mission stopped before step {i}: {where.message}. Completed: {done}")
            if step["say"]:
                await self._narrate(session, step["say"], i, len(steps))
            if self._overlay and step["action"] != "say":
                await asyncio.to_thread(watchdog.tool_call, step["action"], step["args"])
            started = time.monotonic()
            result = await asyncio.to_thread(missions.run_step, step)   # runs while she speaks
            log.info("Mission step %d/%d %s %s ok=%s %.0fms %r", i, len(steps), step["action"], step["args"],
                     result.ok, (time.monotonic() - started) * 1000, result.message[:200])
            if self._overlay and step["action"] != "say":
                await asyncio.to_thread(watchdog.tool_result, step["action"], step["args"], result.ok, result.message)
            if not result.ok:
                return ActionResult(False, (
                    f"Mission stopped at step {i} ({step['action']} {json.dumps(step['args'], ensure_ascii=False)}): "
                    f"{result.message}. Completed steps: {done}. Tell the user exactly this and ask how to continue. "
                    "Do NOT substitute another target or improvise a replacement step."))
            done.append(f"{i}: {step['action']} ok")
            if step["say"]:
                try:  # let this line finish before the next one starts
                    await asyncio.wait_for(self._turn_done.wait(), self.NARRATION_WAIT_SECONDS)
                except asyncio.TimeoutError:
                    pass
        extra = f" Notes: {'; '.join(notes)}." if notes else ""
        return ActionResult(True, f"Mission completed: all {len(steps)} steps done and verified ({done}).{extra} "
                                  "Briefly tell the user it is finished. Do not redo any step yourself.")

    async def _run_call(self, session, call) -> None:
        from google.genai import types
        log.info("Gemini action started: session=%s call=%s name=%s args=%r",
                 self._audit_session, call.id, call.name, call.args or {})
        self._gemini_inflight = self._gemini_inflight[-20:] + [(call.name, dict(call.args or {}), time.monotonic())]
        background = call.name in NON_BLOCKING_ACTIONS
        if background:
            self._running_background += 1
        else:
            self._running_blocking += 1
        if self._overlay:
            await asyncio.to_thread(watchdog.tool_call, call.name, call.args or {})
        try:
            already = self._jev_already(call)
            redirect = self._mission_redirect(call)
            if already is not None:
                result = ActionResult(already["ok"], already["message"])
            elif redirect is not None:
                result = redirect
            elif call.name == "run_mission":
                result = await self._run_mission(session, call.args or {})
            elif call.name == "get_recent_actions":
                result = ActionResult(True, LiveSession._get_recent_actions(self, (call.args or {}).get("target")))
            else:
                window = await asyncio.to_thread(LiveSession._current_window)
                action = asyncio.create_task(asyncio.to_thread(self._input_guard.run, run_action, call.name, call.args or {}))
                try:
                    result = await asyncio.shield(action)
                except asyncio.CancelledError:
                    # Cancellation cannot stop an OS action already running.
                    # Finish it before allowing the next conversation.
                    await action
                    raise
                if call.name == "read_file" and result.ok and "[assistant note] This file is a multi-step script" in result.message:
                    self._script_read_at = time.monotonic()
                    # The planner fills missing step details from the script itself.
                    self._script_text = result.message.split("\n\n[assistant note]", 1)[0]
                if call.name == "close_window" and result.ok:
                    self._action_log = [e for e in self._action_log if e["window"] != window]
                else:
                    self._action_log.append({"action": call.name, "args": call.args or {}, "ok": result.ok, "message": result.message, "call_id": call.id, "ts": time.time(), "window": window})
                    self._action_log = self._action_log[-100:]
            response = {"ok": result.ok, "message": result.message}
        except Exception:
            log.exception("Gemini action failed: %s", call.name)
            response = {"ok": False, "message": "Action failed; do not assume completion."}
        log.info("Gemini action finished: session=%s call=%s name=%s ok=%s result_chars=%d message=%r",
                 self._audit_session, call.id, call.name, response["ok"],
                 len(response["message"]), response["message"][:1800])
        if background:
            self._running_background -= 1
        else:
            self._running_blocking -= 1
            # The model now composes its reply to this result: still busy.
            self._awaiting_since = time.monotonic()
        if self._overlay:
            await asyncio.to_thread(watchdog.tool_result, call.name, call.args or {}, response["ok"], response["message"])
        if call.id not in self._cancelled:
            kwargs = {}
            if call.name in NON_BLOCKING_ACTIONS:
                # WHEN_IDLE (not INTERRUPT): fold the result in once the
                # model naturally has nothing else to say, rather than
                # cutting off whatever it's telling the user at that moment
                # just because this background action happened to finish.
                kwargs["scheduling"] = types.FunctionResponseScheduling.WHEN_IDLE
            await session.send_tool_response(function_responses=types.FunctionResponse(
                id=call.id, name=call.name, response=response, **kwargs))
            log.info("Gemini tool response delivered: session=%s call=%s interrupted=%s",
                     self._audit_session, call.id, call.id in self._interrupted_calls)
        self._pending_calls.discard(call.id)

    # Self-interruption guard. Real session 2026-09-23 13:45: she was cut off
    # four times mid-sentence by her OWN voice leaking back through the mic
    # (echo transcribed as the user: "because the", "Because the", "because
    # the"), losing ~22s of speech. While her audio plays (+ a short tail) the
    # mic stream carries silence instead, unless it is clearly the user
    # barging in: sustained loudness well above her echo. Logged user
    # interruptions measured RMS 5100-12000; her echo 1700-4600.
    ECHO_TAIL_SECONDS = 0.35
    BARGE_FLOOR_RMS = 6000.0
    BARGE_FRAMES = 5            # 5 x 20ms of sustained loud speech

    # Stuck-turn guard. Real sessions 2026-09-23 (23:38, 23:44 and five
    # more over a day): the user's words were transcribed, then no reply for
    # 40-108s; repeats were transcribed too and finally answered together as
    # ONE turn -- Gemini's end-of-speech detection never closed the turn.
    # Reproduced against the live API (STATUS.md): steady room noise, even
    # at 60% mic gain, is answered in <1s, but background SPEECH-LIKE sound
    # (TV, people, at a tenth of the user's level) keeps the turn open for
    # 40s+; audio_stream_end does not close it. 0.8s of digital silence does,
    # reply ~0.9s later. So once the user's words have stopped for
    # SPLICE_AFTER_SECONDS with no reply started, send silence -- and keep
    # it until her reply starts (at most SPLICE_SECONDS): a louder background
    # talker otherwise counts as a new user turn and cuts her reply off
    # before it starts (probe: closed 3 times, never answered). That is the
    # same rule as while she speaks: only the user's own loud voice passes.
    # "The user's words" are both transcripts and runs of speech-loud mic
    # frames: under background speech the live API also withheld the
    # transcript until the turn closed.
    # With a clean room Gemini's own 600ms end-of-speech has already fired
    # by then, so this only ever acts on a turn that is stuck anyway. A
    # few frames that could be the user talking again (logged user speech
    # 5100+, room at session gain well below; one frame is just a keyboard
    # click) end or prevent the splice: a missed splice is only the old
    # behavior, a clipped sentence is worse.
    SPLICE_AFTER_SECONDS = 1.5
    SPLICE_SECONDS = 4.0
    SPLICE_ABORT_RMS = 3500.0
    SPLICE_ABORT_FRAMES = 3

    def _replied(self) -> None:
        if self._spliced_at:
            log.info("Stuck-turn guard: reply %.1fs after the silence splice", time.monotonic() - self._spliced_at)
            self._spliced_at = 0.0
        self._splice_armed = False
        self._splice_until = 0.0

    def _splice(self, chunk: bytes, rms: float, now: float, speaking: bool) -> bytes | None:
        """Silence to send instead of this frame, if a stuck turn needs closing."""
        loud = rms >= self.SPLICE_ABORT_RMS
        self._loud_run = self._loud_run + 1 if loud else 0
        if self._loud_run >= self.SPLICE_ABORT_FRAMES and not speaking:
            # The user talking (again): wait for them to stop first.
            self._last_loud_at, self._splice_armed, self._splice_until = now, True, 0.0
            return None
        if self._splice_until:
            if now < self._splice_until:
                return bytes(len(chunk))
            self._splice_until = 0.0
            return None
        if not self._splice_armed or speaking or loud:
            return None
        last_words = max(self._last_user_speech, self._last_loud_at)
        if now - last_words < self.SPLICE_AFTER_SECONDS:
            return None
        levels = sorted(r for ts, r, _ in self._mic_levels if now - ts <= 1.0)
        log.info("Stuck-turn guard: no reply %.1fs after the user's last words; sending silence until she "
                 "replies, at most %.1fs "
                 "(mic_rms_1s p50=%.0f max=%.0f)", now - last_words, self.SPLICE_SECONDS,
                 levels[len(levels) // 2] if levels else 0, levels[-1] if levels else 0)
        self._splice_armed = False
        self._splice_until = now + self.SPLICE_SECONDS
        self._spliced_at = now
        self._splices += 1
        return bytes(len(chunk))

    def _gate(self, chunk: bytes, rms: float, now: float) -> list[bytes]:
        """Chunks to send for this mic frame (silence while she speaks)."""
        speaking = now < self._playback_until + self.ECHO_TAIL_SECONDS
        silence = self._splice(chunk, rms, now, speaking)
        if silence is not None:
            return [silence]
        if not speaking:
            self._barge_run.clear()
            self._barge_open = False
            return [chunk]
        if self._barge_open:
            return [chunk]
        echo = sorted(self._echo_levels)[int(len(self._echo_levels) * 0.9)] if self._echo_levels else 0.0
        threshold = max(self.BARGE_FLOOR_RMS, 1.6 * echo)
        if rms < threshold:
            # Only quieter frames describe her echo; the user's own loud
            # frames must not raise the bar they are measured against.
            self._echo_levels.append(rms)
        if rms >= threshold:
            self._barge_run.append(chunk)
            if len(self._barge_run) >= self.BARGE_FRAMES:
                # Clearly the user: let it through, onset included.
                self._barge_open = True
                self._barge_count += 1
                held, self._barge_run = list(self._barge_run), deque(maxlen=self.BARGE_FRAMES)
                return held
        else:
            self._barge_run.clear()
        self._gated_frames += 1
        return [bytes(len(chunk))]

    async def _send_audio(self, session, mic):
        from google.genai import types
        while True:
            chunk = await mic.stdout.read(640)
            if not chunk:
                raise RuntimeError("Microphone stream ended")
            samples = np.frombuffer(chunk[:len(chunk) // 2 * 2], dtype="<i2").astype(np.float32)
            rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
            if samples.size:
                self._mic_levels.append((time.monotonic(), rms, int(np.max(np.abs(samples)))))
            for part in self._gate(chunk, rms, time.monotonic()):
                await session.send_realtime_input(audio=types.Blob(data=part, mime_type="audio/pcm;rate=16000"))

    @staticmethod
    async def _stop_process(process):
        if process is None:
            return
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), 2)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def _play_audio(self):
        while True:
            generation, raw = await self._audio.get()
            if generation != self._generation:
                continue
            if self._speaker is None or self._speaker.returncode is not None or generation != self._speaker_generation:
                await self._stop_process(self._speaker)
                self._speaker = await asyncio.create_subprocess_exec(
                    "pw-play", "--rate", "24000", "--channels", "1", "--format", "s16",
                    "--latency", PLAYBACK_LATENCY, "--target", self._echo.sink, "-a", "-", stdin=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL)
                self._speaker_generation = generation
                if generation != self._generation:
                    continue
            try:
                self._speaker.stdin.write(raw)
                await self._speaker.stdin.drain()
                self._last_playback_at = time.monotonic()
                self._submitted_audio_bytes += len(raw)
            except (BrokenPipeError, ConnectionResetError):
                if generation != self._generation:
                    continue
                raise
            self._state = "speaking"
            samples = np.frombuffer(raw[:len(raw) // 2 * 2], dtype="<i2").astype(float)
            self._level = min(1.0, float(np.sqrt(np.mean(samples * samples))) / 12000) if samples.size else 0
            # Stay PLAYBACK_LEAD ahead of the speaker instead of sleeping each
            # chunk's full duration. pw-play reads the pipe when the audio
            # graph needs data; the old lock-step writer left it empty at
            # exactly that moment, so any scheduling delay became a gap or
            # click. Measured on real Gemini audio (STATUS.md 2026-09-23):
            # 1.7-2.1 gaps/s of speech before, 0-0.2/s after -- the same as
            # playing the file directly. Interruptions still stop at once:
            # they kill the pw-play process, cushion included.
            now = time.monotonic()
            self._playback_until = max(self._playback_until, now) + len(raw) / 48000
            ahead = self._playback_until - now - PLAYBACK_LEAD
            if ahead > 0:
                await asyncio.sleep(ahead)

    # Silence after the last transcribed user words before "you finished,
    # she is working on it" -- Gemini's own end-of-speech wait is 600ms.
    USER_PAUSE_SECONDS = 0.7
    # Noise can be transcribed without Gemini answering at all; never stay
    # "thinking" forever on a reply that is not coming.
    REPLY_WAIT_SECONDS = 15

    def _display_state(self, now=None):
        """What the overlay shows. Gemini Live itself only exposes audio
        arriving, so "thinking" is derived: a tool she must wait for is
        running (MyApi, reading files, ...), you have stopped talking and
        her reply has not started, or a background desktop/browser task is
        still working. While you are mid-sentence it stays "listening"."""
        now = time.monotonic() if now is None else now
        if self._state == "speaking":
            if now < self._playback_until:
                return "speaking"
            # The cushion has played out and nothing new was queued.
            self._state, self._level = "listening", 0.0
        if now - self._last_user_speech < self.USER_PAUSE_SECONDS:
            return "listening"
        awaiting = self._awaiting_since and now - self._awaiting_since < self.REPLY_WAIT_SECONDS
        if self._running_blocking or awaiting or self._running_background:
            return "thinking"
        return "listening"

    async def _visuals(self):
        previous = None
        while True:
            state = self._display_state()
            if state != previous:
                await asyncio.to_thread(watchdog.state, state)
                previous = state
            await asyncio.to_thread(watchdog.level, self._level)
            await asyncio.sleep(0.1)

    async def run(self):
        from google import genai
        client = genai.Client(api_key=Path(self.config.gemini_api_key_path).read_text().strip())
        mic = None
        tasks = []
        try:
            # Do not silently fall back to a raw mic: that reintroduces
            # self-interruptions while claiming full-duplex audio works.
            if self.config.watchdog_enabled:
                # Open the overlay before echo-cancel setup and the Live
                # handshake so "connecting" is visible for that whole wait
                # (it used to open only after connecting, so it never showed).
                self._overlay = True
                await asyncio.to_thread(watchdog.start, self.config.watchdog_display_mode)
                await asyncio.to_thread(watchdog.state, "connecting")
            await self._echo.start(self.config.mic_device, self.config.gemini_mic_volume_percent)
            async with client.aio.live.connect(model=self.config.gemini_model, config=build_live_config(self.config)) as session:
                argv = ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", "--latency", "20ms"]
                argv.extend(["--target", self._echo.source])
                mic = await asyncio.create_subprocess_exec(*argv, "-a", "-", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                log.info("Gemini Live connected: %s; full-duplex tools enabled", self.config.gemini_model)
                log.info("Gemini speech detection: start=LOW end=LOW prefix=300ms silence=600ms; real interruptions enabled")
                if self.on_connected:
                    self.on_connected()
                await asyncio.to_thread(status_icon.set_live, True)
                workers = [self._send_audio(session, mic), self._receive(session), self._play_audio(), self._tools(session), self._hangup.wait()]
                if self.config.watchdog_enabled:
                    workers.append(self._visuals())
                if self.config.jev_fast_path:
                    workers.append(self._fast_path())
                workers.append(self._announcer(session))
                tasks = [asyncio.create_task(worker) for worker in workers]
                await announce_update(session)
                done, _ = await asyncio.wait(tasks, timeout=self.config.max_session_seconds, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        finally:
            log.info("Gemini session audio summary: session=%s interruptions=%d received_audio_bytes=%d submitted_audio_bytes=%d "
                     "echo_gated_frames=%d user_barge_ins=%d stuck_turn_splices=%d",
                     self._audit_session, self._interruptions, self._received_audio_bytes, self._submitted_audio_bytes,
                     self._gated_frames, self._barge_count, self._splices)
            from ..execution.browser_jev import cancel_browser_tasks
            cancel_browser_tasks()
            self._hangup.set()
            for task in tasks:
                task.cancel()
            for task in self._bg_tasks:
                task.cancel()
            await asyncio.gather(*tasks, *self._bg_tasks, return_exceptions=True)
            await self._stop_process(mic)
            await self._stop_process(self._speaker)
            try:
                await self._echo.close()
            except Exception:
                log.exception("Could not unload session echo cancellation")
            await client.aio.aclose()
            await asyncio.to_thread(status_icon.set_live, False)
            if self._overlay:
                self._overlay = False
                await asyncio.to_thread(watchdog.stop)
            await asyncio.to_thread(append_session, self._transcript, self.config.context_retention_hours)
