#!/usr/bin/env python3
"""Phase 1 spike: can we actually open a WebRTC session against gpt-live-1?

Not the real voice module — just proves the negotiation (SDP offer -> POST
/v1/live/sessions -> SDP answer -> ICE connects) works before building the
daemon's audio pipeline and event handling around it. Sends a moment of
test-tone audio (audiotestsrc), not the mic — that's the next spike.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
from gi.repository import GLib, Gst, GstSdp, GstWebRTC  # noqa: E402

Gst.init(None)

KEY_PATH = os.path.expanduser("~/.config/omavoice/key")
API_URL = "https://api.openai.com/v1/live/sessions"

loop = GLib.MainLoop()
pipeline = Gst.parse_launch(
    "webrtcbin name=webrtc bundle-policy=max-bundle "
    "audiotestsrc is-live=true wave=sine ! audioconvert ! audioresample "
    "! opusenc ! rtpopuspay pt=111 ! webrtc."
)
webrtc = pipeline.get_by_name("webrtc")


def read_key() -> str:
    with open(KEY_PATH) as f:
        return f.read().strip()


def on_negotiation_needed(element):
    print("negotiation-needed -> creating offer", flush=True)
    promise = Gst.Promise.new_with_change_func(on_offer_created, element, None)
    element.emit("create-offer", None, promise)


def on_offer_created(promise, element, _):
    reply = promise.get_reply()
    offer = reply.get_value("offer")
    promise2 = Gst.Promise.new()
    element.emit("set-local-description", offer, promise2)
    promise2.interrupt()

    sdp_text = offer.sdp.as_text()
    print(f"--- local offer ({len(sdp_text)} bytes) ---", flush=True)

    try:
        req = urllib.request.Request(
            API_URL,
            data=json.dumps(
                {
                    "session": {"model": "gpt-live-1"},
                    "transport": {"type": "webrtc", "sdp": sdp_text},
                }
            ).encode(),
            headers={
                "Authorization": f"Bearer {read_key()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.read().decode(), flush=True)
        loop.quit()
        return

    print("--- server response keys:", list(body.keys()), "---", flush=True)
    answer_sdp = (
        body.get("transport", {}).get("sdp")
        or body.get("sdp")
        or body.get("answer", {}).get("sdp")
    )
    if not answer_sdp:
        print("FULL RESPONSE:", json.dumps(body, indent=2)[:3000], flush=True)
        loop.quit()
        return

    print(f"--- got remote answer ({len(answer_sdp)} bytes) ---", flush=True)
    _, sdpmsg = GstSdp.SDPMessage.new()
    GstSdp.sdp_message_parse_buffer(answer_sdp.encode(), sdpmsg)
    answer = GstWebRTC.WebRTCSessionDescription.new(
        GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg
    )
    promise3 = Gst.Promise.new()
    webrtc.emit("set-remote-description", answer, promise3)
    promise3.interrupt()
    print("--- remote description set, waiting for ICE ---", flush=True)


def on_ice_state(element, _pspec):
    state = element.get_property("ice-connection-state")
    print("ICE state ->", state, flush=True)
    if state == GstWebRTC.WebRTCICEConnectionState.CONNECTED:
        print("CONNECTED — negotiation succeeded", flush=True)
        GLib.timeout_add_seconds(3, lambda: (loop.quit(), False)[1])


webrtc.connect("on-negotiation-needed", on_negotiation_needed)
webrtc.connect("notify::ice-connection-state", on_ice_state)

pipeline.set_state(Gst.State.PLAYING)
GLib.timeout_add_seconds(20, lambda: (print("timeout"), loop.quit(), False)[2])

try:
    loop.run()
finally:
    pipeline.set_state(Gst.State.NULL)
