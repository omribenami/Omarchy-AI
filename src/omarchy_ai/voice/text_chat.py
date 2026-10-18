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

A connection Gemini closes for a transient reason (core/retry.py
classify_live: 1011 internal error, 1006, 1001, a network blip) is opened
again once, with the same config and this session's turns carried over
(voice/fallback.py carry_over), and the typed line it never answered is sent
again -- unless a tool call for it had started, which may have acted: then
the user is told instead. Journal 2026-10-17 08:09 and 09:50: two 1011s
each lost the user's line ("Turn on the backyard fan").
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time

from ..core import logs
from ..execution import passwords
from . import extensions, fallback

log = logging.getLogger("omarchy_ai.voice.text_chat")

HUD_TARGET = "assistantChat"
MAX_MESSAGES = 60
MAX_TEXT = 8000
PUSH_DELAY = 0.12  # coalesce transcription deltas into one HUD update

TEXT_NOTE = (
    "\n\nTEXT CHAT: This conversation is typed, not spoken. The user reads your replies on screen and "
    "cannot hear you. Keep replies short and readable, and never ask them to say something out loud; "
    "they type. Each running task has a live stage indicator directly above the text field. Do not narrate "
    "routine task progress, ask whether the user saw an update, or ask for acknowledgement of it; use the "
    "indicator. Reply only for a real approval, question, final result, or when the user asks for status. "
    "Everything else (tools, tasks, results) works exactly as in a voice conversation."
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
    # As a voice call connects, so one of the user's own extensions may hold it (voice/extensions.py).
    from .gemini_live import gemini_client, live_connect
    client = gemini_client(config)
    return client, live_connect(client, config, config.gemini_model, live_config)


def ended_message(error: BaseException, *, retried: bool = False) -> str:
    """What the user reads when the session ended for good, with the reason."""
    why = fallback.ended_reason(error)
    head = "Connection to Gemini ended" + (f" ({why})" if why else "")
    if retried:
        head += " and reconnecting did not help"
    return head + ". Send again to reconnect."


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
        self._loop: asyncio.AbstractEventLoop | None = None
        # The typed line Gemini has not answered yet (its transcript entry),
        # and how many tool calls the session had seen when it went out.
        self._unanswered: dict | None = None
        self._calls_at = 0
        # A skill saved from here is noted in the log (core/conversations.py
        # add_note). This chat has no saved conversation, so its notes are the
        # ones with no conversation id; they arrive on worker threads.
        from ..core import conversations
        conversations.NOTE_LISTENERS.append(self._on_note)

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
        self._loop = asyncio.get_running_loop()
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

    def _on_note(self, cid: str, text: str) -> None:
        if cid or self._loop is None or self._task is None or self._task.done():
            return  # another talk's note, or no chat open to show it in

        def show():
            self._add("note", text)
            self._open_reply = False  # her next words start a new line below it
            self._schedule_push()
        self._loop.call_soon_threadsafe(show)

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
            self._unanswered = None
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
            if await extensions.typed(adapter, text):
                # One of the user's own extensions took it; whatever it answered is already shown.
                if self.status == "thinking":
                    self.status = "ready"
                self._schedule_push()
                continue
            # A typed "pass: X" goes to the keyring; the model and the
            # transcript get "[password]" (execution/passwords.py).
            adapter._capture_password(text)
            text = passwords.for_speech(text)
            entry = {"role": "user", "text": text}
            adapter._transcript.append(entry)
            self._unanswered, self._calls_at = entry, len(adapter._seen)
            adapter._turn_id = logs.new_id()  # each typed line is a user turn (core/logs.py)
            adapter._input_guard.heard_user(text)
            # A typed line is the user answering: announcements said before
            # it count as heard (acknowledged_ids), as speech does in a call.
            adapter.user_spoke_at = adapter._last_user_speech = time.monotonic()
            await session.send_client_content(turns={"role": "user", "parts": [{"text": text}]}, turn_complete=True)

    async def _reconnected(self, adapter, session, queue: asyncio.Queue, resend: dict | None) -> None:
        """The new connection after a drop: this session's turns as context,
        then the unanswered line (`resend`) as the next typed turn."""
        reason, adapter._dropped_restart = adapter._dropped_restart, ""
        if resend is not None:
            # The sender adds it again when it goes out.
            for i in range(len(adapter._transcript) - 1, -1, -1):
                if adapter._transcript[i] is resend:
                    del adapter._transcript[i]
                    break
        if context := fallback.carry_over(adapter._transcript):
            await session.send_client_content(turns={"role": "user", "parts": [{"text": context}]},
                                              turn_complete=False)
        if resend is not None:
            waiting = [resend["text"]]
            while not queue.empty():
                waiting.append(queue.get_nowait())
            for text in waiting:
                queue.put_nowait(text)
        log.warning("Gemini session reconnected after %s: session=%s (text chat; resent the unanswered line: %s)",
                    reason or "a drop", adapter._audit_session, resend is not None)

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
        from .gemini_live import GeminiLiveSession, build_live_config, origin_note

        from ..core import identities
        adapter = GeminiLiveSession(self.config)
        logs.set_ids(session_id=adapter._audit_session)  # this task's log lines (core/logs.py)
        # Typed at this computer: its owner (core/identities.py), no voice match needed.
        adapter.known_identity = identities.Identity(identities.owner(), "typed")
        # No saved conversation here: the channel says where the user is (core/origin.py).
        adapter.channel = "desktop-text"
        adapter.on_message = lambda message: self._on_message(message, adapter)
        self._adapter = adapter
        self.status = "connecting"
        self._schedule_push()
        tasks: list[asyncio.Task] = []
        client = None
        attempt = 0
        resend: dict | None = None
        try:
            from .prompt_version import prompt_surface

            with prompt_surface("chat"):
                live_config = build_live_config(self.config)
            live_config["system_instruction"] += TEXT_NOTE + origin_note(channel="desktop-text")
            if who := identities.describe(adapter.known_identity):
                live_config["system_instruction"] += "\n\nWHO IS TYPING: " + who
            client, connection = self._connect(self.config, live_config)
            while True:
                try:
                    async with connection as session:
                        if attempt:
                            await self._reconnected(adapter, session, queue, resend)
                        else:
                            agenda.mark_briefed()  # this prompt carried the pending results
                            agenda.attach_call(adapter, asyncio.get_running_loop())
                            log.info("text chat connected: session=%s model=%s", adapter._audit_session,
                                     self.config.gemini_model)
                        if self.status == "connecting":
                            self.status = "thinking"
                        self._schedule_push()
                        tasks = [asyncio.create_task(c) for c in (
                            self._sender(adapter, session, queue), adapter._receive(session),
                            adapter._tools(session), adapter._announcer(session), adapter._result_watch(session),
                            self._drain_audio(adapter),
                            self._idle_watch(adapter), adapter._hangup.wait())]
                        done, _ = await asyncio.wait(tasks, timeout=self.config.max_session_seconds,
                                                     return_when=asyncio.FIRST_COMPLETED)
                        if not any(not t.cancelled() and t.exception() is not None for t in done):
                            adapter._hangup.set()  # from here on a new message starts a new session
                        for task in done:
                            task.result()
                    break
                except Exception as error:  # noqa: BLE001 -- a transient close reconnects once, else below
                    # A call for the unanswered line had started: it may have acted, so it is not sent again.
                    started = len(adapter._seen) > self._calls_at
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    tasks = []
                    attempt += 1
                    if not adapter._resume_dropped(error):
                        raise
                    resend = None if started else self._unanswered
                    if self._unanswered is not None and started:
                        self._add("note", "Lost the connection to Gemini while that was being done; it may or "
                                          "may not have finished. Reconnected: check before sending it again.")
                        self._open_reply = False
                        self.status = "ready"
                        self._schedule_push()
                    self._unanswered = None
                    if client is not None:
                        await client.aio.aclose()
                    client = None
                    client, connection = self._connect(self.config, live_config)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            log.exception("text chat session failed")
            from ..core import quota
            if quota.is_quota_error(str(error)):
                quota.report("gemini", f"text chat: {error}", user_initiated=True)
                self._fail("Gemini quota used up.")
            else:
                self._fail(ended_message(error, retried=attempt > 1))  # attempt counts the failed connections
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
