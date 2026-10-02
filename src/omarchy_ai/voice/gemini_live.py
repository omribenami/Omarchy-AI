"""Full-duplex Gemini Live audio and desktop actions."""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
import uuid
import threading
import time
from pathlib import Path
import numpy as np
from .. import myapi
from ..core import alert_clips, updates
from ..core.history import append_session
from ..execution import catalog
from ..execution.actions import run_action, ActionResult
from ..execution.verified_input import InputGuard, writes_externally
from . import status_icon, watchdog
from .tv_mic import Receiver as TvMicReceiver
from .live import build_session_config, LiveSession
from .echo_cancel import EchoCancellation
from . import escalation, fallback, switchboard
from .myapi_refinement import refine_myapi_result

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
    "myapi_list_services", "myapi_vault_list", "myapi_service_methods", "myapi_call", "myapi_write",
    "myapi", "myapi_execute", "myapi_gmail_send", "myapi_gmail_reply",
    "myapi_gmail_search", "myapi_gmail_search_attachments", "myapi_gmail_download_attachment",
    # The catalog holds slow tools (casting, MyApi, issues); its quick ones
    # just report when she is idle.
    "use_tool",
}

# What each tool does, for the switchboard's question.
from ..execution.tools import TOOLS as _TOOLS  # noqa: E402
_TOOL_TEXT = {t["name"]: t["description"] for t in _TOOLS}

# Output buffering for pw-play (see _play_audio). 40ms plus a lock-step
# writer produced audible gaps/clicks on this 2-core machine under load.
PLAYBACK_LATENCY = "100ms"
PLAYBACK_LEAD = 0.3

# Gemini Live closes the whole session with websocket code 1007 when a
# function response is too large. MyApi can legitimately return hundreds of
# kilobytes for one item (a Gmail message includes transport headers, MIME
# bodies, and provider metadata), so bound every action at the protocol edge.
# Keep both ends: JSON responses commonly put the requested data near the
# front and status/provider metadata near the end.
MAX_TOOL_RESPONSE_CHARS = 40_000


def _bounded_tool_message(message: str) -> str:
    if len(message) <= MAX_TOOL_RESPONSE_CHARS:
        return message
    marker = (
        f"\n...[tool result truncated from {len(message):,} characters. "
        "Use a narrower query if more detail is needed]...\n"
    )
    available = MAX_TOOL_RESPONSE_CHARS - len(marker)
    head = available // 2
    return message[:head] + marker + message[-(available - head):]


def build_live_config(config):
    shared = build_session_config(config)
    declared = shared["delegation"]["responses"]["tools"]
    instructions = shared["instructions"]
    if getattr(config, "tool_picker", False):
        myapi_on = getattr(config, "myapi_enabled", False) and myapi.is_connected()
        declared = [t for t in declared if t["name"] == "end_conversation"] + catalog.declared(myapi_on)
        instructions += (
            "\n\nTOOL CATALOG: Only your most used tools are declared directly. Every other tool named in these "
            "instructions (start_casting, set_reminder, run_omarchy_command, report_issue, list_commands, "
            "submit_sudo_password, browser_control, schedule_task, and the rest) is reached with use_tool: pass "
            "`name` and `args` when you know them, or just `request` and Jev picks the right tool in a fraction "
            "of a second. Never tell the user you lack a tool before asking use_tool. Catalog (* = required): "
            + catalog.signatures(myapi_on))
    tools = [{"name": t["name"], "description": t["description"],
              "parameters_json_schema": t["parameters"],
              "behavior": "NON_BLOCKING" if t["name"] in NON_BLOCKING_ACTIONS else "BLOCKING"}
             for t in declared]
    return {"response_modalities": ["AUDIO"], "system_instruction": instructions,
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
            # No built-in {"google_search": {}}: on this key it fails the whole
            # connect with "1011 You exceeded your current quota" (probed
            # 2026-09-26; the same config without it connects fine).
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
        self._announcements_said = []  # their text: the "request" behind calls she makes to report them
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
        self.from_paired_phone = False  # set by phone/gemini.py; gates unlock_screen
        self._offers_turn, self._offers = 0.0, 0  # use_tool offers in this user turn (_offer_result)
        self._awaiting_since = 0.0  # 0: no reply pending
        self._playback_until = 0.0  # monotonic time the queued speech finishes
        self._echo_levels = deque(maxlen=150)   # mic RMS while she speaks (~3s)
        self._barge_run = deque(maxlen=5)
        self._barge_open = False
        self._gated_frames = 0
        self._barge_count = 0
        self._splice_armed = False   # user words transcribed, no reply yet
        self._splice_abort_rms = self.SPLICE_ABORT_RMS  # the phone bridge sets its own (phone/gemini.py)
        self._splice_until = 0.0
        self._loud_run = 0
        self._speech_levels = deque(maxlen=200)  # rms of loud-run frames, logged for calibration
        self._last_loud_at = 0.0
        self._spliced_at = 0.0
        self._force_close_at = 0.0
        self._splices = 0
        self._working = {}  # background call id -> what/started/heard_at/noted/notes (_task_status)
        self._last_audio_at = 0.0
        self._last_turn_done_at = 0.0
        self._forced_turn_closures = 0
        # Jev fast path (voice/jev_fast.py): the current utterance, whether
        # Jev has judged it, and what Jev last did (dedupe against Gemini).
        self._turn_done = asyncio.Event()  # set on every Gemini turn_complete
        self._utterance: list[str] = []
        self._password_seen = ""
        self._utterance_checked = False
        self._jev_done = None  # (tool, args, monotonic time, message, text)
        # Switchboard (voice/switchboard.py): the fast pass's route for the
        # latest request, and the second pass that decides every tool call.
        self._route_hint = None  # {"route", "p", "request", "at"}
        self._proactive_task_turn = ""  # latest request handed to Task Runtime by the fast route
        self._switchboard = switchboard.Switchboard()
        # Escalation (voice/escalation.py): repeated failures or giving up
        # hand the job to the Task Runtime without waiting for her to.
        self._escalator = escalation.Escalator()
        self._escalation_check = None
        self._escalation_again = False
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
        # Session-scoped capabilities (for example phone-chat file sharing).
        # They are declared only by that session and never enter the global action catalog.
        self.extra_tools: dict[str, object] = {}
        self._echo = EchoCancellation()
        # Numeric diagnostics only: no microphone recording or speech text in
        # interruption logs. A short window captures noise just before barge-in.
        self._mic_levels = deque(maxlen=50)
        self._last_playback_at = None
        self._interruptions = 0
        self._received_audio_bytes = 0
        self._submitted_audio_bytes = 0
        # TV microphone while casting (see _send_audio); levels logged at the
        # end to calibrate the echo/barge-in gate, which was tuned on the desktop mic.
        self._tv_mic = None
        self._tv_frames = 0
        self._tv_levels = deque(maxlen=3000)

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
                            self._capture_password()
                            self._splice_armed = True
                            self._input_guard.heard_user()
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
                            self._last_audio_at = time.monotonic()
                            self._received_audio_bytes += len(part.inline_data.data)
                            self._audio.put_nowait((self._generation, part.inline_data.data))
                if server.turn_complete or server.interrupted:
                    self._awaiting_since = 0.0
                if server.turn_complete:
                    self._last_turn_done_at = time.monotonic()
                    self._turn_done.set()
                    self._maybe_escalate()
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
            decision, route = await asyncio.to_thread(jev_fast.judge, text)
            if route:
                self._route_hint = {**route, "request": text, "at": time.monotonic()}
            # A whole job must not depend on Gemini remembering to make one
            # particular tool call.  The old failure-only escalator could not
            # see the common failure mode where she successfully fetched one
            # input, said she was continuing, and then simply stopped.  A
            # confident first-pass whole-task route is safe to hand off: the
            # Task Runtime owns continuation and still enforces every approval
            # boundary itself.
            if (route and route.get("route") == "whole_task" and route.get("p", 0) >= 0.9
                    and self._proactive_task_turn != text):
                self._proactive_task_turn = text
                groups, _ = self._user_turns()
                goal = escalation.proactive_goal(groups)
                await self._escalate(goal)
                log.info("Jev fast path: %r -> proactive start_task route=%s", text[:120], route)
                continue
            if decision is None:
                log.info("Jev fast path: no fast command in %r route=%s (%.0fms)", text[:120], route,
                         (time.monotonic() - started) * 1000)
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

    def notice(self, text: str) -> bool:
        """Say an automatic notice (e.g. core/quota.py: another provider ran
        out) from any thread. False when no conversation loop is running."""
        loop = getattr(self, "_loop", None)
        if loop is None or loop.is_closed() or self._hangup.is_set():
            return False
        loop.call_soon_threadsafe(self._announcements.put_nowait, {"notice": text})
        return True

    def announce(self, entry: dict) -> None:
        """A heartbeat result arrived while this conversation is open."""
        self._announcements.put_nowait(entry)

    def acknowledged_ids(self) -> list[str]:
        """Results the user heard AND answered (spoke after them)."""
        return [i for i, said in self._announced if self.user_spoke_at > said]

    # Real phone call 2026-09-28 08:37:57: she started a Gmail fetch without
    # a word, her turn ended 12ms later, and 26s of silence followed until
    # the user hung up. NON_BLOCKING lets the user talk during a task, but
    # nothing made HER talk (STATUS.md 2026-09-23). The user: "I need that
    # feedback that she heard me and is doing what I asked".
    ACK_AFTER_SECONDS = 1.5
    PROGRESS_EVERY_SECONDS = 12.0
    MAX_PROGRESS_NOTES = 3

    def _status_due(self, now: float) -> tuple[str, str, dict] | None:
        """(call id, kind, entry) of a running task she should speak about now."""
        # Flux renders the authoritative live task stage in the chat itself.
        # Speaking periodic "still working" updates there is redundant and
        # led the model to ask whether the user had seen each one.
        if self.from_paired_phone:
            return None
        if (self._running_blocking or not self._audio.empty() or self._display_state(now) == "speaking"
                or now - max(self._last_user_speech, self._last_loud_at) < 1.0):
            return None
        for call_id, w in sorted(self._working.items(), key=lambda item: item[1]["started"]):
            # Her turn that started the task must be over, or the prompt cuts it off.
            if self._last_turn_done_at < max(w["started"], w["noted"]):
                continue
            if not w["noted"]:
                if self._last_audio_at > w["heard_at"]:
                    w["noted"] = w["started"]  # she already said something about it
                elif now - w["started"] >= self.ACK_AFTER_SECONDS:
                    return call_id, "ack", w
            elif (w["notes"] < self.MAX_PROGRESS_NOTES
                  and now - max(w["noted"], self._last_audio_at) >= self.PROGRESS_EVERY_SECONDS):
                return call_id, "progress", w
        return None

    async def _task_status(self, session) -> None:
        """Say "heard you, on it" when a background task starts silently, and
        "still on it" while it runs long."""
        from google.genai import types
        while not self._hangup.is_set():
            await asyncio.sleep(0.2)
            now = time.monotonic()
            due = self._status_due(now)
            if due is None:
                continue
            call_id, kind, w = due
            if kind == "ack":
                text = (f"[Automatic status, not from the user] You started this and it is still running: "
                        f"{w['what']}. Tell the user in one short sentence, in their language, that you heard "
                        "them and are doing it now. Do not call any tool; the result comes to you by itself.")
            else:
                text = (f"[Automatic status, not from the user] Still running after {now - w['started']:.0f}s: "
                        f"{w['what']}. Tell the user in a few words, in their language, that you are still on "
                        "it. Do not call any tool.")
                w["notes"] += 1
            w["noted"] = now
            await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=text)]),
                                              turn_complete=True)
            log.info("Task status prompted: session=%s call=%s kind=%s after=%.1fs",
                     self._audit_session, call_id, kind, now - w["started"])

    async def _say_clip(self, kind: str, model: str | None = None) -> None:
        """A pre-rendered notice (voice/fallback.py), heard where she is heard."""
        if self.from_paired_phone:
            data = await asyncio.to_thread(fallback.pcm, kind, model)
            if data:
                self._audio.put_nowait((self._generation, data))
                await asyncio.sleep(len(data) / 48000 + 0.3)  # 24kHz s16: let it play out
        else:
            await asyncio.to_thread(fallback.play_local, kind, model)

    async def _start_models(self) -> list[str]:
        """The models this conversation may use, in order. A default that
        failed minutes ago is skipped, and the user is told once."""
        models = fallback.chain(self.config)
        if models[0] != self.config.gemini_model:
            log.warning("Gemini Live starting on fallback %s: %s failed recently", models[0], self.config.gemini_model)
            if fallback.should_announce():
                await self._say_clip("switching", self.config.gemini_model)
        return models

    async def _fall_back(self, models: list[str], index: int, error: BaseException) -> bool:
        """After models[index] failed: True to go on with the next model,
        False to let the error end the conversation."""
        if self._hangup.is_set() or not fallback.is_provider_failure(error):
            return False
        model = models[index]
        fallback.mark_down(model, error)
        if index + 1 >= len(models):
            if len(models) > 1:
                await self._say_clip("failed")
            return False
        log.warning("Gemini Live falling back: session=%s %s -> %s", self._audit_session, model, models[index + 1])
        if model == self.config.gemini_model and fallback.should_announce():
            await self._say_clip("switching", model)
        return True

    async def _carry_over(self, session) -> None:
        text = fallback.carry_over(self._transcript)
        if text:
            await session.send_client_content(turns={"role": "user", "parts": [{"text": text}]}, turn_complete=False)

    def _idle(self) -> bool:
        now = time.monotonic()
        # The phone bridge plays from _audio without tracking _playback_until:
        # queued speech means she is still talking.
        return (self._display_state(now) == "listening" and now - self._last_user_speech > 1.5
                and not self._running_blocking and self._audio.empty())

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
            if entry is not None and entry.get("notice"):
                while not self._idle():
                    await asyncio.sleep(0.2)
                await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=(
                    "[Automatic notice] Tell the user now, briefly, in their language, then carry on: "
                    + entry["notice"]))]), turn_complete=True)
                log.info("Automatic notice said: session=%s %r", self._audit_session, entry["notice"][:120])
                continue
            if entry is not None and entry.get("outcome") == "waiting_approval" and not self._still_waiting(entry):
                # 2026-09-27 10:23: a task cancelled minutes before was still
                # announced from the inbox as needing approval.
                from ..core import agenda
                agenda.mark_delivered([entry["id"]])
                log.info("Approval no longer pending, not announced: task=%s", entry.get("task_id"))
                continue
            if entry is not None and entry.get("outcome") == "waiting_approval":
                while not self._idle():
                    await asyncio.sleep(0.2)
                opener = ("You started this conversation yourself because a background task needs the user's "
                          "approval. " if self.proactive and not self._announced else "")
                await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=(
                    f"[Approval needed, automatic] {opener}A background task is paused until the user approves or "
                    f"denies: {entry.get('title')}: {entry.get('detail', '')[:700]}. Tell the user now, briefly, in "
                    "their language: which task, what exactly it wants to do, and why that needs approval (the "
                    "risk). Ask: approve or deny? If they ask what it changes or want details, call task_status "
                    f"with task_id {entry.get('task_id')} and explain its change_preview (commits, files, the "
                    "actual diff) and files_modified in plain words; never guess the change. On their answer call "
                    f"task_respond with task_id {entry.get('task_id')} and approve true or false. It is also "
                    "waiting in the envelope at the top right of the screen."))]), turn_complete=True)
                self._announced.append((entry.get("id"), time.monotonic()))
                self._announcements_said.append(f"approval needed: {entry.get('title')}")
                log.info("Approval announced: session=%s task=%s", self._audit_session, entry.get("task_id"))
                continue
            if entry is not None:
                while not self._idle():
                    await asyncio.sleep(0.2)
                what = f"{entry.get('title')}: {entry.get('detail', '')[:500]}"
                opener = ("You started this conversation yourself because a scheduled check finished. "
                          if self.proactive and not self._announced else "")
                follow_up = ("Do not ask whether they saw or heard this update. The task chat already shows live "
                             "progress; only ask a question when this result explicitly requires their decision.")
                await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=(
                    f"[Heartbeat result, automatic] {opener}Tell the user now, briefly, in their language: {what}. "
                    f"{follow_up} If they do not answer, say nothing more."))]), turn_complete=True)
                self._announced.append((entry.get("id"), time.monotonic()))
                self._announcements_said.append(what)
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

    def _user_turns(self) -> tuple[list[str], str]:
        """The user's turns so far, and what she said after the last one."""
        groups, current, reply = [], [], []
        for entry in self._transcript:
            if entry["role"] == "user":
                current.append(entry["text"])
                reply = []
            else:
                reply.append(entry["text"])
                if current:
                    groups.append(" ".join("".join(current).split()))
                    current = []
        if current:
            groups.append(" ".join("".join(current).split()))
        return groups, " ".join("".join(reply).split())

    def _maybe_escalate(self) -> None:
        # due() may ask Jev (escalation.jev_judge): only with a failure on
        # record, off the event loop, one check at a time -- and a failure or
        # reply that arrives during a check gets a check of its own after it.
        if not self._escalator.pending(time.monotonic()):
            return
        if self._escalation_check is not None:
            self._escalation_again = True
            return
        self._escalation_check = asyncio.create_task(self._check_escalation())
        self._bg_tasks.add(self._escalation_check)
        self._escalation_check.add_done_callback(self._bg_tasks.discard)

    async def _check_escalation(self) -> None:
        try:
            while True:
                self._escalation_again = False
                groups, reply = self._user_turns()
                goal = await asyncio.to_thread(self._escalator.due, time.monotonic(), groups, reply)
                if goal:
                    await self._escalate(goal)
                    return
                if not self._escalation_again:
                    return
        finally:
            self._escalation_check = None

    async def _escalate(self, goal: str) -> None:
        log.info("Escalating to the Task Runtime: session=%s goal=%r", self._audit_session, goal[:600])
        result = await asyncio.to_thread(run_action, "start_task", {"goal": goal})
        try:
            started = json.loads(result.message) if result.ok else {}
        except ValueError:
            started = {}
        task_id = started.get("task_id")
        if not task_id:
            log.warning("Escalation did not start a task: session=%s %s", self._audit_session, result.message[:300])
            return
        self._escalator.observe_call("start_task", {"goal": goal}, True, result.message, time.monotonic())
        log.info("Escalated: session=%s task=%s%s", self._audit_session, task_id,
                 " (added to the task already doing this job)" if started.get("existing") else "")
        self._announcements.put_nowait({"notice": escalation.notice(task_id, bool(started.get("existing")))})

    def _switchboard_context(self) -> switchboard.Context:
        """The user's latest words, a few earlier turns, the fast pass's
        route for them, and the calls the model made since."""
        groups, _ = self._user_turns()
        request = groups[-1] if groups else ""
        if not request and self._announcements_said:
            request = "(no user words: the assistant is reporting a scheduled result) " + self._announcements_said[-1]
        hint = self._route_hint
        if hint and (time.monotonic() - hint["at"] > 120 or hint["request"] not in request):
            hint = None
        since = self._last_user_speech
        calls = [(name, args) for name, args, at in self._gemini_inflight if at >= since]
        return switchboard.Context(request, groups[-4:-1], {k: hint[k] for k in ("route", "p")} if hint else None,
                                   calls)

    async def _pick(self, call) -> catalog.Resolution:
        """use_tool: Jev picks the catalog tool (execution/catalog.py)."""
        groups, _ = self._user_turns()

        def fill(tool, request, heard):
            from .omarchy import GatewayClient
            return catalog.fill_args(tool, request, heard,
                                     lambda system, user: GatewayClient(self.config).complete_json(system, user,
                                                                                                    timeout=8))
        pick = await asyncio.to_thread(catalog.resolve, dict(call.args or {}), groups[-1] if groups else "",
                                       myapi_on=self.config.myapi_enabled and myapi.is_connected(), fill=fill)
        log.info("Catalog: call=%s request=%r -> %s %s", call.id, (call.args or {}).get("request", "")[:120],
                 pick.tool if pick.run else "offer", pick.evidence)
        return pick

    @staticmethod
    def _still_waiting(entry: dict) -> bool:
        try:
            from ..runtime.task import WAITING_APPROVAL, TaskStore
            task = TaskStore().load(str(entry.get("task_id") or ""))
        except Exception:  # noqa: BLE001 -- when unsure, say it
            return True
        return task is None or task.status == WAITING_APPROVAL

    def _run_tool(self, name: str, args: dict) -> ActionResult:
        """run_action, except unlock_screen from the paired phone (the only
        place it may run; run_action itself refuses it)."""
        if name == "unlock_screen" and self.from_paired_phone:
            from ..execution.actions import unlock_screen_for_paired_phone
            return unlock_screen_for_paired_phone()
        return run_action(name, args)

    MAX_OFFERS = 2  # per user turn: request -> parameters -> the real call needs at most two

    def _offer_result(self, pick: catalog.Resolution) -> ActionResult:
        """An offer of parameters or candidates is a lookup, not a failure --
        until it repeats. Journal 2026-09-27 01:33-01:34 and 02:03-02:04: the
        model re-sent use_tool without args ~60 and 18 times in a row, every
        offer ok=True, so escalation never saw a failure."""
        if pick.message.startswith("No catalog tool does this"):
            return ActionResult(False, "Missing capability: " + pick.message)
        if self._offers_turn != self._last_user_speech:
            self._offers_turn, self._offers = self._last_user_speech, 0
        self._offers += 1
        if self._offers <= self.MAX_OFFERS:
            return ActionResult(True, pick.message)
        return ActionResult(False, "Not run: use_tool has returned parameters again and again without a complete "
                                   "call. Stop calling use_tool for this. Either call it once exactly as "
                                   "use_tool(name=..., args={...}) with every required parameter inside `args`, "
                                   "or tell the user in one short sentence what you still need.")

    async def _review(self, call, name: str, args: dict) -> switchboard.Verdict:
        ctx = self._switchboard_context()
        ctx.description = _TOOL_TEXT.get(name) or catalog.catalog().get(name, {}).get("description", "")
        verdict = await asyncio.to_thread(self._switchboard.review, name, args, ctx)
        log.info("Switchboard: call=%s name=%s decision=%s%s evidence=%s (%.0fms)", call.id, name, verdict.action,
                 f" -> {verdict.tool}" if verdict.action == "reroute" else "", verdict.evidence, verdict.ms)
        return verdict

    async def _run_call(self, session, call) -> None:
        from google.genai import types
        original_request = self._switchboard_context().request
        log.info("Gemini action started: session=%s call=%s name=%s args=%r",
                 self._audit_session, call.id, call.name, call.args or {})
        self._gemini_inflight = self._gemini_inflight[-20:] + [(call.name, dict(call.args or {}), time.monotonic())]
        background = call.name in NON_BLOCKING_ACTIONS
        if background:
            self._running_background += 1
            args = call.args or {}
            self._working[call.id] = {
                "what": str(args.get("request") or call.name.replace("_", " "))[:160],
                "started": time.monotonic(), "heard_at": self._last_user_speech, "noted": 0.0, "notes": 0}
        else:
            self._running_blocking += 1
        if self._overlay:
            await asyncio.to_thread(watchdog.tool_call, call.name, call.args or {})
        ran = call.name  # the tool that actually ran (a reroute changes it)
        try:
            already = self._jev_already(call)
            redirect = self._mission_redirect(call)
            if already is not None:
                result = ActionResult(already["ok"], already["message"])
            elif redirect is not None:
                result = redirect
            elif call.name in self.extra_tools:
                result = await asyncio.to_thread(self.extra_tools[call.name], dict(call.args or {}))
            elif call.name == "run_mission":
                result = await self._run_mission(session, call.args or {})
            elif call.name == "get_recent_actions":
                result = ActionResult(True, LiveSession._get_recent_actions(self, (call.args or {}).get("target")))
            elif call.name == "use_tool" and not (pick := await self._pick(call)).run:
                result = self._offer_result(pick)
            else:
                tool, args = call.name, dict(call.args or {})
                if call.name == "use_tool":
                    tool, args = pick.tool, dict(pick.args)
                    ran = tool
                # Second pass: Jev makes the final routing decision on every
                # call -- except a tool Jev itself just picked for this request.
                # A read-only call starts at once and is only used if Jev lets
                # it run, so reads cost no extra latency.
                if call.name == "use_tool" and pick.picked:
                    review = asyncio.create_task(asyncio.sleep(0, switchboard.Verdict(
                        "execute", tool, args, evidence={"picked": pick.evidence})))
                else:
                    review = asyncio.create_task(self._review(call, tool, args))
                early = (asyncio.create_task(asyncio.to_thread(self._input_guard.run, run_action, tool, args))
                         if tool in switchboard.READ_ONLY else None)
                verdict = await review
                name, prefix = tool, ""
                if verdict.action == "reroute":
                    name, args, prefix = verdict.tool, verdict.args, verdict.message + " "
                    ran = name
                window = await asyncio.to_thread(LiveSession._current_window)
                if verdict.action in ("reject", "ask"):
                    action = None
                    result = ActionResult(False, verdict.message)
                elif early is not None and name == tool:
                    action = early
                else:
                    if writes_externally(name, args):
                        # The switchboard's dedicated `confirmed` judgment is
                        # semantic and multilingual. InputGuard still pins that
                        # approval to this exact payload before execution.
                        self._input_guard.approve_external_write(name, args)
                    action = asyncio.create_task(asyncio.to_thread(self._input_guard.run, self._run_tool, name, args))
                try:
                    if action is not None:
                        result = await asyncio.shield(action)
                        if prefix:
                            result = ActionResult(result.ok, prefix + result.message)
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
            message = result.message
            if result.ok and ran.startswith("myapi_"):
                refinement_request = str((call.args or {}).get("request") or original_request)
                try:
                    refined = await asyncio.to_thread(
                        refine_myapi_result, self.config, refinement_request, message
                    )
                except Exception as exc:  # noqa: BLE001 -- raw bounded result is the safe fallback
                    log.warning("MyApi refinement unavailable: session=%s call=%s: %s",
                                self._audit_session, call.id, str(exc)[:160])
                    refined = message
                if refined != message:
                    log.info(
                        "MyApi result refined: session=%s call=%s raw_chars=%d refined_chars=%d",
                        self._audit_session, call.id, len(message), len(refined),
                    )
                message = refined
            response = {"ok": result.ok, "message": _bounded_tool_message(message)}
        except Exception:
            log.exception("Gemini action failed: %s", call.name)
            response = {"ok": False, "message": "Action failed; do not assume completion."}
        log.info("Gemini action finished: session=%s call=%s name=%s ok=%s result_chars=%d message=%r",
                 self._audit_session, call.id, call.name, response["ok"],
                 len(response["message"]), response["message"][:1800])
        self._escalator.observe_call(ran, dict(call.args or {}), response["ok"], response["message"],
                                     time.monotonic())
        self._maybe_escalate()
        if background:
            self._running_background -= 1
            self._working.pop(call.id, None)
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
            try:
                await session.send_tool_response(function_responses=types.FunctionResponse(
                    id=call.id, name=call.name, response=response, **kwargs))
            except Exception as exc:  # noqa: BLE001
                # The connection it was asked on failed (fallback.py); a
                # background result can outlive it.
                log.warning("Gemini tool response lost: session=%s call=%s: %s", self._audit_session, call.id,
                            str(exc)[:160])
                self._pending_calls.discard(call.id)
                return
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
    # 1.5s split a slow explanation into fragments that were acted on
    # (2026-09-24 22:43-22:44, the unasked `exit`; STATUS.md). 2.0s per the user.
    SPLICE_AFTER_SECONDS = 2.0
    SPLICE_SECONDS = 4.0
    SPLICE_ABORT_RMS = 3500.0
    SPLICE_ABORT_FRAMES = 3
    FORCE_CLOSE_AFTER_SECONDS = 8.0

    def _capture_password(self, text: str | None = None) -> None:
        """A password the user states ("pass: X") goes to GNOME Keyring at
        once, so terminal_sudo / an SSH prompt can use it and every file and
        log masks it (execution/passwords.py). Off the receive loop: the
        keyring write is a subprocess."""
        from ..execution import passwords
        values = passwords._spoken_values(text if text is not None else "".join(self._utterance))
        if values and values[-1] != self._password_seen:
            self._password_seen = values[-1]
            threading.Thread(target=passwords.store_spoken, args=(values[-1],), daemon=True).start()

    def _replied(self) -> None:
        self._input_guard.assistant_replied()
        if self._spliced_at:
            log.info("Stuck-turn guard: reply %.1fs after the silence splice", time.monotonic() - self._spliced_at)
            self._spliced_at = 0.0
        self._splice_armed = False
        self._splice_until = 0.0
        self._force_close_at = 0.0

    def _splice(self, chunk: bytes, rms: float, now: float, speaking: bool) -> bytes | None:
        """Silence to send instead of this frame, if a stuck turn needs closing."""
        loud = rms >= self._splice_abort_rms
        self._loud_run = self._loud_run + 1 if loud else 0
        if self._loud_run >= self.SPLICE_ABORT_FRAMES and not speaking:
            # The user talking (again): wait for them to stop first.
            self._speech_levels.append(rms)
            self._last_loud_at, self._splice_armed, self._splice_until = now, True, 0.0
            self._force_close_at = 0.0
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
        speech = sorted(self._speech_levels)
        log.info("Stuck-turn guard: no reply %.1fs after the user's last words; sending silence until she "
                 "replies, at most %.1fs "
                 "(mic_rms_1s p50=%.0f max=%.0f; loud-run speech p50=%.0f; abort_rms=%.0f)",
                 now - last_words, self.SPLICE_SECONDS,
                 levels[len(levels) // 2] if levels else 0, levels[-1] if levels else 0,
                 speech[len(speech) // 2] if speech else 0, self._splice_abort_rms)
        self._splice_armed = False
        self._splice_until = now + self.SPLICE_SECONDS
        self._spliced_at = now
        self._force_close_at = now
        self._splices += 1
        return bytes(len(chunk))

    async def _stuck_turn_recovery(self, session) -> None:
        """Force-close a turn when even the silence splice did not unblock it."""
        from google.genai import types
        while True:
            await asyncio.sleep(0.1)
            started = self._force_close_at
            if not started or time.monotonic() - started < self.FORCE_CLOSE_AFTER_SECONDS:
                continue
            # A text turn_complete is the only operation observed to unstick
            # the current Gemini Live regression after streamed silence did
            # not. Keep the instruction neutral so the preceding audio remains
            # the request being answered.
            self._force_close_at = 0.0
            await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=(
                "[Turn recovery] The user finished speaking. Respond to their preceding request now."))]),
                turn_complete=True)
            self._forced_turn_closures += 1
            log.warning("Stuck-turn guard: silence splice got no reply for %.1fs; forced turn closure",
                        self.FORCE_CLOSE_AFTER_SECONDS)

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
            # While the screen is mirrored the Android TV receiver streams its
            # own microphone back (voice/tv_mic.py): the user is at the TV, so
            # it replaces the desktop mic, which keeps pacing the stream and
            # takes over again 0.35s after the TV audio stops.
            remote = self._tv_mic.read(len(chunk)) if self._tv_mic is not None else None
            if remote is not None:
                chunk = remote
                self._tv_frames += 1
            samples = np.frombuffer(chunk[:len(chunk) // 2 * 2], dtype="<i2").astype(np.float32)
            rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
            if remote is not None:
                self._tv_levels.append(rms)
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
        self._loop = asyncio.get_running_loop()
        # Desktop voice talks show in the phone's conversation list too, and the
        # tasks started here report back into them (core/conversations.py).
        from ..core import conversations
        self.conversation = await asyncio.to_thread(conversations.desktop)
        self._transcript = conversations.Transcript(self.conversation, self._transcript)
        conversations.CURRENT.set(self.conversation)
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
            from ..display import assistant_huds
            await asyncio.to_thread(assistant_huds.open_automatic, self.config)
            await self._echo.start(self.config.mic_device, self.config.gemini_mic_volume_percent)
            try:
                self._tv_mic = TvMicReceiver(16000)
            except OSError as exc:
                log.warning("TV microphone socket unavailable; desktop microphone only: %s", exc)
            models = await self._start_models()
            for index, model in enumerate(models):
                try:
                    async with client.aio.live.connect(model=model, config=build_live_config(self.config)) as session:
                        if mic is None:
                            argv = ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", "--latency", "20ms"]
                            argv.extend(["--target", self._echo.source])
                            mic = await asyncio.create_subprocess_exec(*argv, "-a", "-", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                        log.info("Gemini Live connected: %s; full-duplex tools enabled", model)
                        if index:
                            await self._carry_over(session)
                        else:
                            log.info("Gemini speech detection: start=LOW end=LOW prefix=300ms silence=600ms; real interruptions enabled")
                            if self.on_connected:
                                self.on_connected()
                            await asyncio.to_thread(status_icon.set_live, True)
                        workers = [self._send_audio(session, mic), self._receive(session), self._play_audio(),
                                   self._tools(session), self._stuck_turn_recovery(session), self._task_status(session),
                                   self._hangup.wait()]
                        if self.config.watchdog_enabled:
                            workers.append(self._visuals())
                        if self.config.jev_fast_path:
                            workers.append(self._fast_path())
                        workers.append(self._announcer(session))
                        tasks = [asyncio.create_task(worker) for worker in workers]
                        if not index:
                            await announce_update(session)
                        done, _ = await asyncio.wait(tasks, timeout=self.config.max_session_seconds, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            task.result()
                    break
                except Exception as error:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    tasks = []
                    if not await self._fall_back(models, index, error):
                        raise
        finally:
            log.info("Gemini session audio summary: session=%s interruptions=%d received_audio_bytes=%d submitted_audio_bytes=%d "
                     "echo_gated_frames=%d user_barge_ins=%d stuck_turn_splices=%d forced_turn_closures=%d",
                     self._audit_session, self._interruptions, self._received_audio_bytes, self._submitted_audio_bytes,
                     self._gated_frames, self._barge_count, self._splices, self._forced_turn_closures)
            if self._tv_mic is not None:
                levels = sorted(self._tv_levels)
                log.info("Gemini TV microphone: session=%s tv_frames=%d rms_p50=%.0f rms_p90=%.0f rms_max=%.0f",
                         self._audit_session, self._tv_frames, levels[len(levels) // 2] if levels else 0,
                         levels[len(levels) * 9 // 10] if levels else 0, levels[-1] if levels else 0)
                self._tv_mic.close()
                self._tv_mic = None
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
            await asyncio.to_thread(append_session, list(self._transcript), self.config.context_retention_hours)
            await asyncio.to_thread(conversations.flush, self.conversation)
            # Alerts in the language just spoken, while a model works (core/alert_clips.py).
            alert_clips.prepare_in_background(self.config)
