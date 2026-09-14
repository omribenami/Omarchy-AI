#!/usr/bin/env python3
"""Phase 2 spike: prove the ScreenCast portal handshake works standalone.

Uses libportal (Xdp GObject-introspection bindings, confirmed present on
this machine — see docs/ADR-0001-architecture.md D5) to drive
org.freedesktop.portal.ScreenCast: CreateSession -> SelectSources (via
create_screencast_session) -> Start -> OpenPipeWireRemote.

This will very likely pop a real GUI consent dialog on the live desktop
(xdg-desktop-portal-hyprland's screencast picker) — that's expected; it
needs a human to click through it once, same as any first-run screen-share
prompt. Run this interactively, not backgrounded.

On success, prints the PipeWire node id and passes the portal fd + node id
to a short `pipewiresrc` test pipeline (via gst-launch-1.0, spawned as a
subprocess) that pulls a few real frames and reports their size — proof the
whole chain (portal -> PipeWire -> GStreamer) delivers real screen pixels,
before any of this touches webrtcbin or Android.
"""

import subprocess
import sys

import gi

gi.require_version("Xdp", "1.0")
from gi.repository import Xdp, GLib, Gio  # noqa: E402

loop = GLib.MainLoop()
result = {}


def on_session_created(portal, res, data):
    try:
        session = portal.create_screencast_session_finish(res)
    except GLib.Error as e:
        print(f"create_screencast_session FAILED: {e}", file=sys.stderr)
        loop.quit()
        return
    result["session"] = session
    print("Session created, calling start() -- watch for a consent dialog...")
    session.start(None, None, on_started, None)


def on_started(session, res, data):
    try:
        ok = session.start_finish(res)
    except GLib.Error as e:
        print(f"start() FAILED: {e}", file=sys.stderr)
        loop.quit()
        return
    print(f"start() finished, ok={ok}")
    streams = session.get_streams()
    print(f"streams variant: {streams}")
    # streams is a GVariant of type a(ua{sv}) -- array of (node_id, props)
    for entry in streams:
        node_id = entry[0]
        props = entry[1]
        print(f"  stream node_id={node_id} props={props}")
        result.setdefault("node_ids", []).append(node_id)
    fd = session.open_pipewire_remote()
    print(f"open_pipewire_remote() fd={fd}")
    result["fd"] = fd
    result["session_obj"] = session
    loop.quit()


def main():
    portal = Xdp.Portal.new()
    portal.create_screencast_session(
        Xdp.OutputType.MONITOR,
        Xdp.ScreencastFlags.NONE,
        Xdp.CursorMode.EMBEDDED,
        Xdp.PersistMode.PERSISTENT,
        None,
        None,
        on_session_created,
        None,
    )
    loop.run()

    if "fd" not in result or not result.get("node_ids"):
        print("Portal handshake did not produce a usable fd/node_id.", file=sys.stderr)
        sys.exit(1)

    fd = result["fd"]
    node_id = result["node_ids"][0]
    print(f"\nHandshake OK. fd={fd} node_id={node_id}")
    print("Pulling 30 real frames via gst-launch-1.0 pipewiresrc to prove real pixels flow...")

    # fd is only valid in this process; pass it to a child via passing the
    # fd across exec (Python keeps fds open across subprocess.Popen unless
    # close_fds strips them -- pass close_fds=False and reference the same
    # fd number since GStreamer's pipewiresrc fd= takes a raw fd number).
    pipeline = (
        f"pipewiresrc fd={fd} path={node_id} num-buffers=30 ! "
        "videoconvert ! video/x-raw,format=I420 ! "
        "fakesink sync=false"
    )
    proc = subprocess.run(
        ["gst-launch-1.0", "-v", *pipeline.split()],
        pass_fds=(fd,),
        capture_output=True,
        text=True,
        timeout=15,
    )
    print("--- gst-launch-1.0 stdout (tail) ---")
    print("\n".join(proc.stdout.splitlines()[-15:]))
    print("--- gst-launch-1.0 stderr (tail) ---")
    print("\n".join(proc.stderr.splitlines()[-15:]))
    print(f"exit code: {proc.returncode}")
    sys.exit(0 if proc.returncode == 0 else 2)


if __name__ == "__main__":
    main()
