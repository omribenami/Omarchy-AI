"""Phone bridge: a local HTTP server letting a phone on the same LAN talk
to Omarchy from a browser page.

The browser does its own WebRTC directly to OpenAI's Live API (real audio
never passes through this process) — this server's only two jobs are:
(1) relay the browser's SDP offer to `POST /v1/live/sessions` (which needs
the real API key, so the browser can't call it directly) and hand back the
answer, and (2) execute tool calls the model makes during that session,
the same `execution.actions.run_action` the desktop's LiveSession uses.

Deliberately stdlib-only (`http.server`), not a new dependency: this is a
handful of small JSON/static-file endpoints, not a real API surface.
`ThreadingHTTPServer` so one phone's tool call (e.g. describe_screen,
multi-second) can't stall another concurrent request.

Beta, no pairing/auth yet — see config.py's `phone_bridge_enabled` comment
and STATUS.md. Bound to 0.0.0.0 so a phone on the LAN can actually reach
it; that also means anyone else on the LAN can, while it's running.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..config import Config
from ..execution.actions import run_action
from ..voice.live import build_session_config

log = logging.getLogger("omarchy_ai.phone.server")

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_LIVE_SESSIONS_URL = "https://api.openai.com/v1/live/sessions"


def _read_api_key(config: Config) -> str:
    with open(config.api_key_path) as f:
        return f.read().strip()


def _relay_offer(config: Config, offer_sdp: str) -> str:
    """POSTs the browser's SDP offer to OpenAI (server-side, real key) and
    returns the answer SDP — same request shape as the desktop LiveSession,
    built from the same build_session_config so the two can't drift."""
    body = json.dumps(
        {
            "session": build_session_config(config),
            "transport": {"type": "webrtc", "sdp": offer_sdp},
        }
    ).encode()
    req = urllib.request.Request(
        _LIVE_SESSIONS_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {_read_api_key(config)}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        response = json.loads(resp.read())
    return response["transport"]["sdp"]


class _Handler(BaseHTTPRequestHandler):
    config: Config  # set on the class by serve_forever() below
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 — stdlib override
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def do_GET(self) -> None:  # noqa: N802 — stdlib method name
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            index = _STATIC_DIR / "index.html"
            try:
                body = index.read_bytes()
            except FileNotFoundError:
                self._send_json(500, {"error": "phone bridge static page missing"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 — stdlib method name
        path = self.path.split("?", 1)[0]
        if path == "/api/live/offer":
            self._handle_offer()
            return
        if path == "/api/tool":
            self._handle_tool()
            return
        self._send_json(404, {"error": "not found"})

    def _handle_offer(self) -> None:
        try:
            data = self._read_json_body()
            offer_sdp = data["sdp"]
        except (json.JSONDecodeError, KeyError):
            self._send_json(400, {"error": "expected {\"sdp\": \"...\"}"})
            return
        try:
            answer_sdp = _relay_offer(self.config, offer_sdp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            log.error("phone bridge: session creation failed: HTTP %s %s", e.code, detail)
            self._send_json(502, {"error": f"OpenAI session creation failed: HTTP {e.code}"})
            return
        except Exception:  # noqa: BLE001
            log.exception("phone bridge: offer relay failed")
            self._send_json(500, {"error": "offer relay failed"})
            return
        self._send_json(200, {"sdp": answer_sdp})

    def _handle_tool(self) -> None:
        try:
            data = self._read_json_body()
            name = data["name"]
            args = data.get("arguments") or {}
        except (json.JSONDecodeError, KeyError):
            self._send_json(400, {"error": "expected {\"name\": \"...\", \"arguments\": {...}}"})
            return
        log.info("phone bridge tool call: %s(%s)", name, args)
        if name == "get_recent_actions":
            # No per-session action log here (unlike LiveSession's
            # self._action_log) — the phone bridge has no session object
            # to hang one off. Graceful empty result rather than an
            # "unknown action" error, since this is a real tool the model
            # may reach for.
            self._send_json(200, {"ok": True, "message": "[]"})
            return
        try:
            result = run_action(name, args)
        except Exception:  # noqa: BLE001
            log.exception("phone bridge: tool call %s failed", name)
            self._send_json(200, {"ok": False, "message": f"{name} failed unexpectedly"})
            return
        log.info("phone bridge tool result: ok=%s message=%r", result.ok, result.message)
        self._send_json(200, {"ok": result.ok, "message": result.message})


def start(config: Config) -> ThreadingHTTPServer | None:
    """Starts the phone bridge in a background thread. Returns the server
    (call .shutdown() to stop it) or None if it's disabled."""
    if not config.phone_bridge_enabled:
        return None
    _Handler.config = config
    server = ThreadingHTTPServer(("0.0.0.0", config.phone_bridge_port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="phone-bridge")
    thread.start()
    log.info(
        "phone bridge listening on 0.0.0.0:%d (beta, no pairing/auth yet)",
        config.phone_bridge_port,
    )
    return server
