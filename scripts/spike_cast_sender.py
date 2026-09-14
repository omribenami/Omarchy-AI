#!/usr/bin/env python3
"""Phase 2: Linux-side webrtcbin sender for Android TV casting.

Proves the whole capture -> encode -> WebRTC chain end to end on real
hardware before any of this touches the Android app (same "GStreamer-only
proof of concept first" convention as scripts/spike_live_webrtc.py in
Phase 1). Combines:

- Video: `wlr-screencopy-unstable-v1` (via scripts/wlr_screencopy_capture.py,
  proven in isolation by scripts/spike_cast_wlr_screencopy.py -- 60fps
  sustained, 0 failures, over 40+ real seconds) feeds a GStreamer `appsrc`
  directly with raw wl_shm frames -> `openh264enc` (software H.264; no
  GStreamer VAAPI plugin is installed on this machine -- see
  docs/DEPENDENCIES.md/ADR-0001 D6 revision).
- Audio: unchanged, `pipewiresrc target-object=<monitor-source>` capturing
  the default sink's monitor (system audio) -> `opusenc`. Never touched by
  this change -- a different PipeWire producer than the video path that
  was never implicated in the bug below.
- Signaling: connects to `omarchy_ai.display.signaling` as role=sender over
  a plain `websockets` connection running in its own thread (GStreamer's
  main loop is GLib-based, not asyncio -- bridged via
  `GLib.idle_add`/`asyncio.run_coroutine_threadsafe`, same split this repo
  already uses between the GLib (webrtcbin) and asyncio (aiortc) worlds).

### Why video no longer goes through the portal/PipeWire (real history)

The original video path was `org.freedesktop.portal.ScreenCast` (libportal's
`Xdp` GI bindings) -> a PipeWire node -> `pipewiresrc`. Confirmed live, real
hardware, repeatedly: `xdg-desktop-portal-hyprland`'s own internal
screencopy->PipeWire producer runs itself out of buffers almost immediately
after the first frame or two and never recovers --
`journalctl --user -u xdg-desktop-portal-hyprland` showed an endless
`[screencopy/pipewire] Out of buffers` / `Building modifiers for dma` loop,
11,000+ repeats across 2 days of testing, matching
`hyprwm/xdg-desktop-portal-hyprland#434` exactly. Two consumer-side
mitigations (`always-copy`/bounded buffer pool, then a PipeWire
`support.dmabuf.modifiers=false` config drop-in matching a documented
community fix) were each tried and each confirmed live NOT to fix it -- the
PipeWire config setting in particular governs generic PipeWire
client/session-manager buffer negotiation, not xdph's own
compositor-internal DMA-BUF screencopy code, which is a separate path that
setting doesn't touch. Full trail in STATUS.md/ADR-0001 D5.

Rather than continue tuning the same broken portal->PipeWire bridge, the
video path was replaced with a fundamentally different one: talk to
Hyprland's wlr-screencopy-unstable-v1 Wayland global directly (the same
protocol `grim` uses, proven reliable dozens of times this session) and
push frames into the pipeline ourselves via `appsrc`, bypassing the portal
(and its buffer-pool bug) entirely. Side benefit, confirmed live: this also
needs no screen-share consent dialog at all -- wlr-screencopy is
compositor-policy-gated, not portal-consent-gated, on this Hyprland build.
See scripts/spike_cast_wlr_screencopy.py and
scripts/wlr_screencopy_capture.py for the isolated proof and the shared
capture implementation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import threading
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
from gi.repository import GLib, Gst, GstSdp, GstWebRTC  # noqa: E402

import websockets  # noqa: E402

from wlr_screencopy_capture import WL_SHM_TO_GST_FORMAT, ScreencopyGrabber  # noqa: E402

log = logging.getLogger("spike_cast_sender")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

Gst.init(None)

DEFAULT_AUDIO_MONITOR = "alsa_output.pci-0000_00_1b.0.analog-stereo.monitor"


class Sender:
    def __init__(self, args):
        self.args = args
        self.loop = GLib.MainLoop()
        self.pipeline: Gst.Pipeline | None = None
        self.webrtc: Gst.Element | None = None
        self.appsrc: Gst.Element | None = None
        self.ws = None
        self.ws_loop: asyncio.AbstractEventLoop | None = None
        self.ws_ready = threading.Event()

        self.grabber = ScreencopyGrabber(output_index=args.output_index)
        self._capture_t0: float | None = None
        self._wayland_fd_watch_id: int | None = None
        self._next_capture_timeout_id: int | None = None
        self._interval_ms = max(1, round(1000 / args.fps))

        # Periodic 4s-window frame stats, same cadence/format as Android's
        # EglRenderer log on the receiver side, so the two can be compared
        # directly during a live test.
        self._window_start = time.monotonic()
        self._window_frames = 0
        self._window_fails = 0

    # ---------- signaling (asyncio thread) ----------

    def start_signaling_thread(self):
        t = threading.Thread(target=self._signaling_thread_main, daemon=True)
        t.start()
        if not self.ws_ready.wait(timeout=10):
            log.error("signaling connection did not come up in time")
            sys.exit(1)

    def _signaling_thread_main(self):
        asyncio.run(self._signaling_main())

    async def _signaling_main(self):
        self.ws_loop = asyncio.get_running_loop()
        url = self.args.signaling_url
        log.info("connecting to signaling server at %s", url)
        async with websockets.connect(url) as ws:
            self.ws = ws
            await ws.send(json.dumps({"role": "sender"}))
            self.ws_ready.set()
            log.info("registered as sender")
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("bad signaling message: %r", raw)
                    continue
                GLib.idle_add(self._handle_signaling_message, msg)

    def send_signaling(self, msg: dict):
        if self.ws is None or self.ws_loop is None:
            log.warning("signaling not connected yet, dropping %s", msg.get("type"))
            return
        asyncio.run_coroutine_threadsafe(self.ws.send(json.dumps(msg)), self.ws_loop)

    # ---------- signaling message handling (GLib thread) ----------

    def _handle_signaling_message(self, msg: dict):
        kind = msg.get("type")
        if kind == "answer":
            log.info("got remote answer (%d bytes)", len(msg.get("sdp", "")))
            _, sdpmsg = GstSdp.SDPMessage.new()
            GstSdp.sdp_message_parse_buffer(msg["sdp"].encode(), sdpmsg)
            answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg)
            promise = Gst.Promise.new()
            self.webrtc.emit("set-remote-description", answer, promise)
            promise.interrupt()
        elif kind == "ice":
            self.webrtc.emit(
                "add-ice-candidate", msg.get("sdpMLineIndex", 0), msg.get("candidate", "")
            )
        elif kind == "bye":
            log.info("viewer said bye")
        else:
            log.warning("unhandled signaling message type: %s", kind)
        return False  # GLib.idle_add: don't repeat

    # ---------- wlr-screencopy video capture (GLib thread) ----------

    def start_video_capture(self):
        """Connect to the compositor's wlr-screencopy global and start
        pulling frames. The pipeline itself isn't built until the first
        frame arrives (build_pipeline needs the real width/height/format,
        which we only learn from the compositor's first "buffer" event) --
        mirrors the old acquire_screencast() -> build_pipeline() shape,
        just with a different source of those parameters."""
        self.grabber.connect()
        self.grabber.on_frame = self._on_capture_frame
        # Wire the Wayland display's fd into the SAME GLib main loop that
        # already drives webrtcbin's bus/promise callbacks -- no extra
        # thread needed, matches this script's existing GLib-for-pipeline /
        # asyncio-for-signaling split.
        self._wayland_fd_watch_id = GLib.io_add_watch(
            self.grabber.display.get_fd(), GLib.IO_IN, self._on_wayland_fd_readable
        )
        self._fire_capture()

    def _on_wayland_fd_readable(self, _fd, _condition):
        self.grabber.display.read()
        while self.grabber.display.dispatch(block=False) > 0:
            pass
        self.grabber.display.flush()
        return True  # keep watching

    def _fire_capture(self):
        self._next_capture_timeout_id = None
        self.grabber.capture_next(overlay_cursor=self.args.overlay_cursor)
        self.grabber.display.flush()
        return False  # one-shot GLib.timeout_add

    def _schedule_next_capture(self):
        # Pace captures to roughly the target fps instead of pulling as
        # fast as the compositor allows (proven capable of ~60fps in
        # isolation) -- no reason to burn CPU copying/encoding frames the
        # downstream videorate would just drop anyway.
        self._next_capture_timeout_id = GLib.timeout_add(self._interval_ms, self._fire_capture)

    def _on_capture_frame(self, ok: bool, data: bytes | None, width: int, height: int, format_: int):
        if not ok:
            self._window_fails += 1
            self._schedule_next_capture()
            self._maybe_report_window()
            return

        if self.pipeline is None:
            gst_format = WL_SHM_TO_GST_FORMAT.get(format_)
            if gst_format is None:
                log.error("unsupported wl_shm format %d, no GStreamer mapping -- aborting", format_)
                self.loop.quit()
                return
            self.build_pipeline(width, height, gst_format)

        self._push_frame(data)
        self._window_frames += 1
        self._schedule_next_capture()
        self._maybe_report_window()

    def _push_frame(self, data: bytes):
        if self._capture_t0 is None:
            self._capture_t0 = time.monotonic()
        buf = Gst.Buffer.new_wrapped(data)
        buf.pts = int((time.monotonic() - self._capture_t0) * Gst.SECOND)
        ret = self.appsrc.emit("push-buffer", buf)
        if ret != Gst.FlowReturn.OK:
            log.warning("appsrc push-buffer returned %s", ret)

    def _maybe_report_window(self):
        now = time.monotonic()
        if now - self._window_start < 4.0:
            return
        log.info(
            "[capture] Frames received: %d. Rendered: %d. (last %.1fs, %d failed)",
            self._window_frames, self._window_frames, now - self._window_start, self._window_fails,
        )
        if self._window_frames == 0:
            log.error(
                "[capture] STALL: zero frames captured in the last window -- this is the "
                "exact symptom the wlr-screencopy path was supposed to avoid"
            )
        self._window_frames = 0
        self._window_fails = 0
        self._window_start = now

    # ---------- pipeline ----------

    def build_pipeline(self, width: int, height: int, gst_format: str):
        a = self.args
        # appsrc caps use framerate=0/1 ("variable/unknown") deliberately:
        # frames arrive live with real per-buffer PTS (set in _push_frame,
        # relative to first-frame time) rather than a fixed cadence, and
        # videorate downstream uses those timestamps -- not the nominal
        # input caps framerate -- to retime to the fixed output rate. This
        # is the standard pattern for a live/variable-rate appsrc source.
        video = (
            f'appsrc name=vidsrc is-live=true format=time do-timestamp=false '
            f'caps="video/x-raw,format={gst_format},width={width},height={height},framerate=0/1" ! '
            "videoconvert ! videorate ! videoscale ! "
            f"video/x-raw,format=I420,framerate={a.fps}/1 ! "
            "queue max-size-buffers=2 leaky=downstream ! "
            f"openh264enc bitrate={a.bitrate} rate-control=bitrate complexity=low "
            f"gop-size={a.fps * 2} usage-type=screen ! "
            "video/x-h264,profile=constrained-baseline ! "
            "h264parse config-interval=-1 ! "
            "rtph264pay config-interval=-1 pt=96 ! "
            "queue ! webrtc.sink_0"
        )
        # Audio branch: UNCHANGED from the original portal-based sender.
        # This was never implicated in the video freeze bug -- it captures
        # a continuous PCM stream from the default sink's *monitor*, a
        # fundamentally different PipeWire producer than the
        # screencopy/DMA-BUF-negotiated video stream the portal used to
        # own, so it doesn't go through the wedged code path at all and
        # doesn't need replacing.
        audio = (
            f"pipewiresrc target-object={a.audio_source} do-timestamp=true ! "
            "audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2 ! "
            "queue max-size-buffers=50 leaky=downstream ! "
            "opusenc bitrate=64000 ! rtpopuspay pt=111 ! "
            "queue ! webrtc.sink_1"
        )
        launch = f"webrtcbin name=webrtc bundle-policy=max-bundle {video} {audio}"
        log.info("pipeline: %s", launch)
        self.pipeline = Gst.parse_launch(launch)
        self.appsrc = self.pipeline.get_by_name("vidsrc")
        self.webrtc = self.pipeline.get_by_name("webrtc")
        self.webrtc.connect("on-negotiation-needed", self._on_negotiation_needed)
        self.webrtc.connect("on-ice-candidate", self._on_ice_candidate)
        self.webrtc.connect("notify::ice-connection-state", self._on_ice_state)
        self.webrtc.connect("notify::connection-state", self._on_conn_state)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_error)
        bus.connect("message::warning", self._on_bus_warning)

        if self.args.probe_buffers:
            self._install_debug_probes()

        self.pipeline.set_state(Gst.State.PLAYING)
        log.info("pipeline set to PLAYING (%dx%d %s from wlr-screencopy)", width, height, gst_format)

    # ---------- diagnostic buffer-counting probes (--probe-buffers) ----------

    def _install_debug_probes(self):
        """Count buffers at each stage of the video branch -- useful for
        seeing exactly where any stall happens: at appsrc itself (capture
        loop stopped pushing) or somewhere further down the chain
        (videorate/encoder/webrtcbin)."""
        points = [
            ("vidsrc", "src"),
            ("videorate0", "src"),
            ("openh264enc0", "src"),
        ]
        self._probe_counts = {}
        for name, pad_name in points:
            el = self.pipeline.get_by_name(name)
            if el is None:
                log.warning("probe: no element named %s in pipeline", name)
                continue
            pad = el.get_static_pad(pad_name)
            self._probe_counts[name] = 0
            pad.add_probe(Gst.PadProbeType.BUFFER, self._probe_cb, name)
        log.info("debug buffer-counting probes installed on %s", list(self._probe_counts))

    def _probe_cb(self, _pad, info, name):
        buf = info.get_buffer()
        self._probe_counts[name] += 1
        n = self._probe_counts[name]
        if n <= 5 or n % 30 == 0:
            pts = buf.pts / Gst.SECOND if buf.pts != Gst.CLOCK_TIME_NONE else -1
            log.info("PROBE %-14s buffer #%d pts=%.3fs", name, n, pts)
        return Gst.PadProbeReturn.OK

    def _on_bus_error(self, _bus, message):
        err, debug = message.parse_error()
        log.error("GStreamer ERROR: %s (%s)", err, debug)

    def _on_bus_warning(self, _bus, message):
        err, debug = message.parse_warning()
        log.warning("GStreamer WARNING: %s (%s)", err, debug)

    def _on_negotiation_needed(self, element):
        log.info("negotiation-needed -> creating offer")
        promise = Gst.Promise.new_with_change_func(self._on_offer_created, element, None)
        element.emit("create-offer", None, promise)

    def _on_offer_created(self, promise, element, _data):
        reply = promise.get_reply()
        offer = reply.get_value("offer")
        promise2 = Gst.Promise.new()
        element.emit("set-local-description", offer, promise2)
        promise2.interrupt()
        sdp_text = offer.sdp.as_text()
        log.info("local offer created (%d bytes), sending to signaling server", len(sdp_text))
        self.send_signaling({"type": "offer", "sdp": sdp_text})

    def _on_ice_candidate(self, _element, mlineindex, candidate):
        self.send_signaling({"type": "ice", "sdpMLineIndex": mlineindex, "candidate": candidate})

    def _on_ice_state(self, element, _pspec):
        state = element.get_property("ice-connection-state")
        log.info("ICE connection state -> %s", state)

    def _on_conn_state(self, element, _pspec):
        state = element.get_property("connection-state")
        log.info("WebRTC connection state -> %s", state)

    def run(self):
        self.start_signaling_thread()
        self.start_video_capture()
        try:
            self.loop.run()
        finally:
            if self.pipeline:
                self.pipeline.set_state(Gst.State.NULL)
            # Same explicit-disconnect fix as spike_cast_wlr_screencopy.py:
            # leaving Wayland proxy cleanup to GC at interpreter shutdown
            # segfaults (confirmed live via coredumpctl). Must run before
            # process exit.
            self.grabber.disconnect()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--signaling-url", default="ws://localhost:8765")
    p.add_argument("--bitrate", type=int, default=2_500_000, help="video bitrate, bits/sec")
    p.add_argument("--fps", type=int, default=15)
    p.add_argument("--audio-source", default=DEFAULT_AUDIO_MONITOR)
    p.add_argument("--output-index", type=int, default=0, help="which wl_output to capture (0 = first)")
    p.add_argument("--overlay-cursor", type=int, default=0, choices=(0, 1))
    p.add_argument(
        "--probe-buffers",
        action="store_true",
        help="log a buffer-arrival count at vidsrc/videorate/openh264enc",
    )
    args = p.parse_args()
    Sender(args).run()


if __name__ == "__main__":
    main()
