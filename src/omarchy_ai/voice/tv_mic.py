"""Bounded local return-audio handoff; the desktop mic remains the fallback."""
from __future__ import annotations

import os
import logging
from pathlib import Path
import socket
import threading
import time

import av

log = logging.getLogger("omarchy_ai.voice.tv_mic")


def path(rate: int) -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / f"omarchy-tv-mic-{rate}.sock"


class Receiver:
    def __init__(self, rate: int):
        self.rate = rate
        self.buffer = bytearray()
        self.last_received = 0.0
        self.using_tv = False
        self.stats = dict(received_samples=0, dropped_samples=0, padded_samples=0, reads=0)
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.path = path(rate)
        self.path.unlink(missing_ok=True)
        try:
            self.socket.bind(str(self.path))
        except OSError:
            self.socket.close()
            raise
        self.path.chmod(0o600)
        self.socket.settimeout(.2)
        self.thread = threading.Thread(target=self._receive, daemon=True, name="tv-microphone")
        self.thread.start()

    def _receive(self):
        resampler = av.AudioResampler(format="s16", layout="mono", rate=self.rate)
        while not self.closed.is_set():
            try:
                data = self.socket.recv(8193)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                with self.lock:
                    self.buffer.clear()
                    self.last_received = 0
                continue
            if len(data) > 8192 or len(data) % 2:
                continue
            frame = av.AudioFrame(format="s16", layout="mono", samples=len(data) // 2)
            frame.sample_rate = 48000
            frame.planes[0].update(data)
            pcm = b"".join(bytes(f.planes[0])[:f.samples * 2] for f in resampler.resample(frame))
            with self.lock:
                now = time.monotonic()
                if now - self.last_received > .35:
                    self.buffer.clear()
                self.buffer.extend(pcm)
                self.stats['received_samples'] += len(pcm) // 2
                self.stats['dropped_samples'] += max(0, len(self.buffer) - self.rate // 2) // 2
                # At most 250ms, so a delayed consumer cannot replay old speech.
                self.buffer = self.buffer[-self.rate // 2:]
                self.last_received = now

    def read(self, size: int) -> bytes | None:
        with self.lock:
            if time.monotonic() - self.last_received > .35:
                self.buffer.clear()
                if self.using_tv:
                    log.info("TV microphone unavailable; using desktop microphone (%d Hz)", self.rate)
                    self.using_tv = False
                return None
            if not self.using_tv:
                log.info("using TV microphone (%d Hz)", self.rate)
                self.using_tv = True
            data = bytes(self.buffer[:size])
            del self.buffer[:size]
            self.stats['reads'] += 1
            self.stats['padded_samples'] += (size - len(data)) // 2
            return data.ljust(size, b"\0")

    def diagnostics(self) -> dict:
        with self.lock:
            result = dict(self.stats, buffered_samples=len(self.buffer) // 2)
            self.stats = dict.fromkeys(self.stats, 0)
            return result

    def close(self):
        self.closed.set()
        self.socket.close()
        self.thread.join(timeout=.5)
        self.path.unlink(missing_ok=True)


def forward(sock: socket.socket, data: bytes) -> None:
    for rate in (16000, 48000):
        try:
            sock.sendto(data, str(path(rate)))
        except OSError:
            pass  # The wake/live consumer may be transitioning or stopped.
