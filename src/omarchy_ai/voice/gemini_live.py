"""Full-duplex Gemini Live audio and desktop actions."""
from __future__ import annotations

import asyncio
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


def build_live_config(config):
    shared = build_session_config(config)
    tools = [{"name": t["name"], "description": t["description"],
              "parameters_json_schema": t["parameters"], "behavior": "BLOCKING"}
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
            "[Automatic wake notice] Briefly announce this verified notice, then listen: "
            + notice + " Do not install anything without an explicit user update request."}]}, turn_complete=True)


class GeminiLiveSession:
    def __init__(self, config):
        self.config = config
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
        self._generation = 0
        self._speaker = None
        self._speaker_generation = -1
        self._action_log = []
        self._input_guard = InputGuard()
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
                if server.model_turn:
                    for part in server.model_turn.parts or []:
                        if part.inline_data and part.inline_data.data:
                            self._received_audio_bytes += len(part.inline_data.data)
                            self._audio.put_nowait((self._generation, part.inline_data.data))
                if server.turn_complete:
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
            log.info("Gemini action started: session=%s call=%s name=%s args=%r",
                     self._audit_session, call.id, call.name, call.args or {})
            self._pending_calls.add(call.id)
            try:
                if call.name == "get_recent_actions":
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
            if call.id not in self._cancelled:
                await session.send_tool_response(function_responses=types.FunctionResponse(
                    id=call.id, name=call.name, response=response))
                log.info("Gemini tool response delivered: session=%s call=%s interrupted=%s",
                         self._audit_session, call.id, call.id in self._interrupted_calls)
            self._pending_calls.discard(call.id)

    async def _send_audio(self, session, mic):
        from google.genai import types
        while True:
            chunk = await mic.stdout.read(640)
            if not chunk:
                raise RuntimeError("Microphone stream ended")
            samples = np.frombuffer(chunk[:len(chunk) // 2 * 2], dtype="<i2").astype(np.float32)
            if samples.size:
                self._mic_levels.append((time.monotonic(), float(np.sqrt(np.mean(samples * samples))),
                                         int(np.max(np.abs(samples)))))
            await session.send_realtime_input(audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"))

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
                    "--latency", "40ms", "--target", self._echo.sink, "-a", "-", stdin=asyncio.subprocess.PIPE,
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
            await asyncio.sleep(len(raw) / 48000)
            if self._audio.empty():
                self._state, self._level = "listening", 0.0

    async def _visuals(self):
        previous = None
        while True:
            state = self._state
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
                if self.config.watchdog_enabled:
                    await asyncio.to_thread(watchdog.start, self.config.watchdog_display_mode)
                workers = [self._send_audio(session, mic), self._receive(session), self._play_audio(), self._tools(session), self._hangup.wait()]
                if self.config.watchdog_enabled:
                    workers.append(self._visuals())
                tasks = [asyncio.create_task(worker) for worker in workers]
                await announce_update(session)
                done, _ = await asyncio.wait(tasks, timeout=self.config.max_session_seconds, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        finally:
            log.info("Gemini session audio summary: session=%s interruptions=%d received_audio_bytes=%d submitted_audio_bytes=%d",
                     self._audit_session, self._interruptions, self._received_audio_bytes, self._submitted_audio_bytes)
            from ..execution.browser_jev import cancel_browser_tasks
            cancel_browser_tasks()
            self._hangup.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._stop_process(mic)
            await self._stop_process(self._speaker)
            try:
                await self._echo.close()
            except Exception:
                log.exception("Could not unload session echo cancellation")
            await client.aio.aclose()
            await asyncio.to_thread(status_icon.set_live, False)
            if self.config.watchdog_enabled:
                await asyncio.to_thread(watchdog.stop)
            await asyncio.to_thread(append_session, self._transcript, self.config.context_retention_hours)
