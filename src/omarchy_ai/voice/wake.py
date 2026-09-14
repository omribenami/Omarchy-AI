"""Wake-word detection: openWakeWord over a continuous pw-record stream.

Ported from jarvisd (~/Git/jarvisd/src/jarvisd/wake.py), which proved this
exact pattern out — no changes to the detection logic itself.
"""

from __future__ import annotations

import logging
import threading

import openwakeword
from openwakeword.model import Model

from ..config import Config
from . import audio

log = logging.getLogger("omarchy_ai.voice.wake")


def _resolve_model_path(config: Config) -> str:
    if config.custom_wake_model_path:
        return config.custom_wake_model_path
    for path in openwakeword.get_pretrained_model_paths():
        if config.wake_word in path:
            return path
    available = ", ".join(openwakeword.get_pretrained_model_paths())
    raise ValueError(
        f"wake word '{config.wake_word}' not found among bundled models: {available}"
    )


class WakeWordDetector:
    def __init__(self, config: Config):
        self.config = config
        model_path = _resolve_model_path(config)
        self._model = Model(wakeword_model_paths=[model_path])
        self.model_name = next(iter(self._model.models))
        log.info("loaded wake word model: %s", self.model_name)

    def listen(self, stop_event: threading.Event) -> bool:
        """Block until the wake word fires or stop_event is set.

        Returns True on a real detection, False if asked to stop.
        """
        proc = audio.open_stream(self.config.mic_device)
        self._model.reset()
        consecutive = 0
        try:
            while not stop_event.is_set():
                frame = audio.read_frame(proc)
                if frame is None:
                    log.warning("wake audio stream ended unexpectedly, restarting")
                    return False
                scores = self._model.predict(frame)
                score = scores.get(self.model_name, 0.0)
                if score >= self.config.wake_threshold:
                    consecutive += 1
                    if consecutive >= self.config.wake_trigger_frames:
                        log.info("wake word detected (score=%.2f)", score)
                        return True
                else:
                    consecutive = 0
            return False
        finally:
            audio.close_stream(proc)
