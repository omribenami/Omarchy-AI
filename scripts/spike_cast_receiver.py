#!/usr/bin/env python3
"""Phase 2 spike: trivial Python webrtcbin receiver, proving media actually
flows out of scripts/spike_cast_sender.py before writing any Android code.

Connects to the signaling server as role=viewer, answers the sender's
offer, and on `pad-added` links whatever comes in:
  - video -> h264 depay/parse/decode -> a real on-screen window
    (`autovideosink`), so you can visually confirm the desktop is actually
    being mirrored.
  - audio -> opus depay/decode -> `fakesink` with a buffer-count probe
    (NOT played out loud on purpose: this machine's audio *source* for the
    sender is the default output's own monitor, so playing decoded audio
    back out the same output would feed straight back into the capture --
    a real feedback loop, not a hypothetical one. The probe still proves
    real audio frames are arriving, just without piping them back through
    the speaker they were captured from.)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import threading

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
from gi.repository import GLib, Gst, GstSdp, GstWebRTC  # noqa: E402

import websockets  # noqa: E402

log = logging.getLogger("spike_cast_receiver")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

Gst.init(None)

video_frame_count = 0
audio_buffer_count = 0


class Receiver:
    def __init__(self, args):
        self.args = args
        self.loop = GLib.MainLoop()
        self.pipeline = Gst.Pipeline.new("receiver")
        self.webrtc = Gst.ElementFactory.make("webrtcbin", "webrtc")
        self.webrtc.set_property("bundle-policy", "max-bundle")
        self.pipeline.add(self.webrtc)
        self.webrtc.connect("pad-added", self._on_pad_added)
        self.webrtc.connect("on-ice-candidate", self._on_ice_candidate)
        self.webrtc.connect("notify::ice-connection-state", self._on_ice_state)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", lambda _b, m: log.error("GST ERROR: %s", m.parse_error()))
        bus.connect("message::warning", lambda _b, m: log.warning("GST WARN: %s", m.parse_warning()))

        self.ws = None
        self.ws_loop = None
        self.ws_ready = threading.Event()

    def start_signaling_thread(self):
        t = threading.Thread(target=lambda: asyncio.run(self._signaling_main()), daemon=True)
        t.start()
        self.ws_ready.wait(timeout=10)

    async def _signaling_main(self):
        self.ws_loop = asyncio.get_running_loop()
        async with websockets.connect(self.args.signaling_url) as ws:
            self.ws = ws
            await ws.send(json.dumps({"role": "viewer"}))
            self.ws_ready.set()
            log.info("registered as viewer")
            async for raw in ws:
                msg = json.loads(raw)
                GLib.idle_add(self._handle_signaling_message, msg)

    def send_signaling(self, msg):
        if self.ws is None:
            return
        asyncio.run_coroutine_threadsafe(self.ws.send(json.dumps(msg)), self.ws_loop)

    def _handle_signaling_message(self, msg):
        kind = msg.get("type")
        if kind == "offer":
            log.info("got offer (%d bytes)", len(msg.get("sdp", "")))
            _, sdpmsg = GstSdp.SDPMessage.new()
            GstSdp.sdp_message_parse_buffer(msg["sdp"].encode(), sdpmsg)
            offer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.OFFER, sdpmsg)
            promise = Gst.Promise.new_with_change_func(self._on_remote_set, None, None)
            self.webrtc.emit("set-remote-description", offer, promise)
        elif kind == "ice":
            self.webrtc.emit("add-ice-candidate", msg.get("sdpMLineIndex", 0), msg.get("candidate", ""))
        return False

    def _on_remote_set(self, _promise, *_):
        log.info("remote description set, creating answer")
        promise = Gst.Promise.new_with_change_func(self._on_answer_created, None, None)
        self.webrtc.emit("create-answer", None, promise)

    def _on_answer_created(self, promise, *_):
        reply = promise.get_reply()
        answer = reply.get_value("answer")
        promise2 = Gst.Promise.new()
        self.webrtc.emit("set-local-description", answer, promise2)
        promise2.interrupt()
        sdp_text = answer.sdp.as_text()
        log.info("local answer created, sending to signaling server")
        self.send_signaling({"type": "answer", "sdp": sdp_text})

    def _on_ice_candidate(self, _element, mlineindex, candidate):
        self.send_signaling({"type": "ice", "sdpMLineIndex": mlineindex, "candidate": candidate})

    def _on_ice_state(self, element, _pspec):
        log.info("ICE connection state -> %s", element.get_property("ice-connection-state"))

    def _on_pad_added(self, _element, pad):
        if pad.direction != Gst.PadDirection.SRC:
            return
        caps = pad.get_current_caps() or pad.query_caps(None)
        s = caps.get_structure(0)
        name = s.get_name()
        log.info("pad-added: %s caps=%s", pad.get_name(), caps.to_string())

        if name == "application/x-rtp":
            media = s.get_string("media") if s.has_field("media") else None
            enc = s.get_string("encoding-name") if s.has_field("encoding-name") else None
            log.info("  rtp media=%s encoding=%s", media, enc)
            if media == "video" or enc == "H264":
                self._link_video(pad)
            elif media == "audio" or enc == "OPUS":
                self._link_audio(pad)

    def _link_video(self, pad):
        depay = Gst.ElementFactory.make("rtph264depay", None)
        parse = Gst.ElementFactory.make("h264parse", None)
        dec = Gst.ElementFactory.make("avdec_h264", None) or Gst.ElementFactory.make("openh264dec", None)
        conv = Gst.ElementFactory.make("videoconvert", None)
        probe_id = Gst.ElementFactory.make("identity", None)
        sink = Gst.ElementFactory.make("autovideosink", None)
        sink.set_property("sync", False)
        for e in (depay, parse, dec, conv, probe_id, sink):
            self.pipeline.add(e)
            e.sync_state_with_parent()
        pad.link(depay.get_static_pad("sink"))
        depay.link(parse)
        parse.link(dec)
        dec.link(conv)
        conv.link(probe_id)
        probe_id.link(sink)

        def _cb(_pad, info):
            global video_frame_count
            video_frame_count += 1
            if video_frame_count % 30 == 0:
                log.info("video frames received: %d", video_frame_count)
            return Gst.PadProbeReturn.OK

        probe_id.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, _cb)
        log.info("video branch linked (decoder=%s)", dec.get_factory().get_name())

    def _link_audio(self, pad):
        depay = Gst.ElementFactory.make("rtpopusdepay", None)
        dec = Gst.ElementFactory.make("opusdec", None)
        conv = Gst.ElementFactory.make("audioconvert", None)
        sink = Gst.ElementFactory.make("fakesink", None)
        sink.set_property("sync", False)
        for e in (depay, dec, conv, sink):
            self.pipeline.add(e)
            e.sync_state_with_parent()
        pad.link(depay.get_static_pad("sink"))
        depay.link(dec)
        dec.link(conv)
        conv.link(sink)

        def _cb(_pad, info):
            global audio_buffer_count
            audio_buffer_count += 1
            if audio_buffer_count % 100 == 0:
                log.info("audio buffers received: %d", audio_buffer_count)
            return Gst.PadProbeReturn.OK

        conv.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, _cb)
        log.info("audio branch linked (decoded, sent to fakesink -- see module docstring for why)")

    def run(self):
        self.start_signaling_thread()
        self.pipeline.set_state(Gst.State.PLAYING)
        try:
            self.loop.run()
        finally:
            self.pipeline.set_state(Gst.State.NULL)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--signaling-url", default="ws://localhost:8765")
    args = p.parse_args()
    Receiver(args).run()


if __name__ == "__main__":
    main()
