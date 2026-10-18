"""Full-duplex Gemini Live audio and desktop actions."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import deque
import uuid
import threading
import time
from pathlib import Path
import numpy as np
from .. import myapi
from ..core import alert_clips, identities, logs, quota, retry, updates
from ..core import origin as origins
from ..core.history import append_session
from ..execution import catalog, user_tools
from ..execution import provenance, runner
from ..execution.actions import run_action, ActionResult
from ..execution.verified_input import InputGuard, writes_externally
from . import status_icon, watchdog
from .tv_mic import Receiver as TvMicReceiver
from .live import build_session_config, LiveSession
from .prompt_version import resolve_prompt_version
from .echo_cancel import EchoCancellation
from . import escalation, extensions, fallback, speaker_id, switchboard
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
_SENT_BACK = ("Not run:", "MyApi action not run")  # a switchboard verdict, not a tool failing
# ... and the other answers to a call in which nothing ran: a guard's, or Jev's fast path got there first.
_NOT_RUN = (*_SENT_BACK, "Not executed:", "Already done by the Jev fast path")

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


PROACTIVE_TASK_P = 0.8

# English names for config.yaml's user_languages; ISO codes pass through.
_LANGUAGE_CODES = {
    "arabic": "ar", "bengali": "bn", "bulgarian": "bg", "catalan": "ca", "chinese": "zh", "croatian": "hr",
    "czech": "cs", "danish": "da", "dutch": "nl", "english": "en", "estonian": "et", "finnish": "fi",
    "french": "fr", "german": "de", "greek": "el", "hebrew": "he", "hindi": "hi", "hungarian": "hu",
    "indonesian": "id", "italian": "it", "japanese": "ja", "korean": "ko", "latvian": "lv", "lithuanian": "lt",
    "malay": "ms", "norwegian": "no", "persian": "fa", "polish": "pl", "portuguese": "pt", "romanian": "ro",
    "russian": "ru", "serbian": "sr", "slovak": "sk", "slovenian": "sl", "spanish": "es", "swedish": "sv",
    "tamil": "ta", "thai": "th", "turkish": "tr", "ukrainian": "uk", "urdu": "ur", "vietnamese": "vi",
}


def transcription_languages(names) -> list[str]:
    """BCP-47 hints for the input transcription, from the user's languages.
    Journal 2026-10-05 16:04-16:09 (phone): five Hebrew turns were transcribed
    as 'malas notas', 'baixa Sage' and Arabic; Jev's
    routing, the history and the alert-clip language all read that text.
    Probe the same day, 1s noisy Hebrew fragments on gemini-3.8-live: auto
    detection gave 'del Brezal', 'faccia', 'migraine'; with ["he", "en"] it
    gave Hebrew for the same audio. A name it does not know is left out, and
    with none known detection stays automatic."""
    codes = []
    for name in names or []:
        value = str(name).strip()
        code = _LANGUAGE_CODES.get(value.lower()) or (
            value if re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*", value) else None)
        if code and code not in codes:
            codes.append(code)
    return codes


def origin_note(conversation: str | None = None, channel: str | None = None) -> str:
    """Where the user is, for a live session's instructions (core/origin.py):
    the conversation's channel, or `channel` when there is no conversation.
    Who is speaking is said separately ([Speaker], WHO IS SPEAKING/TYPING).
    2026-10-15 19:58: "make it a bit calmer here", said to the speaker in a
    room, became the night light on this computer's screen."""
    try:
        info = origins.current(conversation)
    except Exception:  # noqa: BLE001 -- the session starts without it
        return ""
    if not info.get("channel") and channel in origins.CHANNELS:
        talking_to, at_computer = origins.CHANNELS[channel]
        info.update(channel=channel, talking_to=talking_to, at_this_computer=at_computer)
    text = origins.describe({**info, "person": None})
    return f"\n\nWHERE THE USER IS: {text}" if text else ""


def build_live_config(config):
    shared = build_session_config(config)
    declared = shared["delegation"]["responses"]["tools"]
    instructions = shared["instructions"]
    # Prompt v2 (voice/prompt_v2.py) already carries the catalog and speaker
    # text in its own wording; only the legacy prompt gets these appended.
    legacy = resolve_prompt_version(config) == "legacy"
    if getattr(config, "tool_picker", False):
        myapi_on = getattr(config, "myapi_enabled", False) and myapi.is_connected()
        declared = [t for t in declared if t["name"] == "end_conversation"] + catalog.declared(myapi_on)
    if legacy and getattr(config, "tool_picker", False):
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
    if legacy and getattr(config, "speaker_id_enabled", False):
        instructions += (
            "\n\nWHO IS SPEAKING: this machine recognizes enrolled household members by their voice. A message "
            "that starts with [Speaker] names the person talking from then on, until another one arrives; it is "
            "not a request and needs no reply. On their own phone it is certain; by voice it is a match. Treat 'I', 'my' and 'me' as that person (their computer, calendar, "
            "messages, preferences). Be personal with them: call them by their first name, in your first reply "
            "after you learn who it is and regularly after that, the way someone who knows the family would "
            "('Yes, Dana?', 'It ends at 20:45, Dana. Anything else I can help you with?'), and follow what the "
            "household notes say about treating that person. Before any [Speaker] "
            "message, or after one saying the voice is not recognized, you do not know who is speaking: do not "
            "guess, and ask before acting on something that depends on whose it is. If someone only SAYS who "
            "they are, you may talk to them as that person (name, tone), but a spoken name is not proof: nothing "
            "private or consequential of that person's on its strength. When a recognized person asks you to "
            "remember or improve the recognition of their voice, call remember_voice with their name; if it says "
            "more speech is needed, ask them to talk for about ten seconds and call it again.")
        household = speaker_id.people()
        if household:
            instructions += ("\nThe household: " + "; ".join(
                speaker_id.describe(key, entry) for key, entry in household.items()) + ".")
        if style := speaker_id.house_style():
            instructions += "\nThe household's own words on how to be with them: " + style
    if getattr(config, "speaker_id_enabled", False):
        tools.append({
            "name": "remember_voice",
            "description": ("Add what the person speaking has just said to their own voice recordings, so they are "
                            "recognized better on this microphone. Only for a person who is already recognized by "
                            "voice in this conversation; it refuses anyone else."),
            "parameters_json_schema": {"type": "object", "properties": {
                "name": {"type": "string", "description": "The person's name, as in the [Speaker] message."}},
                "required": ["name"]},
            "behavior": "BLOCKING"})
    languages = transcription_languages(getattr(config, "user_languages", None))
    return {"response_modalities": ["AUDIO"], "system_instruction": instructions,
            "input_audio_transcription": ({"language_codes": languages} if languages else {}),
            "output_audio_transcription": {},
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


class StuckTurn(fallback.Reconnect):
    """The model stopped answering and a forced turn closure did not help."""


def gemini_client(config):
    """google-genai's client, or None without a Gemini key on this machine:
    the conversation then runs on the Gateway models of the chain alone."""
    from google import genai
    try:
        return genai.Client(api_key=Path(config.gemini_api_key_path).read_text().strip())
    except OSError:
        return None


def live_connect(client, config, model: str, live_config: dict):
    """`async with` the Live session of one model of the chain: Google's own
    connection, or the same interface over Vercel AI Gateway."""
    def regular():
        if fallback.via_gateway(model):
            from . import gateway_live
            return gateway_live.connect(config, model, live_config)
        if client is None:
            raise ConnectionError(f"No Gemini API key for {model}")
        return client.aio.live.connect(model=model, config=live_config)
    return extensions.live_session(config, model, live_config, regular)  # or the user's own (voice/extensions.py)


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
        self._blocking_calls = set()  # pending calls the model must wait for (stuck-turn guard)
        self._tool_done_at = 0.0
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
        # Who is talking when it is known without a voice (core/identities.py):
        # the person of a paired phone (phone/gemini.py), the owner typing (voice/text_chat.py).
        self.known_identity: identities.Identity | None = None
        # The channel when no conversation in context says it (core/origin.py
        # CHANNELS): the desktop text chat has no saved conversation.
        self.channel: str | None = None
        self._task_seen: dict = {}  # task id -> (what task_status said, monotonic time) (_task_status_repeat)
        self._offers_turn, self._offers = 0.0, 0  # use_tool offers in this user turn (_offer_result)
        self._refused_turn, self._refused = 0.0, {}  # calls Jev sent back in this user turn (_sent_back)
        self._awaiting_since = 0.0  # 0: no reply pending
        self._playback_until = 0.0  # monotonic time the queued speech finishes
        self._echo_levels = deque(maxlen=150)   # mic RMS while she speaks (~3s)
        self._barge_run = deque(maxlen=5)
        self._barge_open = False
        self._gated_frames = 0
        self._barge_count = 0
        self._splice_armed = False   # user words transcribed, no reply yet
        self._splice_abort_rms = self.SPLICE_ABORT_RMS  # the phone bridge sets its own (phone/gemini.py)
        self._voice_rms: float | None = None  # speech for speaker recognition; the satellite sets a lower one
        self._splice_until = 0.0
        self._loud_run = 0
        self._speech_levels = deque(maxlen=200)  # rms of loud-run frames, logged for calibration
        self._last_loud_at = 0.0
        self._spliced_at = 0.0
        self._force_close_at = 0.0
        self._stalled_since = 0.0  # first splice since her last reply
        self._forced_at = 0.0      # a forced closure still waiting for her reply
        self._stall_restarts = 0
        self._stalled_restart = False
        # Reconnects after the connection closed under her (_resume_dropped):
        # one in a row, counted again once the new connection completes a turn.
        self._drop_restarts = 0
        self._dropped_restart = ""  # why the last one closed, until the new connection says so
        self.restart_stalled = False   # set where the caller reconnects on StuckTurn (run(), phone/gemini.py)
        self.ack_silent_work = False   # set where _say_clip is heard by the user (same two places)
        self._silent_since = 0.0   # start of her first blocking call since the user's latest words
        self._voices = speaker_id.Tracker() if getattr(config, "speaker_id_enabled", False) else None
        self._silent_acked = False
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
        # Unbacked-claim guard (voice/escalation.py): a reply that says it is
        # being done when no tool that acts ran for the user's latest words.
        self._executed = []        # (tool that ran, its args, monotonic time it returned)
        self._claim_mark = -1      # where the user's latest words were when her turn last completed
        self._claim_since = 0.0    # her completed turn before those words: what ran since is for them
        self._claim_checked = -1   # the mark already judged, or answered by an automatic prompt
        self._claim_check = None
        self._claim_again = False
        self._fast_judging = False  # the fast path is judging or running the latest utterance
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
        # The user's latest words, for log correlation (core/logs.py): a new
        # turn starts with the first words heard after her turn ended.
        self._turn_id: str | None = None
        self._turn_open = False
        self.on_connected = None
        self.on_message = None
        # Session-scoped capabilities (for example phone-chat file sharing).
        # They are declared only by that session and never enter the global action catalog.
        self.extra_tools: dict[str, object] = {}
        if self._voices is not None:
            self.extra_tools["remember_voice"] = self._remember_voice
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
                    runner.default_runner().cancel(owner=self, tags=set(message.tool_call_cancellation.ids or []),
                                                   reason="cancelled by the conversation")
                if message.tool_call:
                    self._replied()
                    for call in message.tool_call.function_calls or []:
                        if call.name == "end_conversation":
                            from ..execution.browser_jev import cancel_browser_tasks
                            cancel_browser_tasks()
                            runner.default_runner().cancel(owner=self, reason="the conversation ended")
                            self._hangup.set()
                            return
                        if call.id not in self._seen:
                            self._seen.add(call.id)
                            self._pending_calls.add(call.id)
                            if call.name not in NON_BLOCKING_ACTIONS:
                                self._blocking_calls.add(call.id)
                            self._calls.put_nowait(call)
                server = message.server_content
                if not server:
                    continue
                for role, transcription in (("user", server.input_transcription), ("assistant", server.output_transcription)):
                    if transcription and transcription.text:
                        self._transcript.append({"role": role, "text": transcription.text})
                        if role == "user":
                            if not self._turn_open:
                                self._turn_id, self._turn_open = logs.new_id(), True
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
                            # Her own voice echoed back (reading an email aloud) is
                            # not the user's words for the taint check.
                            self._input_guard.heard_user(
                                transcription.text if self.user_spoke_at == self._last_user_speech else "")
                            try:
                                taken = await extensions.heard(self, transcription.text)
                            except fallback.Reconnect:
                                # The extension took these words and wants a new connection.
                                self._utterance = []
                                if self.restart_stalled:
                                    raise
                                self._hangup.set()  # this conversation cannot reconnect: the next one will
                                return
                            if taken:
                                self._utterance = []  # an extension took these words: none for the fast path
                if server.interrupted or server.turn_complete:
                    self._turn_open = False
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
                    self._drop_restarts = 0  # this connection works: a later drop may reconnect again
                    self._maybe_check_claim(bool(server.interrupted))  # reads the previous _last_turn_done_at
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
                self._call_done(call.id)
                continue
            if call.id in self._interrupted_calls:
                await session.send_tool_response(function_responses=types.FunctionResponse(
                    id=call.id, name=call.name, response={"ok": False,
                    "message": "Not executed: user interrupted before this action started. Wait for the current request."}))
                self._call_done(call.id)
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
            self._fast_judging = False
            await asyncio.sleep(0.1)
            if not self._utterance or self._utterance_checked:
                continue
            if time.monotonic() - self._last_user_speech < self.USER_PAUSE_SECONDS:
                continue
            self._utterance_checked = self._fast_judging = True  # until back at the top (_judge_claim waits)
            text = " ".join("".join(self._utterance).split())
            started = time.monotonic()
            decision, route = await asyncio.to_thread(jev_fast.judge, text, origin=self._origin())
            if route:
                self._route_hint = {**route, "request": text, "at": time.monotonic()}
            # A whole job must not depend on Gemini remembering to make one
            # particular tool call.  The old failure-only escalator could not
            # see the common failure mode where she successfully fetched one
            # input, said she was continuing, and then simply stopped.  A
            # confident first-pass whole-task route is safe to hand off: the
            # Task Runtime owns continuation and still enforces every approval
            # boundary itself.
            # 0.9 handed off one request in ten days of journal (2026-09-25 to
            # 10-05). Six scored 0.8-0.89 and five of those were whole jobs she
            # then lost her way in step by step ("fix it all", the expense
            # report twice, "do what I was supposed to do", ssh to the home
            # server and find a folder); below 0.8 they are mostly single steps.
            if (route and route.get("route") == "whole_task" and route.get("p", 0) >= PROACTIVE_TASK_P
                    and self._proactive_task_turn != text):
                self._proactive_task_turn = text
                groups, _ = self._user_turns()
                goal = escalation.proactive_goal(groups)
                await self._escalate(goal, proactive=True)
                log.info("Jev fast path: %r -> proactive start_task route=%s", text[:120], route)
                continue
            if decision is None:
                log.info("Jev fast path: no fast command in %r route=%s (%.0fms)", text[:120], route,
                         (time.monotonic() - started) * 1000)
                await self._check_dispute()
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
            self._as_speaker()
            result = await self._tool(tool, args, jev_fast.execute, guarded=False)
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
        return self._jev_already_named(call.name, dict(call.args or {}))

    def _jev_already_named(self, name: str, given: dict):
        """_jev_already for a tool by name: a use_tool call is checked as the tool it resolved to."""
        done = self._jev_done
        if not done or time.monotonic() - done[2] > self.JEV_DEDUPE_SECONDS:
            return None
        tool, args, _, message, text = done
        if name != tool:
            return None
        if given == args:
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

    # Journal 2026-10-05 18:36:23-18:36:49 and 18:38:16-18:38:28 (phone): she
    # answered a spoken request with three and then two BLOCKING terminal
    # calls and not one word; the user kept asking whether she was there.
    # The model cannot speak, or be prompted to, until a blocking call
    # returns, so this is a pre-rendered clip (voice/fallback.py), once per
    # silent stretch and only after words the user spoke aloud.
    SILENT_WORK_HEARD_SECONDS = 30.0

    def _silent_work_due(self, now: float) -> bool:
        if (not self.ack_silent_work or self._silent_acked or not self._silent_since
                or not self._running_blocking or not self._audio.empty()
                or now - self._silent_since < self.ACK_AFTER_SECONDS):
            return False
        heard = max(self._last_user_speech, self._last_loud_at)
        return (bool(heard) and self._last_audio_at < heard
                and self._silent_since - heard < self.SILENT_WORK_HEARD_SECONDS)

    async def _task_status(self, session) -> None:
        """Say "heard you, on it" when a background task starts silently, and
        "still on it" while it runs long."""
        from google.genai import types
        while not self._hangup.is_set():
            await asyncio.sleep(0.2)
            now = time.monotonic()
            if self._silent_work_due(now):
                self._silent_acked = True
                log.info("Silent work acknowledged: session=%s after=%.1fs", self._audit_session,
                         now - self._silent_since)
                await self._say_clip("working")
                continue
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
            failed = fallback.is_down(self.config.gemini_model)  # else its key is not on this machine
            log.warning("Voice conversation starting on %s: %s %s", models[0], self.config.gemini_model,
                        "failed recently" if failed else "has no API key")
            if failed and fallback.should_announce():
                await self._say_clip("switching", self.config.gemini_model)
        return models

    async def _fall_back(self, models: list[str], index: int, error: BaseException) -> int | None:
        """After models[index] failed: the index of the model to go on with,
        or None to let the error end the conversation."""
        # A model that keeps stalling after MAX_STALL_RESTARTS reconnects is failing too.
        if self._hangup.is_set() or not (isinstance(error, StuckTurn) or fallback.is_provider_failure(error)):
            return None
        model = models[index]
        fallback.mark_down(model, error)
        if quota.is_quota_error(str(error)):
            # The user still has to know, even though the conversation goes on.
            quota.report("vercel" if fallback.via_gateway(model) else "gemini", f"{model}: {error}",
                         user_initiated=True)
        later = fallback.next_index(models, index, error)
        if later is None:
            if len(models) > 1:
                await self._say_clip("failed")
            return None
        log.warning("Voice model falling back: session=%s %s -> %s", self._audit_session, model, models[later])
        if model == self.config.gemini_model and fallback.should_announce():
            await self._say_clip("switching", model)
        self._stall_restarts = 0
        self._reset_stall()
        return later

    async def _carry_over(self, session) -> None:
        stalled, self._stalled_restart = self._stalled_restart, False
        dropped, self._dropped_restart = self._dropped_restart, ""
        if dropped:
            log.warning("Gemini session reconnected after %s: session=%s", dropped, self._audit_session)
        text = fallback.carry_over(self._transcript, stalled, dropped=bool(dropped))
        if text:
            # After a stall or a drop she owes the user an answer: that one is a turn of its own.
            await session.send_client_content(turns={"role": "user", "parts": [{"text": text}]},
                                              turn_complete=stalled or bool(dropped))
            if stalled or dropped:
                self._awaiting_since = time.monotonic()

    def _reset_stall(self) -> None:
        self._stalled_since = self._forced_at = self._force_close_at = self._spliced_at = self._splice_until = 0.0
        self._splice_armed = False

    def _resume_stalled(self, error: BaseException) -> bool:
        """After fallback.Reconnect (a stalled turn, or a session the
        provider ended on schedule): True to connect to the same model
        again and go on."""
        if not isinstance(error, fallback.Reconnect) or self._hangup.is_set():
            return False
        stalled = isinstance(error, StuckTurn)
        if stalled and self._stall_restarts >= self.MAX_STALL_RESTARTS:
            return False  # _fall_back moves to the next model
        self._stall_restarts += stalled
        self._stalled_restart = stalled
        self._reset_stall()
        log.warning("Voice conversation reconnecting: session=%s stalled=%s restart=%d: %s", self._audit_session,
                    stalled, self._stall_restarts, str(error)[:120])
        return True

    MAX_DROP_RESTARTS = 1

    def _resume_dropped(self, error: BaseException) -> bool:
        """After the connection closed under the conversation for a
        transient reason (core/retry.py classify_live: 1011, 1006, 1001, a
        network blip; not quota, policy or auth): True to connect to the same
        model once more, with the same config, and carry the conversation
        over (fallback.DROPPED). Only once in a row: when that connection
        fails before it completes a turn, _fall_back decides as before."""
        if self._hangup.is_set() or isinstance(error, fallback.Reconnect):
            return False
        failure = retry.classify_live(error)
        if not retry.live_transient(failure) or self._drop_restarts >= self.MAX_DROP_RESTARTS:
            return False
        self._drop_restarts += 1
        self._dropped_restart = failure.detail.split(" ", 1)[0] if failure.detail[:1].isdigit() else failure.kind
        self._reset_stall()
        # The calls asked on the closed connection cannot be answered on the
        # new one (a late background result is logged as lost), and the
        # blocking one cancelled with its worker never counted itself down.
        self._pending_calls.clear()
        self._blocking_calls.clear()
        self._running_blocking = 0
        log.warning("Gemini session dropped: session=%s %s (%s); reconnecting once", self._audit_session,
                    failure.kind, failure.detail[:120])
        return True

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
                self._claim_checked = self._user_mark()  # her next turn answers this, not the user
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
                self._claim_checked = self._user_mark()
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
                self._claim_checked = self._user_mark()
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

    async def _check_dispute(self) -> None:
        """The user says her result is wrong (voice/escalation.py): arm the
        self-check for her next tool result, or hand the job over the second time."""
        if not self._escalator.reviewable(time.monotonic()):
            return
        groups, _ = self._user_turns()
        said, seen_user = [], False
        for entry in reversed(self._transcript):  # what she said before the user's latest words
            if entry["role"] == "user":
                if said:
                    break
                seen_user = True
            elif seen_user:
                said.append(entry["text"])
        reply = " ".join("".join(reversed(said)).split())
        goal = await asyncio.to_thread(self._escalator.disputed, time.monotonic(), groups, reply)
        if goal:
            await self._escalate(goal)
        elif self._escalator._review_note:
            log.info("Self-check armed: session=%s the user disputes her result: %r", self._audit_session,
                     (groups[-1] if groups else "")[:120])

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

    def _user_mark(self) -> int:
        """Where the user's latest words are in the transcript (-1: none yet)."""
        return next((i for i in range(len(self._transcript) - 1, -1, -1) if self._transcript[i]["role"] == "user"), -1)

    @staticmethod
    def _task_running() -> bool:
        """A background task of this conversation is at work: what she says
        about it is not a claim with nothing behind it."""
        try:
            from ..core import conversations
            from ..runtime.task import ACTIVE, TaskStore
            cid = conversations.CURRENT.get()
            return any(t.status in ACTIVE and t.source == "voice" and t.conversation == cid
                       for t in TaskStore().list(20))
        except Exception:  # noqa: BLE001 -- when unsure, let Jev judge the reply
            return False

    # Her words can be transcribed just after the turn that spoke them ends.
    CLAIM_SETTLE_SECONDS = 0.4
    # ... and the fast path may still be judging, or doing, what the user said.
    CLAIM_FAST_WAIT_SECONDS = 6.0

    def _maybe_check_claim(self, interrupted: bool = False) -> None:
        # Called when her turn completes. 2026-10-12: "on it, starting the
        # countdown" after only reading the skill, then "I'm making sure it is
        # running" to a typed "I heard nothing": two complete turns, no call
        # that acts, nothing running. Code finds such a turn; Jev reads it
        # (escalation.jev_unbacked) off the event loop, one check at a time.
        mark = self._user_mark()
        if mark != self._claim_mark:
            self._claim_mark, self._claim_since = mark, self._last_turn_done_at
        if interrupted or mark < 0 or mark == self._claim_checked:
            return
        if self._claim_check is not None:
            self._claim_again = True
            return
        self._claim_check = asyncio.create_task(self._check_claim())
        self._bg_tasks.add(self._claim_check)
        self._claim_check.add_done_callback(self._bg_tasks.discard)

    async def _check_claim(self) -> None:
        try:
            while True:
                self._claim_again = False
                await self._judge_claim()
                if not self._claim_again:
                    return
        finally:
            self._claim_check = None

    async def _judge_claim(self) -> None:
        mark, since = self._claim_mark, self._claim_since
        await asyncio.sleep(self.CLAIM_SETTLE_SECONDS)
        fast, waited = getattr(self.config, "jev_fast_path", False), time.monotonic() + self.CLAIM_FAST_WAIT_SECONDS
        while ((self._fast_judging or (fast and self._utterance and not self._utterance_checked))
               and time.monotonic() < waited):
            await asyncio.sleep(0.1)
        if mark != self._user_mark() or mark == self._claim_checked:
            return  # the user went on speaking, or an automatic prompt followed: not this turn
        groups, reply = self._user_turns()
        if (not groups or not reply or self._pending_calls or self._running_background or self._running_blocking
                or self._working):
            return  # nothing said, or a call still runs: the turn that reports it is the one to judge
        self._claim_checked = mark  # each user turn is judged once
        acted = [tool for tool, args, at in self._executed if at >= since and not switchboard._reads(tool, args)]
        if (acted or (self._jev_done and self._jev_done[2] >= since) or self._fast_judging
                or (self._proactive_task_turn and self._proactive_task_turn in groups[-1])
                or not self._escalator.claimable(time.monotonic())):
            return  # something that acts ran for these words, or the job was just handed over
        if await asyncio.to_thread(self._task_running):
            return
        goal, evidence = await asyncio.to_thread(self._escalator.unbacked, time.monotonic(), groups, reply, since)
        if not goal:
            log.info("Unbacked claim: none in %r -> %r evidence=%s", groups[-1][:120], reply[:120], evidence)
            return
        log.info("Unbacked claim: %r -> %r with no tool that acts; handing it over evidence=%s", groups[-1][:120],
                 reply[:120], evidence)
        await self._escalate(goal, unbacked=True)

    async def _read_skill_wanted(self, call, result: ActionResult, heard: str, asked: str,
                                 request: str) -> ActionResult | None:
        """She read a skill the user asked her to carry out: it is started
        here (execution/actions.py run_skill) and she gets that result in
        place of the text. 2026-10-12: given a skill's five steps as text, she
        said "on it" and made no other call. None: leave her the text."""
        from ..core import skills
        try:
            found = json.loads(result.message)
        except ValueError:
            return None
        name = (found.get("skill") or found.get("name")) if isinstance(found, dict) else None
        if not name:
            return None
        skill = {"name": name, "description": found.get("description", ""),
                 "body": found.get("instructions") or found.get("body") or ""}
        if not await asyncio.to_thread(skills.wants_run, heard or asked, skill, asked=asked if heard else ""):
            return None
        started = await self._tool("run_skill", {"name": name, "request": request}, run_action, call.id,
                                   guarded=False)
        log.info("Skill run: call=%s skill=%s request=%r -> ok=%s %r", call.id, name, request[:120], started.ok,
                 started.message[:160])
        if not started.ok:
            return started
        return ActionResult(True, f"The user asked for the skill '{name}' to be carried out, so it was started "
                                  "instead of only read: " + started.message)

    async def _escalate(self, goal: str, proactive: bool = False, unbacked: bool = False) -> None:
        log.info("Escalating to the Task Runtime: session=%s goal=%r", self._audit_session, goal[:600])
        self._as_speaker()  # the task runs as them (runtime/runtime.py)
        result = await self._tool("start_task", {"goal": goal}, run_action, guarded=False)
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
        self._announcements.put_nowait({"notice": escalation.notice(task_id, bool(started.get("existing")), proactive,
                                                                    unbacked)})

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
        # Her last call, when it failed (not one Jev itself sent back): what she
        # does next is recovery, whatever the user's latest words were.
        ran = [e for e in self._action_log[-6:] if not str(e.get("message", "")).startswith(_SENT_BACK)]
        last = ran[-1] if ran else None
        failed = ({"tool": last["action"], "args": last["args"], "error": last["message"]}
                  if last and not last["ok"] and time.time() - last.get("ts", 0) < 180 else None)
        return switchboard.Context(request, groups[-4:-1], {k: hint[k] for k in ("route", "p")} if hint else None,
                                   calls, failed=failed, origin=self._origin() or None)

    def _origin(self) -> dict:
        """Where the request being handled came from (core/origin.py), None
        fields dropped, for every Jev decision on it: the fast pass, the
        catalog pick and the switchboard. Read from the conversation in this
        task's context (run(), voice/satellite.py and phone/gemini.py set it
        before the workers start; asyncio.to_thread carries it into Jev's
        thread) and the person talking now, with `channel` as the fallback."""
        self._as_speaker()
        info = origins.current()
        if not info.get("channel") and self.channel in origins.CHANNELS:
            talking_to, at_computer = origins.CHANNELS[self.channel]
            info.update(channel=self.channel, talking_to=talking_to, at_this_computer=at_computer)
        return {k: v for k, v in info.items() if v is not None}

    async def _pick(self, call) -> catalog.Resolution:
        """use_tool: Jev picks the catalog tool (execution/catalog.py)."""
        groups, _ = self._user_turns()

        def fill(tool, request, heard):
            from .omarchy import GatewayClient
            return catalog.fill_args(tool, request, heard,
                                     lambda system, user: GatewayClient(self.config).complete_json(system, user,
                                                                                                    timeout=8))
        pick = await asyncio.to_thread(catalog.resolve, dict(call.args or {}), groups[-1] if groups else "",
                                       myapi_on=self.config.myapi_enabled and myapi.is_connected(), fill=fill,
                                       origin=self._origin())
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

    def identity(self) -> identities.Identity:
        """Who is talking now: a known identity (attached phone, typed) for
        the whole session, else the voice recognized last, else nobody."""
        if self.known_identity is not None and self.known_identity.person:
            return self.known_identity
        speaker = self._voices.speaker if self._voices is not None else None
        return identities.Identity(speaker, "voice") if speaker else identities.Identity()

    def _as_speaker(self) -> None:
        """Tools run from here on act as whoever is talking now (asyncio.to_thread
        copies this into their thread). Each worker task has its own context."""
        identities.set_current(self.identity())

    async def _tool(self, name: str, args: dict, execute, call_id: str | None = None, *, guarded: bool = True):
        """One tool call on the ToolRunner: its tool's deadline (a hung call
        is answered "timed out after N s; nothing was confirmed"), cancelled
        with this conversation or its call id. `guarded` runs it through
        InputGuard, as the model's own calls always were."""
        result = await runner.default_runner().arun(
            name, args, execute=execute, wrap=self._input_guard.run if guarded else None, owner=self, tag=call_id)
        if getattr(result, "untrusted", False):  # an unguarded call's content taints this conversation too
            self._input_guard.taint.mark(getattr(result, "provenance", ""))
        return result

    def _run_tool(self, name: str, args: dict) -> ActionResult:
        """run_action, except unlock_screen from the paired phone (the only
        place it may run; run_action itself refuses it)."""
        if name == "unlock_screen" and self.from_paired_phone:
            from ..execution.actions import unlock_screen_for_paired_phone
            return unlock_screen_for_paired_phone()
        return run_action(name, args)

    # The focused window is only a note in the action log, so it never holds
    # a tool back for long. Real bug (2026-10-10): the compositor had died,
    # hyprctl hung for its full 10s in front of every tool call, and a smart-home
    # light took 11s. A user tool does not wait for it at all.
    WINDOW_NOTE_SECONDS = 0.25

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

    TASK_REPEAT_SECONDS = 10

    def _task_status_repeat(self, args: dict, result: ActionResult) -> ActionResult:
        """task_status for a task that has not changed since she asked a moment
        ago: a short answer that says so. Journal 2026-10-15 20:48:06-20:48:38:
        about 40 task_status calls in 32 seconds for one task, each the full
        payload. Her next poll within TASK_REPEAT_SECONDS is short again."""
        if not result.ok or (args or {}).get("list"):
            return result
        try:
            summary = json.loads(result.message)
        except ValueError:
            return result
        if not isinstance(summary, dict) or not summary.get("id"):
            return result
        seen = {k: summary.get(k) for k in ("status", "phase", "steps", "last_step", "pending_approval",
                                            "question", "result", "errors")}
        now = time.monotonic()
        self._task_seen = {k: v for k, v in self._task_seen.items() if now - v[1] <= self.TASK_REPEAT_SECONDS}
        before = self._task_seen.get(summary["id"])
        self._task_seen[summary["id"]] = (seen, now)
        if before is None or before[0] != seen:
            return result
        short = {"id": summary["id"], "status": summary.get("status"), "phase": summary.get("phase"),
                 "steps": summary.get("steps"), "unchanged": True,
                 "note": (f"Nothing changed since you checked {now - before[1]:.0f}s ago. The user will be told "
                          "when this task changes or finishes, without you checking. Stop calling task_status for "
                          "it now; tell the user it is still working, or go on with the conversation.")}
        if summary.get("question"):
            short["question"] = summary["question"]
        return ActionResult(True, json.dumps(short, ensure_ascii=False))

    MAX_SAME_REFUSAL = 2  # the same call, sent back twice: the third answer says stop

    def _sent_back(self, name: str, args: dict, message: str) -> ActionResult:
        """A call Jev sent back (reject or ask). Journal 2026-10-07 21:01: the
        same terminal_task came back nine times in seven seconds, each time
        with the same cached verdict and the same message."""
        if self._refused_turn != self._last_user_speech:
            self._refused_turn, self._refused = self._last_user_speech, {}
        key = json.dumps([name, args], sort_keys=True, ensure_ascii=False, default=str)
        self._refused[key] = self._refused.get(key, 0) + 1
        if self._refused[key] <= self.MAX_SAME_REFUSAL:
            return ActionResult(False, message)
        return ActionResult(False, f"Not run: this exact {name} call was already sent back {self._refused[key] - 1} "
                                   "times and will be sent back again. Stop sending it. Either make a different "
                                   "call that does what the user asked, or tell the user in one short sentence "
                                   "what you could not do and ask how they want to go on.")

    async def _review(self, call, name: str, args: dict) -> switchboard.Verdict:
        ctx = self._switchboard_context()
        ctx.description = _TOOL_TEXT.get(name) or catalog.catalog().get(name, {}).get("description", "")
        verdict = await asyncio.to_thread(self._switchboard.review, name, args, ctx)
        log.info("Switchboard: call=%s name=%s decision=%s%s evidence=%s (%.0fms)", call.id, name, verdict.action,
                 f" -> {verdict.tool}" if verdict.action == "reroute" else "", verdict.evidence, verdict.ms)
        return verdict

    async def _run_call(self, session, call) -> None:
        # Its log lines, and its tool's (worker included), carry these (core/logs.py).
        with logs.bind(session_id=getattr(self, "_audit_session", None), turn_id=getattr(self, "_turn_id", None),
                       tool_call_id=call.id):
            await self._run_call_bound(session, call)

    async def _run_call_bound(self, session, call) -> None:
        from google.genai import types
        self._as_speaker()  # checked at each call: the speaker can change mid-session
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
            if self._silent_since <= max(self._last_user_speech, self._last_loud_at):
                # Her first call since the user's latest words.
                self._silent_since, self._silent_acked = time.monotonic(), False
        if self._overlay:
            await asyncio.to_thread(watchdog.tool_call, call.name, call.args or {})
        ran, ran_args = call.name, dict(call.args or {})  # the tool that actually ran (a reroute changes it)
        try:
            already = self._jev_already(call)
            redirect = self._mission_redirect(call)
            if already is not None:
                result = ActionResult(already["ok"], already["message"])
            elif redirect is not None:
                result = redirect
            elif call.name in self.extra_tools:
                result = await self._tool(call.name, dict(call.args or {}),
                                          lambda name, args: self.extra_tools[name](args), call.id, guarded=False)
            elif call.name == "run_mission":
                result = await self._run_mission(session, call.args or {})
            elif call.name == "get_recent_actions":
                result = ActionResult(True, LiveSession._get_recent_actions(self, (call.args or {}).get("target")))
            elif call.name == "use_tool" and not (pick := await self._pick(call)).run:
                result = self._offer_result(pick)
            elif call.name == "use_tool" and (again := self._jev_already_named(pick.tool, dict(pick.args))) is not None:
                ran = pick.tool
                result = ActionResult(again["ok"], again["message"])
            elif call.name == "use_tool" and pick.tool == "get_recent_actions":
                ran = pick.tool
                result = ActionResult(True, LiveSession._get_recent_actions(self, pick.args.get("target")))
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
                early = (asyncio.create_task(self._tool(tool, args, run_action, call.id))
                         if tool in switchboard.READ_ONLY else None)
                verdict = await review
                name, prefix = tool, ""
                if verdict.action == "reroute":
                    name, args, prefix = verdict.tool, verdict.args, verdict.message + " "
                    ran = name
                ran_args = dict(args or {})
                looking = asyncio.create_task(asyncio.to_thread(LiveSession._current_window))
                if not user_tools.exists(name):
                    # A desktop action can change the focus: note the window first.
                    await asyncio.wait({looking}, timeout=self.WINDOW_NOTE_SECONDS)
                if verdict.action in ("reject", "ask"):
                    action = None
                    result = self._sent_back(tool, args, verdict.message)
                elif early is not None and name == tool:
                    action = early
                else:
                    if writes_externally(name, args):
                        # The switchboard's dedicated `confirmed` judgment is
                        # semantic and multilingual. InputGuard still pins that
                        # approval to this exact payload before execution.
                        self._input_guard.approve_external_write(name, args)
                    action = asyncio.create_task(self._tool(name, args, self._run_tool, call.id))
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
                window = looking.result() if looking.done() and not looking.exception() else {}
                if ran == "task_status" and action is not None:
                    result = self._task_status_repeat(ran_args, result)
                if call.name == "read_file" and result.ok and "[assistant note] This file is a multi-step script" in result.message:
                    self._script_read_at = time.monotonic()
                    # The planner fills missing step details from the script itself.
                    self._script_text = provenance.unfence(result.message).split("\n\n[assistant note]", 1)[0]
                if ran == "close_window" and result.ok:
                    self._action_log = [e for e in self._action_log if e["window"] != window]
                else:
                    self._action_log.append({"action": call.name, "args": call.args or {}, "ok": result.ok, "message": result.message, "call_id": call.id, "ts": time.time(), "window": window})
                    self._action_log = self._action_log[-100:]
            if ran in ("find_skill", "load_skill") and result.ok:
                # The user's own words say whether to carry it out. find_skill's
                # request is those words with references resolved (the task's
                # title); use_tool's only says which tool was wanted.
                resolved = str(ran_args.get("request") or "")
                asked = resolved or str((call.args or {}).get("request") or "")
                heard = "" if original_request.startswith("(no user words:") else original_request
                if (started := await self._read_skill_wanted(call, result, heard, asked,
                                                             resolved or heard or asked)) is not None:
                    ran, ran_args, result = "run_skill", {}, started
            message = result.message
            if result.ok and ran.startswith("myapi_"):
                refinement_request = str((call.args or {}).get("request") or original_request)
                try:
                    # Ranked inside its fence: the content stays marked untrusted.
                    refined = await asyncio.to_thread(
                        provenance.rewrap, message,
                        lambda inner: refine_myapi_result(self.config, refinement_request, inner),
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
        if ran != "use_tool" and not response["message"].startswith(_NOT_RUN):
            self._executed = self._executed[-20:] + [(ran, ran_args, time.monotonic())]
        # A skill started is a task started: nothing else escalates the same job.
        self._escalator.observe_call("start_task" if ran == "run_skill" and response["ok"] else ran,
                                     dict(call.args or {}), response["ok"], response["message"], time.monotonic())
        self._maybe_escalate()
        if call.name not in NON_BLOCKING_ACTIONS and (note := self._escalator.take_review_note(time.monotonic())):
            # In the result she is reading now, not a prompt rule (see _SCRIPT_HINT).
            response = {**response, "message": response["message"] + note}
            log.info("Self-check delivered: session=%s call=%s", self._audit_session, call.id)
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
                self._call_done(call.id)
                return
            log.info("Gemini tool response delivered: session=%s call=%s interrupted=%s",
                     self._audit_session, call.id, call.id in self._interrupted_calls)
        self._call_done(call.id)

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
    # Journal 2026-10-05 16:04:52-16:05:02 (phone): she was inside an 8s
    # terminal_read when a late transcript re-armed the guard. The model
    # cannot speak until a blocking tool returns, so the 4s of silence
    # covered whatever the user said next and the forced closure then cut
    # in on her own answer ("Gemini interrupted"). A blocking call in
    # flight is her reply: the guard waits for it, then counts from its end.
    FORCE_CLOSE_AFTER_SECONDS = 8.0
    # Journal 2026-10-05 18:37:16-18:38:16 (phone): 60s without a reply. The
    # user repeated themselves seven times and each repeat put the forced
    # closure off again, so it ran once; on 2026-10-04 14:29 (65s) never.
    # Once STALL_CAP_SECONDS have passed since the first unanswered splice,
    # the closure goes out the next time the user stops, not 8s after.
    # And the closure itself is often ignored: of 29 in eight days, 10 were
    # answered within 4s, 3 after 8-10s, 16 after 18s+ or never. If none
    # comes within RESTART_AFTER_SECONDS the connection is replaced and the
    # transcript carried over (_resume_stalled), as for a failed model.
    STALL_CAP_SECONDS = 10.0
    RESTART_AFTER_SECONDS = 5.0
    MAX_STALL_RESTARTS = 3

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

    def _call_done(self, call_id) -> None:
        self._pending_calls.discard(call_id)
        if call_id in self._blocking_calls:
            self._blocking_calls.discard(call_id)
            self._tool_done_at = time.monotonic()

    def _replied(self) -> None:
        self._input_guard.assistant_replied()
        if self._spliced_at:
            log.info("Stuck-turn guard: reply %.1fs after the silence splice", time.monotonic() - self._spliced_at)
            self._spliced_at = 0.0
        self._splice_armed = False
        self._splice_until = 0.0
        self._force_close_at = 0.0
        self._stalled_since = self._forced_at = 0.0

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
        if not self._splice_armed or speaking or loud or self._blocking_calls:
            return None
        last_words = max(self._last_user_speech, self._last_loud_at, self._tool_done_at)
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
        if not self._stalled_since:
            self._stalled_since = now
        elif now - self._stalled_since >= self.STALL_CAP_SECONDS:
            self._force_close_at = now - self.FORCE_CLOSE_AFTER_SECONDS  # the user has stopped again: close now
        self._splices += 1
        return bytes(len(chunk))

    def _remember_voice(self, args: dict) -> ActionResult:
        """The recognized speaker adds a recording of themself (speaker_id.remember)."""
        name = str(args.get("name") or "").strip()
        if not name:
            return ActionResult(False, "Whose voice? Pass the person's name.")
        source = "phone" if self.from_paired_phone else "desk"
        ok, message = speaker_id.remember(name, self._voices.recent(), source)
        return ActionResult(ok, message)

    # A recognized speaker is forgotten only when the voice is clearly no one
    # enrolled, twice in a row: the owner's own speech often scores 0.28-0.37,
    # just under speaker_id.MATCH (journal 2026-10), and must not flap.
    STRANGER_SCORE = 0.2
    STRANGER_WINDOWS = 2

    async def _speaker_watch(self, session) -> None:
        """Tell the model who is speaking when the voice is recognized, and
        again when it becomes someone else (voice/speaker_id.py). A session
        whose person is already known (their own phone) needs no voice match."""
        if self.known_identity is not None and self.known_identity.person:
            await session.send_client_content(turns={"role": "user", "parts": [{"text": (
                f"[Speaker] {identities.describe(self.known_identity)} That holds for this whole conversation. "
                "This is context, not a request.")}]}, turn_complete=False)
            return await asyncio.Event().wait()
        if self._voices is None:
            return await asyncio.Event().wait()  # off: a worker that never ends, like the others
        identifier = speaker_id.Identifier()
        if not await asyncio.to_thread(identifier.ready):
            log.warning("Speaker recognition is on but nobody is enrolled: run omarchy-ai-settings enroll-speakers")
            return await asyncio.Event().wait()
        # Said before anyone speaks: with nothing said, she named whoever the
        # household notes made likely and claimed to recognize them (journal
        # 2026-10-09 20:40: one person greeted by another's name, "of course I
        # recognize you", no judgement made). Repeated once when the first judgement is nobody.
        unknown = ("[Speaker] {}. You do not know who is speaking: do not call them by a household member's name "
                   "and do not say you recognize them, unless they tell you who they are. "
                   "This is context, not a request.")
        if self._voices.speaker is None:
            await session.send_client_content(turns={"role": "user", "parts": [{"text": unknown.format(
                "Nobody's voice has been recognized in this conversation yet")}]}, turn_complete=False)
        told_unknown = False
        strangers = 0  # windows in a row where no enrolled voice came close
        try:
            while True:
                await asyncio.sleep(0.25)
                samples = self._voices.due()
                if samples is None:
                    continue
                name, scores = await asyncio.to_thread(identifier.identify, samples)
                log.info("Speaker recognition: session=%s speaker=%s scores=%s", self._audit_session, name,
                         {k: round(v, 2) for k, v in scores.items()})
                if name is None and self._voices.speaker is None and not told_unknown:
                    told_unknown = True
                    await session.send_client_content(turns={"role": "user", "parts": [{"text": unknown.format(
                        "The voice speaking now is not recognized as anyone enrolled")}]}, turn_complete=False)
                stranger = name is None and max(scores.values(), default=0.0) < self.STRANGER_SCORE
                strangers = strangers + 1 if stranger else 0
                if strangers >= self.STRANGER_WINDOWS and self._voices.speaker is not None:
                    # Someone else is talking: nobody's tools (MyApi) act for the last one recognized.
                    self._voices.speaker = None
                    await session.send_client_content(turns={"role": "user", "parts": [{"text": unknown.format(
                        "The voice speaking now is not recognized as anyone enrolled")}]}, turn_complete=False)
                    continue
                if name is None or name == self._voices.speaker:
                    continue  # an unsure stretch changes nothing
                self._voices.speaker = name  # tools now run as them (identity())
                who = speaker_id.describe(name, (await asyncio.to_thread(speaker_id.people)).get(name))
                await session.send_client_content(turns={"role": "user", "parts": [{"text": (
                    f"[Speaker] The person speaking now, recognized by their voice: {who}. "
                    "This is context, not a request.")}]}, turn_complete=False)
        finally:
            # For setting the speech level from what a conversation really held.
            voices = self._voices
            log.info("Speaker recognition heard: session=%s mic=%.1fs speech=%.1fs kept=%.1fs level=%.0f speaker=%s",
                     self._audit_session, voices.heard / speaker_id.RATE, voices.loud / speaker_id.RATE,
                     voices.kept / speaker_id.RATE, self._voice_rms or self._splice_abort_rms, voices.speaker)

    async def _stuck_turn_recovery(self, session) -> None:
        """Force-close a turn when even the silence splice did not unblock it."""
        from google.genai import types
        while True:
            await asyncio.sleep(0.1)
            if self._forced_at and time.monotonic() - self._forced_at >= self.RESTART_AFTER_SECONDS:
                # A pending call's result may still wake her, and a restart would lose it.
                if not self._pending_calls:
                    self._forced_at = 0.0
                    if self.restart_stalled:
                        raise StuckTurn("The voice model gave no reply to a forced turn closure")
            started = self._force_close_at
            if not started or time.monotonic() - started < self.FORCE_CLOSE_AFTER_SECONDS:
                continue
            if self._blocking_calls:
                self._force_close_at = 0.0
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
            self._forced_at = time.monotonic()
            log.warning("Stuck-turn guard: silence splice got no reply for %.1fs; forced turn closure",
                        self.FORCE_CLOSE_AFTER_SECONDS)

    def _gate(self, chunk: bytes, rms: float, now: float) -> list[bytes]:
        """Chunks to send for this mic frame (silence while she speaks)."""
        speaking = now < self._playback_until + self.ECHO_TAIL_SECONDS
        if self._voices is not None:
            # The user's own loud frames open an utterance; her echo is never a voice to recognize.
            self._voices.add(chunk, rms >= (self._voice_rms or self._splice_abort_rms), echo=speaking)
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
        client = gemini_client(self.config)
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
            self.restart_stalled = self.ack_silent_work = True
            index = attempt = 0
            while True:
                model = models[index]
                try:
                    live_config = build_live_config(self.config)
                    live_config["system_instruction"] += origin_note(self.conversation)
                    async with live_connect(client, self.config, model, live_config) as session:
                        if mic is None:
                            argv = ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", "--latency", "20ms"]
                            argv.extend(["--target", self._echo.source])
                            mic = await asyncio.create_subprocess_exec(*argv, "-a", "-", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                        log.info("Gemini Live connected: %s; full-duplex tools enabled", model)
                        if attempt:
                            await self._carry_over(session)
                        else:
                            log.info("Gemini speech detection: start=LOW end=LOW prefix=300ms silence=600ms; real interruptions enabled")
                            if self.on_connected:
                                self.on_connected()
                            await asyncio.to_thread(status_icon.set_live, True)
                        workers = [self._send_audio(session, mic), self._receive(session), self._play_audio(),
                                   self._tools(session), self._stuck_turn_recovery(session), self._task_status(session),
                                   self._speaker_watch(session), self._hangup.wait()]
                        if self.config.watchdog_enabled:
                            workers.append(self._visuals())
                        if self.config.jev_fast_path:
                            workers.append(self._fast_path())
                        workers.append(self._announcer(session))
                        tasks = [asyncio.create_task(worker) for worker in workers]
                        if not attempt:
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
                    attempt += 1
                    if self._resume_stalled(error) or self._resume_dropped(error):
                        continue
                    index = await self._fall_back(models, index, error)
                    if index is None:
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
            runner.default_runner().cancel(owner=self, reason="the conversation ended")
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
            if client is not None:
                await client.aio.aclose()
            await asyncio.to_thread(status_icon.set_live, False)
            if self._overlay:
                self._overlay = False
                await asyncio.to_thread(watchdog.stop)
            await asyncio.to_thread(append_session, list(self._transcript), self.config.context_retention_hours)
            await asyncio.to_thread(conversations.flush, self.conversation)
            # Alerts in the language just spoken, while a model works (core/alert_clips.py).
            alert_clips.prepare_in_background(self.config)
