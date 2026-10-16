#!/usr/bin/env python3
"""Phase 1 spike, take 2: gpt-live-1 two-way audio via aiortc instead of
GStreamer's webrtcbin.

Switched after webrtcbin hit two real, distinct bugs in a row (a streaming-
thread deadlock on incoming tracks, then a receive-path routing issue where
zero audio frames ever arrived despite a confirmed live, gpt-5-backed
conversation over the data channel — see docs/DEPENDENCIES.md / STATUS.md
for the full trail). aiortc is pure Python, async-native, and the standard
tool for exactly this "Python client talks to a WebRTC voice API" shape —
no opaque C pipeline threading to fight. webrtcbin stays the right choice
for the TV-casting subsystem, which needs tight PipeWire+VAAPI integration
that GStreamer is actually good at.

Mic in and model audio out both go through raw pw-record/pw-play
subprocesses, same as the rest of this project.
"""
from __future__ import annotations

import asyncio
import fractions
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request
import wave

import av
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack

OUTPUT_GAIN = 4.0  # measured RMS was ~451/32767 (~-37dBFS) — objectively quiet

KEY_PATH = os.path.expanduser("~/.config/omavoice/key")
API_URL = "https://api.openai.com/v1/live/sessions"
RUN_SECONDS = 45
SPEAK_WINDOW_SECONDS = 6  # time given to actually say something before response.create
RATE = 48000
FRAME_SAMPLES = 960  # 20ms at 48kHz, the standard WebRTC frame size


def read_key() -> str:
    with open(KEY_PATH) as f:
        return f.read().strip()


class MicTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self):
        super().__init__()
        self._proc = subprocess.Popen(
            [
                "pw-record", "--rate", str(RATE), "--channels", "1",
                "--format", "s16", "--latency", "20ms", "-a", "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
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


PREBUFFER_FRAMES = 25  # ~500ms at 20ms/frame — generous cushion for a long,
# multi-turn conversation under real network jitter, not just a short test.
# A smaller (160ms) one-time cushion was enough for a brief single reply but
# came back under sustained load (long conversation, simultaneous mic
# capture). An adaptive re-buffer-on-empty approach was tried and reverted
# — resetting on every empty queue risked mistaking a normal pause between
# conversation turns (nothing being said, nothing to play) for an underrun
# and adding audible stutters of its own. A larger flat cushion is simpler
# and doesn't have that failure mode; the cost is ~500ms of extra latency
# once at the start of each response, which is an acceptable trade for a
# voice assistant.
REBUFFER_GRACE_EMPTY_POLLS = 3  # only re-prime after several consecutive
# empty reads, not the first one — avoids treating a normal short pause
# between sentences as an underrun.


def _playback_thread(proc, q: queue.Queue, stop: threading.Event):
    # A dedicated OS thread draining a queue, fully decoupled from asyncio's
    # scheduling — necessary but not sufficient on its own (confirmed: the
    # saved-file playback test came back clean while the live stream still
    # had static, isolating the cause to real-time delivery timing).
    primed = False
    empty_polls = 0
    while not stop.is_set():
        if not primed:
            if q.qsize() < PREBUFFER_FRAMES:
                time.sleep(0.01)
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


async def play_remote_audio(track):
    proc = subprocess.Popen(
        [
            "pw-play", "-v", "--rate", str(RATE), "--channels", "2",
            "--format", "s16", "--latency", "200ms", "-a", "-",
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def _log_stderr():
        for line in proc.stderr:
            text = line.decode(errors="replace").rstrip()
            if text:
                print(f"pw-play: {text}", flush=True)

    threading.Thread(target=_log_stderr, daemon=True).start()
    q: queue.Queue = queue.Queue()
    stop = threading.Event()
    thread = threading.Thread(target=_playback_thread, args=(proc, q, stop), daemon=True)
    thread.start()

    # Diagnostic experiment: keep native stereo end to end (matches the
    # source exactly — confirmed via logging: s16/stereo/48000) instead of
    # downmixing to mono, to test whether the resampler's downmix math is
    # contributing to the residual static — real packet loss is ruled out
    # (packetsLost=0 throughout, confirmed via pc.getStats()).
    resampler = av.AudioResampler(format="s16", layout="stereo", rate=RATE)
    frames = 0

    mono_wav = wave.open("/tmp/oma-debug-resampled-mono.wav", "wb")
    mono_wav.setnchannels(2)
    mono_wav.setsampwidth(2)
    mono_wav.setframerate(RATE)

    try:
        while True:
            frame = await track.recv()
            if frames == 0:
                print(
                    f"*** first remote audio frame: format={frame.format.name} "
                    f"layout={frame.layout.name} rate={frame.sample_rate} "
                    f"samples={frame.samples} ***",
                    flush=True,
                )
            frames += 1
            for resampled in resampler.resample(frame):
                pcm = bytes(resampled.planes[0])[: resampled.samples * 2 * 2]
                samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                boosted = np.clip(samples * OUTPUT_GAIN, -32768, 32767).astype(np.int16)
                out = boosted.tobytes()
                mono_wav.writeframes(out)
                q.put_nowait(out)
    except Exception as e:  # noqa: BLE001
        print(f"playback loop ended: {e} (after {frames} frames)", flush=True)
    finally:
        mono_wav.close()
        stop.set()
        q.put_nowait(None)
        thread.join(timeout=2)
        proc.stdin.close()
        proc.wait()


async def main():
    pc = RTCPeerConnection()
    pc.addTrack(MicTrack())
    dc = pc.createDataChannel("oai-events")

    @dc.on("open")
    def on_open():
        print(
            "*** data channel open — SPEAK NOW, response requested in "
            f"{SPEAK_WINDOW_SECONDS}s ***",
            flush=True,
        )

        async def _delayed_response():
            await asyncio.sleep(SPEAK_WINDOW_SECONDS)
            print("*** requesting a response now ***", flush=True)
            dc.send(json.dumps({"type": "response.create"}))

        asyncio.ensure_future(_delayed_response())

    @dc.on("message")
    def on_message(message):
        print(f"data channel <- {message[:300]}", flush=True)

    @pc.on("track")
    def on_track(track):
        print(f"*** remote track arrived: {track.kind} ***", flush=True)
        if track.kind == "audio":
            asyncio.ensure_future(play_remote_audio(track))

    @pc.on("connectionstatechange")
    async def on_state_change():
        print(f"connection state -> {pc.connectionState}", flush=True)

    async def _report_stats():
        while True:
            await asyncio.sleep(5)
            try:
                stats = await pc.getStats()
            except Exception:  # noqa: BLE001
                continue
            for s in stats.values():
                if getattr(s, "type", "") == "inbound-rtp" and getattr(s, "kind", "") == "audio":
                    print(
                        f"*** RTP stats: packetsLost={getattr(s, 'packetsLost', '?')} "
                        f"packetsReceived={getattr(s, 'packetsReceived', '?')} "
                        f"jitter={getattr(s, 'jitter', '?')} "
                        f"concealedSamples={getattr(s, 'concealedSamples', '?')} ***",
                        flush=True,
                    )

    asyncio.ensure_future(_report_stats())

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    while pc.iceGatheringState != "complete":
        await asyncio.sleep(0.1)

    sdp_text = pc.localDescription.sdp
    print(f"--- local offer ({len(sdp_text)} bytes) ---", flush=True)

    body = json.dumps(
        {
            "session": {
                "model": "gpt-live-1",
                "delegation": {
                    "type": "responses",
                    "responses": {"model": "gpt-5"},
                },
                "instructions": (
                    "You are Oma, a voice assistant for a Linux desktop "
                    "called Omarchy. Keep responses brief and "
                    "conversational. Respond specifically to what the user "
                    "actually said — do not give a generic greeting. If you "
                    "genuinely did not hear anything from them, say so "
                    "plainly and ask them to try again, rather than "
                    "greeting them as if they'd spoken."
                ),
                "audio": {"output": {"voice": "marin"}},
            },
            "transport": {"type": "webrtc", "sdp": sdp_text},
        }
    ).encode()

    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {read_key()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            response = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.read().decode(), flush=True)
        return

    answer_sdp = response["transport"]["sdp"]
    print(f"--- got remote answer ({len(answer_sdp)} bytes) ---", flush=True)
    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))
    print(f"--- remote description set — running for {RUN_SECONDS}s ---", flush=True)

    await asyncio.sleep(RUN_SECONDS)
    print("time's up, hanging up", flush=True)
    await pc.close()


if __name__ == "__main__":
    asyncio.run(main())
