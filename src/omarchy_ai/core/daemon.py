"""Listen for a wake word or panel activation, hold one conversation,
then return to listening. There is no always-on remote voice connection.
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import signal
import threading

from ..config import Config, ensure_dirs, load_config
from ..execution import tile_logs
from . import updates
from ..phone import server as phone_server
from ..voice import feedback, control
from ..voice.live import LiveSession
from ..voice.gemini_live import GeminiLiveSession
from ..voice.omarchy import OmarchySession
from ..voice.wake import WakeWordDetector

log = logging.getLogger("omarchy_ai.core.daemon")


class OmaDaemon:
    def __init__(self) -> None:
        ensure_dirs()
        # Any files already in the tile-log cache dir are from a previous
        # process — no tracking thread survives a restart to ever delete
        # them on close, so start clean rather than accumulate orphans.
        tile_logs.sweep_stale()
        self.config: Config = load_config()
        self.wake_detector = WakeWordDetector(self.config)
        self._stop = threading.Event()
        self._listen_stop = threading.Event()
        self._manual = False
        self._state = 'listening'
        self._last_error = None
        # Runs the whole time the daemon is up (not per-conversation like
        # LiveSession) — a phone tap should work any time, no wake word
        # needed. start() itself is a no-op returning None when
        # config.phone_bridge_enabled is off.
        self._phone_server = phone_server.start(self.config)
        # Heartbeat results waiting for a conversation (see _on_agenda_result).
        self._pending_announcements = []
        self._session = None

    def panel_command(self, command):
        if command == 'activate':
            if self._state != 'listening':
                return {'state': self._state, 'error': 'A conversation is already starting or active.'}
            self._state = 'starting'
            self._manual = True
            self._listen_stop.set()
        elif command != 'status':
            return {'error': 'Unknown assistant command'}
        config = getattr(self, 'config', None)
        return {'state': self._state, 'provider': getattr(config, 'provider', None),
                'error_detail': getattr(self, '_last_error', None)}

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        def terminate():
            # The previous process ignored SIGTERM for systemd's full 90s and
            # logged nothing (2026-09-22). If shutdown stalls again, dump
            # every thread's stack to the journal so the blocker is visible.
            log.info("SIGTERM received; shutting down")
            faulthandler.dump_traceback_later(10, exit=False)
            self.stop()
            task.cancel()
        # Let active audio sessions unload their private PipeWire modules
        # when systemd restarts us, instead of exiting before their finally.
        loop.add_signal_handler(signal.SIGTERM, terminate)
        server = await control.serve(self.panel_command)
        async def refresh_updates():
            # Update "sub-agent": checks on its own schedule and notifies the
            # assistant, instead of any conversation waiting on GitHub.
            announced = None
            while True:
                try:
                    result = await asyncio.to_thread(updates.check_updates)
                    latest = result.get("latest_version") if result.get("available") else None
                    session = self._session
                    if latest and latest != announced and session is not None and hasattr(session, "announce"):
                        session.announce({"id": f"update-{latest}", "title": "Update available",
                                          "detail": updates.wake_notice()})
                        announced = latest
                except Exception:
                    log.warning("Update check unavailable", exc_info=True)
                await asyncio.sleep(updates.CACHE_SECONDS)
        update_checker = asyncio.create_task(refresh_updates())
        try:
            # Task Runtime events (done, needs approval, question) reach an
            # open conversation; they always raise a desktop notification too.
            from ..runtime import service as task_service
            task_service.announce_to(lambda: self._session, loop)
        except Exception:
            log.warning("Task runtime unavailable", exc_info=True)
        async def heartbeat():
            # Scheduled tasks and watches run here, conversation or not.
            from . import agenda
            while True:
                try:
                    await asyncio.to_thread(agenda.tick)
                except Exception:
                    log.warning("Heartbeat tick failed", exc_info=True)
                await asyncio.sleep(max(15, self.config.heartbeat_seconds))
        heart = asyncio.create_task(heartbeat()) if self.config.heartbeat_enabled else None
        async def handover():
            # Co-pilot: work started in the background while the user was
            # busy moves onto their screen once they stop touching it.
            from ..execution import browser_jev, operator, workbench
            while True:
                await asyncio.sleep(3)
                try:
                    if not (workbench.pending_handover or browser_jev._background_task):
                        continue
                    if not await asyncio.to_thread(operator.user_idle):
                        continue
                    for name in list(workbench.pending_handover):
                        workbench.pending_handover.discard(name)
                        if workbench.exists(name):
                            result = await asyncio.to_thread(workbench.show, name)
                            log.info("handover: assistant terminal %s -> user's screen (%s)", name, result.message)
                    if browser_jev._background_task:
                        browser_jev._background_task = False
                        await asyncio.to_thread(browser_jev.show_running_task)
                        log.info("handover: background browser task -> user's screen")
                except Exception:
                    log.warning("handover check failed", exc_info=True)
        handing = asyncio.create_task(handover())
        from . import agenda
        agenda.listeners.append(lambda entry: loop.call_soon_threadsafe(self._on_agenda_result, entry))
        try:
            async with server:
                await self._run_sessions()
        finally:
            update_checker.cancel()
            handing.cancel()
            if heart is not None:
                heart.cancel()
            await asyncio.gather(update_checker, *([heart] if heart else []), return_exceptions=True)
            loop.remove_signal_handler(signal.SIGTERM)
            control.socket_path().unlink(missing_ok=True)

    def _on_agenda_result(self, entry: dict) -> None:
        """Jev's heartbeat produced a result: tell the user now. Real gap
        (2026-09-23): "let me know when Claude is done", then "bye"; the
        watch fired at 01:03 and only a desktop popup appeared."""
        session = self._session
        if session is not None and hasattr(session, 'announce'):  # starting or active
            session.announce(entry)
            return
        self._pending_announcements.append(entry)
        if self._state == 'listening' and self.config.provider == "gemini":
            log.info("heartbeat result %s: waking the assistant to report it", entry.get("id"))
            self._manual = True
            self._listen_stop.set()

    async def _run_sessions(self) -> None:
        log.info(
            "omarchy-ai ready — say any of: %s",
            ", ".join(self.wake_detector.model_names),
        )
        loop = asyncio.get_event_loop()
        while not self._stop.is_set():
            woke = await loop.run_in_executor(
                None, self.wake_detector.listen, self._listen_stop
            )
            if self._stop.is_set():
                break
            manual = self._manual
            self._manual = False
            self._listen_stop.clear()
            if not woke and not manual:
                continue

            # No update check here: the background checker (refresh_updates)
            # owns it and tells an open conversation when it finds one, so a
            # slow GitHub never delays her first word (it could wait 2s).
            self._state = 'active'
            feedback.play(feedback.WAKE())
            log.info("wake word detected, starting live session")
            if manual:
                log.info('conversation activated from assistant panel')
            if self.config.provider == "gemini":
                self._state = 'starting'
                announcements, self._pending_announcements = self._pending_announcements, []
                session = GeminiLiveSession(self.config, announcements=announcements)
                self._session = session
                session.on_connected = lambda: setattr(self, '_state', 'active')
            elif self.config.provider == "omarchy":
                self._state = 'starting'
                session = OmarchySession(self.config)
                session.on_connected = lambda: setattr(self, '_state', 'active')
            else:
                session = LiveSession(self.config, mic_source='desktop' if manual else self.wake_detector.last_source)
            from . import quota
            if isinstance(session, GeminiLiveSession):
                # Another provider running out is said by this conversation;
                # Gemini itself running out falls back to the local clip.
                quota.speaker = lambda provider, text: provider != "gemini" and session.notice(text)
            try:
                self._last_error = None
                self._session = session
                await session.run()
                # Pending results in the prompt and results said aloud count
                # as delivered only if the user actually spoke; otherwise they
                # stay pending and she catches up next time.
                from . import agenda
                if getattr(session, 'user_spoke_at', 1):
                    agenda.mark_briefed()
                else:
                    agenda.forget_briefed()
                if hasattr(session, 'acknowledged_ids'):
                    agenda.mark_delivered(session.acknowledged_ids())
            except Exception as error:  # noqa: BLE001
                log.exception("live session crashed")
                self._last_error = "Connection ended unexpectedly. Check the service log, then try again."
                from . import quota
                # 2026-09-19 12:04: Gemini's "1011 Resource has been exhausted"
                # ended the conversation with no word to the user.
                if self.config.provider == "gemini" and quota.is_quota_error(str(error)):
                    quota.report("gemini", str(error), user_initiated=True)
                    self._last_error = "Gemini quota used up."
                # Keep the selected provider; never silently switch credentials.
            finally:
                from ..execution.browser_jev import cancel_browser_tasks
                cancel_browser_tasks()
                quota.speaker = None
            self._session = None
            feedback.play(feedback.HANGUP())
            log.info("session ended, back to listening")
            self._state = 'listening'

    def stop(self) -> None:
        self._stop.set()
        self._listen_stop.set()
        if self._phone_server is not None:
            self._phone_server.shutdown()


def main() -> None:
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    daemon = OmaDaemon()
    try:
        asyncio.run(daemon.run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        daemon.stop()
    log.info("event loop finished; exiting")
