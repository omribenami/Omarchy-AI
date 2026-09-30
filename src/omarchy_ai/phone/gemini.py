"""Authenticated phone WebRTC audio bridged to Gemini's server-side socket.

Browser audio stays on its existing media track (and speaker routing).
No API keys or tool execution authority are sent to the browser.
"""
import asyncio
import concurrent.futures
import fractions
import json
import logging
import threading
import time
from pathlib import Path

import av
import numpy as np
from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from google import genai
from google.genai import types

from ..core.history import append_session
from ..voice.gemini_live import GeminiLiveSession, build_live_config

log = logging.getLogger(__name__)

# The stuck-turn guard's "the user is talking again" level for phone audio.
# The desktop value (3500) was measured on the desktop mic at 35% gain; the
# phone browser applies its own gain control and noise suppression, so the
# same voice can arrive quieter. Lower means the guard backs off sooner
# (never clips a quiet speaker, may splice less under loud background).
# Calibrate from the "loud-run speech p50" in the guard's log lines.
PHONE_SPLICE_ABORT_RMS = 1500.0
_slots = threading.BoundedSemaphore(2)
# Phone calls in progress: (their loop, adapter, set once the call is saved).
_calls: list = []
_calls_lock = threading.Lock()


def _end_previous_calls(timeout: float = 3.0) -> None:
    """A new call from the phone ends the calls before it, and waits until
    their conversation is saved, so the new one starts with it as context.
    2026-09-30 13:36: a text chat's link died without either side noticing;
    the server kept the call open, so its conversation was never saved and
    the reconnected chat had no memory of it."""
    with _calls_lock:
        current = list(_calls)
    for loop, adapter, _ in current:
        try:
            loop.call_soon_threadsafe(adapter._hangup.set)
        except RuntimeError:  # its loop already closed
            pass
    for _, _, saved in current:
        saved.wait(timeout)


def _phone_message(raw):
    """Return a validated (kind, text) data-channel message, or None."""
    if not isinstance(raw, str) or len(raw) > 32768:
        raise ValueError('Message too large')
    message = json.loads(raw)
    if message.get('type') == 'omarchy.context':
        text = message.get('text')
        if not isinstance(text, str):
            raise ValueError('Invalid context')
        text = text.strip()
        return ('context', text) if text else None
    item = message.get('item') or {}
    if message.get('type') == 'response.item.create' and item.get('type') == 'message' and item.get('role') == 'user':
        text = '\n'.join(p.get('text', '') for p in item.get('content', []) if p.get('type') == 'input_text')
        return ('user', text) if text else None
    return None


class OutputAudio(AudioStreamTrack):
    def __init__(self, adapter):
        super().__init__()
        self.adapter = adapter
        self.buffer = bytearray()
        self.generation = adapter._generation
        self.pts = 0
        self.started = None

    async def recv(self):
        loop = asyncio.get_running_loop()
        if self.started is None:
            self.started = loop.time()
        await asyncio.sleep(max(0, self.started + self.pts / 24000 - loop.time()))
        if self.generation != self.adapter._generation:
            self.buffer.clear()
            self.generation = self.adapter._generation
        while len(self.buffer) < 960 and not self.adapter._audio.empty():
            generation, data = self.adapter._audio.get_nowait()
            if generation == self.generation:
                self.buffer.extend(data)
        data = bytes(self.buffer[:960])
        del self.buffer[:960]
        frame = av.AudioFrame(format='s16', layout='mono', samples=480)
        frame.planes[0].update(data.ljust(960, b'\0'))
        frame.sample_rate = 24000
        frame.time_base = fractions.Fraction(1, 24000)
        frame.pts = self.pts
        self.pts += 480
        return frame


async def _serve(config, sdp, answer, cancelled):
    # No STUN: with iceServers unset aiortc falls back to Google's STUN and
    # waits out its timeout before answering -- measured 5.01s per phone
    # session vs 0.01s without (2026-09-23). The phone reaches this machine
    # over the LAN or Tailscale, where host candidates are all that is used.
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    adapter = GeminiLiveSession(config)
    adapter._splice_abort_rms = PHONE_SPLICE_ABORT_RMS
    adapter.from_paired_phone = True  # only started for a paired session (/api/live/offer)
    peer.addTrack(OutputAudio(adapter))
    incoming = asyncio.Queue(maxsize=1)
    messages = asyncio.Queue(maxsize=32)
    channel = None
    tasks = []
    client = genai.Client(api_key=Path(config.gemini_api_key_path).read_text().strip())
    call = (asyncio.get_running_loop(), adapter, threading.Event())
    with _calls_lock:
        _calls.append(call)

    def emit(event):
        if channel is not None and channel.readyState == 'open':
            channel.send(json.dumps(event))

    def notify(message):
        content = message.server_content
        if not content:
            return
        for name, transcript in (('input', content.input_transcription), ('output', content.output_transcription)):
            if transcript and transcript.text:
                emit({'type': f'session.{name}_transcript.delta', 'delta': transcript.text})
        if content.turn_complete or content.interrupted:
            emit({'type': 'response.event', 'event': {'type': 'response.completed'}})
    adapter.on_message = notify

    @peer.on('track')
    def track_received(track):
        if track.kind == 'audio' and incoming.empty():
            incoming.put_nowait(track)

    @peer.on('datachannel')
    def data_received(value):
        nonlocal channel
        channel = value

        @channel.on('message')
        def message_received(raw):
            try:
                message = _phone_message(raw)
                if message:
                    messages.put_nowait(message)
            except (ValueError, TypeError, AttributeError, asyncio.QueueFull):
                emit({'type': 'error', 'error': {'message': 'Invalid message or too many pending messages'}})

        @channel.on('close')
        def closed():
            adapter._hangup.set()

    @peer.on('connectionstatechange')
    async def changed():
        if peer.connectionState in ('failed', 'closed', 'disconnected'):
            adapter._hangup.set()

    track = None

    async def send_audio(session):
        nonlocal track
        if track is None:  # kept across a model fallback: the call itself stays up
            track = await incoming.get()
        resampler = av.AudioResampler(format='s16', layout='mono', rate=16000)
        # Numbers only, no audio: the incoming format once, and every 30 s the
        # arrival gaps and bursts of the phone's frames. Calls from Flux's
        # WebView stalled after 2-3 turns while the phone's browser ran 20+
        # (2026-09-28), with the same levels; delivery timing is the next suspect.
        described = False
        last_arrival = window_start = time.monotonic()
        gaps = []
        received = 0.0
        try:
            while True:
                incoming_frame = await track.recv()
                arrival = time.monotonic()
                if not described:
                    log.info('phone audio in: %s Hz, %s, %s samples/frame, %s', incoming_frame.sample_rate,
                             incoming_frame.layout.name, incoming_frame.samples, incoming_frame.format.name)
                    described = True
                gaps.append(arrival - last_arrival)
                last_arrival = arrival
                received += incoming_frame.samples / (incoming_frame.sample_rate or 48000)
                if arrival - window_start >= 30:
                    ordered = sorted(gaps)
                    log.info('phone audio timing: %.1fs of audio in %.1fs; frame gap p50=%.0fms p99=%.0fms max=%.0fms; '
                             '>200ms gaps=%d', received, arrival - window_start, 1000 * ordered[len(ordered) // 2],
                             1000 * ordered[int(len(ordered) * .99)], 1000 * ordered[-1], sum(g > .2 for g in gaps))
                    window_start, gaps, received = arrival, [], 0.0
                for frame in resampler.resample(incoming_frame):
                    pcm = bytes(frame.planes[0])[:frame.samples * 2]
                    # Same stuck-turn guard as the desktop mic (2026-09-24 21:07:50-21:09:26:
                    # a 96s freeze on the phone, where audio used to bypass it). The phone
                    # does its own echo cancellation, so only the guard part applies here.
                    samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32)
                    rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
                    now = time.monotonic()
                    adapter._mic_levels.append((now, rms, int(np.max(np.abs(samples))) if samples.size else 0))
                    for part in adapter._gate(pcm, rms, now):
                        await session.send_realtime_input(audio=types.Blob(data=part, mime_type='audio/pcm;rate=16000'))
        except MediaStreamError:
            adapter._hangup.set()

    async def send_text(session):
        told = False
        while True:
            kind, text = await messages.get()
            if kind == 'context':
                await session.send_client_content(
                    turns={'role': 'user', 'parts': [{'text': '[Context from the phone (a notification, or this chat so far), not a request; use it to understand the next user message]\n' + text}]},
                    turn_complete=False)
                continue
            if not told:
                # Only Text mode types, and it plays no audio: tell her once
                # so replies are written to be read (turn_complete=False adds
                # context without a reply of its own; probed 2026-09-26).
                from ..voice.text_chat import TEXT_NOTE
                await session.send_client_content(
                    turns={'role': 'user', 'parts': [{'text': '[Context, not a request]' + TEXT_NOTE}]},
                    turn_complete=False)
                told = True
            adapter._transcript.append({'role': 'user', 'text': text})
            adapter._capture_password(text)  # a typed "pass: X" goes to the keyring too
            adapter._input_guard.heard_user()
            emit({'type': 'response.event', 'event': {'type': 'response.created'}})
            await session.send_client_content(turns={'role': 'user', 'parts': [{'text': text}]}, turn_complete=True)

    async def watch_offer():
        # Reap abandoned HTTP requests and peers that never connect.
        for _ in range(300):
            if cancelled.is_set():
                adapter._hangup.set()
                return
            if peer.connectionState == 'connected':
                await adapter._hangup.wait()
                return
            await asyncio.sleep(.1)
        adapter._hangup.set()

    try:
        from ..core import agenda
        # Google's side can fail mid-call (voice/fallback.py): the WebRTC call
        # stays up, only the Gemini connection moves to the next model.
        models = await adapter._start_models()
        peer_tasks = []
        for index, model in enumerate(models):
            session_tasks = []
            try:
                async with client.aio.live.connect(model=model, config=build_live_config(config)) as session:
                    if not answer.done():
                        agenda.mark_briefed()  # this prompt carried the heartbeat's pending results
                        # Task results and heartbeat results arriving mid-call are said
                        # here (2026-09-26: a task ended while the user was on the phone
                        # and they heard nothing).
                        agenda.attach_call(adapter, asyncio.get_running_loop())
                        await peer.setRemoteDescription(RTCSessionDescription(sdp=sdp, type='offer'))
                        await peer.setLocalDescription(await peer.createAnswer())
                        if cancelled.is_set():
                            return
                        answer.set_result(peer.localDescription.sdp)
                        peer_tasks = [asyncio.create_task(coro) for coro in (
                            watch_offer(), adapter._hangup.wait(),
                            *([adapter._fast_path()] if config.jev_fast_path else []))]
                    if index:
                        await adapter._carry_over(session)
                    log.info('Phone Gemini WebRTC bridge ready: %s', model)
                    session_tasks = [asyncio.create_task(coro) for coro in (
                        send_audio(session), send_text(session), adapter._receive(session),
                        adapter._tools(session), adapter._announcer(session),
                        # 2026-09-28 08:52: three splices, no reply, the user unheard. Over
                        # 3 days 22 of 62 phone splices were never answered (desktop:
                        # 15 rescued by this forced closure, which the phone lacked).
                        adapter._stuck_turn_recovery(session), adapter._task_status(session))]
                    tasks = peer_tasks + session_tasks
                    done, _ = await asyncio.wait(tasks, timeout=config.max_session_seconds,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                break
            except Exception as error:
                for task in session_tasks:
                    task.cancel()
                await asyncio.gather(*session_tasks, return_exceptions=True)
                tasks = list(peer_tasks)
                if not await adapter._fall_back(models, index, error):
                    raise
    except Exception as error:
        from ..core import quota
        out_of_quota = quota.is_quota_error(str(error))
        if out_of_quota:
            quota.report("gemini", f"phone: {error}", user_initiated=True)
        if not answer.done():
            answer.set_exception(error)
        else:
            log.exception('Phone Gemini session failed')
            emit({'type': 'error', 'error': {'message': 'Gemini quota used up.' if out_of_quota
                                             else 'Gemini connection ended. Please reconnect.'}})
    finally:
        try:
            from ..core import agenda
            agenda.detach_call(adapter)
            agenda.mark_delivered(adapter.acknowledged_ids())  # heard and answered on this call
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await peer.close()
            await client.aio.aclose()
            await asyncio.to_thread(append_session, adapter._transcript, config.context_retention_hours)
            from ..core import alert_clips
            alert_clips.prepare_in_background(config)  # alerts in the language just spoken
        finally:
            with _calls_lock:
                if call in _calls:
                    _calls.remove(call)
            call[2].set()


def relay_offer(config, sdp):
    _end_previous_calls()
    if not _slots.acquire(blocking=False):
        raise RuntimeError('Two Gemini phone sessions are already active; hang up one first')
    answer = concurrent.futures.Future()
    cancelled = threading.Event()

    def run():
        try:
            asyncio.run(_serve(config, sdp, answer, cancelled))
        except Exception as error:
            if not answer.done():
                answer.set_exception(error)
            log.exception('Phone Gemini bridge stopped')
        finally:
            _slots.release()
    threading.Thread(target=run, daemon=True, name='phone-gemini').start()
    try:
        return answer.result(timeout=18)
    except BaseException:
        cancelled.set()
        raise
