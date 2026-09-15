"""Tone cues for state changes — same synthesize-on-the-fly approach
jarvisd used (no bundled audio assets, no TTS dependency).
"""

from __future__ import annotations

import logging
import subprocess
import wave
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np

log = logging.getLogger("omarchy_ai.voice.feedback")

_SAMPLE_RATE = 16000


def _tone(freq: float, ms: int, volume: float = 0.25) -> np.ndarray:
    n = int(_SAMPLE_RATE * ms / 1000)
    t = np.arange(n) / _SAMPLE_RATE
    wave_form = np.sin(2 * np.pi * freq * t)
    fade = min(200, n // 4)
    if fade > 0:
        env = np.ones(n)
        env[:fade] = np.linspace(0, 1, fade)
        env[-fade:] = np.linspace(1, 0, fade)
        wave_form *= env
    return (wave_form * volume * 32767).astype(np.int16)


def _sequence(*parts: np.ndarray, gap_ms: int = 30) -> np.ndarray:
    gap = np.zeros(int(_SAMPLE_RATE * gap_ms / 1000), dtype=np.int16)
    out = []
    for i, p in enumerate(parts):
        out.append(p)
        if i != len(parts) - 1:
            out.append(gap)
    return np.concatenate(out) if out else np.array([], dtype=np.int16)


WAKE = lambda: _sequence(_tone(880, 70), _tone(1320, 90))
HANGUP = lambda: _sequence(_tone(600, 100), _tone(400, 140))


def play(samples: np.ndarray) -> None:
    try:
        with NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = Path(f.name)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(_SAMPLE_RATE)
            w.writeframes(samples.tobytes())
        subprocess.run(["pw-play", str(path)], timeout=5, check=False)
    except Exception:  # noqa: BLE001
        log.debug("failed to play cue", exc_info=True)
    finally:
        try:
            path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
