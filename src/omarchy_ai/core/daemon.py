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
            faulthandler.dump_traceback_later(20, exit=False)
            self.stop()
            task.cancel()
        # Let active audio sessions unload their private PipeWire modules
        # when systemd restarts us, instead of exiting before their finally.
        loop.add_signal_handler(signal.SIGTERM, terminate)
        server = await control.serve(self.panel_command)
        async def refresh_updates():
            while True:
                try:
                    await asyncio.to_thread(updates.check_updates)
                except Exception:
                    log.warning("Update check unavailable", exc_info=True)
                await asyncio.sleep(updates.CACHE_SECONDS)
        update_checker = asyncio.create_task(refresh_updates())
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
        try:
            async with server:
                await self._run_sessions()
        finally:
            update_checker.cancel()
            if heart is not None:
                heart.cancel()
            await asyncio.gather(update_checker, *([heart] if heart else []), return_exceptions=True)
            loop.remove_signal_handler(signal.SIGTERM)
            control.socket_path().unlink(missing_ok=True)

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

            # Cached checks normally return immediately. An offline GitHub must
            # never hold up wake activation; a timed-out refresh can finish later.
            try:
                await asyncio.wait_for(asyncio.to_thread(updates.check_updates), timeout=2)
            except Exception:
                log.debug("Wake update refresh deferred", exc_info=True)
            self._state = 'active'
            feedback.play(feedback.WAKE())
            log.info("wake word detected, starting live session")
            if manual:
                log.info('conversation activated from assistant panel')
            if self.config.provider == "gemini":
                self._state = 'starting'
                session = GeminiLiveSession(self.config)
                session.on_connected = lambda: setattr(self, '_state', 'active')
            elif self.config.provider == "omarchy":
                self._state = 'starting'
                session = OmarchySession(self.config)
                session.on_connected = lambda: setattr(self, '_state', 'active')
            else:
                session = LiveSession(self.config, mic_source='desktop' if manual else self.wake_detector.last_source)
            try:
                self._last_error = None
                await session.run()
                # The session prompt carried the heartbeat's pending results.
                from . import agenda
                agenda.mark_briefed()
            except Exception:  # noqa: BLE001
                log.exception("live session crashed")
                self._last_error = "Connection ended unexpectedly. Check the service log, then try again."
                # Keep the selected provider; never silently switch credentials.
            finally:
                from ..execution.browser_jev import cancel_browser_tasks
                cancel_browser_tasks()
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
