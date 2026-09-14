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

    async def handle(self, ws: ServerConnection) -> None:
        role: str | None = None
        try:
            first = await ws.recv()
            msg = json.loads(first)
            role = msg.get("role")
            if role not in ("sender", "viewer"):
                log.warning("rejecting connection with bad first message: %r", msg)
                await ws.close(1008, "first message must be {'role': 'sender'|'viewer'}")
                return

            old = self.peers.get(role)
            if old is not None and old is not ws:
                log.info("replacing existing %s connection", role)
                await old.close(1000, "replaced by new connection")
            self.peers[role] = ws
            log.info("%s connected (%s)", role, ws.remote_address)

            async for raw in ws:
                other_role = "viewer" if role == "sender" else "sender"
                other = self.peers.get(other_role)
                try:
                    parsed = json.loads(raw)
                    kind = parsed.get("type", "?")
                except (json.JSONDecodeError, AttributeError):
                    kind = "?"
                if other is None:
                    log.warning(
                        "%s sent %s but no %s is connected -- dropped",
                        role, kind, other_role,
                    )
                    continue
                log.info("relay %s -> %s: %s", role, other_role, kind)
                await other.send(raw)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            if role is not None and self.peers.get(role) is ws:
                del self.peers[role]
                log.info("%s disconnected", role)


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
