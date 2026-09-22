"""The gpt-live-1 WebRTC client: one conversational session per call to
`run()`. Refactored from scripts/spike_live_aiortc.py, which proved out
every piece of this against the real API — see STATUS.md for the full
debugging trail (session schema, audio format/volume/jitter fixes).

Uses aiortc, not GStreamer's webrtcbin — see ADR-0001 D7 for why (webrtcbin
hit two distinct real bugs on the receive path; aiortc worked first try).
"""

from __future__ import annotations

import asyncio
import fractions
import json
import logging
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request

import av
import numpy as np
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack
from rapidfuzz import fuzz

from .. import myapi
from ..config import MYAPI_INSTRUCTIONS, Config
from ..core.history import append_session, load_recent_context
from ..core import updates
from ..core.memory import load_preferences
from ..execution.actions import run_action
from ..execution.verified_input import InputGuard
from ..execution.tools import MYAPI_TOOLS, TOOLS
from . import status_icon, watchdog
from .tv_mic import Receiver as TvMicReceiver

log = logging.getLogger("omarchy_ai.voice.live")

RATE = 48000
FRAME_SAMPLES = 960  # 20ms at 48kHz, the standard WebRTC frame size
OUTPUT_GAIN = 4.0  # measured RMS was ~451/32767 (~-37dBFS) without this
PREBUFFER_FRAMES = 15  # ~300ms: enough to avoid underruns without visibly leading speech
REBUFFER_GRACE_EMPTY_POLLS = 3


def _is_capability_demo(text: str) -> bool:
    """Whether this turn asks to demonstrate actions, not merely discuss them."""
    normalized = " ".join(text.lower().split())
    return any(phrase in normalized for phrase in (
        "demo", "demonstrate", "show me what you can do", "show your capabilities",
        "show me your capabilities", "demonstration",
    ))


def build_session_config(config: Config) -> dict:
    """Assembles the `session` object for the /v1/live/sessions POST body
    -- instructions (base + learned preferences + recent context) plus the
    model/tools config. Shared by the desktop LiveSession below (aiortc)
    and the phone bridge (phone/server.py, browser WebRTC) -- both talk to
    the exact same OpenAI session shape, so this has to stay one function,
    not two copies that can quietly drift apart."""
    instructions = config.instructions
    instructions += (
        "\n\nJEV DESKTOP DELEGATION: You remain the live conversational model. "
        "For supported native OS tasks (workspaces, focusing windows, output volume/mute, "
        "brightness, installed themes, bar panels), prefer desktop_task with the complete "
        "current user goal. Its Jev loop handles typed decisions and native verification "
        "without a language-model round trip per action. It can take up to about 25 "
        "seconds and runs in the background: say a brief present-tense line ('on it', "
        "'switching that now') right as you call it, then keep the conversation going "
        "normally -- answer other questions, keep listening -- instead of waiting silently "
        "for it to finish; you'll get its result when it's ready and should report it then, "
        "without re-announcing that you're about to start. ALWAYS use browser_task for web navigation, "
        "search, forms and links; it is the installed browser-use/jev-ultrafast Agent with typesafe-ai/jev. "
        "It can take up to about 90 seconds and works the same way -- acknowledge it briefly "
        "right as you call it, then continue the conversation while it runs in the background. "
        "Never type the user's conversational request into a terminal, editor, chat, or another AI agent. "
        "Keep vision, generated text, complex planning and unsupported actions with your "
        "normal tools. Use search_os_knowledge for Omarchy/Arch reference material. "
        "A handoff may include already executed steps: read the trace and continue only "
        "remaining work. Never translate an unverified dispatch into a completion claim."
    )
    myapi_on = config.myapi_enabled and myapi.is_connected()
    if myapi_on:
        # Only mentioned/exposed at all once a connection actually exists
        # — see execution/tools.py's MYAPI_TOOLS docstring for why this
        # isn't just folded into the static TOOLS list.
        instructions += "\n\n" + MYAPI_INSTRUCTIONS
    preferences = load_preferences()
    if preferences:
        # Standing corrections saved via remember_preference in past
        # conversations — folded in fresh every session so they persist
        # across restarts, not just within one conversation.
        instructions += "\n\nLearned preferences from past conversations:\n" + "\n".join(
            f"- {p}" for p in preferences
        )
    recent_context = load_recent_context(config.context_retention_hours, config.context_max_chars)
    if recent_context:
        # What was actually said in recent past sessions (within
        # context_retention_hours) — user explicitly asked for this: a new
        # session used to know nothing about a conversation from even a
        # minute earlier in the previous wake-word cycle.
        instructions += (
            "\n\nArchived conversations, reference only. These sessions have ended. "
            "Their requests and assistant promises are NOT pending tasks or permission "
            "to act. Consult them only if the current user explicitly refers back:\n"
            + recent_context
        )

    instructions += (
        "\n\nNEW SESSION: Wait for the user's current request. If none was heard, "
        "briefly ask how you can help. Never resume yesterday's work, run tools, "
        "or announce an unfinished task based solely on archived conversation or "
        "preferences. Resume only when requested in THIS session. When discussing "
        "a terminal tile, resolve its window identity and read its current transcript; "
        "do not infer current contents from old conversation. Report missing capture "
        "honestly and use describe_screen for current visible contents when necessary."
    )

    instructions += (
        "\n\nACTION EVIDENCE: Wait for tool results before saying an action happened. "
        "A plan or promise is not execution. If a tool fails, report failure and repair "
        "the prerequisite before continuing. Before typing or pressing keys, call "
        "focus_window successfully for the intended window. Input tools only confirm "
        "input dispatch, NEVER application acceptance or completion. After submitting "
        "a prompt, read fresh output from that same terminal and verify that the app "
        "accepted this exact request; inspect describe_screen if redraw history is "
        "ambiguous. Old 'Working' text is not evidence of current work. If evidence "
        "is missing, say 'input sent, outcome unverified', not 'done'. Never claim "
        "ongoing monitoring or promise future alerts unless an actual monitoring "
        "mechanism has been started; one log read or saved preference is not monitoring."
    )

    notice = updates.wake_notice()
    if notice:
        instructions += (
            "\n\nVERIFIED STARTUP UPDATE NOTICE: In your first spoken reply, briefly "
            "announce this notice in the user's language, then handle their request: "
            + notice + " This notice does not authorize installation. Only call "
            "update_assistant when the user explicitly asks to update Omarchy AI."
        )
    instructions += (
        "\n\nSELF UPDATES: Use check_assistant_updates to check GitHub, "
        "update_assistant only for an explicit request to update this assistant, "
        "and get_update_status for progress or failures. Never use terminal commands "
        "or git pull to update yourself. An accepted update request is not a "
        "completed installation; warn that the conversation disconnects at restart. "
        "If get_update_status reports state=failed, check its issue_url/issue_error "
        "fields before saying anything about reporting it: issue_url means a GitHub "
        "issue was already filed automatically -- tell the user that, with the URL, "
        "don't offer to file one. issue_error means it tried and could not (commonly "
        "no GitHub token configured on this machine) -- say that honestly. Only call "
        "report_issue yourself if the user explicitly asks to report/file an issue "
        "(for this or anything else) and no automatic one already exists for it. "
        "Never say you can, will, or did file a GitHub issue unless report_issue (or "
        "the automatic update-failure report above) actually returned success -- that "
        "is a real external action with a real result, not something to promise."
    )

    return {
        "model": config.live_model,
        "delegation": {
            "type": "responses",
            "responses": {
                "model": config.responses_model,
                "reasoning": {"effort": config.responses_reasoning_effort},
                # Without this the model apparently never considers calling
                # the tool at all — zero function_call events across a full
                # real conversation where the user clearly asked to end it.
                # "auto" makes tool use available rather than off by default.
                "tool_choice": "auto",
                # Confirmed by probing the real API: tools live under
                # delegation.responses, not at the session top level
                # (session.tools is rejected as unknown_parameter).
                "tools": [
                    {
                        "type": "function",
                        "name": "end_conversation",
                        "description": (
                            "Call this when the user indicates they want to "
                            "end the conversation — e.g. saying goodbye, "
                            "stop, that's all, or similar — in whatever "
                            "language they used. Do not call it for "
                            "unrelated use of similar words (e.g. 'can we "
                            "stop for a second')."
                        ),
                        "parameters": {"type": "object", "properties": {}, "required": []},
                    },
                    *TOOLS,
                    *(MYAPI_TOOLS if myapi_on else []),
                ],
            },
        },
        "instructions": instructions,
        "audio": {"output": {"voice": config.voice}},
    }


class MicTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, device: str | None = None, *, source: str = "desktop"):
        super().__init__()
        argv = [
            "pw-record", "--rate", str(RATE), "--channels", "1",
            "--format", "s16", "--latency", "20ms", "-a",
        ]
        if device:
            argv += ["--target", device]
        argv.append("-")
        self._proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._tv_mic = TvMicReceiver(RATE) if source == "tv" else None
        self._samples_sent = 0
        self._frame_bytes = FRAME_SAMPLES * 2

    async def recv(self):
        loop = asyncio.get_event_loop()
        buf = b""
        while len(buf) < self._frame_bytes:
            chunk = await loop.run_in_executor(
                None, self._proc.stdout.read, self._frame_bytes - len(buf)
            )
            if not chunk:
                break
            buf += chunk
        remote = self._tv_mic.read(self._frame_bytes) if self._tv_mic is not None else None
        if remote is not None:
            buf = remote
        frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
        frame.planes[0].update(buf.ljust(self._frame_bytes, b"\x00"))
        frame.sample_rate = RATE
        frame.pts = self._samples_sent
        frame.time_base = fractions.Fraction(1, RATE)
        self._samples_sent += FRAME_SAMPLES
        return frame

    def stop(self) -> None:
        super().stop()
        if self._tv_mic is not None:
            self._tv_mic.close()
        if self._proc.poll() is None:
            self._proc.terminate()


def _playback_thread(
    proc: subprocess.Popen,
    q: queue.Queue,
    stop: threading.Event,
    on_playback_started,
) -> None:
    primed = False
    empty_polls = 0
    while not stop.is_set():
        if not primed:
            if q.qsize() < PREBUFFER_FRAMES:
                threading.Event().wait(0.01)
                continue
            primed = True
            empty_polls = 0
        try:
            data = q.get(timeout=0.1)
            empty_polls = 0
        except queue.Empty:
            empty_polls += 1
            if empty_polls >= REBUFFER_GRACE_EMPTY_POLLS:
                primed = False
            continue
        if data is None:
            break
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
            # The transcript can arrive well before the audio. Tell the
            # overlay that we are speaking only when the first buffered
            # chunk is actually handed to PipeWire, rather than making the
            # visualizer run a second or two ahead of audible speech.
            on_playback_started()
        except (BrokenPipeError, ValueError):
            break


class LiveSession:
    """One wake-to-hangup conversation with gpt-live-1."""

    # Phrases the assistant itself would plausibly say when signing off —
    # checked against its own output, not the user's speech. Confirmed live
    # that the end_conversation tool call is unreliable on this very new
    # API (zero function_call events across a full session where the user
    # clearly said "bye"), so this is the actual mechanism, not a backup.
    # It works because the model's own phrasing is far more predictable
    # than free-form user speech, and `instructions` explicitly tells it to
    # say a farewell when the user wants to end — this reads that back.
    _FAREWELL_MARKERS = (
        # "take care" deliberately excluded — confirmed live false
        # positive: the model said "let me take care of that" while about
        # to run a tool, not goodbye. Genuinely ambiguous now that it also
        # narrates handling requests, not just safe to assume.
        "goodbye", "good bye", "bye for now", "bye!", "bye.",
        "farewell", "talk to you later", "take care of yourself",
        "take care now",
        # Hebrew — this mechanism was English-only, confirmed live as a
        # real gap: the model's own Hebrew farewell never matched, so a
        # Hebrew conversation never hung up via this path either.
        # להתראות/נתראה (goodbye/see you — essentially unambiguous, always
        # a farewell) and ביי (bye, a common loanword). "שלום" deliberately
        # excluded — it means both "hello" and "goodbye"/"peace", the same
        # kind of genuine ambiguity that excluded "take care" above.
        "להתראות", "נתראה", "ביי",
    )

    def __init__(self, config: Config, *, mic_source: str = "desktop"):
        self.config = config
        self.mic_source = mic_source
        self._pc: RTCPeerConnection | None = None
        self._hangup = asyncio.Event()
        self._input_buffer = ""
        self._output_buffer = ""
        self._farewell_scheduled = False
        self._dc = None
        # Set by output-transcript events and consumed by the playback
        # thread. threading.Event makes the handoff safe without blocking
        # the WebRTC event loop.
        self._awaiting_playback_start = threading.Event()
        # The model sometimes narrates a demo before it emits its first
        # function call. Hold that narration back; once a real desktop
        # action starts, normal audio resumes for the result and next step.
        self._demo_waiting_for_action = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._handled_call_ids: set[str] = set()
        # A live response can emit several function calls in one burst.
        # Desktop actions must complete in their received order so a demo
        # cannot describe a sequence while its actual changes are deferred.
        self._tool_lock: asyncio.Lock | None = None
        # In-memory only, per conversation — resets every session. A
        # per-window ("per-tile") log so the model can recall what it's
        # already done to a specific window rather than only the last
        # thing overall.
        self._action_log: list[dict] = []
        self._input_guard = InputGuard()
        # This session's own turns, flushed to core/history.py on hangup so
        # the *next* session (potentially minutes or days later) can recall
        # it — separate from _action_log, which never leaves memory.
        self._transcript: list[dict] = []
        # Read once per session — the daemon only reloads config on
        # restart, so this can't change mid-conversation anyway. Gates the
        # overlay's visibility (start() call site) and whether the
        # real-time playback loop bothers computing/dispatching amplitude
        # levels for the visualizer display mode at all.
        self._watchdog_on = bool(config.watchdog_enabled)
        self._watchdog_wants_levels = self._watchdog_on and config.watchdog_display_mode in (
            "visualizer",
            "both",
        )
        # Last state actually sent to the overlay. Real bug this caught:
        # the "speaking" transition used to be gated on
        # `not self._output_buffer` (a proxy for "first delta of a new
        # utterance") rather than on whether the overlay was already
        # showing "speaking" — so a tool call mid-response (state
        # "thinking") interleaved with speech (the exact pattern this
        # project's own instructions ask for: "briefly confirm what you
        # did after calling a tool") left the buffer non-empty when speech
        # resumed, the emptiness check never passed again, and the
        # visualizer got stuck on "thinking" — cleared and dark — for the
        # rest of that response. Deduping on the actual last-sent state
        # instead of a buffer-emptiness proxy fixes every call site at
        # once, not just this one.
        self._watchdog_state: str | None = None

    def _set_watchdog_state(self, label: str) -> None:
        if not self._watchdog_on or label == self._watchdog_state:
            return
        self._watchdog_state = label
        watchdog.state(label)

    def _read_key(self) -> str:
        with open(self.config.api_key_path) as f:
            return f.read().strip()

    def _check_exit_phrase(self, text: str) -> bool:
        norm = text.strip().lower()
        # WRatio's partial-match component inflates scores badly for very
        # short strings — confirmed live: a single "i" matched "finish" at
        # or above threshold and hung up a real conversation the user
        # hadn't tried to end. Below this length there isn't enough text
        # for a meaningful comparison at all.
        if len(norm) < 4:
            return False
        for phrase in self.config.exit_phrases:
            # fuzz.ratio (whole-string Levenshtein) rather than WRatio: no
            # partial/substring matching, so a short target phrase can't
            # score high against an unrelated fragment just because it
            # happens to be a substring of something longer.
            if fuzz.ratio(norm, phrase) >= self.config.exit_phrase_score_threshold:
                log.info("exit phrase matched: %r ~ %r", norm, phrase)
                return True
        return False

    async def _play_remote_audio(self, track) -> None:
        proc = subprocess.Popen(
            [
                "pw-play", "--rate", str(RATE), "--channels", "1",
                "--format", "s16", "--latency", "120ms", "-a", "-",
            ],
            stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        q: queue.Queue = queue.Queue()
        stop = threading.Event()
        def on_playback_started() -> None:
            if self._awaiting_playback_start.is_set():
                self._awaiting_playback_start.clear()
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(self._set_watchdog_state, "speaking")

        thread = threading.Thread(
            target=_playback_thread, args=(proc, q, stop, on_playback_started), daemon=True
        )
        thread.start()

        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        # Throttle amplitude dispatch to ~10Hz (within the 8-12Hz ask) —
        # frames arrive every 20ms (50Hz), far more often than the
        # visualizer needs, and watchdog.level() is fire-and-forget but
        # still not free (a real Popen call) per invocation.
        last_level_dispatch = 0.0
        level_interval = 1.0 / 10.0
        try:
            while True:
                frame = await track.recv()
                for resampled in resampler.resample(frame):
                    pcm = bytes(resampled.planes[0])[: resampled.samples * 2]
                    if self._demo_waiting_for_action.is_set():
                        # Deliberately discard pre-action audio rather than
                        # replay a promise after the action has happened.
                        continue
                    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                    boosted = np.clip(samples * OUTPUT_GAIN, -32768, 32767).astype(np.int16)
                    q.put_nowait(boosted.tobytes())
                    if self._watchdog_wants_levels:
                        now = time.monotonic()
                        if now - last_level_dispatch >= level_interval:
                            last_level_dispatch = now
                            # Normalized against the ~3856 RMS this project
                            # measured post-gain-fix for normal speech (see
                            # STATUS.md) — 12000 gives headroom so loud
                            # passages actually reach the top of the scale
                            # instead of pinning it constantly.
                            rms = float(
                                np.sqrt(np.mean(np.square(boosted.astype(np.float32))))
                            )
                            watchdog.level(min(1.0, rms / 12000.0))
        except Exception:  # noqa: BLE001
            log.debug("playback loop ended", exc_info=True)
        finally:
            stop.set()
            q.put_nowait(None)
            thread.join(timeout=2)
            proc.stdin.close()
            proc.wait()

    def _check_function_call(self, event: dict) -> None:
        """Look for a tool call inside a response.event wrapper.

        Schema not fully documented for this brand-new API — checks the
        shape actually observed live (response.output_item.done, item.type
        == "function_call"). Only acts on .done (not .added) so arguments
        are complete, and dedupes by call_id since a real conversation can
        legitimately call the same tool more than once.
        """
        inner = event.get("event", {})
        if inner.get("type") != "response.output_item.done":
            return
        item = inner.get("item", {})
        if item.get("type") != "function_call":
            return
        call_id = item.get("call_id") or item.get("id")
        name = item.get("name")
        if not call_id or call_id in self._handled_call_ids:
            return
        self._handled_call_ids.add(call_id)

        if name == "end_conversation":
            from ..execution.desktop_jev import cancel_desktop_tasks
            cancel_desktop_tasks()
            log.info("end_conversation tool call received")
            self._hangup.set()
            return

        raw_args = item.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            args = {}

        if name == "get_recent_actions":
            output = self._get_recent_actions(args.get("target"))
            self._send_function_result(call_id, True, output)
            return

        # Executing inline here (this is a sync callback off the data
        # channel) blocks the whole asyncio event loop for however long the
        # action takes — confirmed live as a real bug: describe_screen's
        # multi-second vision API call froze the loop long enough that
        # aiortc's own connection handling underneath it broke. Every
        # action, not just slow ones, needs to run off-loop.
        asyncio.ensure_future(self._run_tool_call(call_id, name, args))

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

    def _check_farewell(self, text: str) -> bool:
        norm = text.strip().lower()
        return any(marker in norm for marker in self._FAREWELL_MARKERS)

    async def _delayed_hangup(self, grace_seconds: float = 2.0) -> None:
        # Give the farewell line time to actually finish playing before
        # the connection (and its audio track) closes out from under it.
        await asyncio.sleep(grace_seconds)
        self._hangup.set()

    def _on_data_message(self, message: str) -> None:
        try:
            event = json.loads(message)
        except json.JSONDecodeError:
            return
        etype = event.get("type", "")
        if etype == "session.input_transcript.delta":
            # _set_watchdog_state itself dedupes on the *actual* last-sent
            # state now, not buffer emptiness — safe to call on every
            # delta, only the real transitions reach the overlay.
            self._set_watchdog_state("listening")
            self._awaiting_playback_start.clear()
            self._input_buffer += event.get("delta", "")
            if _is_capability_demo(self._input_buffer):
                self._demo_waiting_for_action.set()
            if self._output_buffer:
                self._transcript.append({"role": "assistant", "text": self._output_buffer})
            self._output_buffer = ""
            self._farewell_scheduled = False
            if self._check_exit_phrase(self._input_buffer):
                self._hangup.set()
        elif etype == "session.output_transcript.delta":
            # Text reaches us ahead of the audio stream. The playback
            # thread switches the visual state only after it has supplied
            # the first audio chunk to PipeWire.
            self._awaiting_playback_start.set()
            # The model started replying — the user's turn is over. Reset
            # the input buffer so the next utterance is judged on its own.
            if self._input_buffer:
                self._transcript.append({"role": "user", "text": self._input_buffer})
            self._input_buffer = ""
            self._output_buffer += event.get("delta", "")
            if not self._farewell_scheduled and self._check_farewell(self._output_buffer):
                self._farewell_scheduled = True
                log.info("assistant said a farewell (%r) — hanging up shortly", self._output_buffer)
                asyncio.ensure_future(self._delayed_hangup())
        elif etype == "response.event":
            self._check_function_call(event)
        elif etype == "response.function_call_arguments.done" and event.get("name") == "end_conversation":
            log.info("end_conversation tool call received (top-level event)")
            self._hangup.set()
        elif etype == "error":
            err = event.get("error", {})
            log.warning("gpt-live-1 error event: %s", err)
            # Confirmed live: a billing error (credit_balance_exhausted)
            # repeats every ~15-20s forever with no self-recovery — the
            # daemon just silently burned minutes retrying an error that
            # can only be fixed outside the process. Recognize this class
            # of unrecoverable error and hang up immediately instead.
            code = (err.get("code") or "").lower()
            message = (err.get("message") or "").lower()
            if "credit" in code or "quota" in code or "credit" in message or "billing" in message:
                log.error(
                    "unrecoverable billing/quota error, hanging up: %s",
                    err.get("message"),
                )
                self._hangup.set()
        log.debug("data channel event: %s", message[:500])

    async def run(self) -> None:
        """Connect, converse, and return once the session ends (exit
        phrase, the safety timeout, or a connection failure)."""
        t_start = time.monotonic()
        self._loop = asyncio.get_running_loop()
        # aiortc defaults to Google's public STUN server when no
        # configuration is given (RTCIceTransport.getDefaultIceServers),
        # and aioice's gatherer waits up to 5s for a STUN reply before
        # falling back — confirmed live, in source: that 5s wait was the
        # entire "slow to start hearing" latency. Every connection so far
        # has worked on host candidates alone (this machine's NAT allows
        # outbound-initiated connections without needing a reflexive
        # candidate), so skip STUN entirely rather than wait for it.
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self._pc = pc
        mic = MicTrack(self.config.mic_device, source=self.mic_source)
        pc.addTrack(mic)
        dc = pc.createDataChannel("oai-events")
        self._dc = dc

        @dc.on("open")
        def on_open():
            # Firing response.create the instant the channel opens leaves
            # no time to actually say anything — confirmed live, twice: the
            # model just gives a generic scripted greeting from
            # `instructions` regardless of what (if anything) the user
            # said. A fixed delay is a stopgap for a real VAD-based "user
            # finished talking" signal (see STATUS.md next actions).
            log.info(
                "data channel open — %ds to speak before requesting a response",
                self.config.speak_window_seconds,
            )

            async def _delayed_response():
                await asyncio.sleep(self.config.speak_window_seconds)
                # A conversation can end well inside the speak window — a
                # quick "never mind", the exit phrase, a dropped
                # connection. Confirmed live: a 4-second session left this
                # timer to fire 3s after hangup and raise InvalidStateError
                # into an unretrieved task, one full traceback per short
                # session. Nothing downstream was broken by it, but an
                # unhandled exception in a long-lived daemon is exactly
                # what hides the next real one.
                if self._hangup.is_set() or dc.readyState != "open":
                    log.debug("speak window elapsed after hangup, not requesting a response")
                    return
                log.info("requesting a response now")
                dc.send(json.dumps({"type": "response.create"}))

            asyncio.ensure_future(_delayed_response())

        @dc.on("message")
        def on_message(message):
            self._on_data_message(message)

        @pc.on("track")
        def on_track(track):
            if track.kind == "audio":
                asyncio.ensure_future(self._play_remote_audio(track))

        @pc.on("connectionstatechange")
        async def on_state_change():
            log.info("connection state -> %s", pc.connectionState)
            if pc.connectionState in ("failed", "closed"):
                self._hangup.set()

        try:
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            while pc.iceGatheringState != "complete":
                await asyncio.sleep(0.1)

            body = json.dumps(
                {
                    "session": build_session_config(self.config),
                    "transport": {"type": "webrtc", "sdp": pc.localDescription.sdp},
                }
            ).encode()
            req = urllib.request.Request(
                "https://api.openai.com/v1/live/sessions",
                data=body,
                headers={
                    "Authorization": f"Bearer {self._read_key()}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    response = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                log.error("session creation failed: HTTP %s %s", e.code, e.read().decode())
                return

            answer_sdp = response["transport"]["sdp"]
            await pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))
            log.info("live session connected (%.2fs)", time.monotonic() - t_start)
            # Bar-icon status dot — unconditional, not gated by
            # _watchdog_on: this reflects true connection state regardless
            # of whether the optional Watch Dogs overlay is enabled.
            status_icon.set_live(True)
            if self._watchdog_on:
                watchdog.start(self.config.watchdog_display_mode)
                self._watchdog_state = None  # fresh session, no state sent yet
            self._set_watchdog_state("listening")

            try:
                await asyncio.wait_for(
                    self._hangup.wait(), timeout=self.config.max_session_seconds
                )
            except asyncio.TimeoutError:
                log.info("session hit max_session_seconds, hanging up")
        finally:
            from ..execution.desktop_jev import cancel_desktop_tasks
            cancel_desktop_tasks()
            status_icon.set_live(False)
            if self._watchdog_on:
                watchdog.stop()
            if self._input_buffer:
                self._transcript.append({"role": "user", "text": self._input_buffer})
            if self._output_buffer:
                self._transcript.append({"role": "assistant", "text": self._output_buffer})
            append_session(self._transcript, self.config.context_retention_hours)
            mic.stop()
            await pc.close()
            self._pc = None
