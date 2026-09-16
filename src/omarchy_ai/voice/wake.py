"""Wake-word detection: openWakeWord over a continuous pw-record stream.

Ported from jarvisd (~/Git/jarvisd/src/jarvisd/wake.py), which proved this
exact pattern out — no changes to the detection logic itself.
"""

from __future__ import annotations

import logging
import threading
import os
import time
import numpy as np

import openwakeword
from openwakeword.model import Model

from ..config import Config
from . import audio

log = logging.getLogger("omarchy_ai.voice.wake")


def _resolve_model_paths(config: Config) -> list[str]:
    if config.custom_wake_model_paths:
        return list(config.custom_wake_model_paths)
    if config.custom_wake_model_path:
        return [config.custom_wake_model_path]
    for path in openwakeword.get_pretrained_model_paths():
        if config.wake_word in path:
            return [path]
    available = ", ".join(openwakeword.get_pretrained_model_paths())
    raise ValueError(
        f"wake word '{config.wake_word}' not found among bundled models: {available}"
    )


class WakeWordDetector:
    def __init__(self, config: Config):
        self.config = config
        model_paths = _resolve_model_paths(config)
        self._model = Model(wakeword_model_paths=model_paths)
        # Each microphone needs its own feature history. Interleaving frames
        # or replacing desktop audio with a silent TV stream breaks detection.
        self._tv_model = Model(wakeword_model_paths=model_paths)
        self._diagnostics = os.environ.get("OMARCHY_AI_WAKE_DIAGNOSTICS") == "1"
        self._tv_probe = Model(wakeword_model_paths=model_paths) if self._diagnostics else None
        self.last_source = "desktop"
        # Multiple models score every frame in parallel — anyone whose
        # trained wake word is loaded can wake it, not just a single fixed
        # phrase. Each needs its own consecutive-frame counter since they
        # cross threshold independently.
        self.model_names = list(self._model.models)
        log.info("loaded wake word model(s): %s", ", ".join(self.model_names))

    def listen(self, stop_event: threading.Event) -> bool:
        """Block until any loaded wake word fires or stop_event is set.

        Returns True on a real detection, False if asked to stop.
        """
        proc = audio.open_stream(self.config.mic_device)
        self._model.reset()
        self._tv_model.reset()
        if self._tv_probe is not None:
            self._tv_probe.reset()
        metrics = {"desktop_score": 0.0, "tv_score": 0.0, "tv_gain4_score": 0.0, "desktop_rms": 0.0, "tv_rms": 0.0}
        next_report = time.monotonic() + 5
        consecutive = {source: dict.fromkeys(self.model_names, 0) for source in ("desktop", "tv")}
        tv_present = False
        self.last_source = "desktop"
        try:
            while not stop_event.is_set():
                frame = audio.read_frame(proc)
                if frame is None:
                    log.warning("wake audio stream ended unexpectedly, restarting")
                    return False
                sources = [("desktop", self._model, frame)]
                remote = proc.tv_mic.read(audio.FRAME_BYTES)
                if remote is not None:
                    sources.append(("tv", self._tv_model, np.frombuffer(remote, dtype=np.int16)))
                    tv_present = True
                elif tv_present:
                    self._tv_model.reset()
                    consecutive["tv"] = dict.fromkeys(self.model_names, 0)
                    tv_present = False
                for source, model, samples in sources:
                    scores = model.predict(samples)
                    if self._diagnostics:
                        metrics[source + "_score"] = max(metrics[source + "_score"], max(scores.values(), default=0.0))
                        metrics[source + "_rms"] = max(metrics[source + "_rms"], float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))))
                        if source == "tv":
                            boosted = np.clip(samples.astype(np.float32) * 4, -32768, 32767).astype(np.int16)
                            probe = self._tv_probe.predict(boosted)
                            metrics["tv_gain4_score"] = max(metrics["tv_gain4_score"], max(probe.values(), default=0.0))
                    for name in self.model_names:
                        score = scores.get(name, 0.0)
                        if score >= self.config.wake_threshold:
                            consecutive[source][name] += 1
                            if consecutive[source][name] >= self.config.wake_trigger_frames:
                                self.last_source = source
                                log.info("wake word detected: %s (score=%.2f, microphone=%s)", name, score, source)
                                return True
                        else:
                            consecutive[source][name] = 0
                if self._diagnostics and time.monotonic() >= next_report:
                    log.info("wake diagnostics: %s", {k: round(float(v), 4) for k, v in metrics.items()})
                    log.info("TV PCM diagnostics: %s", proc.tv_mic.diagnostics())
                    metrics = dict.fromkeys(metrics, 0.0)
                    next_report = time.monotonic() + 5
            return False
        finally:
            audio.close_stream(proc)
