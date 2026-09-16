"""Minimal WebSocket signaling relay for the Phase 2 casting subsystem.

`webrtcbin` (the Linux sender, see `encoder.py`/ADR-0001 D7) and the Android
receiver app both need to exchange one SDP offer/answer pair and a handful
of ICE candidates before a WebRTC connection can form directly between
them. WebRTC intentionally leaves that exchange ("signaling") up to the
application — this is the whole of it: a tiny relay both sides connect to
over the LAN, no persistence, no auth beyond "on the same network and knows
the port."

Protocol (JSON text frames):
  First message from a client:  {"role": "sender" | "viewer"}
  Every message after that is opaque to the server and is relayed verbatim
  to the *other* role's currently-connected socket(s):
    {"type": "offer",  "sdp": "..."}
    {"type": "answer", "sdp": "..."}
    {"type": "ice", "candidate": "...", "sdpMLineIndex": N, "sdpMid": "..."}
    {"type": "bye"}

Only one sender and one viewer are expected at a time (one Linux desktop,
one TV) — this is deliberately not a general room/broadcast server. If a
second socket registers with a role that's already taken, the older socket
is dropped in favor of the new one (covers "the sender script restarted").

**Offer/ICE buffering (added after a real, 100%-reproducible bug):**
confirmed live with hard evidence from 7/7 real `start_casting` attempts on
2026-09-14 (`/tmp/omarchy-signaling.log`) — the sender connects and creates
its WebRTC offer near-instantly (300-700ms), but the Android receiver app
takes longer than that to cold-start (APK load, ART, native WebRTC init,
its own signaling connect) and register as `role: viewer`. The relay used
to just drop the sender's offer on the floor ("sender sent offer but no
viewer is connected -- dropped"), so every cast silently failed while
`start_casting` still reported success (it only checks the sender
subprocess didn't immediately die, never that a handshake completed). Fixed
by buffering the sender's most recent offer and any ICE candidates sent
while no viewer is connected, and replaying them, in order, the moment a
viewer registers. Only the sender->viewer direction is buffered — this
project's own evidence is 100% sender-before-viewer; there's no evidence of
the reverse (viewer's answer/ICE arriving before the sender is listening)
happening on this network, so that direction still just drops with a
warning, same as before, rather than adding buffering with no observed
need for it. A *new* sender connection (a new cast attempt) discards any
stale buffered offer/ICE from a previous attempt rather than accumulating
it or replaying it into the new session.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

import websockets
from websockets.asyncio.server import ServerConnection, serve

log = logging.getLogger("omarchy_ai.display.signaling")

DEFAULT_PORT = 8765


class Relay:
    def __init__(self) -> None:
        self.peers: dict[str, ServerConnection] = {}  # role -> socket
        # See module docstring "Offer/ICE buffering" -- state buffered from
        # the sender while no viewer is connected yet, replayed once one
        # registers. Cleared (not accumulated) whenever a new sender
        # connects, so a stale offer from a previous failed attempt can
        # never be replayed into a later session.
        self.pending_offer: dict | None = None
        self.pending_ice: list[dict] = []

    async def _flush_pending_to_viewer(self, viewer: ServerConnection) -> None:
        if self.pending_offer is not None:
            log.info("replaying buffered offer to newly-connected viewer")
            await viewer.send(json.dumps(self.pending_offer))
            self.pending_offer = None
        if self.pending_ice:
            log.info("replaying %d buffered ICE candidate(s) to newly-connected viewer", len(self.pending_ice))
            for candidate in self.pending_ice:
                await viewer.send(json.dumps(candidate))
            self.pending_ice = []

    async def handle(self, ws: ServerConnection) -> None:
        role: str | None = None
        try:
            first = await asyncio.wait_for(ws.recv(), timeout=10)
            msg = json.loads(first)
            role = msg.get("role") if isinstance(msg, dict) else None
            if role not in ("sender", "viewer"):
                log.warning("rejecting connection with bad first message: %r", msg)
                await ws.close(1008, "first message must be {'role': 'sender'|'viewer'}")
                return

            old = self.peers.get(role)
            self.peers[role] = ws
            if old is not None and old is not ws:
                log.info("replacing existing %s connection", role)
                await old.close(1000, "replaced by new connection")
            self.peers[role] = ws
            log.info("%s connected (%s)", role, ws.remote_address)

            if role == "sender":
                # A fresh sender connection means a new cast attempt --
                # discard any stale buffered offer/ICE from a previous one
                # rather than ever replaying it into this new session.
                if self.pending_offer is not None or self.pending_ice:
                    log.info("new sender connected -- discarding stale buffered offer/ICE from a previous attempt")
                self.pending_offer = None
                self.pending_ice = []
                viewer = self.peers.get("viewer")
                if viewer is not None:
                    try:
                        await viewer.send(json.dumps({"type": "bye"}))
                    except websockets.exceptions.ConnectionClosed:
                        pass
            elif role == "viewer":
                await self._flush_pending_to_viewer(ws)

            async for raw in ws:
                if self.peers.get(role) is not ws:
                    break
                other_role = "viewer" if role == "sender" else "sender"
                other = self.peers.get(other_role)
                try:
                    parsed = json.loads(raw)
                    kind = parsed.get("type", "?")
                except (json.JSONDecodeError, AttributeError):
                    kind = "?"
                    parsed = None

                if other is not None:
                    log.info("relay %s -> %s: %s", role, other_role, kind)
                    try:
                        await other.send(raw)
                        continue
                    except websockets.exceptions.ConnectionClosed:
                        if self.peers.get(other_role) is other:
                            del self.peers[other_role]

                if role == "sender" and parsed is not None and kind in ("offer", "ice"):
                    if kind == "offer":
                        log.info("no viewer connected yet -- buffering sender's offer")
                        self.pending_offer = parsed
                        self.pending_ice = []
                    else:
                        log.info("no viewer connected yet -- buffering sender's ICE candidate")
                        if len(self.pending_ice) < 128:
                            self.pending_ice.append(parsed)
                    continue

                log.warning(
                    "%s sent %s but no %s is connected -- dropped",
                    role, kind, other_role,
                )
        except (websockets.exceptions.ConnectionClosed, asyncio.TimeoutError):
            pass
        except (json.JSONDecodeError, TypeError):
            await ws.close(1008, "invalid registration")
        finally:
            if role is not None and self.peers.get(role) is ws:
                del self.peers[role]
                log.info("%s disconnected", role)
                if role == "sender":
                    self.pending_offer = None
                    self.pending_ice = []
                other = self.peers.get("viewer" if role == "sender" else "sender")
                if other is not None:
                    try:
                        await other.send(json.dumps({"type": "bye"}))
                    except websockets.exceptions.ConnectionClosed:
                        pass


async def run_server(host: str, port: int) -> None:
    relay = Relay()
    async with serve(relay.handle, host, port) as server:
        log.info("signaling server listening on ws://%s:%d", host, port)
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces, so the TV can reach it)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    asyncio.run(run_server(args.host, args.port))


if __name__ == "__main__":
    main()
