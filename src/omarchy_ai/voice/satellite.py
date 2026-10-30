"""A voice satellite: an ESPHome voice-assistant speaker in another room that
wakes on the desktop's own wake word and then holds a live conversation.

An ESPHome voice assistant is built for turns: wake word, one request, the
microphone closes, one reply. A live model needs the opposite, a microphone
that stays open while she speaks. The satellite's firmware therefore only
streams its microphone over the ESPHome native API (`voice_assistant` with
`use_wake_word: true`, started with `voice_assistant.start_continuous`; see
docs/voice-satellite.md), and this module, in the place Home Assistant
normally has, decides everything else:

- it listens to that stream with the same openWakeWord model, threshold and
  trigger count as voice/wake.py, so the satellite hears the word the
  desktop hears;
- after the word the stream goes to a Gemini Live session, the same one a
  desktop call uses (tools, context, announcements), until she ends the
  conversation or nobody has spoken for satellite_silence_seconds;
- her speech does NOT go through the device's pipeline (its reply stage
  closes the microphone). It is played by the device's media player as an
  announcement: an MP3 it fetches from here while she is still speaking.
  The microphone stays open, so the user can interrupt her; the device's
  own echo cancellation keeps her from hearing herself.

The device protocol, from ESPHome's voice_assistant.cpp (2026.8):

  device  VoiceAssistantRequest(start, flags)  -> _on_start, returns 0
  device  VoiceAssistantAudio ...              -> _on_audio (wake word, then Gemini)
  us      RUN_START                            at the wake word: ducks its music
  us      WAKE_WORD_END, STT_START             ring: waiting for words
  us      STT_VAD_START                        ring: hearing words
  us      TTS_START                            ring: replying (changes no state)
  us      media player: play <url> as an announcement
  us      RUN_END                              the conversation is over
  device  starts streaming again by itself, for the next wake word

STT_VAD_END, INTENT_PROGRESS and TTS_END are never sent: each of them moves
the firmware out of "streaming microphone". Nor is the media player's stop
command: on the real device (2026-09-30) stopping an announcement that was
still streaming crashed the firmware in its audio mixer (interrupt watchdog,
reboot) on the second try. An interruption ends the MP3 stream instead, as a
normal end of file: 5 of 5 clean, and the speaker is quiet within the lead.
"""
from __future__ import annotations

import asyncio
import copy
import collections
import datetime
import hmac
import json
import logging
import os
import secrets
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

log = logging.getLogger("omarchy_ai.voice.satellite")


def quiet_now(config, now: datetime.datetime | None = None) -> bool:
    """Inside satellite_quiet_hours ("22:00-07:00", local time): a speaker
    starts no conversation by itself. A malformed value means none."""
    spec = str(getattr(config, "satellite_quiet_hours", "") or "").strip()
    if not spec:
        return False
    try:
        start, end = (datetime.time.fromisoformat(part.strip()) for part in spec.split("-"))
    except ValueError:
        log.warning("satellite_quiet_hours %r is not HH:MM-HH:MM; ignored", spec)
        return False
    at = (now or datetime.datetime.now()).time()
    return start <= at < end if start <= end else (at >= start or at < end)


def _wake_threshold(config) -> float:
    """Use speaker-specific tuning when configured, otherwise the shared value."""
    threshold = config.satellite_wake_threshold
    return float(config.wake_threshold if threshold is None else threshold)

WAKE_FRAME_BYTES = 2560      # 1280 samples, 80 ms: openWakeWord's frame
GATE_FRAME_BYTES = 640       # 20 ms: the frame the session's echo gate was tuned on
USE_WAKE_WORD = 2            # VoiceAssistantCommandFlag.USE_WAKE_WORD
MIC_BUFFER_CHUNKS = 600      # ~19 s of 32 ms chunks while Gemini connects
# Connected, but no microphone audio for this long: subscribe again (_stream_watch),
# then twice as long after each try that did not help, up to the maximum.
STREAM_QUIET_SECONDS = 30
STREAM_RETRY_MAX_SECONDS = 600
# How often the stream watch runs (and the device would be pinged: _keep_alive).
KEEPALIVE_SECONDS = 10
# Off: with a ping every 10 s the M5 Atom dropped every 10-50 s instead of
# every 40-140 s (2026-10-26 18:20, live), and its own log showed the API
# over budget and a crash record (IllegalInstruction): the drops are the
# device's, and extra traffic makes them worse.
PING_DEVICE = False
# A satellite_state_command that exits is started again after this long,
# twice as long after each run shorter than HOOK_HEALTHY_SECONDS.
HOOK_RETRY_SECONDS = 5
HOOK_RETRY_MAX_SECONDS = 300
HOOK_HEALTHY_SECONDS = 60
# A reply fetch with nothing new to send for this long ends.
REPLY_IDLE_SECONDS = 60
# Unexpected disconnects are logged as warnings at most this often.
DROP_WARN_SECONDS = 600
# A display (a wall tablet) asks here for an open task question to be put on
# a speaker (_serve_ask, runtime/asking.py). Its body is small JSON.
ASK_PATH = "/question/ask"
ASK_BODY_MAX = 4096
ASK_TOKEN_MIN = 16
MP3_BYTES_PER_SECOND = 8000  # 64 kbit/s
# The reply is generated faster than it is spoken. Sending it only this far
# ahead of the speaker keeps an interruption quick: what was not sent yet is
# never played.
STREAM_LEAD_SECONDS = 0.5
# From "play this URL" to sound: fetch, decoder start, first buffer.
PLAY_START_SECONDS = 0.5
# With satellite_cast: a Cast device starts a load later, and stops at once
# when told (CastSpeaker.stop), so it may be sent further ahead.
CAST_START_SECONDS = 2.5
CAST_LEAD_SECONDS = 3.0
# Speech that follows this soon joins the reply being played instead of
# starting a new announcement, which would cut the end of the first.
STREAM_LINGER_SECONDS = 1.0
# A reminder to say while the device is between runs: wait this long for it
# to stream its microphone again before calling out.
CALL_OUT_WAIT_SECONDS = 15.0

SATELLITE_NOTE = (
    "\n\nVOICE SATELLITE: The user is talking to you through a smart speaker in another room, not at the "
    "computer. They cannot see the screen, so say what they need to know instead of pointing at it, and keep "
    "replies short. Everything else (tools, tasks, results, ending the conversation) works exactly as in a "
    "conversation at the computer. A reminder for today or a camera watch asked here is also said on this speaker "
    "when it fires, without asking; announce_on_speaker false only if the user says not to."
)


class ReplyStream:
    """A stretch of her speech as an MP3 that is fetched while she speaks.

    MP3 because the device's decoder rejected a WAV stream and plays its own
    sounds from MP3/FLAC; chunked because the length is unknown.
    """

    def __init__(self, start: float = PLAY_START_SECONDS, lead: float = STREAM_LEAD_SECONDS):
        import av
        self.start, self.lead = start, lead   # until the speaker plays it; how far ahead it is sent
        self.path = f"/reply/{secrets.token_urlsafe(12)}.mp3"
        self.chunks: asyncio.Queue = asyncio.Queue()
        self.seconds = 0.0
        self._until = 0.0
        self.closed = False
        self.delivered = False              # one fetch sent all of it
        self.cut = asyncio.Event()          # interrupted: stop sending at once
        self._codec = av.CodecContext.create("libmp3lame", "w")
        self._codec.sample_rate = 24000
        self._codec.format = "s16p"
        self._codec.layout = "mono"
        self._codec.bit_rate = MP3_BYTES_PER_SECOND * 8
        self._codec.open()
        self._resampler = av.AudioResampler(format="s16p", layout="mono", rate=24000,
                                            frame_size=self._codec.frame_size)

    def feed(self, pcm: bytes) -> None:
        """24 kHz mono s16, as Gemini Live sends it."""
        import av
        pcm = pcm[:len(pcm) // 2 * 2]
        if self.closed or not pcm:
            return
        frame = av.AudioFrame.from_ndarray(np.frombuffer(pcm, dtype="<i2").reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = 24000
        self.seconds += frame.samples / 24000
        # Gemini sends speech about as fast as it is spoken, sometimes with a
        # gap after the first piece: the speaker then waits, and ends later.
        self._until = max(self._until, time.monotonic()) + frame.samples / 24000
        self._encode(self._resampler.resample(frame))

    def _encode(self, frames) -> None:
        data = b"".join(bytes(packet) for frame in frames for packet in self._codec.encode(frame))
        if data:
            self.chunks.put_nowait(data)

    def until(self) -> float:
        """When the speaker finishes what was fed so far (monotonic)."""
        return self._until + self.start

    def finish(self) -> None:
        if self.closed:
            return
        self.feed(bytes(9600))  # 200 ms of silence: the decoder may drop the last frames
        self.closed = True
        self._encode(self._resampler.resample(None))
        data = b"".join(bytes(packet) for packet in self._codec.encode(None))
        if data:
            self.chunks.put_nowait(data)
        self.chunks.put_nowait(None)

    def stop(self) -> None:
        """Interrupted: nothing more is sent."""
        self.closed = True
        self.cut.set()
        self.chunks.put_nowait(None)


class Satellite:
    def __init__(self, config):
        self.config = config
        self._client = None
        self._reconnect = None
        self._unsubscribe = None
        self._server = None
        self._model = None
        self._pool = ThreadPoolExecutor(1, thread_name_prefix="satellite-wake")
        self._phase = "off"                 # off | idle | wake | talk
        self._wake_buffer = bytearray()
        self._wake_frames: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._mic: asyncio.Queue = asyncio.Queue(maxsize=MIC_BUFFER_CHUNKS)
        self._streams: dict[str, ReplyStream] = {}
        self._stream: ReplyStream | None = None
        self._spoke_until = 0.0             # when her last speech finished (or will)
        self._hearing = False               # ring shows "hearing words"
        self._last_message = 0.0            # anything from Gemini: she is still at it
        # For a display elsewhere (satellite_state_command): what she is
        # doing, and her voice level timed to when the speaker plays it.
        self._shown = "idle"
        self._levels: collections.deque = collections.deque(maxlen=600)   # (when played, level 0..1)
        self._hook_lines: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._hook_owner = self             # the satellite whose state command gets this one's lines
        self._adapter = None
        self._session_task: asyncio.Task | None = None
        self._tasks: list[asyncio.Task] = []
        self._address = ""
        self._device_heard = 0.0            # last microphone audio or stream start from the device
        self._subscribed = 0.0              # last time the device was asked for its voice assistant
        self._connected_at = 0.0
        self._drops = 0                     # unexpected disconnects not yet logged as a warning
        self._drop_warned = -DROP_WARN_SECONDS
        self._fetches: set[asyncio.Task] = set()  # reply fetches being served (_serve_reply)
        self._runs = 0                      # device runs seen: a late RUN_END must not end a newer one
        self._player = None                 # the device's media player entity key
        self._light = None                  # its LED's entity key (satellite_led_colors)
        self._lit = ""                      # the LED state last set
        self._cast = None                   # CastSpeaker saying her replies (satellite_cast), else the device
        self._gain = float(config.satellite_mic_gain)
        self._talk_channel = int(config.satellite_talk_channel)
        self._diagnostics = os.environ.get("OMARCHY_AI_WAKE_DIAGNOSTICS") == "1"
        self._metrics = {"score": 0.0, "rms": 0.0}
        self._talk_levels: list[float] = []  # frame rms of one conversation, for level_summary
        self._loop = None
        self._waiting: list[dict] = []      # results for the next conversation to say (announce)
        self._calling: asyncio.Task | None = None

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        from aioesphomeapi import APIClient, ReconnectLogic
        from openwakeword.model import Model
        from .wake import _resolve_model_paths

        self._loop = asyncio.get_running_loop()
        self._model = await asyncio.to_thread(Model, wakeword_model_paths=_resolve_model_paths(self.config))
        self._server = await asyncio.start_server(self._serve_reply, "0.0.0.0", self.config.satellite_http_port)
        key_path = Path(self.config.satellite_key_path)
        key = key_path.read_text().strip() if key_path.is_file() else None
        self._client = APIClient(self.config.satellite_host, self.config.satellite_port, "", noise_psk=key or None,
                                 client_info="omarchy-ai")
        self._reconnect = ReconnectLogic(client=self._client, on_connect=self._connected,
                                         on_disconnect=self._disconnected, on_connect_error=self._connect_error,
                                         name=self.config.satellite_host)
        if getattr(self.config, "satellite_cast", None):
            from .cast_speaker import CastSpeaker
            self._cast = CastSpeaker(self.config.satellite_cast)
            log.info("voice satellite %s answers through the Cast speaker %s", self.config.satellite_host,
                     self.config.satellite_cast)
        self._tasks = [asyncio.create_task(self._wake_loop()), asyncio.create_task(self._stream_watch())]
        if self.config.satellite_state_command:
            self._tasks.append(asyncio.create_task(self._state_loop()))
            if self._hook_owner is self:
                self._tasks += [asyncio.create_task(self._hook_loop()), asyncio.create_task(self._question_loop())]
        await self._reconnect.start()
        log.info("voice satellite %s: waiting for it; wake word(s): %s", self.config.satellite_host,
                 ", ".join(self._model.models))

    async def stop(self) -> None:
        if _running.get(self.config.satellite_host) is self:
            del _running[self.config.satellite_host]
        if self._calling is not None:
            self._calling.cancel()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._adapter is not None:
            self._adapter._hangup.set()
        if self._session_task is not None:
            await asyncio.gather(self._session_task, return_exceptions=True)
        if self._reconnect is not None:
            await self._reconnect.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                log.debug("satellite disconnect failed", exc_info=True)
        if self._server is not None:
            self._server.close()
        for task in list(self._fetches):
            task.cancel()
        await asyncio.gather(*self._fetches, return_exceptions=True)
        if self._cast is not None:
            self._cast.close()
        self._pool.shutdown(wait=False)

    async def _connected(self) -> None:
        from aioesphomeapi import LightInfo, MediaPlayerInfo
        info = await self._client.device_info()
        entities, _ = await self._client.list_entities_services()
        self._player = next((e.key for e in entities if isinstance(e, MediaPlayerInfo)), None)
        self._light = next((e.key for e in entities if isinstance(e, LightInfo)), None)
        self._lit = ""
        if self._player is None:
            log.error("voice satellite %s has no media player; her replies cannot be played", info.name)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((self.config.satellite_host, self.config.satellite_port))  # picks a route, sends nothing
            self._address = f"http://{probe.getsockname()[0]}:{self.config.satellite_http_port}"
        self._subscribe()
        self._phase = "idle"
        self._connected_at = time.monotonic()
        log.info("voice satellite connected: %s (ESPHome %s)", info.name, info.esphome_version)

    def _subscribe(self) -> None:
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:  # noqa: BLE001 -- the link may be gone
                pass
        self._unsubscribe = self._client.subscribe_voice_assistant(
            handle_start=self._on_start, handle_stop=self._on_stop, handle_audio=self._on_audio)
        self._subscribed = time.monotonic()

    async def _stream_watch(self) -> None:
        """The device serves ONE voice-assistant client: the first to
        subscribe. A second one is refused without a word (the firmware only
        logs it), so being connected is not being heard. Real case,
        2026-10-01: after a power cut another assistant's service came back
        first and held the device; this one logged "connected" and was deaf
        for hours. A device that sends nothing is asked again after
        STREAM_QUIET_SECONDS, then twice as long after each try, up to
        STREAM_RETRY_MAX_SECONDS: asking is not free. On an M5 Atom
        (2026-10-24..26) the device dropped the connection within 0.3 s of
        every re-subscribe, and asking every 30 s kept it reconnecting all
        day (589 of 1151 drops came right after one)."""
        warned = -float(STREAM_RETRY_MAX_SECONDS)   # last warning (monotonic)
        deaf = False                                 # warned about this quiet stretch
        retry = STREAM_QUIET_SECONDS
        while True:
            await asyncio.sleep(KEEPALIVE_SECONDS)
            now = time.monotonic()
            if self._phase == "off" or self._unsubscribe is None:
                continue
            self._keep_alive()
            if now - self._device_heard < STREAM_QUIET_SECONDS:
                if deaf:
                    log.info("voice satellite %s is streaming its microphone again", self.config.satellite_host)
                    deaf = False
                retry = STREAM_QUIET_SECONDS
                continue
            if now - self._subscribed < retry:
                continue
            if now - warned >= STREAM_RETRY_MAX_SECONDS:
                log.warning("voice satellite %s is connected but sends no microphone audio; another client "
                            "probably holds its voice assistant (it serves only one). Asking again, less often "
                            "each time (next in %ds).", self.config.satellite_host,
                            min(STREAM_RETRY_MAX_SECONDS, retry * 2))
                warned, deaf = now, True
            else:
                log.debug("voice satellite %s still sends no audio; asking again", self.config.satellite_host)
            retry = min(STREAM_RETRY_MAX_SECONDS, retry * 2)
            try:
                self._subscribe()
            except Exception as error:  # noqa: BLE001
                log.debug("satellite re-subscribe failed: %s", error)

    def _keep_alive(self) -> None:
        """Ping the device. The client library only pings when it has heard
        nothing for a while, and a streaming device is never quiet, so the
        device itself heard nothing from us: ESPHome then pings after 60 s.
        On an M5 Atom (2026-10-24..26) the connection dropped when that ping
        was due, 60 or 120 s after a connect while it streamed, about 500
        times. Our own ping every KEEPALIVE_SECONDS keeps the device from
        needing one."""
        if not PING_DEVICE:
            return
        from aioesphomeapi.api_pb2 import PingRequest
        connection = getattr(self._client, "_connection", None)
        if connection is None:
            return
        try:
            connection.send_message(PingRequest())
        except Exception as error:  # noqa: BLE001 -- the link may be gone; _disconnected follows
            log.debug("satellite keepalive not sent: %s", error)

    async def _disconnected(self, expected: bool) -> None:
        now = time.monotonic()
        lasted = now - self._connected_at if self._connected_at else 0.0
        self._connected_at = 0.0
        # A device that keeps dropping is one warning per DROP_WARN_SECONDS
        # with a count, not a line a minute (aioesphomeapi logs each one too).
        if expected or now - self._drop_warned >= DROP_WARN_SECONDS:
            more = f"; {self._drops} more drop(s) since the last warning" if self._drops else ""
            log.warning("voice satellite %s disconnected (expected=%s) after %ds connected%s",
                        self.config.satellite_host, expected, lasted, more)
            if not expected:
                self._drop_warned, self._drops = now, 0
        else:
            self._drops += 1
            log.debug("voice satellite %s disconnected after %ds connected", self.config.satellite_host, lasted)
        self._unsubscribe = None
        self._phase = "off"
        self._hang_up()
        self._show("idle")

    async def _connect_error(self, error: Exception) -> None:
        log.debug("voice satellite not reachable: %s", error)

    def _event(self, name: str, data: dict | None = None) -> None:
        from aioesphomeapi import VoiceAssistantEventType
        try:
            self._client.send_voice_assistant_event(getattr(VoiceAssistantEventType, "VOICE_ASSISTANT_" + name), data)
        except Exception as error:  # noqa: BLE001 -- the link dropped; _disconnected ends the conversation
            log.warning("satellite event %s not sent: %s", name, error)

    def _play(self, url: str) -> None:
        """Play this URL as an announcement (on the Cast speaker when one is set)."""
        if self._cast is not None:
            self._cast.play(url)
            return
        if self._player is None:
            return
        try:
            self._client.media_player_command(self._player, media_url=url, announcement=True)
        except Exception as error:  # noqa: BLE001
            log.warning("satellite media player command not sent: %s", error)

    # ------------------------------------------- state for another display
    def _show(self, state: str) -> None:
        """idle | listening | speaking, to the state command. An idle
        speaker is not told idle again: an M5 Atom that drops every minute
        would otherwise end the other speaker's conversation on a display."""
        if state == "idle":
            self._led("")
        if state == "idle" == self._shown:
            return
        self._shown = state
        if state != "speaking":
            self._levels.clear()
        self._tell({"state": state})

    def _led(self, state: str) -> None:
        """The device's LED for wake | listening | speaking, off for "" (or a
        state with no colour), from satellite_led_colors. Sent on the same
        connection right after the event it follows, so it wins over the
        firmware's own colour for that event."""
        colors = getattr(self.config, "satellite_led_colors", None)
        if not colors or self._light is None or state == self._lit:
            return
        self._lit = state
        rgb = colors.get(state) if state else None
        try:
            if rgb:
                self._client.light_command(self._light, state=True, brightness=1.0,
                                           rgb=tuple(max(0, min(255, int(c))) / 255 for c in rgb[:3]),
                                           effect="None", transition_length=0)
            else:
                self._client.light_command(self._light, state=False, transition_length=0)
        except Exception as error:  # noqa: BLE001 -- the link dropped; _disconnected follows
            log.debug("satellite LED not set: %s", error)

    def _tell(self, message: dict) -> None:
        """One line for the state command, which all satellites share (it
        runs once, for the first): each line names its speaker (host)."""
        owner = self._hook_owner
        if self.config.satellite_state_command and not owner._hook_lines.full():
            owner._hook_lines.put_nowait({**message, "speaker": self.config.satellite_host})

    async def _state_loop(self) -> None:
        """Her voice level as the speaker plays it (10 a second), and the
        state again every few seconds so a display that missed a line, or
        whose assistant died, does not stay stuck."""
        repeated = 0.0
        while True:
            await asyncio.sleep(0.1)
            now = time.monotonic()
            level = None
            while self._levels and self._levels[0][0] <= now:
                level = max(level or 0.0, self._levels.popleft()[1])
            if level is not None:
                self._tell({"state": "speaking", "level": round(level, 3)})
                repeated = now
            elif self._shown != "idle" and now - repeated > 4:
                self._tell({"state": self._shown})
                repeated = now

    async def _hook_loop(self) -> None:
        """The user's program (satellite_state_command) gets one JSON object
        per line on its standard input for as long as the assistant runs.

        One that exits is started again, after HOOK_RETRY_SECONDS and twice
        as long after each run shorter than HOOK_HEALTHY_SECONDS. Its exit is
        noticed when it happens, not at the next line: a program that died
        at its start (2026-10-24..26: a KeyError in its own config) was
        found dead and restarted at nearly every line, 1219 warnings in four
        days. Lines that waited meanwhile are stale; a new run gets the
        current state."""
        command = self.config.satellite_state_command
        failures = 0
        capped = False
        while True:
            try:
                process = await asyncio.create_subprocess_shell(
                    command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
            except OSError as error:
                log.warning("satellite state command could not start: %s", error)
                return
            started = time.monotonic()
            reason = None
            exited = asyncio.ensure_future(process.wait())
            getter = None
            try:
                while True:
                    getter = asyncio.ensure_future(self._hook_lines.get())
                    await asyncio.wait((getter, exited), return_when=asyncio.FIRST_COMPLETED)
                    if not getter.done():
                        break
                    process.stdin.write((json.dumps(getter.result()) + "\n").encode())
                    await asyncio.wait_for(process.stdin.drain(), 2)
            except (ConnectionError, OSError, asyncio.TimeoutError) as error:
                reason = str(error) or type(error).__name__
            except asyncio.CancelledError:
                if process.returncode is None:
                    process.terminate()
                raise
            finally:
                if getter is not None:
                    getter.cancel()
                if process.returncode is None:
                    process.kill()
                await asyncio.gather(exited, return_exceptions=True)
            if reason is None:
                reason = f"exit status {process.returncode}"
            failures = 0 if time.monotonic() - started >= HOOK_HEALTHY_SECONDS else failures + 1
            delay = min(HOOK_RETRY_MAX_SECONDS, HOOK_RETRY_SECONDS * 2 ** max(0, failures - 1))
            # A warning for the first stop, and one when it settles at the
            # longest wait; the retries between are debug lines.
            if failures <= 1 or delay == HOOK_RETRY_MAX_SECONDS and not capped:
                log.warning("satellite state command stopped (%s); starting it again in %ds%s", reason, delay,
                            "; it keeps stopping right after its start" if failures > 1 else "")
            else:
                log.debug("satellite state command stopped again (%s); next start in %ds", reason, delay)
            capped = delay == HOOK_RETRY_MAX_SECONDS
            await asyncio.sleep(delay)
            while not self._hook_lines.empty():
                self._hook_lines.get_nowait()
            self._tell({"state": self._shown})

    async def _question_loop(self) -> None:
        """The open task questions for the state command, again every
        QUESTION_REFRESH_SECONDS (runtime/asking.py): a display that missed a
        line, or restarted, shows the questions waiting now; one answered in
        another process goes away."""
        from ..runtime import asking
        await asyncio.sleep(5)
        while True:
            try:
                await asyncio.to_thread(asking.sync, True)
            except Exception:  # noqa: BLE001 -- the display must not stop the speaker
                log.debug("open task questions not sent to the state command", exc_info=True)
            await asyncio.sleep(asking.QUESTION_REFRESH_SECONDS)

    # ------------------------------------------------------- reply over HTTP
    async def _serve_reply(self, reader, writer) -> None:
        """One fetch of a reply. The device may fetch the same reply more
        than once (2026-10-24: up to four connections, reset by the device,
        within 300 ms), so the end of a reply is put back for the next
        reader. A fetch must also end by itself: asyncio drops its own
        reference to this task when the device hangs up, and a task left
        waiting on a reply that never ends was then garbage-collected
        mid-await ("Task was destroyed but it is pending", then 301
        "cannot reuse already awaited coroutine" errors). _fetches holds it."""
        task = asyncio.current_task()
        self._fetches.add(task)
        try:
            request = await asyncio.wait_for(reader.readline(), 5)
            headers = {}
            while (header := await asyncio.wait_for(reader.readline(), 5)) not in (b"\r\n", b"\n", b""):
                name, _, value = header.decode("latin-1").partition(":")
                if len(headers) < 50:
                    headers[name.strip().lower()] = value.strip()
            parts = request.split()
            if len(parts) >= 2 and parts[1].split(b"?")[0] == ASK_PATH.encode():
                await self._serve_ask(parts[0], headers, reader, writer)
                return
            stream = self._streams.get(parts[1].decode("ascii", "ignore")) if len(parts) >= 2 else None
            # A reply already sent in full (or cut off) has nothing left: an
            # empty 200 made an M5 Atom fetch it again about ten times a
            # second for two hours (2026-10-27 20:55-23:06, 48940 fetches).
            gone = stream is not None and (stream.delivered or stream.cut.is_set())
            if gone:
                log.debug("satellite speech fetched again after its end: %s", stream.path)
            if stream is None or gone or parts[0] != b"GET":
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            started = time.monotonic()
            sent = 0
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n"
                         b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n")
            while not stream.cut.is_set():
                data = await asyncio.wait_for(stream.chunks.get(), REPLY_IDLE_SECONDS)
                if data is None:
                    stream.chunks.put_nowait(None)  # for any other fetch of it
                    break
                writer.write(b"%x\r\n" % len(data) + data + b"\r\n")
                await writer.drain()
                sent += len(data)
                ahead = (sent / MP3_BYTES_PER_SECOND - (time.monotonic() - started)
                         - getattr(stream, "lead", STREAM_LEAD_SECONDS))
                if ahead > 0:
                    try:
                        await asyncio.wait_for(stream.cut.wait(), ahead)
                    except asyncio.TimeoutError:
                        pass
            # Also when interrupted: the device then sees a normal end of file.
            writer.write(b"0\r\n\r\n")
            await writer.drain()
            stream.delivered = stream.delivered or sent > 0
            log.info("satellite speech delivered: %.1fs%s", stream.seconds, " (interrupted)" if stream.cut.is_set() else "")
        except (asyncio.TimeoutError, ConnectionError, OSError) as error:
            log.info("satellite speech fetch ended early: %s", str(error) or type(error).__name__)
        finally:
            self._fetches.discard(task)
            writer.close()

    async def _serve_ask(self, method: bytes, headers: dict, reader, writer) -> None:
        """POST /question/ask {"token", "id", "speaker"?}: a display (the
        wall tablet's banner) asks for an open task question to be put on a
        speaker, so the user answers it by voice (runtime/asking.py
        ask_from_display). Off unless satellite_ask_token_path holds a token
        (a random one of the user's, mode 0600). The request carries only a
        question id: nothing it sends is said or run, so the most a stolen
        token does is have a speaker ask a question that is waiting anyway.
        A plain-text body from a page needs no CORS preflight; the answer
        allows any origin so the page can read it."""
        from ..runtime import asking
        status, message = 405, "POST only"
        if method == b"OPTIONS":
            status, message = 204, ""
        elif method == b"POST":
            token = _ask_token(self.config)
            try:
                length = int(headers.get("content-length") or 0)
            except ValueError:
                length = -1
            if not token:
                status, message = 403, "asking from a display is off"
            elif not 0 < length <= ASK_BODY_MAX:
                status, message = 400, "expected a small JSON body"
            else:
                try:
                    data = json.loads(await asyncio.wait_for(reader.readexactly(length), 5))
                except (ValueError, asyncio.IncompleteReadError):
                    data = None
                if not isinstance(data, dict):
                    status, message = 400, "expected a JSON object"
                elif not hmac.compare_digest(str(data.get("token") or "").encode(), token.encode()):
                    log.warning("satellite: a display's question request had a wrong token; refused")
                    status, message = 403, "wrong token"
                else:
                    speaker = data.get("speaker")
                    status, message = await asyncio.to_thread(
                        asking.ask_from_display, str(data.get("id") or ""),
                        speaker if isinstance(speaker, str) else None)
        payload = json.dumps({"ok": status < 300, "message": message}).encode() if status != 204 else b""
        reason = {202: "Accepted", 204: "No Content", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
                  405: "Method Not Allowed", 429: "Too Many Requests", 503: "Service Unavailable"}.get(status, "OK")
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
                     "Access-Control-Allow-Origin: *\r\nAccess-Control-Allow-Methods: POST, OPTIONS\r\n"
                     "Access-Control-Allow-Headers: Content-Type\r\n"
                     f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload)
        await writer.drain()

    # ------------------------------------------------------ from the device
    async def _on_start(self, conversation_id, flags, audio_settings, wake_word_phrase) -> int:
        """The device opened its microphone. With the wake-word flag it only
        wants the word found; without it (its button) the conversation
        starts now."""
        self._device_heard = time.monotonic()
        self._wake_buffer.clear()
        self._runs += 1
        if self._phase == "talk":
            self._hang_up()  # it restarted its stream on its own: that conversation is over
        if flags & USE_WAKE_WORD:
            self._pool.submit(self._model.reset)
            self._phase = "wake"
        else:
            self._begin(woke=False)
        return 0  # audio comes over this connection, not to a UDP port

    async def _on_stop(self, aborted: bool) -> None:
        if self._phase == "talk":
            log.info("satellite stopped the conversation (aborted=%s)", aborted)
            self._cut()  # its button: she stops talking too
            self._hang_up()
        self._phase = "idle"

    async def _on_audio(self, data: bytes, data2: bytes | None = None) -> None:
        self._device_heard = time.monotonic()
        if self._phase == "wake":
            self._wake_buffer.extend(data)
            while len(self._wake_buffer) >= WAKE_FRAME_BYTES:
                frame = bytes(self._wake_buffer[:WAKE_FRAME_BYTES])
                del self._wake_buffer[:WAKE_FRAME_BYTES]
                if not self._wake_frames.full():  # behind by 4 s: skip, never queue old audio
                    self._wake_frames.put_nowait(frame)
        elif self._phase == "talk":
            if self._mic.full():
                self._mic.get_nowait()
            # A Voice PE sends two channels. Measured in a room with a hum
            # (2026-09-30): the first never falls silent (rms 300-650, three
            # quarters of it below 300 Hz), so Gemini never heard a request
            # end; the second is noise-suppressed (rms 1 between words).
            self._mic.put_nowait(data2 if self._talk_channel == 1 and data2 else data)

    def _amplified(self, pcm: bytes) -> np.ndarray:
        samples = np.frombuffer(pcm[:len(pcm) // 2 * 2], dtype="<i2")
        if self._gain != 1.0:
            samples = np.clip(samples.astype(np.float32) * self._gain, -32768, 32767).astype(np.int16)
        return samples

    def _predict(self, frame: bytes) -> dict:
        samples = self._amplified(frame)
        scores = self._model.predict(samples)
        if self._diagnostics:
            self._metrics["score"] = max(self._metrics["score"], max(scores.values(), default=0.0))
            self._metrics["rms"] = max(self._metrics["rms"], float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))))
        return scores

    async def _wake_loop(self) -> None:
        loop = asyncio.get_running_loop()
        names = list(self._model.models)
        consecutive = dict.fromkeys(names, 0)
        next_report = time.monotonic() + 5
        while True:
            frame = await self._wake_frames.get()
            if self._phase != "wake":
                consecutive = dict.fromkeys(names, 0)
                continue
            scores = await loop.run_in_executor(self._pool, self._predict, frame)
            if self._diagnostics and time.monotonic() >= next_report:
                log.info("satellite wake diagnostics: %s", {k: round(float(v), 4) for k, v in self._metrics.items()})
                self._metrics = dict.fromkeys(self._metrics, 0.0)
                next_report = time.monotonic() + 5
            # Keep far-field speaker tuning independent from the desktop and
            # phone detectors. Existing configurations inherit the shared
            # threshold.
            threshold = _wake_threshold(self.config)
            for name in names:
                score = scores.get(name, 0.0)
                if score < threshold:
                    consecutive[name] = 0
                    continue
                consecutive[name] += 1
                if consecutive[name] >= self.config.wake_trigger_frames and self._phase == "wake":
                    log.info("wake word detected: %s (score=%.2f, microphone=satellite)", name, score)
                    consecutive = dict.fromkeys(names, 0)
                    self._begin(woke=True)
                    break

    # ------------------------------------------------------- a conversation
    def _announce(self, entry: dict, here: bool = False) -> None:
        """Say a result on the speaker (a reminder the user asked to hear
        there). Runs on this loop. An open conversation says it, once: one
        attached to the agenda already got it from announce_to_calls, unless
        it was sent `here` only (a task's question: ask())."""
        from ..core import agenda
        adapter = self._adapter
        if adapter is not None and not adapter._hangup.is_set():
            if here or not agenda.attached(adapter):
                adapter.announce(entry)  # still connecting: said once it is up
            return
        if quiet_now(self.config):
            log.info("satellite quiet hours: not starting a conversation to say %r", str(entry.get("title"))[:80])
            return  # it stays in the inbox for the next conversation
        self._waiting.append(dict(entry))
        if self._phase == "wake":
            self._begin(woke=True, proactive=True)
        elif self._phase != "talk" and (self._calling is None or self._calling.done()):
            self._calling = asyncio.create_task(self._call_out())
        # talk without an open conversation: the next one starts and says it

    async def _call_out(self) -> None:
        """The device is between runs (or its conversation is ending): call
        out as soon as it streams its microphone again."""
        deadline = time.monotonic() + CALL_OUT_WAIT_SECONDS
        while self._waiting and self._phase != "wake" and time.monotonic() < deadline:
            if self._phase == "talk" and self._adapter is None:
                return  # a conversation is starting: it takes _waiting
            await asyncio.sleep(0.2)
        if not self._waiting:
            return
        if quiet_now(self.config):
            log.info("satellite quiet hours: %d result(s) not said on the speaker", len(self._waiting))
            self._waiting.clear()
            return
        if self._phase == "wake":
            self._begin(woke=True, proactive=True)
        elif self._phase != "talk":
            log.warning("satellite not streaming (%s); %d result(s) not said on the speaker", self._phase,
                        len(self._waiting))
            self._waiting.clear()

    def _begin(self, woke: bool, proactive: bool = False) -> None:
        while not self._mic.empty():
            self._mic.get_nowait()
        self._phase = "talk"
        self._hearing = False
        self._event("RUN_START")
        if woke:
            self._event("WAKE_WORD_END")
        self._event("STT_START")
        self._show("listening")
        self._led("wake")
        self._session_task = asyncio.create_task(self._run_session(self._session_task, self._runs, proactive))

    def _hang_up(self) -> None:
        if self._adapter is not None:
            self._adapter._hangup.set()

    def _speak(self, pcm: bytes) -> None:
        now = time.monotonic()
        stream = self._stream
        if stream is None or stream.closed:
            stream = self._stream = (ReplyStream(PLAY_START_SECONDS + CAST_START_SECONDS, CAST_LEAD_SECONDS)
                                     if self._cast is not None else ReplyStream())
            self._streams[stream.path] = stream
            for path in list(self._streams)[:-4]:
                del self._streams[path]
            self._play(self._address + stream.path)
            self._event("TTS_START", {"text": "..."})
            self._hearing = False
            self._show("speaking")
            self._led("speaking")
        if self.config.satellite_state_command:
            # One level per 100 ms of speech, on the desktop overlay's scale,
            # stamped with the moment the speaker reaches it.
            samples = np.frombuffer(pcm[:len(pcm) // 2 * 2], dtype="<i2").astype(np.float32)
            starts = max(stream._until, now) + stream.start
            for offset in range(0, samples.size, 2400):
                window = samples[offset:offset + 2400]
                self._levels.append((starts + offset / 24000,
                                     min(1.0, float(np.sqrt(np.mean(window * window))) / 12000)))
        stream.feed(pcm)
        self._spoke_until = max(now, stream.until())
        adapter = self._adapter
        if adapter is not None:
            # What _play_audio keeps for the desktop speaker: the echo gate,
            # announcements that wait for her to finish, and whose words count.
            adapter._playback_until = self._spoke_until
            adapter._state = "speaking"

    def _cut(self) -> bool:
        """Stop her speech: nothing more is sent, so the speaker plays out
        the lead and falls quiet. True if she was speaking."""
        stream, self._stream = self._stream, None
        if stream is None or stream.closed:
            return False
        stream.stop()
        if self._cast is not None:
            self._cast.stop()
        self._spoke_until = time.monotonic() + STREAM_LEAD_SECONDS
        return True

    def _interrupted(self) -> None:
        """The user spoke over her."""
        if self._cut():
            self._event("STT_START")
            self._show("listening")
            self._led("listening")

    def _on_message(self, message) -> None:
        """Runs inside the adapter's receive loop, before it handles the
        message, so her speech and an interruption stay in order."""
        self._last_message = time.monotonic()
        server = message.server_content
        if not server:
            return
        heard = server.input_transcription
        if heard and heard.text and not self._hearing and time.monotonic() > self._spoke_until:
            self._hearing = True
            self._event("STT_VAD_START")
        if server.interrupted:
            self._interrupted()
        if server.model_turn:
            for part in server.model_turn.parts or []:
                if part.inline_data and part.inline_data.data:
                    self._speak(part.inline_data.data)

    async def _speech_watch(self) -> None:
        """Close a stretch of speech once the speaker has played it."""
        while True:
            await asyncio.sleep(0.1)
            stream = self._stream
            if stream is not None and not stream.closed and time.monotonic() > stream.until() + STREAM_LINGER_SECONDS:
                stream.finish()
                self._stream = None
                self._event("STT_START")  # ring: waiting for words again
                self._show("listening")
                self._led("listening")

    async def _send_audio(self, adapter, session) -> None:
        from google.genai import types
        pending = bytearray()
        while True:
            pending.extend(await self._mic.get())
            while len(pending) >= GATE_FRAME_BYTES:
                samples = self._amplified(bytes(pending[:GATE_FRAME_BYTES]))
                del pending[:GATE_FRAME_BYTES]
                levels = samples.astype(np.float32)
                rms = float(np.sqrt(np.mean(levels * levels)))
                now = time.monotonic()
                adapter._mic_levels.append((now, rms, int(np.max(np.abs(levels)))))
                if now >= adapter._playback_until + adapter.ECHO_TAIL_SECONDS and len(self._talk_levels) < MAX_TALK_LEVELS:
                    self._talk_levels.append(rms)  # the room and the user, not her own voice
                # The session's stuck-turn guard, as on the desktop (its echo gate is off).
                for part in adapter._gate(samples.tobytes(), rms, now):
                    await session.send_realtime_input(audio=types.Blob(data=part, mime_type="audio/pcm;rate=16000"))

    @staticmethod
    async def _drain_audio(adapter) -> None:
        # _on_message already took her speech; keep the adapter's queue empty.
        while True:
            await adapter._audio.get()

    async def _silence_watch(self, adapter, started: float) -> None:
        """Nobody at the speaker any more: back to the wake word. A desktop
        call stays open until told to stop; a microphone in a room must not."""
        limit = max(5, int(self.config.satellite_silence_seconds))
        while True:
            await asyncio.sleep(0.5)
            now = time.monotonic()
            if adapter._running_blocking or not adapter._calls.empty() or now < self._spoke_until:
                continue
            # Not only speech counts: 2026-09-30 22:23 a request took 15 s
            # (stuck turn, then a tool), and the conversation was ended one
            # second after the tool returned, before she could answer.
            quiet = now - max(started, adapter._last_user_speech, self._spoke_until, adapter._last_audio_at,
                              self._last_message, adapter._awaiting_since)
            if quiet > limit:
                log.info("satellite: nobody spoke for %ds; ending the conversation", limit)
                return

    async def _run_session(self, previous: asyncio.Task | None, run: int, proactive: bool = False) -> None:
        from ..core import agenda, conversations
        from ..core.history import append_session
        from .gemini_live import GeminiLiveSession, build_live_config, gemini_client, live_connect, origin_note

        if previous is not None and not previous.done():
            await asyncio.gather(previous, return_exceptions=True)  # it saves the conversation this one continues
        woke = time.monotonic()
        # Proactive: she called out to say these, and the announcer hangs up
        # if nobody answers (PROACTIVE_SILENCE_SECONDS); otherwise she says
        # them in the conversation the user started.
        waiting, self._waiting = self._waiting, []
        adapter = GeminiLiveSession(self.config, announcements=waiting if proactive else None)
        from ..core import logs
        logs.set_ids(session_id=adapter._audit_session)  # this task's log lines (core/logs.py)
        for entry in [] if proactive else waiting:
            adapter.announce(entry)
        adapter.on_message = self._on_message
        adapter.channel = "satellite"  # core/origin.py, if the conversation cannot say
        adapter.hold_until_spoken = not proactive  # a (maybe false) wake word: nothing until the user speaks
        adapter._loop = asyncio.get_running_loop()  # notice() from other threads (quota)
        # No echo gate: the device cancels her voice itself (microphone level
        # while she spoke = the quiet level), and the desktop's gate blanked
        # the quiet parts of words said over her: "turn off the dining light"
        # arrived as "the dining light", and the light was turned ON
        # (2026-09-30 22:24). With a floor of 0 every frame passes.
        adapter.BARGE_FLOOR_RMS = 0.0
        adapter._splice_abort_rms = float(self.config.satellite_speech_rms)
        adapter._voice_rms = float(self.config.satellite_voice_rms or 0.4 * adapter._splice_abort_rms)
        self._adapter = adapter
        self._stream, self._spoke_until = None, 0.0
        tasks: list[asyncio.Task] = []
        client = None
        failed = False
        try:
            # She called out to ask a task's question: in that task's own chat
            # when it was asked on this speaker (runtime/asking.py), so the
            # spoken answer lands next to its card.
            asked_in = next((e.get("conversation") for e in waiting if proactive and e.get("conversation")), "")
            conversation = await asyncio.to_thread(self._conversation_for, asked_in)
            adapter._transcript = conversations.Transcript(conversation, adapter._transcript)
            conversations.CURRENT.set(conversation)
            prepared = time.monotonic()
            live_config = build_live_config(self.config)
            live_config["system_instruction"] += SATELLITE_NOTE + origin_note(conversation)
            built = time.monotonic()
            client = gemini_client(self.config)
            attempt = 0
            while True:
                try:
                    async with live_connect(client, self.config, self.config.gemini_model, live_config) as session:
                        if attempt:
                            await adapter._carry_over(session)  # she says she lost the connection
                        else:
                            agenda.mark_briefed()  # this prompt carried the pending results
                            agenda.attach_call(adapter, asyncio.get_running_loop())
                            log.info("satellite conversation started: session=%s model=%s (conversation %.1fs, "
                                     "prompt %.1fs, connect %.1fs)", adapter._audit_session,
                                     self.config.gemini_model, prepared - woke, built - prepared,
                                     time.monotonic() - built)
                        if self._stream is None:
                            self._led("listening")  # connected: talk now
                        workers = [self._send_audio(adapter, session), adapter._receive(session),
                                   adapter._tools(session), adapter._announcer(session),
                                   adapter._stuck_turn_recovery(session), adapter._task_status(session),
                                   adapter._speaker_watch(session), self._drain_audio(adapter), self._speech_watch(),
                                   self._silence_watch(adapter, time.monotonic()), adapter._hangup.wait()]
                        if self.config.jev_fast_path:
                            workers.append(adapter._fast_path())
                        tasks = [asyncio.create_task(worker) for worker in workers]
                        done, _ = await asyncio.wait(tasks, timeout=self.config.max_session_seconds,
                                                     return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            task.result()
                    break
                except Exception as error:  # noqa: BLE001 -- a transient close reconnects once, else as before
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    tasks = []
                    attempt += 1
                    if not adapter._resume_dropped(error):
                        raise
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            failed = True
            log.exception("satellite conversation failed")
            from ..core import quota
            if quota.is_quota_error(str(error)):
                quota.report("gemini", f"satellite: {error}", user_initiated=True)
        finally:
            adapter._hangup.set()
            agenda.detach_call(adapter)
            agenda.mark_delivered(adapter.acknowledged_ids())
            for task in tasks:
                task.cancel()
            for task in adapter._bg_tasks:
                task.cancel()
            await asyncio.gather(*tasks, *adapter._bg_tasks, return_exceptions=True)
            if client is not None:
                await client.aio.aclose()
            if adapter._transcript:
                await asyncio.to_thread(append_session, list(adapter._transcript), self.config.context_retention_hours)
                await asyncio.to_thread(conversations.flush, adapter._transcript.conversation)
            # Her goodbye plays to the end before the device is released.
            stream, self._stream = self._stream, None
            if stream is not None and not stream.closed:
                stream.finish()
                await asyncio.sleep(min(30.0, max(0.0, stream.until() - time.monotonic()) + 0.3))
            if self._adapter is adapter:
                self._adapter = None
                if self._phase == "talk" and self._runs == run:
                    if failed:
                        self._event("ERROR", {"code": "intent-failed", "message": "The assistant is not available"})
                    self._event("RUN_END")
                    self._phase = "idle"  # the device starts its stream again: _on_start
                self._show("idle")
            log.info("satellite conversation ended: session=%s", adapter._audit_session)
            log.info("satellite microphone levels: session=%s %s", adapter._audit_session,
                     level_summary(self._talk_levels, adapter._voice_rms, adapter._splice_abort_rms))
            self._talk_levels = []


    def _conversation_for(self, asked_in: str) -> str:
        from ..core import conversations
        host = self.config.satellite_host
        if asked_in and conversations.exists(asked_in) and conversations.device(asked_in) == host:
            return asked_in
        return conversations.desktop("satellite", host)


MAX_TALK_LEVELS = 200_000  # frames of one conversation kept for its level summary


def level_summary(levels: list[float], voice_rms: float | None, speech_rms: float) -> str:
    """What the microphone held while she was not speaking, for setting
    satellite_voice_rms and satellite_speech_rms from real conversations:
    the low percentiles are the room, the high ones the user's speech, and
    the two shares say how much of it each configured level lets through."""
    if not levels:
        return "frames=0"
    ordered = sorted(levels)
    at = lambda q: ordered[min(len(ordered) - 1, int(q * len(ordered)))]  # noqa: E731
    share = lambda level: 100 * sum(1 for v in ordered if v >= level) / len(ordered)  # noqa: E731
    voice = voice_rms or speech_rms
    return (f"frames={len(ordered)} " + " ".join(f"p{int(q * 100)}={at(q):.0f}" for q in (.1, .25, .5, .75, .9, .95, .99))
            + f" max={ordered[-1]:.0f} at_or_above_voice_rms({voice:.0f})={share(voice):.1f}%"
            + f" at_or_above_speech_rms({speech_rms:.0f})={share(speech_rms):.1f}%")


async def start(config) -> Satellite | None:
    """The satellite named in config.yaml (satellite_host), or None."""
    if not getattr(config, "satellite_host", None):
        return None
    if getattr(config, "provider", "gemini") != "gemini":
        log.warning("voice satellite needs the Gemini provider; not started")
        return None
    satellites = []
    configs = [config]
    second_host = getattr(config, "satellite_host_2", None)
    if second_host:
        second = copy.copy(config)
        second.satellite_host = second_host
        second.satellite_http_port = int(getattr(config, "satellite_http_port_2", 8768))
        second.satellite_room = getattr(config, "satellite_room_2", "") or config.satellite_room
        second.satellite_cast = getattr(config, "satellite_cast_2", None)
        second.satellite_led_colors = getattr(config, "satellite_led_colors_2", None)
        configs.append(second)
    owner = None
    for satellite_config in configs:
        satellite = Satellite(satellite_config)
        # One state command for all of them, run by the first, so two
        # programs do not race the same display. Live 2026-10-28: with the
        # second speaker's hook switched off, a conversation on the Voice PE
        # no longer reached the wall tablet's house plan.
        satellite._hook_owner = owner or satellite
        try:
            await satellite.start()
        except Exception:  # noqa: BLE001 -- one satellite must not stop the others
            log.exception("voice satellite %s could not start", satellite_config.satellite_host)
            continue
        owner = owner or satellite
        satellites.append(satellite)
    if not satellites:
        return None
    _running.clear()
    _running.update((s.config.satellite_host, s) for s in satellites)  # the first is the default speaker
    return SatelliteGroup(satellites)


class SatelliteGroup:
    """Lifecycle wrapper for one or more independently streaming satellites."""

    def __init__(self, satellites: list[Satellite]):
        self.satellites = satellites

    async def stop(self) -> None:
        await asyncio.gather(*(s.stop() for s in self.satellites), return_exceptions=True)


_running: dict[str, Satellite] = {}  # the daemon's satellites by host, for announce()


def announce(entry: dict) -> bool:
    """Say a result on a speaker too (core/daemon.py, a job with
    announce=satellite): the one it was asked on (`speaker`, its host), else
    the first; in its open conversation, or in a short one she starts. True
    if the speaker will say it. Live 2026-10-26: a delivery watch asked on
    the Voice PE could only ever be said on the first speaker."""
    satellite = _running.get(entry.get("speaker") or "") or next(iter(_running.values()), None)
    if satellite is None or satellite._loop is None or satellite._loop.is_closed() or satellite._phase == "off":
        return False
    if satellite._phase != "talk" and quiet_now(satellite.config):
        return False  # quiet hours: the caller tells the user another way
    try:
        here = asyncio.get_running_loop() is satellite._loop
    except RuntimeError:
        here = False
    if here:
        satellite._announce(dict(entry))
    else:
        satellite._loop.call_soon_threadsafe(satellite._announce, dict(entry))
    return True


def _ask_token(config) -> str:
    """The display's token (satellite_ask_token_path), or "" when the route is off."""
    path = getattr(config, "satellite_ask_token_path", None)
    try:
        token = Path(path).expanduser().read_text().strip() if path else ""
    except OSError:
        return ""
    return token if len(token) >= ASK_TOKEN_MIN else ""


def _satellite(host: str | None):
    """The running satellite for a host; the first (default) one for ""."""
    if host:
        return _running.get(host)
    return next(iter(_running.values()), None)


def known(host: str | None) -> bool:
    return _satellite(host) is not None


def reachable(host: str | None) -> bool:
    """The speaker can say something now and hear the answer: connected,
    and in a conversation or streaming its microphone for the wake word."""
    satellite = _satellite(host)
    if satellite is None or satellite._loop is None or satellite._loop.is_closed() or satellite._phase == "off":
        return False
    return satellite._phase == "talk" or time.monotonic() - satellite._device_heard < STREAM_QUIET_SECONDS


def ask(entry: dict) -> bool:
    """Put a task's question on the speaker named by entry["speaker"] (""
    the default one), in its open conversation or in one she starts; the
    user's reply goes to the task (runtime/asking.py). False when that
    speaker is not running."""
    satellite = _satellite(entry.get("speaker") or "")
    if satellite is None or satellite._loop is None or satellite._loop.is_closed() or satellite._phase == "off":
        return False
    if satellite._phase != "talk" and quiet_now(satellite.config):
        return False  # quiet hours: the caller tells the user another way
    try:
        here = asyncio.get_running_loop() is satellite._loop
    except RuntimeError:
        here = False
    if here:
        satellite._announce(dict(entry), True)
    else:
        satellite._loop.call_soon_threadsafe(satellite._announce, dict(entry), True)
    return True


def tell_question(line: dict) -> None:
    """One open-question line for the state command (runtime/asking.py),
    as it is: its "speaker" is where the task was asked, not who tells it."""
    owner = next((s for s in _running.values() if s._hook_owner is s and s.config.satellite_state_command), None)
    if owner is None or owner._loop is None or owner._loop.is_closed():
        return

    def put() -> None:
        if not owner._hook_lines.full():
            owner._hook_lines.put_nowait(dict(line))
    try:
        here = asyncio.get_running_loop() is owner._loop
    except RuntimeError:
        here = False
    if here:
        put()
    else:
        owner._loop.call_soon_threadsafe(put)
