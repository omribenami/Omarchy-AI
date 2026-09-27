"""Typed conversation on the desktop: no microphone, no speaker.

The same Gemini Live session as a voice call (tools, context, task and
heartbeat announcements), driven the way the phone's Text mode drives it:
typed turns go in with send_client_content, the reply is read from Gemini's
output transcription, and the audio it still generates is discarded. A
TEXT-only session is not an option: gemini-3.8-live refuses it (probe
2026-09-26: "1007 The requested combination of response modalities (TEXT)
is not supported by the model"). The same probe measured the first reply
text at 0.55-0.69s after a typed turn.

The HUD (omarchy-ai.chat-hud) is only a view: it sends lines through
`omarchy-ai-settings chat-send` -> the daemon's control socket -> send(),
and gets the whole conversation back through push() after every change, so
a restarted shell or a lost update never leaves it out of step.

The session connects on the first message and closes after
text_chat_idle_minutes without activity; the next message reconnects, and
the archived transcript (context_retention_hours) keeps the thread.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import subprocess
import time

log = logging.getLogger("omarchy_ai.voice.text_chat")

HUD_TARGET = "assistantChat"
MAX_MESSAGES = 60
MAX_TEXT = 8000
PUSH_DELAY = 0.12  # coalesce transcription deltas into one HUD update

TEXT_NOTE = (
    "\n\nTEXT CHAT: This conversation is typed, not spoken. The user reads your replies on screen and "
    "cannot hear you. Keep replies short and readable, and never ask them to say something out loud; "
    "they type. Everything else (tools, tasks, results) works exactly as in a voice conversation."
)


def push_to_hud(state: dict) -> None:
    """Replace the HUD's conversation (best effort, like every HUD)."""
    try:
        proc = subprocess.run(["omarchy-shell", "-q", HUD_TARGET, "setState", json.dumps(state, ensure_ascii=False)],
                              capture_output=True, text=True, timeout=5, check=False)
        if proc.returncode:
            log.warning("chat HUD update failed: %s", (proc.stderr or proc.stdout).strip())
    except (OSError, subprocess.SubprocessError):
        log.debug("chat HUD unavailable", exc_info=True)


def _connect(config, live_config):
    from google import genai
    client = genai.Client(api_key=Path(config.gemini_api_key_path).read_text().strip())
    return client, client.aio.live.connect(model=config.gemini_model, config=live_config)


class TextChat:
    def __init__(self, config, *, push=push_to_hud, connect=_connect):
        self.config = config
        self._push_fn = push
        self._connect = connect
        self.messages: list[dict] = []
        self.status = "idle"            # idle | connecting | thinking | ready | error
        self.error = ""
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._adapter = None
        self._open_reply = False        # the last assistant message is still streaming
        self._push_pending = False
        self._last_activity = 0.0

    # ------------------------------------------------------------------ API
    def send(self, text: str) -> dict:
        """Called on the daemon's loop (control socket)."""
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "empty message"}
        if getattr(self.config, "provider", "gemini") != "gemini":
            self._fail("Text chat needs the Gemini provider (Settings > Provider).")
            return {"ok": False, "error": self.error}
        self._add("user", text[:MAX_TEXT])
        self._open_reply = False
        self._last_activity = time.monotonic()
        ending = self._adapter is not None and self._adapter._hangup.is_set()
        if self._task is None or self._task.done() or ending:
            self._queue = asyncio.Queue()
            self._task = asyncio.get_running_loop().create_task(self._run(self._queue))
        self._queue.put_nowait(text[:MAX_TEXT])
        self.status, self.error = "thinking" if self.status != "connecting" else "connecting", ""
        self._schedule_push()
        return {"ok": True}

    def close(self) -> dict:
        """End the session (the HUD was closed). The log stays for next time."""
        if self._adapter is not None:
            self._adapter._hangup.set()
        self.status = "idle"
        self._schedule_push()
        return {"ok": True}

    def clear(self) -> dict:
        self.messages = []
        return self.close()

    def state(self) -> dict:
        return {"messages": self.messages[-MAX_MESSAGES:], "status": self.status, "error": self.error}

    # ------------------------------------------------------------ internals
    def _add(self, role: str, text: str) -> None:
        self.messages.append({"role": role, "text": text, "at": time.time()})
        del self.messages[:-MAX_MESSAGES]

    def _fail(self, message: str) -> None:
        self.status, self.error = "error", message
        self._add("error", message)
        self._schedule_push()

    def _schedule_push(self) -> None:
        if self._push_pending:
            return
        self._push_pending = True

        async def later():
            await asyncio.sleep(PUSH_DELAY)
            self._push_pending = False
            await asyncio.to_thread(self._push_fn, self.state())
        asyncio.get_running_loop().create_task(later())

    def _on_message(self, message, adapter) -> None:
        content = message.server_content
        if not content:
            return
        out = content.output_transcription
        if out and out.text:
            self._last_activity = time.monotonic()
            if self._open_reply and self.messages and self.messages[-1]["role"] == "assistant":
                self.messages[-1]["text"] += out.text
            else:
                self._add("assistant", out.text.lstrip())
                self._open_reply = True
            self.status = "ready"
            self._schedule_push()
        if content.turn_complete or content.interrupted:
            self._open_reply = False
            if self.status == "thinking" and not adapter._running_blocking:
                self.status = "ready"
            self._schedule_push()

    async def _drain_audio(self, adapter) -> None:
        # Nothing plays her voice; keep _receive from filling the queue.
        while True:
            await adapter._audio.get()

    async def _sender(self, adapter, session, queue) -> None:
        while True:
            text = await queue.get()
            adapter._transcript.append({"role": "user", "text": text})
            adapter._input_guard.heard_user()
            # A typed line is the user answering: announcements said before
            # it count as heard (acknowledged_ids), as speech does in a call.
            adapter.user_spoke_at = adapter._last_user_speech = time.monotonic()
            await session.send_client_content(turns={"role": "user", "parts": [{"text": text}]}, turn_complete=True)

    async def _idle_watch(self, adapter) -> None:
        limit = max(1, int(getattr(self.config, "text_chat_idle_minutes", 10))) * 60
        while True:
            await asyncio.sleep(5)
            if time.monotonic() - self._last_activity > limit and not adapter._running_blocking:
                log.info("text chat idle for %ds; closing the session", limit)
                return

    async def _run(self, queue: asyncio.Queue) -> None:
        from ..core import agenda
        from ..core.history import append_session
        from .gemini_live import GeminiLiveSession, build_live_config

        adapter = GeminiLiveSession(self.config)
        adapter.on_message = lambda message: self._on_message(message, adapter)
        self._adapter = adapter
        self.status = "connecting"
        self._schedule_push()
        tasks: list[asyncio.Task] = []
        client = None
        try:
            live_config = build_live_config(self.config)
            live_config["system_instruction"] += TEXT_NOTE
            client, connection = self._connect(self.config, live_config)
            async with connection as session:
                agenda.mark_briefed()  # this prompt carried the pending results
                agenda.attach_call(adapter, asyncio.get_running_loop())
                log.info("text chat connected: session=%s model=%s", adapter._audit_session, self.config.gemini_model)
                if self.status == "connecting":
                    self.status = "thinking"
                self._schedule_push()
                tasks = [asyncio.create_task(c) for c in (
                    self._sender(adapter, session, queue), adapter._receive(session), adapter._tools(session),
                    adapter._announcer(session), self._drain_audio(adapter), self._idle_watch(adapter),
                    adapter._hangup.wait())]
                done, _ = await asyncio.wait(tasks, timeout=self.config.max_session_seconds,
                                             return_when=asyncio.FIRST_COMPLETED)
                adapter._hangup.set()  # from here on a new message starts a new session
                for task in done:
                    task.result()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            log.exception("text chat session failed")
            from ..core import quota
            if quota.is_quota_error(str(error)):
                quota.report("gemini", f"text chat: {error}", user_initiated=True)
                self._fail("Gemini quota used up.")
            else:
                self._fail("Connection to Gemini ended. Send again to reconnect.")
        finally:
            adapter._hangup.set()
            agenda.detach_call(adapter)
            agenda.mark_delivered(adapter.acknowledged_ids())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if client is not None:
                await client.aio.aclose()
            if adapter._transcript:
                await asyncio.to_thread(append_session, adapter._transcript, self.config.context_retention_hours)
            if self._adapter is adapter:
                self._adapter = None
            if self.status != "error":
                self.status = "idle"
            self._schedule_push()
            log.info("text chat session ended: session=%s", adapter._audit_session)
