"""A local fake of the MyApi endpoints omarchy_ai.myapi.client calls.

It listens on 127.0.0.1 on an ephemeral port, records every request as it
arrives (before answering, so a client that dies mid-call still leaves a
record), and answers each one by the last matching rule: succeed, fail
or hang. Signatures are not checked.

    with FakeMyApi() as fake, fake.patch():
        client = MyApiClient(); client._identity = fake_identity()
        client.call_service("gmail", "/gmail/v1/users/me/messages/send", "POST", body={...})
        assert len(fake.writes) == 1

A request is a write when it can change something: POST .../execute,
POST .../proxy whose proxied method is not GET, and any PUT/PATCH/DELETE.
"""
from __future__ import annotations

import base64
import json
import secrets
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

API_PREFIX = "/api/v1"


def fake_identity() -> dict:
    """An identity file's contents with a key made now (never a stored secret)."""
    key = Ed25519PrivateKey.generate()
    return {
        "private_key": base64.b64encode(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode(),
        "public_key": base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode(),
        "key_fingerprint": secrets.token_hex(8),
        "scope": "full",
        "account": None,
        "connected_at": 0,
    }


@dataclass
class Received:
    method: str
    path: str                      # without the /api/v1 prefix
    query: dict
    headers: dict
    body: object
    service: str | None = None
    write: bool = False

    @property
    def idempotency_key(self) -> str | None:
        return next((v for k, v in self.headers.items() if k.lower() == "idempotency-key"), None)


@dataclass
class _Rule:
    match: str | None
    mode: str                      # succeed | fail | hang
    status: int = 200
    payload: object = None
    released: threading.Event = field(default_factory=threading.Event)


def _is_write(method: str, path: str, body: object) -> bool:
    parts = path.strip("/").split("/")
    if method in ("PUT", "PATCH", "DELETE"):
        return True
    if method != "POST" or len(parts) != 3 or parts[0] != "services":
        return False
    if parts[2] == "execute":
        return True
    if parts[2] == "proxy":
        return str((body or {}).get("method") or "GET").upper() != "GET" if isinstance(body, dict) else True
    return False


def _default_payload(method: str, path: str) -> object:
    parts = path.strip("/").split("/")
    if method == "GET" and parts == ["services"]:
        return {"data": [{"id": "gmail", "name": "gmail", "category": "email", "status": "connected"}]}
    if method == "GET" and len(parts) == 3 and parts[0] == "services" and parts[2] == "methods":
        return {"data": []}
    return {"ok": True, "data": {}}


class FakeMyApi:
    def __init__(self) -> None:
        self.requests: list[Received] = []
        self._rules: list[_Rule] = []
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                fake._serve(self)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-myapi", daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}{API_PREFIX}"

    @property
    def writes(self) -> list[Received]:
        with self._lock:
            return [r for r in self.requests if r.write]

    # -- behaviour ---------------------------------------------------------

    def succeed(self, payload: object = None, match: str | None = None) -> None:
        """Answer 200 with payload (default: a plausible empty answer). match is a path substring."""
        self._add(_Rule(match, "succeed", 200, payload))

    def fail(self, status: int = 500, payload: object = None, match: str | None = None) -> None:
        self._add(_Rule(match, "fail", status, payload if payload is not None else {"error": f"fake HTTP {status}"}))

    def hang(self, match: str | None = None) -> None:
        """Hold matching requests open, unanswered, until release() or close()."""
        self._add(_Rule(match, "hang"))

    def release(self) -> None:
        """Let every hung request go on to the next rule below its hang."""
        with self._lock:
            for rule in self._rules:
                rule.released.set()
            self._rules = [r for r in self._rules if r.mode != "hang"]

    def reset(self) -> None:
        self.release()
        with self._lock:
            self._rules.clear()
            self.requests.clear()

    def patch(self):
        """Point omarchy_ai.myapi.client at this server in the current process."""
        return mock.patch("omarchy_ai.myapi.client.BASE_URL", self.url)

    def close(self) -> None:
        self.release()
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> "FakeMyApi":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- serving -------------------------------------------------------------

    def _add(self, rule: _Rule) -> None:
        with self._lock:
            self._rules.append(rule)

    def _rule_for(self, path: str) -> _Rule | None:
        with self._lock:
            return next((r for r in reversed(self._rules) if r.match is None or r.match in path), None)

    def _serve(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urllib.parse.urlsplit(handler.path)
        path = parsed.path[len(API_PREFIX):] if parsed.path.startswith(API_PREFIX) else parsed.path
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = raw.decode(errors="replace")
        parts = path.strip("/").split("/")
        method = handler.command
        record = Received(method, path, dict(urllib.parse.parse_qsl(parsed.query)), dict(handler.headers.items()),
                          body, parts[1] if len(parts) > 1 and parts[0] == "services" else None,
                          _is_write(method, path, body))
        with self._lock:
            self.requests.append(record)
        rule = self._rule_for(path)
        while rule is not None and rule.mode == "hang":
            rule.released.wait()
            rule = self._rule_for(path)
        status = rule.status if rule else 200
        payload = rule.payload if rule and rule.payload is not None else _default_payload(method, path)
        data = json.dumps(payload).encode()
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
        except OSError:
            pass  # the client gave up (timed out or died) while we hung
