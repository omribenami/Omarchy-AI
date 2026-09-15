#!/usr/bin/env python3
"""Phase 1 spike: real two-way audio with gpt-live-1.

Mic in via pipewiresrc, the model's voice out via whatever audio sink
GStreamer picks by default. No data channel / function-calling yet, no wake
word — this just proves you can actually hear it respond. Runs for
RUN_SECONDS then hangs up.
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
RUN_SECONDS = 45

loop = GLib.MainLoop()
pipeline = Gst.parse_launch(
    "webrtcbin name=webrtc bundle-policy=max-bundle "
    "stun-server=stun://stun.l.google.com:19302 "
    "pipewiresrc ! audioconvert ! level name=miclevel interval=500000000 "
    "! audioresample ! opusenc ! rtpopuspay pt=111 ! webrtc."
)
webrtc = pipeline.get_by_name("webrtc")

_ICE_STATE_NAMES = {
    0: "NEW", 1: "CHECKING", 2: "CONNECTED", 3: "COMPLETED",
    4: "FAILED", 5: "DISCONNECTED", 6: "CLOSED",
}
_frames_received = [0]
bus = pipeline.get_bus()
bus.add_signal_watch()


def on_bus_message(_bus, message):
    if message.type == Gst.MessageType.ELEMENT and message.src.get_name() == "miclevel":
        s = message.get_structure()
        rms = s.get_value("rms")[0]
        print(f"mic level: {rms:.1f} dBFS", flush=True)
    elif message.type == Gst.MessageType.ERROR:
        err, debug = message.parse_error()
        print(f"PIPELINE ERROR: {err} ({debug})", flush=True)


bus.connect("message", on_bus_message)


def read_key() -> str:
    with open(KEY_PATH) as f:
        return f.read().strip()


def _wire_playback(pad):
    print("--- remote track arrived, wiring up playback ---", flush=True)
    decode_bin = Gst.parse_bin_from_description(
        "rtpopusdepay ! opusdec ! audioconvert ! audioresample ! autoaudiosink",
        True,
    )
    pipeline.add(decode_bin)
    decode_bin.sync_state_with_parent()
    sinkpad = decode_bin.get_static_pad("sink")
    link_result = pad.link(sinkpad)
    print(f"--- playback linked (result={link_result}) ---", flush=True)

    def _on_probe(_pad, info):
        if _frames_received[0] == 0:
            print("*** first remote audio frame actually arrived ***", flush=True)
        _frames_received[0] += 1
        return Gst.PadProbeReturn.OK

    pad.add_probe(Gst.PadProbeType.BUFFER, _on_probe)
    return False


def on_pad_added(element, pad):
    if pad.get_direction() != Gst.PadDirection.SRC:
        return
    # webrtcbin fires this on its streaming thread, not the GLib main
    # thread. Adding/linking elements synchronously here can deadlock the
    # rest of the pipeline (confirmed: the mic branch's `level` messages
    # stopped dead the instant this ran) — defer it to the main loop.
    GLib.idle_add(_wire_playback, pad)


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
                    "session": {
                        "model": "gpt-live-1",
                        "delegation": {
                            "type": "responses",
                            "responses": {"model": "gpt-5"},
                        },
                        "instructions": (
                            "You are Oma, a voice assistant for a Linux "
                            "desktop called Omarchy. Keep responses brief "
                            "and conversational. This is a connectivity "
                            "test — if you hear the user, greet them "
                            "briefly and confirm you can hear them."
                        ),
                        "audio": {"output": {"voice": "marin"}},
                    },
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

    answer_sdp = body["transport"]["sdp"]
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
    print(f"ICE state -> {_ICE_STATE_NAMES.get(state, state)}", flush=True)
    if state == GstWebRTC.WebRTCICEConnectionState.CONNECTED:
        print(
            f"CONNECTED — talk into the mic now, you have {RUN_SECONDS}s",
            flush=True,
        )
    elif state == GstWebRTC.WebRTCICEConnectionState.FAILED:
        print(
            f"*** ICE FAILED after {_frames_received[0]} remote audio frames received ***",
            flush=True,
        )


def on_data_channel_open(channel):
    # session.started showed "delegation": {"type": "client"} — the default.
    # That likely means WE (the app) decide when a response happens, rather
    # than the model auto-responding via server-side VAD (which is probably
    # why turn_detection isn't even a recognized field: there's no
    # server-managed turn concept in client-delegation mode). So instead of
    # configuring auto-response, explicitly ask for one.
    print("*** data channel open, requesting a response ***", flush=True)
    channel.emit("send-string", json.dumps({"type": "response.create"}))


def on_data_channel_message(channel, message):
    print(f"data channel <- {message[:300]}", flush=True)


def on_data_channel_error(channel, error):
    print(f"data channel ERROR: {error}", flush=True)


webrtc.connect("pad-added", on_pad_added)
webrtc.connect("on-negotiation-needed", on_negotiation_needed)
webrtc.connect("notify::ice-connection-state", on_ice_state)

# create-data-channel returns None while the element is still in NULL state
# (confirmed empirically) — READY is enough for it to actually work, and has
# to happen before the PLAYING transition triggers on-negotiation-needed, or
# the channel misses the first (only, for this API) SDP offer entirely.
pipeline.set_state(Gst.State.READY)
pipeline.get_state(Gst.CLOCK_TIME_NONE)

data_channel = webrtc.emit("create-data-channel", "oai-events", None)
if data_channel is None:
    print("*** create-data-channel still returned None in READY ***", flush=True)
else:
    data_channel.connect("on-open", on_data_channel_open)
    data_channel.connect("on-message-string", on_data_channel_message)
    data_channel.connect("on-error", on_data_channel_error)
    print("*** data channel created before negotiation ***", flush=True)

pipeline.set_state(Gst.State.PLAYING)
GLib.timeout_add_seconds(
    RUN_SECONDS, lambda: (print("time's up, hanging up", flush=True), loop.quit(), False)[2]
)

try:
    loop.run()
finally:
    pipeline.set_state(Gst.State.NULL)
