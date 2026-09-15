"""The daemon loop: listen for the wake word, hold one conversation,
go back to listening. Nothing here opens a gpt-live-1 connection except in
direct response to the wake word — a live session is billed per second, so
there is no always-on connection.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from ..config import Config, ensure_dirs, load_config
from ..phone import server as phone_server
from ..voice import feedback
from ..voice.live import LiveSession
from ..voice.wake import WakeWordDetector

log = logging.getLogger("omarchy_ai.core.daemon")


class OmaDaemon:
    def __init__(self) -> None:
        ensure_dirs()
        self.config: Config = load_config()
        self.wake_detector = WakeWordDetector(self.config)
        self._stop = threading.Event()
        # Runs the whole time the daemon is up (not per-conversation like
        # LiveSession) — a phone tap should work any time, no wake word
        # needed. start() itself is a no-op returning None when
        # config.phone_bridge_enabled is off.
        self._phone_server = phone_server.start(self.config)

    async def run(self) -> None:
        log.info(
            "omarchy-ai ready — say any of: %s",
            ", ".join(self.wake_detector.model_names),
        )
        loop = asyncio.get_event_loop()
        while not self._stop.is_set():
            woke = await loop.run_in_executor(
                None, self.wake_detector.listen, self._stop
            )
            if self._stop.is_set():
                break
            if not woke:
                continue

            feedback.play(feedback.WAKE())
            log.info("wake word detected, starting live session")
            session = LiveSession(self.config)
            try:
                await session.run()
            except Exception:  # noqa: BLE001
                log.exception("live session crashed")
            feedback.play(feedback.HANGUP())
            log.info("session ended, back to listening")

    def stop(self) -> None:
        self._stop.set()
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
