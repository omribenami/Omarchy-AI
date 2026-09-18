"""pw-record plumbing for the wake-word loop: a continuous raw 16kHz mono
PCM stream, the format openWakeWord expects. Ported from jarvisd
(~/Git/jarvisd/src/jarvisd/audio.py), which proved this pattern out.
"""

from __future__ import annotations

import subprocess

import numpy as np
from .tv_mic import Receiver

RATE = 16000
FRAME_SAMPLES = 1280  # 80ms, the chunk size openWakeWord expects
FRAME_BYTES = FRAME_SAMPLES * 2  # s16 = 2 bytes/sample


def clean_frame(frame: np.ndarray) -> np.ndarray:
    """Suppress DC/low-frequency rumble and idle mic hiss before wake VAD."""
    samples = frame.astype(np.float32, copy=True)
    if samples.size < 2:
        return frame
    # Lightweight high-pass; wake words live well above this range.
    previous_x = previous_y = 0.0
    alpha = 0.969
    for i, value in enumerate(samples):
        y = alpha * (previous_y + value - previous_x)
        samples[i] = y
        previous_x, previous_y = value, y
    rms = float(np.sqrt(np.mean(samples * samples)))
    if rms < 260:
        samples *= 0.08
    return np.clip(samples, -32768, 32767).astype(np.int16)


def _argv(device: str | None) -> list[str]:
    argv = [
        "pw-record",
        "--rate", str(RATE),
        "--channels", "1",
        "--format", "s16",
        "--latency", "20ms",
        "-a",
    ]
    if device:
        argv += ["--target", device]
    argv.append("-")
    return argv


def open_stream(device: str | None = None) -> subprocess.Popen:
    proc = subprocess.Popen(
        _argv(device),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=FRAME_BYTES * 4,
    )
    proc.tv_mic = Receiver(RATE)
    return proc


def close_stream(proc: subprocess.Popen) -> None:
    proc.tv_mic.close()
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1)


def read_frame(proc: subprocess.Popen) -> np.ndarray | None:
    """Block for exactly one 80ms frame, or None if the stream ended."""
    buf = b""
    while len(buf) < FRAME_BYTES:
        chunk = proc.stdout.read(FRAME_BYTES - len(buf))
        if not chunk:
            return None
        buf += chunk
    return clean_frame(np.frombuffer(buf, dtype=np.int16))
