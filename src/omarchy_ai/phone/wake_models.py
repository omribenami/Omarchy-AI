"""The desktop's wake word, for a phone that listens for it on its own.

Flux's "Always listen for Omarchy" runs openWakeWord on the phone: the same
three ONNX models the desktop's voice/wake.py uses (openWakeWord's
melspectrogram and speech-embedding models, then the wake word model
itself, e.g. the user's trained omachy.onnx) and the same threshold. The
phone downloads them from here after it paired, so it always hears the
word the desktop hears, and no Omarchy-specific file ships inside Flux.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from ..config import Config


def files(config: Config) -> dict[str, Path]:
    """The three model files by the name the phone asks for. Only the first
    desktop wake model: a phone listens for one word."""
    import openwakeword
    from ..voice.wake import _resolve_model_paths
    resources = Path(openwakeword.__file__).parent / "resources" / "models"
    return {
        "melspectrogram": resources / "melspectrogram.onnx",
        "embedding": resources / "embedding_model.onnx",
        "wake": Path(_resolve_model_paths(config)[0]),
    }


def describe(config: Config) -> dict:
    """What the phone needs to know before it downloads: the word's name,
    the threshold, and each file's SHA-256 so it only fetches changes."""
    found = files(config)
    missing = [name for name, path in found.items() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"wake model files missing: {', '.join(missing)}")
    return {
        "name": os.path.splitext(found["wake"].name)[0],
        "threshold": float(config.wake_threshold),
        "files": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in found.items()},
    }
