"""Shared wlr-screencopy-unstable-v1 capture core, used by both
scripts/spike_cast_wlr_screencopy.py (standalone proof of concept) and
scripts/spike_cast_sender.py (the real appsrc-fed video branch).

See spike_cast_wlr_screencopy.py's module docstring for the full "why":
this bypasses xdg-desktop-portal's ScreenCast interface entirely (proven
broken -- portal's own internal screencopy->PipeWire producer wedges after
1-2 frames, STATUS.md/ADR-0001 D5) in favor of talking to Hyprland's
wlr-screencopy Wayland global directly, the same protocol `grim` uses.

Import path setup: `scripts/protocols/generated/wlr_screencopy_unstable_v1.py`
was compiled once via pywayland's scanner from the vendored
`scripts/protocols/wlr-screencopy-unstable-v1.xml` (see that file's header
comment / STATUS.md for why it's vendored rather than pacman-installed --
no passwordless sudo in this environment). Any module that imports this
file must first put `scripts/protocols` on `sys.path`.
"""

from __future__ import annotations

import logging
import mmap
import select
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent / "protocols"))

from pywayland.client import Display  # noqa: E402
from pywayland.protocol.wayland import WlOutput, WlRegistry, WlShm  # noqa: E402
from pywayland.utils import AnonymousFile  # noqa: E402

from generated.wlr_screencopy_unstable_v1 import ZwlrScreencopyManagerV1  # noqa: E402

log = logging.getLogger("wlr_screencopy_capture")

# wl_shm format codes we actually handle, and what they mean downstream.
# Both are 32-bit/4-bytes-per-pixel formats, memory order little-endian
# (per the wl_shm.format enum doc comments in the protocol XML: "[31:0]
# A:R:G:B 8:8:8:8 little endian" -> B,G,R,A in memory byte order).
WL_SHM_TO_FFMPEG_PIXFMT = {0: "bgra", 1: "bgr0"}  # argb8888, xrgb8888
WL_SHM_TO_GST_FORMAT = {0: "BGRA", 1: "BGRx"}  # argb8888, xrgb8888
BYTES_PER_PIXEL = {0: 4, 1: 4}

# Callback signature every consumer implements:
#   on_frame(ok: bool, data: bytes | None, width: int, height: int, format_: int)
# `data` is a *fresh copy* of the frame's tightly-packed pixel bytes (row
# padding stripped if the compositor's stride included any -- see
# _on_ready below), safe to hand to anything (including another thread or
# a deferred GLib callback) without racing the next capture, which reuses
# the same underlying wl_shm mmap region.
FrameCallback = Callable[[bool, bytes | None, int, int, int], None]


class ScreencopyGrabber:
    """Owns the Wayland connection and the wlr-screencopy capture loop.

    Reuses a single wl_shm buffer across frames as long as the compositor
    keeps reporting the same format/size (the normal case -- output
    resolution doesn't change mid-session), reallocating only if it does.
    """

    def __init__(self, output_index: int = 0):
        self.display = Display()
        self.shm: WlShm | None = None
        self.manager: ZwlrScreencopyManagerV1 | None = None
        self.outputs: list[WlOutput] = []
        self.output: WlOutput | None = None
        self.output_index = output_index

        self._pool_buffer = None
        self._mmap: mmap.mmap | None = None
        self._buf_key: tuple[int, int, int, int] | None = None  # (format, w, h, stride)
        self._pending: dict | None = None
        self._current_frame = None

        self.frame_count = 0
        self.fail_count = 0

        self.on_frame: FrameCallback = lambda ok, data, w, h, fmt: None  # set by driver

    # ---------- registry / globals ----------

    def connect(self) -> None:
        self.display.connect()
        registry = self.display.get_registry()
        registry.dispatcher["global"] = self._on_global
        self.display.roundtrip()
        if self.shm is None:
            raise RuntimeError("compositor has no wl_shm global")
        if self.manager is None:
            raise RuntimeError(
                "compositor has no zwlr_screencopy_manager_v1 global -- "
                "wlr-screencopy-unstable-v1 is not supported/advertised here"
            )
        if not self.outputs:
            raise RuntimeError("no wl_output globals found")
        if self.output_index >= len(self.outputs):
            raise RuntimeError(
                f"--output-index {self.output_index} out of range "
                f"({len(self.outputs)} output(s) found)"
            )
        self.output = self.outputs[self.output_index]
        log.info(
            "connected: wl_shm ok, zwlr_screencopy_manager_v1 v%d ok, using output #%d of %d",
            self.manager.interface.version,
            self.output_index,
            len(self.outputs),
        )

    def _on_global(self, registry: WlRegistry, id_: int, interface: str, version: int) -> None:
        if interface == "wl_shm":
            self.shm = registry.bind(id_, WlShm, version)
        elif interface == "wl_output":
            self.outputs.append(registry.bind(id_, WlOutput, min(version, 4)))
        elif interface == "zwlr_screencopy_manager_v1":
            self.manager = registry.bind(id_, ZwlrScreencopyManagerV1, version)

    # ---------- capture ----------

    def capture_next(self, overlay_cursor: int = 0) -> None:
        assert self.manager is not None and self.output is not None
        frame = self.manager.capture_output(overlay_cursor, self.output)
        self._pending = {}
        frame.dispatcher["buffer"] = self._on_buffer
        frame.dispatcher["buffer_done"] = self._on_buffer_done
        frame.dispatcher["ready"] = self._on_ready
        frame.dispatcher["failed"] = self._on_failed
        self._current_frame = frame

    def _on_buffer(self, _frame, format_: int, width: int, height: int, stride: int) -> None:
        self._pending = {"format": format_, "width": width, "height": height, "stride": stride}

    def _ensure_buffer(self, format_: int, width: int, height: int, stride: int):
        key = (format_, width, height, stride)
        if key == self._buf_key and self._pool_buffer is not None:
            return self._pool_buffer
        size = stride * height
        if self._pool_buffer is not None:
            self._pool_buffer.destroy()
        if self._mmap is not None:
            self._mmap.close()
        with AnonymousFile(size) as fd:
            self._mmap = mmap.mmap(fd, size, prot=mmap.PROT_READ | mmap.PROT_WRITE, flags=mmap.MAP_SHARED)
            pool = self.shm.create_pool(fd, size)
            self._pool_buffer = pool.create_buffer(0, width, height, stride, format_)
            pool.destroy()
        self._buf_key = key
        log.info(
            "allocated wl_shm buffer: %dx%d stride=%d format=%d (%d bytes)",
            width, height, stride, format_, size,
        )
        return self._pool_buffer

    def _on_buffer_done(self, frame) -> None:
        p = self._pending
        buf = self._ensure_buffer(p["format"], p["width"], p["height"], p["stride"])
        frame.copy(buf)

    def _on_ready(self, frame, _tv_sec_hi: int, _tv_sec_lo: int, _tv_nsec: int) -> None:
        self.frame_count += 1
        format_, width, height, stride = self._buf_key
        bpp = BYTES_PER_PIXEL.get(format_, 4)
        # Copy the frame's pixel bytes out NOW, synchronously, before this
        # method returns and before any consumer (possibly via a deferred
        # GLib timeout) gets a chance to call capture_next() again, which
        # reuses this same mmap region -- the compositor can start
        # overwriting it as soon as the next copy request is serviced.
        # Also strip row padding if stride != width*bpp so every consumer
        # can assume tightly-packed rows matching the declared width (real
        # correctness fix: on this machine stride happened to equal
        # width*4 exactly, 1366*4=5464, so this path wasn't exercised by
        # the local proof run, but it is not safe to assume that always
        # holds -- a stride mismatch left unstripped would show up
        # downstream as skewed/torn-looking video, one row at a time).
        tight_row = width * bpp
        if self._mmap is not None:
            self._mmap.seek(0)
            raw = self._mmap.read(stride * height)
            if stride == tight_row:
                data = bytes(raw)
            else:
                data = b"".join(raw[r * stride : r * stride + tight_row] for r in range(height))
        else:
            data = None
        frame.destroy()
        self._current_frame = None
        self.on_frame(True, data, width, height, format_)

    def _on_failed(self, frame) -> None:
        self.fail_count += 1
        log.warning("frame copy FAILED (failure #%d)", self.fail_count)
        frame.destroy()
        self._current_frame = None
        self.on_frame(False, None, 0, 0, 0)

    def disconnect(self) -> None:
        # See spike_cast_wlr_screencopy.py's run()/finally comment: this
        # MUST be called explicitly before process exit. Leaving cleanup to
        # GC at interpreter shutdown segfaults (confirmed live via
        # coredumpctl: wl_proxy_destroy -> wl_map_insert_at on a proxy
        # whose Display had already been freed by an unordered GC pass).
        # Display.disconnect() destroys all child proxies while the
        # display is still alive, then releases the display itself.
        self.display.disconnect()


def dispatch_with_timeout(display: Display, timeout: float) -> bool:
    """Drain pending events, then wait up to `timeout` seconds for new ones
    on the display fd. Returns True if any event was dispatched. For
    callers driving their own select()-based loop (the standalone spike);
    GLib-integrated callers (the sender) instead wire the display fd into
    the GLib main loop directly -- see spike_cast_sender.py."""
    dispatched = False
    while display.dispatch(block=False) > 0:
        dispatched = True
    display.flush()
    if dispatched:
        return True
    r, _, _ = select.select([display.get_fd()], [], [], timeout)
    if not r:
        return False
    display.read()
    while display.dispatch(block=False) > 0:
        dispatched = True
    return dispatched
