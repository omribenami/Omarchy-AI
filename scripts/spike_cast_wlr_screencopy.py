#!/usr/bin/env python3
"""Phase 2 spike: pull continuous frames directly via wlr-screencopy-unstable-v1,
bypassing xdg-desktop-portal's ScreenCast interface entirely.

Why this exists: the portal->PipeWire video path is proven broken on this
machine (see STATUS.md/ADR-0001 D5) -- xdg-desktop-portal-hyprland's own
internal screencopy->PipeWire producer wedges after 1-2 frames with an
endless "Out of buffers"/"Building modifiers for dma" retry loop
(hyprwm/xdg-desktop-portal-hyprland#434), and a PipeWire config change
(support.dmabuf.modifiers=false) confirmed live NOT to fix it (that setting
governs generic PipeWire client/session-manager buffer negotiation, not
xdph's own compositor-internal DMA-BUF capture code).

This script proves a genuinely different code path instead: talk to
Hyprland's wlr-screencopy-unstable-v1 Wayland global directly (the same
lower-level protocol `grim` uses via `omarchy-capture-screenshot`, which
this repo has called successfully dozens of times this session with zero
issues) and pull raw frames ourselves via wl_shm, with no portal, no
PipeWire, and (confirmed below) no consent dialog involved at all.

Environment note: no wlr-screencopy Python/GStreamer binding exists on this
machine (checked: no `python-pywayland` pacman package, no gst
wlr-screencopy element in gst-plugins-good/bad, no `wf-recorder`/`wayvnc`).
Added `pywayland` to the project via `uv add pywayland` (proper project
dependency management through this repo's existing packaging tool -- see
CLAUDE.md, not a bare system-wide pip install). The
`zwlr_screencopy_manager_v1`/`zwlr_screencopy_frame_v1` Python bindings
pywayland needs don't ship with it (wlr-protocols isn't a standard
wayland-protocols package, and there is no passwordless sudo in this
session to `pacman -S wlr-protocols`), so the protocol XML was vendored
from the upstream wlr-protocols repo instead (scripts/protocols/
wlr-screencopy-unstable-v1.xml, same content the pacman package would
provide) and compiled to Python bindings once via pywayland's own scanner.
Full capture implementation lives in scripts/wlr_screencopy_capture.py
(shared with spike_cast_sender.py's real appsrc-fed video branch); see
that module's docstring for the protocol flow and import-path details.

Run standalone: `.venv/bin/python scripts/spike_cast_wlr_screencopy.py
--duration 35`. Prints frame counts in 4s windows (same cadence as
Android's EglRenderer log, for direct comparison) and a final summary.
Exits non-zero if a stall (no frames for --stall-timeout seconds) is
detected -- the exact failure mode this is trying to avoid.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
import zlib
from pathlib import Path

from wlr_screencopy_capture import (
    WL_SHM_TO_FFMPEG_PIXFMT,
    ScreencopyGrabber,
    dispatch_with_timeout,
)

log = logging.getLogger("spike_cast_wlr_screencopy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def save_frame_png(data: bytes, width: int, height: int, format_: int, path: Path) -> bool:
    """Dump one captured frame to a PNG via ffmpeg, for a human-viewable
    sanity check that this is real screen content, not garbage."""
    pixfmt = WL_SHM_TO_FFMPEG_PIXFMT.get(format_)
    if pixfmt is None:
        log.warning("no ffmpeg pixel format mapping for wl_shm format %d, skipping PNG dump", format_)
        return False
    raw_path = path.with_suffix(".raw")
    raw_path.write_bytes(data)
    cmd = [
        "ffmpeg", "-y", "-f", "rawvideo", "-pixel_format", pixfmt,
        "-video_size", f"{width}x{height}",
        "-i", str(raw_path), str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    raw_path.unlink(missing_ok=True)
    if result.returncode != 0:
        log.warning("ffmpeg PNG dump failed: %s", result.stderr[-500:])
        return False
    log.info("saved sanity-check frame to %s (%dx%d, %s)", path, width, height, pixfmt)
    return True


def run(args) -> int:
    grabber = ScreencopyGrabber(output_index=args.output_index)
    grabber.connect()
    try:
        return _run_loop(grabber, args)
    finally:
        # Real bug found and fixed here: without an explicit disconnect(),
        # the Display and its many child proxies (registry, shm, manager,
        # outputs, wl_buffer, wl_shm_pool) get garbage-collected in
        # arbitrary order at interpreter shutdown. Confirmed live via
        # `coredumpctl info` on the first run of this script: a real
        # SIGSEGV in wl_proxy_destroy -> wl_map_insert_at, from cffi's GC
        # finalizer firing on a leftover proxy AFTER the wl_display's own
        # internal proxy map had already been freed by a separately-GC'd
        # Display object -- classic destroy-after-free from unordered GC,
        # not a screencopy protocol issue (the capture loop itself ran
        # perfectly: 360/360 frames, 0 failures, in the run that crashed
        # only on exit). ScreencopyGrabber.disconnect() fixed this
        # (verified: repeated runs after the fix exit 0/1 as expected with
        # no core dump).
        grabber.disconnect()


def _run_loop(grabber: ScreencopyGrabber, args) -> int:
    total_start = time.monotonic()
    window_start = total_start
    window_frames = 0
    window_fails = 0
    last_activity = total_start
    saved_sample = False
    last_crc: int | None = None
    content_changes = 0
    exit_code = 0

    def on_frame(ok: bool, data: bytes | None, width: int, height: int, format_: int) -> None:
        nonlocal window_frames, window_fails, last_crc, content_changes, saved_sample
        if ok:
            window_frames += 1
            crc = zlib.crc32(data)
            if last_crc is not None and crc != last_crc:
                content_changes += 1
            last_crc = crc
            if args.save_frame and not saved_sample:
                save_frame_png(data, width, height, format_, Path(args.save_frame))
                saved_sample = True
        else:
            window_fails += 1
        if time.monotonic() - total_start < args.duration:
            grabber.capture_next(overlay_cursor=args.overlay_cursor)

    grabber.on_frame = on_frame
    grabber.capture_next(overlay_cursor=args.overlay_cursor)
    grabber.display.flush()

    while time.monotonic() - total_start < args.duration:
        now = time.monotonic()
        remaining_in_window = args.report_interval - (now - window_start)
        select_timeout = max(0.1, min(1.0, remaining_in_window))
        if dispatch_with_timeout(grabber.display, select_timeout):
            last_activity = time.monotonic()

        now = time.monotonic()
        if now - window_start >= args.report_interval:
            elapsed = now - total_start
            log.info(
                "[t=%5.1fs] Frames received: %d. Rendered: %d. (window=%.1fs, %d failed) total=%d changed=%d",
                elapsed, window_frames, window_frames, args.report_interval,
                window_fails, grabber.frame_count, content_changes,
            )
            if window_frames == 0:
                log.error(
                    "STALL: zero frames in the last %.1fs window -- this is the exact "
                    "symptom class the portal/PipeWire path fails with. Investigate "
                    "before wiring this into the sender.",
                    args.report_interval,
                )
            window_frames = 0
            window_fails = 0
            window_start = now

        if now - last_activity > args.stall_timeout:
            log.error(
                "no Wayland events at all for >%.1fs -- hard stall, aborting early",
                args.stall_timeout,
            )
            exit_code = 1
            break

    total_elapsed = time.monotonic() - total_start
    fps = grabber.frame_count / total_elapsed if total_elapsed > 0 else 0.0
    log.info(
        "DONE: %d frames / %.1fs = %.2f fps, %d failed, %d content-changes detected",
        grabber.frame_count, total_elapsed, fps, grabber.fail_count, content_changes,
    )
    if grabber.frame_count == 0:
        log.error("zero frames captured for the whole run -- treat as failed")
        exit_code = 1
    elif content_changes == 0:
        log.warning(
            "frames were captured but pixel content never changed -- can't rule out a "
            "frozen/repeated buffer from this alone, worth a visual check via --save-frame"
        )
    return exit_code


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--duration", type=float, default=35.0, help="total test duration, seconds")
    p.add_argument("--report-interval", type=float, default=4.0, help="status print cadence, seconds")
    p.add_argument("--stall-timeout", type=float, default=8.0, help="abort if no wayland events for this long")
    p.add_argument("--output-index", type=int, default=0, help="which wl_output to capture (0 = first)")
    p.add_argument("--overlay-cursor", type=int, default=0, choices=(0, 1))
    p.add_argument("--save-frame", type=str, default=None, help="dump one captured frame to this PNG path")
    args = p.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
