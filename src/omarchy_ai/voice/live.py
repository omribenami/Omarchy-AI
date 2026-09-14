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
import urllib.error
import urllib.request

import av
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack
from rapidfuzz import fuzz

from ..config import Config

log = logging.getLogger("omarchy_ai.voice.live")

RATE = 48000
FRAME_SAMPLES = 960  # 20ms at 48kHz, the standard WebRTC frame size
OUTPUT_GAIN = 4.0  # measured RMS was ~451/32767 (~-37dBFS) without this
PREBUFFER_FRAMES = 25  # ~500ms — see STATUS.md "Residual static" for why
REBUFFER_GRACE_EMPTY_POLLS = 3


class MicTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, device: str | None = None):
        super().__init__()
        argv = [
            "pw-record", "--rate", str(RATE), "--channels", "1",
            "--format", "s16", "--latency", "20ms", "-a",
        ]
        if device:
            argv += ["--target", device]
        argv.append("-")
        self._proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
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
        frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
        frame.planes[0].update(buf.ljust(self._frame_bytes, b"\x00"))
        frame.sample_rate = RATE
        frame.pts = self._samples_sent
        frame.time_base = fractions.Fraction(1, RATE)
        self._samples_sent += FRAME_SAMPLES
        return frame

    def stop(self) -> None:
        super().stop()
        if self._proc.poll() is None:
            self._proc.terminate()


def _playback_thread(proc: subprocess.Popen, q: queue.Queue, stop: threading.Event) -> None:
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
        "goodbye", "good bye", "bye for now", "bye!", "bye.", "take care",
        "see you", "farewell", "talk to you later", "have a good",
    )

    def __init__(self, config: Config):
        self.config = config
        self._pc: RTCPeerConnection | None = None
        self._hangup = asyncio.Event()
        self._input_buffer = ""
        self._output_buffer = ""
        self._farewell_scheduled = False

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
                "--format", "s16", "--latency", "200ms", "-a", "-",
            ],
            stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        q: queue.Queue = queue.Queue()
        stop = threading.Event()
        thread = threading.Thread(
            target=_playback_thread, args=(proc, q, stop), daemon=True
        )
        thread.start()

        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        try:
            while True:
                frame = await track.recv()
                for resampled in resampler.resample(frame):
                    pcm = bytes(resampled.planes[0])[: resampled.samples * 2]
                    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                    boosted = np.clip(samples * OUTPUT_GAIN, -32768, 32767).astype(np.int16)
                    q.put_nowait(boosted.tobytes())
        except Exception:  # noqa: BLE001
            log.debug("playback loop ended", exc_info=True)
        finally:
            stop.set()
            q.put_nowait(None)
            thread.join(timeout=2)
            proc.stdin.close()
            proc.wait()

    def _check_function_call(self, event: dict) -> None:
        """Look for an end_conversation tool call inside a response.event
        wrapper. Schema not fully documented — this checks the shapes
        actually observed in this session's response.output_item.* events
        (item.type == "function_call") plus the more conventional
        top-level Responses-API streaming event name, in case the real
        session uses that instead. Logged at debug either way so the real
        shape can be confirmed/corrected from a live run.
        """
        inner = event.get("event", {})
        item = inner.get("item", {})
        if (
            inner.get("type") in ("response.output_item.done", "response.output_item.added")
            and item.get("type") == "function_call"
            and item.get("name") == "end_conversation"
        ):
            log.info("end_conversation tool call received")
            self._hangup.set()

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
            self._input_buffer += event.get("delta", "")
            self._output_buffer = ""
            self._farewell_scheduled = False
            if self._check_exit_phrase(self._input_buffer):
                self._hangup.set()
        elif etype == "session.output_transcript.delta":
            # The model started replying — the user's turn is over. Reset
            # the input buffer so the next utterance is judged on its own.
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
            log.warning("gpt-live-1 error event: %s", event.get("error"))
        log.debug("data channel event: %s", message[:500])

    async def run(self) -> None:
        """Connect, converse, and return once the session ends (exit
        phrase, the safety timeout, or a connection failure)."""
        pc = RTCPeerConnection()
        self._pc = pc
        mic = MicTrack(self.config.mic_device)
        pc.addTrack(mic)
        dc = pc.createDataChannel("oai-events")

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
                    "session": {
                        "model": self.config.live_model,
                        "delegation": {
                            "type": "responses",
                            "responses": {
                                "model": self.config.responses_model,
                                # Without this the model apparently never
                                # considers calling the tool at all — zero
                                # function_call events across a full real
                                # conversation where the user clearly asked
                                # to end it. "auto" makes tool use available
                                # rather than off by default.
                                "tool_choice": "auto",
                                # Confirmed by probing the real API: tools
                                # live under delegation.responses, not at
                                # the session top level (session.tools is
                                # rejected as unknown_parameter).
                                "tools": [
                                    {
                                        "type": "function",
                                        "name": "end_conversation",
                                        "description": (
                                            "Call this when the user "
                                            "indicates they want to end the "
                                            "conversation — e.g. saying "
                                            "goodbye, stop, that's all, or "
                                            "similar — in whatever language "
                                            "they used. Do not call it for "
                                            "unrelated use of similar words "
                                            "(e.g. 'can we stop for a "
                                            "second')."
                                        ),
                                        "parameters": {
                                            "type": "object",
                                            "properties": {},
                                            "required": [],
                                        },
                                    }
                                ],
                            },
                        },
                        "instructions": self.config.instructions,
                        "audio": {"output": {"voice": self.config.voice}},
                    },
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
            log.info("live session connected")

            try:
                await asyncio.wait_for(
                    self._hangup.wait(), timeout=self.config.max_session_seconds
                )
            except asyncio.TimeoutError:
                log.info("session hit max_session_seconds, hanging up")
        finally:
            mic.stop()
            await pc.close()
            self._pc = None
