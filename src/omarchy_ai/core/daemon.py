"""Listen for a wake word or panel activation, hold one conversation,
then return to listening. There is no always-on remote voice connection.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from ..config import Config, ensure_dirs, load_config
from ..execution import tile_logs
from ..phone import server as phone_server
from ..voice import feedback, control
from ..voice.live import LiveSession
from ..voice.gemini_live import GeminiLiveSession
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
        return {'state': self._state}

    async def run(self) -> None:
        server = await control.serve(self.panel_command)
        try:
            async with server:
                await self._run_sessions()
        finally:
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

            self._state = 'active'
            feedback.play(feedback.WAKE())
            log.info("wake word detected, starting live session")
            if manual:
                log.info('conversation activated from assistant panel')
            if self.config.provider == "gemini":
                session = GeminiLiveSession(self.config)
            else:
                session = LiveSession(self.config, mic_source='desktop' if manual else self.wake_detector.last_source)
            try:
                await session.run()
            except Exception:  # noqa: BLE001
                log.exception("live session crashed")
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
    except KeyboardInterrupt:
        daemon.stop()
