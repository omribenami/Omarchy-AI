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
import collections
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

WAKE_FRAME_BYTES = 2560      # 1280 samples, 80 ms: openWakeWord's frame
GATE_FRAME_BYTES = 640       # 20 ms: the frame the session's echo gate was tuned on
USE_WAKE_WORD = 2            # VoiceAssistantCommandFlag.USE_WAKE_WORD
MIC_BUFFER_CHUNKS = 600      # ~19 s of 32 ms chunks while Gemini connects
# Connected, but no microphone audio for this long: subscribe again (_stream_watch).
STREAM_QUIET_SECONDS = 30
MP3_BYTES_PER_SECOND = 8000  # 64 kbit/s
# The reply is generated faster than it is spoken. Sending it only this far
# ahead of the speaker keeps an interruption quick: what was not sent yet is
# never played.
STREAM_LEAD_SECONDS = 0.5
# From "play this URL" to sound: fetch, decoder start, first buffer.
PLAY_START_SECONDS = 0.5
# Speech that follows this soon joins the reply being played instead of
# starting a new announcement, which would cut the end of the first.
STREAM_LINGER_SECONDS = 1.0

SATELLITE_NOTE = (
    "\n\nVOICE SATELLITE: The user is talking to you through a smart speaker in another room, not at the "
    "computer. They cannot see the screen, so say what they need to know instead of pointing at it, and keep "
    "replies short. Everything else (tools, tasks, results, ending the conversation) works exactly as in a "
    "conversation at the computer."
)


class ReplyStream:
    """A stretch of her speech as an MP3 that is fetched while she speaks.

    MP3 because the device's decoder rejected a WAV stream and plays its own
    sounds from MP3/FLAC; chunked because the length is unknown.
    """

    def __init__(self):
        import av
        self.path = f"/reply/{secrets.token_urlsafe(12)}.mp3"
        self.chunks: asyncio.Queue = asyncio.Queue()
        self.seconds = 0.0
        self._until = 0.0
        self.closed = False
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
        return self._until + PLAY_START_SECONDS

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
        self._adapter = None
        self._session_task: asyncio.Task | None = None
        self._tasks: list[asyncio.Task] = []
        self._address = ""
        self._device_heard = 0.0            # last microphone audio or stream start from the device
        self._runs = 0                      # device runs seen: a late RUN_END must not end a newer one
        self._player = None                 # the device's media player entity key
        self._gain = float(config.satellite_mic_gain)
        self._talk_channel = int(config.satellite_talk_channel)
        self._diagnostics = os.environ.get("OMARCHY_AI_WAKE_DIAGNOSTICS") == "1"
        self._metrics = {"score": 0.0, "rms": 0.0}

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        from aioesphomeapi import APIClient, ReconnectLogic
        from openwakeword.model import Model
        from .wake import _resolve_model_paths

        self._model = await asyncio.to_thread(Model, wakeword_model_paths=_resolve_model_paths(self.config))
        self._server = await asyncio.start_server(self._serve_reply, "0.0.0.0", self.config.satellite_http_port)
        key_path = Path(self.config.satellite_key_path)
        key = key_path.read_text().strip() if key_path.is_file() else None
        self._client = APIClient(self.config.satellite_host, self.config.satellite_port, "", noise_psk=key or None,
                                 client_info="omarchy-ai")
        self._reconnect = ReconnectLogic(client=self._client, on_connect=self._connected,
                                         on_disconnect=self._disconnected, on_connect_error=self._connect_error,
                                         name=self.config.satellite_host)
        self._tasks = [asyncio.create_task(self._wake_loop()), asyncio.create_task(self._stream_watch())]
        if self.config.satellite_state_command:
            self._tasks += [asyncio.create_task(self._hook_loop()), asyncio.create_task(self._state_loop())]
        await self._reconnect.start()
        log.info("voice satellite %s: waiting for it; wake word(s): %s", self.config.satellite_host,
                 ", ".join(self._model.models))

    async def stop(self) -> None:
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
        self._pool.shutdown(wait=False)

    async def _connected(self) -> None:
        from aioesphomeapi import MediaPlayerInfo
        info = await self._client.device_info()
        entities, _ = await self._client.list_entities_services()
        self._player = next((e.key for e in entities if isinstance(e, MediaPlayerInfo)), None)
        if self._player is None:
            log.error("voice satellite %s has no media player; her replies cannot be played", info.name)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((self.config.satellite_host, self.config.satellite_port))  # picks a route, sends nothing
            self._address = f"http://{probe.getsockname()[0]}:{self.config.satellite_http_port}"
        self._subscribe()
        self._phase = "idle"
        log.info("voice satellite connected: %s (ESPHome %s)", info.name, info.esphome_version)

    def _subscribe(self) -> None:
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:  # noqa: BLE001 -- the link may be gone
                pass
        self._unsubscribe = self._client.subscribe_voice_assistant(
            handle_start=self._on_start, handle_stop=self._on_stop, handle_audio=self._on_audio)
        self._device_heard = time.monotonic()

    async def _stream_watch(self) -> None:
        """The device serves ONE voice-assistant client: the first to
        subscribe. A second one is refused without a word (the firmware only
        logs it), so being connected is not being heard. Real case,
        2026-10-01: after a power cut another assistant's service came back
        first and held the device; this one logged "connected" and was deaf
        for hours. A device that sends nothing is asked again every
        STREAM_QUIET_SECONDS until it starts streaming."""
        warned = 0.0
        while True:
            await asyncio.sleep(10)
            now = time.monotonic()
            if self._phase == "off" or self._unsubscribe is None:
                continue
            if now - self._device_heard < STREAM_QUIET_SECONDS:
                if warned:
                    log.info("voice satellite is streaming its microphone again")
                    warned = 0.0
                continue
            if not warned or now - warned > 600:
                log.warning("voice satellite is connected but sends no microphone audio; another client probably "
                            "holds its voice assistant (it serves only one). Asking again every %ds.",
                            STREAM_QUIET_SECONDS)
                warned = now
            try:
                self._subscribe()
            except Exception as error:  # noqa: BLE001
                log.debug("satellite re-subscribe failed: %s", error)

    async def _disconnected(self, expected: bool) -> None:
        log.warning("voice satellite disconnected (expected=%s)", expected)
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
        """Play this URL as an announcement."""
        if self._player is None:
            return
        try:
            self._client.media_player_command(self._player, media_url=url, announcement=True)
        except Exception as error:  # noqa: BLE001
            log.warning("satellite media player command not sent: %s", error)

    # ------------------------------------------- state for another display
    def _show(self, state: str) -> None:
        """idle | listening | speaking, to the state command."""
        self._shown = state
        if state != "speaking":
            self._levels.clear()
        self._tell({"state": state})

    def _tell(self, message: dict) -> None:
        if self.config.satellite_state_command and not self._hook_lines.full():
            self._hook_lines.put_nowait(message)

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
        per line on its standard input for as long as the assistant runs."""
        command = self.config.satellite_state_command
        while True:
            try:
                process = await asyncio.create_subprocess_shell(
                    command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
            except OSError as error:
                log.warning("satellite state command could not start: %s", error)
                return
            try:
                while process.returncode is None:
                    message = await self._hook_lines.get()
                    process.stdin.write((json.dumps(message) + "\n").encode())
                    await asyncio.wait_for(process.stdin.drain(), 2)
            except (ConnectionError, OSError, asyncio.TimeoutError) as error:
                log.warning("satellite state command stopped taking lines (%s); restarting it", error)
            except asyncio.CancelledError:
                process.terminate()
                raise
            if process.returncode is None:
                process.kill()
            await process.wait()
            await asyncio.sleep(5)

    # ------------------------------------------------------- reply over HTTP
    async def _serve_reply(self, reader, writer) -> None:
        try:
            request = await asyncio.wait_for(reader.readline(), 5)
            while (await asyncio.wait_for(reader.readline(), 5)) not in (b"\r\n", b"\n", b""):
                pass
            parts = request.split()
            stream = self._streams.get(parts[1].decode("ascii", "ignore")) if len(parts) >= 2 else None
            if stream is None or parts[0] != b"GET":
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            started = time.monotonic()
            sent = 0
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n"
                         b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n")
            while not stream.cut.is_set():
                data = await stream.chunks.get()
                if data is None:
                    break
                writer.write(b"%x\r\n" % len(data) + data + b"\r\n")
                await writer.drain()
                sent += len(data)
                ahead = sent / MP3_BYTES_PER_SECOND - (time.monotonic() - started) - STREAM_LEAD_SECONDS
                if ahead > 0:
                    try:
                        await asyncio.wait_for(stream.cut.wait(), ahead)
                    except asyncio.TimeoutError:
                        pass
            # Also when interrupted: the device then sees a normal end of file.
            writer.write(b"0\r\n\r\n")
            await writer.drain()
            log.info("satellite speech delivered: %.1fs%s", stream.seconds, " (interrupted)" if stream.cut.is_set() else "")
        except (asyncio.TimeoutError, ConnectionError, OSError) as error:
            log.info("satellite speech fetch ended early: %s", error)
        finally:
            writer.close()

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
            # Same rule as the desktop detector (voice/wake.py).
            for name in names:
                score = scores.get(name, 0.0)
                if score < self.config.wake_threshold:
                    consecutive[name] = 0
                    continue
                consecutive[name] += 1
                if consecutive[name] >= self.config.wake_trigger_frames and self._phase == "wake":
                    log.info("wake word detected: %s (score=%.2f, microphone=satellite)", name, score)
                    consecutive = dict.fromkeys(names, 0)
                    self._begin(woke=True)
                    break

    # ------------------------------------------------------- a conversation
    def _begin(self, woke: bool) -> None:
        while not self._mic.empty():
            self._mic.get_nowait()
        self._phase = "talk"
        self._hearing = False
        self._event("RUN_START")
        if woke:
            self._event("WAKE_WORD_END")
        self._event("STT_START")
        self._show("listening")
        self._session_task = asyncio.create_task(self._run_session(self._session_task, self._runs))

    def _hang_up(self) -> None:
        if self._adapter is not None:
            self._adapter._hangup.set()

    def _speak(self, pcm: bytes) -> None:
        now = time.monotonic()
        stream = self._stream
        if stream is None or stream.closed:
            stream = self._stream = ReplyStream()
            self._streams[stream.path] = stream
            for path in list(self._streams)[:-4]:
                del self._streams[path]
            self._play(self._address + stream.path)
            self._event("TTS_START", {"text": "..."})
            self._hearing = False
            self._show("speaking")
        if self.config.satellite_state_command:
            # One level per 100 ms of speech, on the desktop overlay's scale,
            # stamped with the moment the speaker reaches it.
            samples = np.frombuffer(pcm[:len(pcm) // 2 * 2], dtype="<i2").astype(np.float32)
            starts = max(stream._until, now) + PLAY_START_SECONDS
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
        self._spoke_until = time.monotonic() + STREAM_LEAD_SECONDS
        return True

    def _interrupted(self) -> None:
        """The user spoke over her."""
        if self._cut():
            self._event("STT_START")
            self._show("listening")

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

    async def _run_session(self, previous: asyncio.Task | None, run: int) -> None:
        from google import genai
        from ..core import agenda, conversations
        from ..core.history import append_session
        from .gemini_live import GeminiLiveSession, build_live_config

        if previous is not None and not previous.done():
            await asyncio.gather(previous, return_exceptions=True)  # it saves the conversation this one continues
        woke = time.monotonic()
        adapter = GeminiLiveSession(self.config)
        adapter.on_message = self._on_message
        adapter._loop = asyncio.get_running_loop()  # notice() from other threads (quota)
        # No echo gate: the device cancels her voice itself (microphone level
        # while she spoke = the quiet level), and the desktop's gate blanked
        # the quiet parts of words said over her: "turn off the dining light"
        # arrived as "the dining light", and the light was turned ON
        # (2026-09-30 22:24). With a floor of 0 every frame passes.
        adapter.BARGE_FLOOR_RMS = 0.0
        adapter._splice_abort_rms = float(self.config.satellite_speech_rms)
        self._adapter = adapter
        self._stream, self._spoke_until = None, 0.0
        tasks: list[asyncio.Task] = []
        client = None
        failed = False
        try:
            conversation = await asyncio.to_thread(conversations.desktop, "satellite")
            adapter._transcript = conversations.Transcript(conversation, adapter._transcript)
            conversations.CURRENT.set(conversation)
            prepared = time.monotonic()
            live_config = build_live_config(self.config)
            live_config["system_instruction"] += SATELLITE_NOTE
            built = time.monotonic()
            client = genai.Client(api_key=Path(self.config.gemini_api_key_path).read_text().strip())
            async with client.aio.live.connect(model=self.config.gemini_model, config=live_config) as session:
                agenda.mark_briefed()  # this prompt carried the pending results
                agenda.attach_call(adapter, asyncio.get_running_loop())
                log.info("satellite conversation started: session=%s model=%s (conversation %.1fs, prompt %.1fs, "
                         "connect %.1fs)", adapter._audit_session, self.config.gemini_model, prepared - woke,
                         built - prepared, time.monotonic() - built)
                workers = [self._send_audio(adapter, session), adapter._receive(session), adapter._tools(session),
                           adapter._announcer(session), adapter._stuck_turn_recovery(session),
                           adapter._task_status(session), self._drain_audio(adapter), self._speech_watch(),
                           self._silence_watch(adapter, time.monotonic()), adapter._hangup.wait()]
                if self.config.jev_fast_path:
                    workers.append(adapter._fast_path())
                tasks = [asyncio.create_task(worker) for worker in workers]
                done, _ = await asyncio.wait(tasks, timeout=self.config.max_session_seconds,
                                             return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
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


async def start(config) -> Satellite | None:
    """The satellite named in config.yaml (satellite_host), or None."""
    if not getattr(config, "satellite_host", None):
        return None
    if getattr(config, "provider", "gemini") != "gemini":
        log.warning("voice satellite needs the Gemini provider; not started")
        return None
    satellite = Satellite(config)
    try:
        await satellite.start()
    except Exception:  # noqa: BLE001 -- a satellite problem must never stop the desktop assistant
        log.exception("voice satellite could not start")
        return None
    return satellite
