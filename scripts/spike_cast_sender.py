#!/usr/bin/env python3
"""Phase 2 spike: Linux-side webrtcbin sender for Android TV casting.

Proves the whole capture -> encode -> WebRTC chain end to end on real
hardware before any of this touches the Android app (same "GStreamer-only
proof of concept first" convention as scripts/spike_live_webrtc.py in
Phase 1). Combines:

- Video: `org.freedesktop.portal.ScreenCast` (via libportal's `Xdp` GI
  bindings, confirmed present -- see scripts/spike_cast_portal.py) for a
  PipeWire node id, then `pipewiresrc` -> `openh264enc` (software H.264;
  no GStreamer VAAPI plugin is installed on this machine -- see
  docs/DEPENDENCIES.md/ADR-0001 D6 revision -- but a real
  videotestsrc benchmark showed openh264enc encoding 720p15 about 10x
  faster than real time on this CPU, so this is not a stopgap, it is the
  real path for now).
- Audio: `pipewiresrc target-object=<monitor-source>` capturing the
  default sink's monitor (system audio) -> `opusenc`.
- Signaling: connects to `omarchy_ai.display.signaling` as role=sender over
  a plain `websockets` connection running in its own thread (GStreamer's
  main loop is GLib-based, not asyncio -- bridged via
  `GLib.idle_add`/`asyncio.run_coroutine_threadsafe`, same split this repo
  already uses between the GLib (webrtcbin) and asyncio (aiortc) worlds).

Run this AFTER scripts/spike_cast_portal.py has proven the portal handshake
works on this desktop at least once (the picker is a one-time-per-session
GUI consent dialog you have to click through as the user).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import threading

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
gi.require_version("Xdp", "1.0")
from gi.repository import GLib, Gst, GstSdp, GstWebRTC, Xdp  # noqa: E402

import websockets  # noqa: E402

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
        self.ws = None
        self.ws_loop: asyncio.AbstractEventLoop | None = None
        self.ws_ready = threading.Event()

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

    # ---------- portal handshake (GLib thread, via libportal) ----------

    def acquire_screencast(self):
        # Keep a strong reference on self -- a local var here would go out
        # of scope the instant this method returns (it only *starts* the
        # async call), and PyGObject can garbage-collect the Portal object
        # mid-flight, silently dropping the pending callback. Confirmed
        # live: without this, _on_session_created never fired, no error,
        # no timeout -- just silence forever, unlike the working standalone
        # spike script where `portal` stays alive in main()'s own frame for
        # the whole loop.run().
        self._portal = Xdp.Portal.new()
        self._portal.create_screencast_session(
            Xdp.OutputType.MONITOR,
            Xdp.ScreencastFlags.NONE,
            Xdp.CursorMode.EMBEDDED,
            Xdp.PersistMode.PERSISTENT,
            None,
            None,
            self._on_session_created,
            None,
        )

    def _on_session_created(self, portal, res, _data):
        try:
            session = portal.create_screencast_session_finish(res)
        except GLib.Error as e:
            log.error("create_screencast_session FAILED: %s", e)
            self.loop.quit()
            return
        log.info("portal session created, calling start() -- click the share dialog if it appears")
        session.start(None, None, self._on_started, None)

    def _on_started(self, session, res, _data):
        try:
            session.start_finish(res)
        except GLib.Error as e:
            log.error("portal start() FAILED: %s", e)
            self.loop.quit()
            return
        streams = session.get_streams()
        node_id = streams[0][0]
        fd = session.open_pipewire_remote()
        log.info("portal handshake OK: node_id=%s fd=%s", node_id, fd)
        self._session = session  # keep alive
        self.build_pipeline(node_id, fd)

    # ---------- pipeline ----------

    def build_pipeline(self, node_id: int, fd: int):
        a = self.args
        # Video freezes after exactly one frame (confirmed live on real
        # hardware, see STATUS.md/ADR-0001 for the full evidence trail):
        # journalctl --user -u xdg-desktop-portal-hyprland showed, in the
        # SAME time window as the one frame that got through, an endless
        # loop of
        #     [screencopy/pipewire] Out of buffers
        #     [sc] Retrying screencopy (1/10)
        #     [pw] Building modifiers for dma
        #     [WARN] [pipewire] Asked for a wl_shm buffer which is legacy.
        # i.e. the portal's screencopy->PipeWire producer runs out of
        # buffers to write into almost immediately and never recovers --
        # not a "only pushes on screen damage" issue (ruled out live: a
        # deliberately-changing foot terminal on screen during the stall
        # produced no new frames either). This matches multiple upstream
        # reports of the identical symptom (GNOME LP#1987631 "Screencast
        # only records one second", hyprwm/xdg-desktop-portal-hyprland#434
        # "DMA-BUF screencopy failure leaves xdph wedged"): the consumer
        # doesn't return PipeWire's buffers to its pool fast/reliably
        # enough (worse when DMA-BUF modifier negotiation is in play, which
        # our own log shows retried on every single attempt), so the
        # portal's small buffer pool empties out and capture wedges solid.
        # Mitigations applied here, in order of how directly they attack
        # that root cause:
        #  - always-copy=true: copy PipeWire's buffer into a fresh GstBuffer
        #    immediately instead of holding a reference into its pool, so
        #    our pipeline can never be the reason a buffer isn't returned.
        #    (Deprecated property, but still implemented; this is the same
        #    workaround documented for LP#1987631 before it was fixed
        #    upstream in gstreamer's videoconvert -- kept here deliberately
        #    since our own logs show the portal-side pool exhaustion is
        #    still happening on current versions: pipewire 1.6.8,
        #    xdg-desktop-portal-hyprland 1.4.1, gstreamer 1.28.6.)
        #  - min-buffers/max-buffers: give the portal's allocator a small,
        #    fixed pool to negotiate against instead of an unbounded default
        #    (max-buffers defaults to INT_MAX), which is a plausible
        #    contributor to the repeated "Building modifiers for dma"
        #    renegotiation churn visible in the portal log.
        #  - keepalive-time: last-resort safety net -- periodically resend
        #    the last good frame so the receiver never sits on a fully dead
        #    stream even if the above doesn't fully fix upstream buffer
        #    exhaustion. Does not fix the underlying stall by itself.
        # RE-TESTED LIVE (same day, real hardware) after the above fix:
        # marginal improvement only (2 frames rendered instead of 1 per
        # Android's EglRenderer log, then the same permanent stall), and a
        # fresh journalctl --user -u xdg-desktop-portal-hyprland pull from
        # that exact test window showed the IDENTICAL loop still firing --
        # confirmed not a one-off: grepping the last 2 days of portal logs
        # found 11,000+ repeats of "Out of buffers", roughly once/sec
        # continuously for the whole duration of every casting test session
        # run in that window, not just "one frame then done". So
        # always-copy/bounded-pool did not touch the real root cause.
        #
        # Investigated further (no human available for a live re-test at
        # the time): whether restricting OUR pipewiresrc consumer to plain
        # system-memory caps (no memory:DMABuf feature) would stop the
        # portal from ever offering a DMA-BUF-backed format in the first
        # place. Conclusion, reasoned from the actual GitHub issue this
        # matches (hyprwm/xdg-desktop-portal-hyprland#434, "DMA-BUF
        # screencopy failure leaves xdph wedged" -- identical log
        # signature) -- the "Building modifiers for dma" line is
        # xdg-desktop-portal-hyprland's OWN internal DMA-BUF capture from
        # the Hyprland compositor (how it gets frames off the GPU in the
        # first place), not something negotiated against our consumer's
        # requested caps; downstream `videoconvert` already implicitly
        # restricts to system-memory raw video today (no
        # memory:DMABuf-feature caps appear anywhere in this pipeline), so
        # this consumer-side lever most likely does not reach the code path
        # that is actually wedging. Keeping the explicit `video/x-raw`
        # capsfilter right after pipewiresrc anyway below -- it is what was
        # asked for, costs nothing, and makes the "no DMA-BUF on our side"
        # intent explicit instead of implicit -- but it is NOT expected to
        # be the fix by itself.
        #
        # The fix that actually matches the upstream mechanism: PipeWire's
        # own DMA-BUF *modifier* negotiation, which is what "Building
        # modifiers for dma" names, and which xdph's internal capture goes
        # through regardless of what any client requests. Applied at
        # ~/.config/pipewire/pipewire.conf.d/98-screencast-no-dmabuf-
        # modifiers.conf (support.dmabuf.modifiers = false), matching a
        # documented community fix for this exact symptom (Arch forum
        # thread id=308493, "[SOLVED] XDPH stuck at building modifiers for
        # dma"). `xdg-desktop-portal-hyprland` is already at 1.4.1, the
        # newest version in Arch's `extra` repo AND the newest GitHub tag
        # (`pacman -Si` and `gh api repos/.../tags` agree) -- no newer
        # package fixes this. pipewire/pipewire-pulse/wireplumber and
        # xdg-desktop-portal-hyprland were restarted to load the new
        # config; confirmed live (machine-verifiable, no human needed):
        # `pw-cli info 0` now reports `support.dmabuf.modifiers = "false"`,
        # and pactl/pipewire came back up cleanly with no new errors.
        # NOT YET RE-CONFIRMED for the actual bug: still needs a human to
        # click through the portal consent picker for a real casting
        # session and confirm "Out of buffers" stops appearing in
        # `journalctl --user -u xdg-desktop-portal-hyprland -f` and frames
        # keep flowing past the first one. This machine's GPU (Intel HD
        # 4000, i915) isn't in the encode path anyway (software
        # openh264enc, no VAAPI plugin installed -- ADR-0001 D6), so
        # disabling DMA-BUF modifiers costs an extra copy at most, not a
        # capability loss, on this hardware.
        video = (
            f"pipewiresrc fd={fd} path={node_id} do-timestamp=true "
            f"always-copy=true min-buffers=2 max-buffers=4 "
            f"keepalive-time={a.keepalive_ms} ! "
            "video/x-raw ! "
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
        log.info("pipeline set to PLAYING")

    # ---------- diagnostic buffer-counting probes (--probe-buffers) ----------

    def _install_debug_probes(self):
        """Count buffers at each stage of the video branch so we can see
        exactly where the "one frame then nothing" stall happens: at the
        PipeWire source itself (portal/compositor stopped pushing) or
        somewhere further down the chain (videorate/encoder/webrtcbin)."""
        points = [
            ("pipewiresrc0", "src"),
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
        self.acquire_screencast()
        try:
            self.loop.run()
        finally:
            if self.pipeline:
                self.pipeline.set_state(Gst.State.NULL)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--signaling-url", default="ws://localhost:8765")
    p.add_argument("--bitrate", type=int, default=2_500_000, help="video bitrate, bits/sec")
    p.add_argument("--fps", type=int, default=15)
    p.add_argument("--audio-source", default=DEFAULT_AUDIO_MONITOR)
    p.add_argument(
        "--probe-buffers",
        action="store_true",
        help="log a buffer-arrival count at pipewiresrc/videorate/openh264enc "
        "(diagnostic for the frozen-after-one-frame bug)",
    )
    p.add_argument(
        "--keepalive-ms",
        type=int,
        default=1000,
        help="pipewiresrc keepalive-time: periodically resend the last buffer "
        "if no new one has arrived (0 = disabled). Safety net only -- see "
        "build_pipeline() for the real fix attempt (always-copy + bounded "
        "buffer count) for the 'Out of buffers' portal-side stall.",
    )
    args = p.parse_args()
    Sender(args).run()


if __name__ == "__main__":
    main()
