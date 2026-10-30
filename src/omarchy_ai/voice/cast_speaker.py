"""A Google Cast speaker that says a voice satellite's replies (satellite_cast).

The satellite keeps its microphone and wake word; her speech, the same MP3
stream the satellite would have played (voice/satellite.py ReplyStream), is
loaded on the Cast device instead. Every call blocks (pychromecast), so all
of them run on one thread, in order: a stop never overtakes the play it
stops.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("omarchy_ai.voice.cast_speaker")

CAST_PORT = 8009
CONNECT_SECONDS = 8.0


class CastSpeaker:
    def __init__(self, host: str):
        self.host = host
        self._cast = None
        self._loaded_at = 0.0            # last play_media (monotonic), for the start time logged
        self._thread = ThreadPoolExecutor(1, thread_name_prefix="cast-speaker")

    def play(self, url: str) -> None:
        self._thread.submit(self._play, url)

    def stop(self) -> None:
        self._thread.submit(self._stop)

    def close(self) -> None:
        self._thread.submit(self._close)
        self._thread.shutdown(wait=False)

    def _device(self):
        cast = self._cast
        if cast is not None and cast.socket_client.is_connected:
            return cast
        import pychromecast
        cast = pychromecast.get_chromecast_from_host((self.host, CAST_PORT, None, None, None),
                                                     timeout=CONNECT_SECONDS)
        cast.wait(timeout=CONNECT_SECONDS)
        cast.media_controller.register_status_listener(_Started(self))
        self._cast = cast
        return cast

    def _play(self, url: str) -> None:
        try:
            controller = self._device().media_controller
            self._loaded_at = time.monotonic()
            controller.play_media(url, "audio/mpeg", stream_type="LIVE", title="Omarchy")
        except Exception as error:  # noqa: BLE001 -- a lost reply must not stop the satellite
            log.warning("cast speaker %s did not take the reply: %s", self.host, error)
            self._cast = None

    def _stop(self) -> None:
        cast = self._cast
        if cast is None:
            return
        try:
            cast.media_controller.stop()
        except Exception as error:  # noqa: BLE001
            log.debug("cast speaker %s stop not sent: %s", self.host, error)

    def _close(self) -> None:
        cast, self._cast = self._cast, None
        if cast is not None:
            try:
                cast.disconnect(timeout=2)
            except Exception:  # noqa: BLE001
                pass


class _Started:
    """Logs how long after a load the device really started playing: the
    figure satellite.CAST_START_SECONDS is set from."""

    def __init__(self, speaker: CastSpeaker):
        self.speaker = speaker

    def new_media_status(self, status) -> None:
        loaded = self.speaker._loaded_at
        if loaded and getattr(status, "player_state", "") == "PLAYING":
            self.speaker._loaded_at = 0.0
            log.info("cast speaker %s started playing %.1fs after the load", self.speaker.host,
                     time.monotonic() - loaded)

    def load_media_failed(self, queue_item_id, error_code) -> None:
        log.warning("cast speaker %s could not play the reply (error %s)", self.speaker.host, error_code)
