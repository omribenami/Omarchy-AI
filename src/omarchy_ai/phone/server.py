"""Phone bridge: an HTTPS server letting a paired phone on LAN or Tailscale talk
to Omarchy from a browser page.

For OpenAI, the browser uses WebRTC directly to its Live API. For Gemini,
the server bridges browser WebRTC audio to Gemini Live with a server-side key.
The server's OpenAI path has two jobs:
(1) relay the browser's SDP offer to `POST /v1/live/sessions` (which needs
the real API key, so the browser can't call it directly) and hand back the
answer, and (2) execute tool calls the model makes during that session,
the same `execution.actions.run_action` the desktop's LiveSession uses.

Deliberately stdlib-only (`http.server`), not a new dependency: this is a
handful of small JSON/static-file endpoints, not a real API surface.
`ThreadingHTTPServer` so one phone's tool call (e.g. describe_screen,
multi-second) can't stall another concurrent request.

Pairing (QR code, minted from the settings panel — see cli/settings.py's
`pair-phone` command): a phone with no valid session cookie gets the
"not paired" page for `GET /`, and a 403 from both API endpoints, full
stop — this is the real access boundary, not just a UI nicety. See
`mint_pairing_token`/`_handle_pair`/`_is_paired` below for the flow.
"""

from __future__ import annotations

import http.cookies
import ipaddress
import json
import logging
import secrets
import shutil
import socket as socket_mod
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..config import CONFIG_DIR, Config
from . import cast_audio, flux_pairing, flux_settings, presence, wake_models
from ..execution.actions import run_action, unlock_screen_for_paired_phone
from ..execution import approval_pin, vision
from ..voice.live import build_session_config

log = logging.getLogger("omarchy_ai.phone.server")

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_LIVE_SESSIONS_URL = "https://api.openai.com/v1/live/sessions"

_CERT_DIR = CONFIG_DIR / "phone_bridge"
_CERT_PATH = _CERT_DIR / "cert.pem"
_KEY_PATH = _CERT_DIR / "key.pem"
_PAIR_TOKEN_PATH = _CERT_DIR / "pending_pair_token.json"
_SESSIONS_PATH = _CERT_DIR / "paired_sessions.json"
_SESSION_COOKIE = "omarchy_session"
_PAIR_TOKEN_TTL_SECONDS = 300  # 5 minutes — long enough to scan, short enough that a stale QR left on screen isn't a standing risk
_SESSION_MAX_AGE_SECONDS = 365 * 24 * 3600  # pairing should survive phone restarts; revoke explicitly instead
_mirror_lock = threading.Lock()
_mirror_on = False
_mirror_started_at: float | None = None


def _write_json_0600(path: Path, data: dict | list) -> None:
    """Same 0600-from-creation pattern as cli/settings.py's API key write
    — no window where a pairing token or session id sits on disk
    world-readable."""
    import os

    _CERT_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
    except Exception:
        os.close(fd)
        raise


def _new_session_cookie() -> str:
    """Records a new paired session and returns its Set-Cookie value."""
    session_id = secrets.token_urlsafe(32)
    sessions = _load_sessions()
    sessions.append(session_id)
    _write_json_0600(_SESSIONS_PATH, sessions)
    cookie = http.cookies.SimpleCookie()
    cookie[_SESSION_COOKIE] = session_id
    cookie[_SESSION_COOKIE]["path"] = "/"
    cookie[_SESSION_COOKIE]["secure"] = True
    cookie[_SESSION_COOKIE]["httponly"] = True
    cookie[_SESSION_COOKIE]["samesite"] = "Strict"
    cookie[_SESSION_COOKIE]["max-age"] = _SESSION_MAX_AGE_SECONDS
    return cookie[_SESSION_COOKIE].OutputString()


def mint_pairing_token(config: Config) -> dict:
    """Called from cli/settings.py's `pair-phone` command (a separate,
    short-lived process — this just writes a file the long-running daemon
    reads, the same "settings writes shared state, daemon reads it"
    pattern config.yaml itself already uses, rather than adding an
    internal HTTP call between the two processes). 5-minute TTL,
    idempotent within it rather than strictly single-use — see
    `_handle_pair`'s own comment for why."""
    token = secrets.token_urlsafe(24)
    expires_at = time.time() + _PAIR_TOKEN_TTL_SECONDS
    _write_json_0600(_PAIR_TOKEN_PATH, {"token": token, "expires_at": expires_at, "used": False})
    ip = _primary_lan_ip() or "127.0.0.1"
    lan_url = f"https://{ip}:{config.phone_bridge_port}/pair?token={token}"
    tail_ip, _ = _tailscale_address()
    tail_url = f"https://{tail_ip}:{config.phone_bridge_port}/pair?token={token}" if tail_ip else None
    return {"token": token, "url": tail_url or lan_url, "lan_url": lan_url,
            "tailscale_url": tail_url, "expires_at": expires_at}


def _load_sessions() -> list[str]:
    try:
        with open(_SESSIONS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def revoke_all_sessions() -> None:
    """Called from cli/settings.py's `revoke-phones` command — kicks
    every currently-paired phone out at once (each one's cookie stops
    matching anything in this file on its next request)."""
    _write_json_0600(_SESSIONS_PATH, [])


def paired_count() -> int:
    return len(_load_sessions())


def _primary_lan_ip() -> str | None:
    """The IP this machine would use to reach the internet — a UDP
    "connect" just picks a route, sends nothing, so this needs no real
    connectivity to 8.8.8.8. Used only to put a real, reachable IP in the
    cert's subjectAltName; browsers reject a cert whose SAN doesn't match
    the address in the URL bar even when the user would otherwise click
    through a self-signed warning."""
    try:
        s = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _tailscale_address() -> tuple[str | None, str | None]:
    """Discover a running tailnet without requiring it for LAN operation."""
    try:
        result = subprocess.run(["tailscale", "status", "--json"],
                                capture_output=True, text=True, timeout=3)
        if result.returncode:
            return None, None
        status = json.loads(result.stdout)
        if status.get("BackendState") != "Running":
            return None, None
        node = status.get("Self") or {}
        addresses = [str(ipaddress.ip_address(ip)) for ip in node.get("TailscaleIPs", [])]
        ipv4 = next((ip for ip in addresses if ":" not in ip), None)
        dns = node.get("DNSName", "").rstrip(".")
        # Only validated hostnames go into OpenSSL's extension expression.
        if not dns or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for c in dns):
            dns = None
        return ipv4, dns
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        return None, None


def _ensure_self_signed_cert() -> tuple[str, str] | None:
    """(Re)generates a self-signed TLS cert/key each daemon start, valid
    for this machine's current LAN and Tailscale addresses.

    Needed because `getUserMedia` (the mic) is refused outright, with no
    permission prompt at all, on anything but a "secure context" — https,
    or localhost — confirmed live: the phone got "microphone permission
    denied" without ever seeing Android/iOS's usual mic-access prompt,
    because a plain http://<lan-ip> page from another device is not a
    secure context in any mainstream mobile browser. A real CA-signed
    cert isn't an option for a bare LAN IP with no domain; a self-signed
    one is the standard fix for exactly this "local device, no domain"
    case — the browser still shows a one-time "connection isn't private"
    warning to click through (unavoidable without installing a CA on the
    phone, out of scope for a beta), but getUserMedia itself then works.

    The cert is reissued on every start rather than cached/reused: this
    machine's LAN IP can change (DHCP), and a cert whose SAN doesn't match
    the current URL's host is refused outright — cheap (<1s) to just always
    issue a fresh one for the IP detected right now. The key itself is kept
    once made, so an app that pins it (Flux's embedded Omarchy screen pins
    the public key on first use) keeps trusting the server across restarts
    and address changes.
    """
    if shutil.which("openssl") is None:
        log.warning("phone bridge: openssl not found, cannot generate a TLS cert — staying on plain HTTP (mic access will fail)")
        return None
    ip = _primary_lan_ip()
    tail_ip, tail_dns = _tailscale_address()
    names = ["IP:127.0.0.1", "DNS:localhost"]
    names += [f"IP:{address}" for address in dict.fromkeys([ip, tail_ip]) if address]
    if tail_dns:
        names.append(f"DNS:{tail_dns}")
    san = "subjectAltName=" + ",".join(names)
    _CERT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _CERT_DIR.chmod(0o700)
    except OSError:
        pass
    r = subprocess.run(
        [
            "openssl", "req", "-x509", "-nodes",
            *(["-key", str(_KEY_PATH)] if _KEY_PATH.is_file()
              else ["-newkey", "rsa:2048", "-keyout", str(_KEY_PATH)]),
            "-out", str(_CERT_PATH),
            "-days", "3650", "-subj", "/CN=omarchy-phone-bridge",
            "-addext", san,
        ],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0:
        log.warning("phone bridge: cert generation failed (%s), staying on plain HTTP", r.stderr.strip()[:300])
        return None
    try:
        _KEY_PATH.chmod(0o600)
    except OSError:
        pass
    return str(_CERT_PATH), str(_KEY_PATH)


def _read_api_key(config: Config) -> str:
    with open(config.api_key_path) as f:
        return f.read().strip()


def _relay_offer(config: Config, offer_sdp: str, conversation: str = "") -> str:
    """POSTs the browser's SDP offer to OpenAI (server-side, real key) and
    returns the answer SDP — same request shape as the desktop LiveSession,
    built from the same build_session_config so the two can't drift."""
    if config.provider == "gemini":
        from .gemini import relay_offer
        return relay_offer(config, offer_sdp, conversation)
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


def pending_approvals() -> dict:
    """Tasks waiting for approval, for the phone's approval card. Only what
    a human needs to decide: the request, its risk and reasons, and for a
    push/commit the change it would publish (runtime/service.py's
    change_preview, the same one the voice assistant explains from)."""
    from ..runtime.service import change_preview, get_runtime
    from ..runtime.task import WAITING_APPROVAL
    items = []
    get_runtime().tidy_approvals()  # never list one that expired or no longer needs asking
    for task in get_runtime().store.list(20):
        request = task.pending_approval
        if task.status != WAITING_APPROVAL or not request:
            continue
        items.append({"task_id": task.id, "goal": task.goal[:600], "fingerprint": request.get("fingerprint"),
                      "kind": request.get("kind"), "subject": str(request.get("subject", ""))[:2000],
                      "risk": request.get("risk"), "reasons": request.get("reasons", []),
                      "preview": change_preview(task.workspace)})
    # A tool the assistant wants to install waits for the user too (2026-09-30:
    # the user looked for it on the phone and it was only on the desktop).
    from ..execution import user_tools
    for request in user_tools.pending_install_requests():
        items.append({"task_id": f"{_TOOL_INSTALL}{request['id']}", "goal": f"Install the assistant tool {request['name']}",
                      "fingerprint": request["id"], "kind": "tool-install", "subject": f"install tool {request['name']}",
                      "risk": "ELEVATED", "reasons": [request["summary"][:300]], "preview": None})
    # A worker's question often is an approval in words ("approve sending this
    # email?", 2026-09-30), so the phone lists them beside the approvals.
    from ..runtime.task import WAITING_USER
    questions = [{"task_id": task.id, "goal": task.goal[:600], "question": str(task.question or "")[:3000]}
                 for task in get_runtime().store.list(20) if task.status == WAITING_USER and task.question]
    from ..execution import flux_approve
    return {"approvals": items, "questions": questions, "pin": approval_pin.status(),
            "fingerprint": bool(items) and flux_approve.available()}


def conversation_get(path: str, query: dict) -> tuple[int, dict]:
    """The phone's conversation list, one conversation, or the one a
    notification is about (core/conversations.py)."""
    from ..core import conversations
    if path == "/api/conversations":
        return 200, conversations.summaries()
    rest = path[len("/api/conversations/"):]
    if rest == "for-notification":
        cid = conversations.for_notification((query.get("title") or [""])[0])
        return (200, {"id": cid}) if cid else (404, {"error": "no conversation for that notification"})
    conv = conversations.get(rest)
    if conv is None:
        return 404, {"error": "no such conversation"}
    from ..execution import flux_approve
    return 200, {**conv, "fingerprint": flux_approve.available()}


def answer_question(data) -> tuple[int, dict]:
    """The paired phone answers a task's question (Yes, No, or its own words)."""
    from ..runtime.service import get_runtime
    if (not isinstance(data, dict) or not isinstance(data.get("task_id"), str)
            or not isinstance(data.get("answer"), str) or not data["answer"].strip()):
        return 400, {"ok": False, "message": "expected {task_id, answer}"}
    result = get_runtime().respond(data["task_id"], answer=data["answer"].strip()[:2000], channel="phone")
    log.info("phone bridge: task %s answered from the phone: %s", data["task_id"], result.get("message"))
    return (200 if result.get("ok") else 409), {"ok": bool(result.get("ok")), "message": result.get("message", "")}


def respond_approval(data: dict) -> tuple[int, dict]:
    """Approve (PIN required) or deny (no PIN: refusing can't do harm) the
    exact request the phone showed. A paired phone plus a PIN the model
    never sees is a human check like the desktop notification's button, so
    it may approve HIGH risk too; BLOCKED never reaches approval at all."""
    from ..runtime.service import get_runtime
    from ..runtime.task import WAITING_APPROVAL
    task_id, fingerprint, approve = data.get("task_id"), data.get("fingerprint"), data.get("approve")
    if not isinstance(task_id, str) or not task_id or not isinstance(approve, bool):
        return 400, {"ok": False, "message": "expected {task_id, fingerprint, approve, pin}"}
    if task_id.startswith(_TOOL_INSTALL):
        from ..execution import user_tools
        request = _install_request(task_id)
        if request is None:
            return 409, {"ok": False, "message": "that install is no longer waiting"}
        if approve:
            ok, reason = approval_pin.verify(data.get("pin") or "")
            if not ok:
                return 403, {"ok": False, "message": reason}
        user_tools.decide_install(request["id"], approve, f"{'approved' if approve else 'declined'} on the phone")
        return 200, {"ok": True, "message": "Installing it." if approve else "Not installing it."}
    runtime = get_runtime()
    task = runtime.store.load(task_id)
    if (not task or task.status != WAITING_APPROVAL
            or (task.pending_approval or {}).get("fingerprint") != fingerprint):
        return 409, {"ok": False, "message": "that request is no longer waiting (already answered or changed)"}
    if approve:
        ok, reason = approval_pin.verify(data.get("pin") or "")
        if not ok:
            log.warning("phone bridge: approval of task %s refused: %s", task_id, reason)
            return 403, {"ok": False, "message": reason}
    log.info("phone bridge: task %s %s from the phone", task_id, "approved" if approve else "denied")
    result = runtime.respond(task_id, approve=approve, channel="phone")
    return (200 if result.get("ok") else 409), {"ok": bool(result.get("ok")), "message": result.get("message", "")}


_TOOL_INSTALL = "tool-install:"


def _install_request(task_id: str) -> dict | None:
    from ..execution import user_tools
    rid = task_id[len(_TOOL_INSTALL):]
    return next((r for r in user_tools.pending_install_requests() if r["id"] == rid), None)


def _fingerprint_install(request: dict) -> None:
    """The fingerprint prompt for a waiting tool install; its signed answer decides it."""
    from ..execution import flux_approve, user_tools
    ok, reason = flux_approve.request(f"Omarchy AI: install the assistant tool {request['name']}")
    log.info("phone bridge: tool install %s: fingerprint %s", request["name"], reason)
    if ok is not None:
        user_tools.decide_install(request["id"], ok, f"{'approved' if ok else 'declined'} with a fingerprint on the phone")


def fingerprint_approval(data) -> tuple[int, dict]:
    """Send the Flux fingerprint prompt for the request the phone showed.
    Nothing is approved here: only the signed fingerprint answer approves."""
    from ..runtime.service import get_runtime
    if not isinstance(data, dict) or not isinstance(data.get("task_id"), str) or not data["task_id"]:
        return 400, {"ok": False, "message": "expected {task_id, fingerprint}"}
    if data["task_id"].startswith(_TOOL_INSTALL):
        from ..execution import flux_approve
        request = _install_request(data["task_id"])
        if request is None:
            return 409, {"ok": False, "message": "that install is no longer waiting"}
        if not flux_approve.available():
            return 409, {"ok": False, "message": "No phone is set up for fingerprint approval (Flux)."}
        threading.Thread(target=_fingerprint_install, args=(request,), daemon=True).start()
        return 200, {"ok": True, "message": "Sent the fingerprint prompt to the phone."}
    result = get_runtime().ask_fingerprint(data["task_id"], data.get("fingerprint") or None)
    return (200 if result.get("ok") else 409), {"ok": bool(result.get("ok")), "message": result.get("message", "")}


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

    def _send_file(self, path: Path, content_type: str, status: int = 200) -> None:
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            self._send_json(500, {"error": f"{path.name} missing"})
            return
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _is_paired(self) -> bool:
        raw_cookie = self.headers.get("Cookie", "")
        # Temporary diagnostic — real user report ("still says not
        # paired" right after a pairing the daemon's own log shows as
        # accepted) with no reproduction available locally (self-signed
        # cert / mobile browser cookie behavior isn't something this dev
        # machine can exercise the same way a phone does). Logging enough
        # to tell "no Cookie header at all" apart from "Cookie header
        # present but doesn't match any paired session" from "matches
        # fine" -- three different real causes, three different fixes.
        if not raw_cookie:
            log.info("phone bridge: GET %s from %s -- no Cookie header at all (UA: %s)",
                      self.path, self.address_string(), self.headers.get("User-Agent", "?"))
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw_cookie)
        except http.cookies.CookieError:
            log.info("phone bridge: GET %s from %s -- Cookie header present but unparseable: %r",
                      self.path, self.address_string(), raw_cookie)
            return False
        morsel = jar.get(_SESSION_COOKIE)
        if morsel is None:
            if raw_cookie:
                log.info("phone bridge: GET %s from %s -- Cookie header present but no %s in it: %r",
                          self.path, self.address_string(), _SESSION_COOKIE, raw_cookie)
            return False
        sessions = _load_sessions()
        if morsel.value not in sessions:
            log.info("phone bridge: GET %s from %s -- session id present but not in paired_sessions.json (%d paired)",
                      self.path, self.address_string(), len(sessions))
            return False
        presence.seen()
        return True

    def do_GET(self) -> None:  # noqa: N802 — stdlib method name
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        if path == "/pair":
            self._handle_pair(urllib.parse.parse_qs(parsed.query))
            return

        if path == "/api/conversations" or path.startswith("/api/conversations/"):
            if not self._is_paired():
                self._send_json(403, {"error": "not paired"})
                return
            self._send_json(*conversation_get(path, urllib.parse.parse_qs(parsed.query)))
            return

        if path == "/api/audio/status":
            if not self._is_paired():
                self._send_json(403, {"error": "not paired"})
                return
            self._send_json(200, {"available": cast_audio.available()})
            return

        if path == "/api/approvals":
            if not self._is_paired():
                self._send_json(403, {"error": "not paired"})
                return
            self._send_json(200, pending_approvals())
            return

        if path == "/mirror/status":
            if not self._is_paired():
                self._send_json(403, {"error": "not paired"}); return
            with _mirror_lock:
                self._send_json(200, {"on": _mirror_on, "started_at": _mirror_started_at})
            return

        if path == "/mirror/stream":
            if not self._is_paired():
                self._send_json(403, {"error": "not paired"}); return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            try:
                while True:
                    with _mirror_lock:
                        if not _mirror_on:
                            break
                    image = vision._capture()
                    if image is None:
                        time.sleep(.3); continue
                    body = image.read_bytes()
                    self.wfile.write(b"--frame\r\nContent-Type: image/png\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body + b"\r\n")
                    self.wfile.flush()
                    time.sleep(.15)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return

        if path == "/api/hello":
            # Unauthenticated on purpose and says nothing private: it lets
            # an app that already knows this computer (Flux, over its own
            # paired link) tell that Omarchy AI answers here before it
            # offers to open the page.
            self._send_json(200, {"service": "omarchy-ai"})
            return

        if path == "/api/wake" or path.startswith("/api/wake/"):
            # The desktop's wake word for a phone that listens for it itself
            # (wake_models.py). Paired phones only: it is the user's own model.
            if not self._is_paired():
                self._send_json(403, {"error": "not paired"})
                return
            try:
                if path == "/api/wake":
                    self._send_json(200, wake_models.describe(self.config))
                    return
                target = wake_models.files(self.config).get(path.removeprefix("/api/wake/").removesuffix(".onnx"))
            except (FileNotFoundError, ValueError) as exc:
                self._send_json(404, {"error": str(exc)})
                return
            if target is None or not target.is_file():
                self._send_json(404, {"error": "not found"})
                return
            self._send_file(target, "application/octet-stream")
            return

        if path == "/api/tvs":
            # For Flux's Mirror to TV: the TVs the desktop can cast to (the same
            # registry its TV overlay shows) and the one it mirrors to now.
            if not self._is_paired():
                self._send_json(403, {"error": "not paired"})
                return
            from ..display import registry, session
            self._send_json(200, {"tvs": registry.get_or_refresh(), "casting": session.target() if session.active() else None})
            return

        if path == "/api/flux/challenge":
            # Public: a nonce and this server's key pin, nothing private.
            if not _CERT_PATH.is_file():
                self._send_json(503, {"error": "no TLS certificate"})
                return
            self._send_json(200, {"nonce": flux_pairing.new_challenge(), "key": flux_pairing.key_pin(_CERT_PATH)})
            return

        if path == "/api/paired":
            # Polled by not_paired.html — a real, live confusion this
            # round: pairing had genuinely succeeded (confirmed in the
            # daemon's own log), but the browser tab/window being looked
            # at was a *different* one that never carried the cookie from
            # the actual /pair redirect, so it kept showing "not paired"
            # forever with nothing to make it re-check. This closes that
            # gap regardless of which specific tab ends up paired.
            self._send_json(200, {"paired": self._is_paired()})
            return

        if path in ("/", "/index.html"):
            if not self._is_paired():
                self._send_file(_STATIC_DIR / "not_paired.html", "text/html; charset=utf-8")
                return
            self._send_file(_STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return

        # A small allowlist of extra static assets (logo image, visualizer
        # JS/CSS pulled out of index.html) — resolved against _STATIC_DIR
        # and rejected if that resolution steps outside it, so a path like
        # /../../etc/passwd can't be used to read arbitrary files.
        candidate = (_STATIC_DIR / path.lstrip("/")).resolve()
        if candidate.is_file() and _STATIC_DIR in candidate.parents:
            content_type = {
                ".png": "image/png", ".svg": "image/svg+xml",
                ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
            }.get(candidate.suffix, "application/octet-stream")
            self._send_file(candidate, content_type)
            return

        self._send_json(404, {"error": "not found"})

    def _handle_flux_pair(self) -> None:
        """A phone that desktop Flux paired signs a challenge instead of
        scanning a QR code — see flux_pairing.py. Answers with the same
        session cookie a QR pairing sets."""
        # Reachable without pairing, so the body is capped before it is read.
        try:
            if int(self.headers.get("Content-Length", "0") or "0") > 8192:
                raise ValueError("body too large")
            data = self._read_json_body()
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except ValueError:
            self._send_json(400, {"error": "bad request"})
            return
        if _CERT_PATH.is_file() and flux_pairing.verify(
            data.get("device"), data.get("nonce"), data.get("signature"), flux_pairing.key_pin(_CERT_PATH),
        ):
            log.info("phone bridge: Flux-paired phone %s accepted from %s", data.get("device"), self.address_string())
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Set-Cookie", _new_session_cookie())
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        log.warning("phone bridge: Flux pairing refused from %s", self.address_string())
        self._send_json(403, {"error": "not a phone that Flux paired with this computer"})

    def _handle_pair(self, query: dict) -> None:
        token = (query.get("token") or [""])[0]
        try:
            with open(_PAIR_TOKEN_PATH) as f:
                pending = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pending = None

        # Deliberately idempotent within the TTL, not strictly single-use
        # (a real bug, confirmed live via journalctl: a phone's QR
        # scanner/browser fired the same link 3 times within 2 seconds --
        # common camera-app "tap to open" preview behavior -- and the
        # 2nd/3rd hits landed on pair_failed.html even though the 1st had
        # already succeeded, which is what the user actually saw). Doesn't
        # meaningfully weaken security: the token is still single-flight
        # in the sense that pressing "QR" again immediately invalidates it
        # (mint_pairing_token overwrites this same file), and it still
        # hard-expires after 5 minutes either way.
        valid = (
            pending is not None
            and token
            and secrets.compare_digest(pending.get("token", ""), token)
            and time.time() < pending.get("expires_at", 0)
        )
        if not valid:
            log.warning("phone bridge: pairing attempt rejected (bad/expired token) from %s", self.address_string())
            self._send_file(_STATIC_DIR / "pair_failed.html", "text/html; charset=utf-8", status=403)
            return

        if not pending.get("used"):
            pending["used"] = True
            _write_json_0600(_PAIR_TOKEN_PATH, pending)

        log.info("phone bridge: new pairing accepted from %s", self.address_string())
        self.send_response(302)
        self.send_header("Set-Cookie", _new_session_cookie())
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 — stdlib method name
        path = self.path.split("?", 1)[0]
        if path == "/api/flux/pair":
            # Unpaired by definition: this is how a Flux phone becomes paired.
            self._handle_flux_pair()
            return
        if path in ("/api/cast", "/api/cast/stop", "/api/ask", "/api/flux/setting"):
            if not self._is_paired():
                self._send_json(403, {"error": "not paired"})
                return
            try:
                if int(self.headers.get("Content-Length", "0") or "0") > 8192:
                    raise ValueError("body too large")
                data = self._read_json_body()
                if not isinstance(data, dict):
                    raise ValueError("not an object")
            except ValueError:
                self._send_json(400, {"ok": False, "message": "bad request"})
                return
            if path == "/api/flux/setting":
                # Flux's Omarchy button beside a feature that config.toml keeps
                # off: the press on the paired phone is the request (flux_settings.py).
                keys = data.get("keys")
                ok, message = flux_settings.enable(keys if isinstance(keys, list) else [])
                log.info("phone bridge: Flux setting %s: %s", keys, message)
                self._send_json(200 if ok else 409, {"ok": ok, "message": message})
                return
            if path == "/api/ask":
                # A Flux button hands Omarchy a job ("turn on remote input on
                # this computer"): the Task Runtime plans it, asks approval where
                # its permissions say so, and reports back like any other task.
                from ..runtime.service import start_task
                request = str(data.get("request") or "").strip()
                if not request:
                    self._send_json(400, {"ok": False, "message": "request is empty"})
                    return
                log.info("phone bridge: Flux asks Omarchy: %s", request[:200])
                result = start_task({"goal": request})
            else:
                target = str(data.get("target") or "").strip()
                result = run_action("stop_casting", {}) if path.endswith("/stop") else run_action("start_casting", {"target": target})
            self._send_json(200 if result.ok else 409, {"ok": result.ok, "message": result.message})
            return
        if path in ("/mirror/start", "/mirror/stop"):
            if not self._is_paired():
                self._send_json(403, {"error": "not paired"}); return
            if path == "/mirror/start":
                try: data = self._read_json_body()
                except (json.JSONDecodeError, ValueError): data = {}
                if data.get("consent") is not True:
                    self._send_json(400, {"error": "explicit consent is required"}); return
            global _mirror_on, _mirror_started_at
            with _mirror_lock:
                _mirror_on = path == "/mirror/start"
                _mirror_started_at = time.time() if _mirror_on else None
                self._send_json(200, {"on": _mirror_on, "started_at": _mirror_started_at})
            return
        if path == "/api/audio/pcm":
            if not self._is_paired():
                self.close_connection = True
                self._send_json(403, {"error": "not paired"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 8192 or length % 2:
                    raise ValueError("invalid PCM length")
                self.connection.settimeout(3)
                data = self.rfile.read(length)
                if len(data) != length:
                    raise ValueError("incomplete PCM")
                cast_audio.send_pcm(data)
                self._send_json(200, {"ok": True})
            except ValueError as e:
                self.close_connection = True
                self._send_json(400, {"error": str(e)})
            except OSError:
                self.close_connection = True
                self._send_json(409, {"error": "TV mirroring is not connected"})
            return
        if path == "/api/live/offer":
            if not self._is_paired():
                self._send_json(403, {"error": "not paired"})
                return
            self._handle_offer()
            return
        if path == "/api/approvals/respond":
            if not self._is_paired():
                self._send_json(403, {"error": "not paired"})
                return
            try:
                data = self._read_json_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json(400, {"ok": False, "message": "invalid JSON"})
                return
            self._send_json(*respond_approval(data if isinstance(data, dict) else {}))
            return
        if path.startswith("/api/conversations/") and path.endswith("/delete"):
            if not self._is_paired():
                self._send_json(403, {"error": "not paired"})
                return
            from ..core import conversations
            ok = conversations.delete(path[len("/api/conversations/"):-len("/delete")])
            self._send_json(200 if ok else 404, {"ok": ok})
            return
        if path == "/api/questions/answer":
            if not self._is_paired():
                self._send_json(403, {"error": "not paired"})
                return
            try:
                data = self._read_json_body()
            except (json.JSONDecodeError, ValueError):
                data = None
            self._send_json(*answer_question(data))
            return
        if path == "/api/approvals/fingerprint":
            # Flux's approvals list: Approve sends the fingerprint prompt, and
            # the signed answer (not this request) is what approves.
            if not self._is_paired():
                self._send_json(403, {"error": "not paired"})
                return
            try:
                data = self._read_json_body()
            except (json.JSONDecodeError, ValueError):
                data = None
            self._send_json(*fingerprint_approval(data))
            return
        if path == "/api/tool":
            if not self._is_paired():
                self._send_json(403, {"error": "not paired"})
                return
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
            # The call's conversation: the one the phone reopened, or a new one.
            from ..core import conversations
            source = "phone-text" if data.get("mode") == "text" else "phone-voice"
            conversation = conversations.continue_or_create(data.get("conversation_id"), source)
            log.info("phone bridge: creating %s session (conversation %s)", self.config.provider, conversation)
            answer_sdp = _relay_offer(self.config, offer_sdp, conversation)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            log.error("phone bridge: session creation failed: HTTP %s %s", e.code, detail)
            from ..core import quota
            if quota.is_quota_error(detail, e.code):
                quota.report("openai", f"phone: HTTP {e.code} {detail}", user_initiated=True)
            self._send_json(502, {"error": f"OpenAI session creation failed: HTTP {e.code}"})
            return
        except Exception:  # noqa: BLE001
            log.exception("phone bridge: offer relay failed")
            self._send_json(500, {"error": "offer relay failed"})
            return
        self._send_json(200, {"sdp": answer_sdp, "conversation_id": conversation})

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
            # Paired phone only (checked before _handle_tool): the one caller
            # allowed to unlock; run_action refuses it for everyone else.
            result = unlock_screen_for_paired_phone() if name == "unlock_screen" else run_action(name, args)
        except Exception:  # noqa: BLE001
            log.exception("phone bridge: tool call %s failed", name)
            self._send_json(200, {"ok": False, "message": f"{name} failed unexpectedly"})
            return
        log.info("phone bridge tool result: ok=%s message=%r", result.ok, result.message)
        self._send_json(200, {"ok": result.ok, "message": result.message})


class PhoneHTTPServer(ThreadingHTTPServer):
    """Keep slow TLS clients out of the single server accept loop."""

    request_queue_size = 32
    tls_context: ssl.SSLContext | None = None
    handshake_timeout = 5
    request_timeout = 30

    def handle_error(self, request, client_address):
        # A phone that closes the page mid-request (Flux's screen closing, a
        # dropped Wi-Fi link) resets its sockets; that is not a server fault,
        # so it gets no traceback. Anything else still does.
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, ssl.SSLEOFError)):
            return
        super().handle_error(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            request.settimeout(self.handshake_timeout)
            if self.tls_context is not None:
                request = self.tls_context.wrap_socket(request, server_side=True)
            request.settimeout(self.request_timeout)
        except OSError:
            self.shutdown_request(request)
            return
        super().process_request_thread(request, client_address)


def start(config: Config) -> ThreadingHTTPServer | None:
    """Starts the phone bridge in a background thread. Returns the server
    (call .shutdown() to stop it) or None if it's disabled."""
    if not config.phone_bridge_enabled:
        return None
    _Handler.config = config
    server = PhoneHTTPServer(("0.0.0.0", config.phone_bridge_port), _Handler)

    cert = _ensure_self_signed_cert()
    scheme = "http"
    if cert is not None:
        cert_path, key_path = cert
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
        # Wrapping the listening socket performs TLS negotiation in accept(),
        # before ThreadingMixIn can start a worker. Idle phone preconnections
        # then freeze every new request indefinitely. Wrap accepted sockets
        # inside their worker instead, with a bounded handshake timeout.
        server.tls_context = ctx
        scheme = "https"

    presence.start()
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="phone-bridge")
    thread.start()
    ip = _primary_lan_ip() or "0.0.0.0"
    log.info(
        "phone bridge listening on %s://%s:%d (%d phone(s) currently paired)%s",
        scheme, ip, config.phone_bridge_port, paired_count(),
        "" if cert else " -- PLAIN HTTP, mic access will not work on a phone",
    )
    return server
