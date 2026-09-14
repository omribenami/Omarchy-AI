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
        portal = Xdp.Portal.new()
        portal.create_screencast_session(
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
        video = (
            f"pipewiresrc fd={fd} path={node_id} do-timestamp=true ! "
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

        self.pipeline.set_state(Gst.State.PLAYING)
        log.info("pipeline set to PLAYING")

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
    args = p.parse_args()
    Sender(args).run()


if __name__ == "__main__":
    main()
