"""MyApi (myapiai.com) client — the ASC (Agentic Secure Connection) Quick
Connect flow.

No bearer token is ever stored. Connecting (`enroll`) mints a local
Ed25519 keypair once and exchanges a short-lived, single-use code (minted
on the user's MyApi dashboard) for approval; every request after that is
signed with the private key rather than authenticated with a stored
credential — headers X-Agent-PublicKey/X-Agent-Signature/X-Agent-Timestamp,
signed message f"{timestamp}:{key_fingerprint}". This is MyApi's own
documented pattern for third-party agents (reverse-engineered from its
`agentic.js` source and its reference `myapi-asc-mcp` client), not
something invented here.

Requires MyApi's Pro/Heavy/Enterprise plan (enforced on MyApi's side, not
checked here) — enroll() surfaces whatever error MyApi returns if the
account isn't eligible.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from ..config import MYAPI_IDENTITY_PATH

log = logging.getLogger("omarchy_ai.myapi")

BASE_URL = "https://www.myapiai.com/api/v1"
_TIMEOUT = 15
# myapiai.com sits behind Cloudflare, which blocks urllib's default
# "Python-urllib/x.y" User-Agent outright (HTTP 403, body "error code:
# 1010") before the request ever reaches the app -- confirmed live by
# comparing a raw urllib request (403, opaque Cloudflare body) against the
# identical request with a real User-Agent set (reaches the app, returns
# real JSON). Any non-default UA works; this one identifies the project.
_USER_AGENT = "omarchy-ai/0.1 (+https://github.com/omribenami/Omarchy-AI)"


class MyApiError(Exception):
    """Carries a human-readable message; reconnect_needed is set when the
    identity was rejected server-side (401/403 — e.g. revoked from the
    MyApi dashboard), so callers can tell 'try again later' apart from
    'reconnect from the settings panel'."""

    def __init__(self, message: str, *, reconnect_needed: bool = False, details: object = None) -> None:
        super().__init__(message)
        self.reconnect_needed = reconnect_needed
        self.details = details


def _write_json_0600(path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with 0600 from the start, same idiom as cli/settings.py's
    # API-key write — no window where this sits on disk world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
    except Exception:
        os.close(fd)
        raise
    os.chmod(path, 0o600)


def is_connected() -> bool:
    """Cheap local existence check only — no network call. Called once per
    voice session (voice/live.py's build_session_config), so it has to
    stay fast regardless of MyApi's own reachability."""
    return MYAPI_IDENTITY_PATH.exists()


def disconnect() -> None:
    """Local-only: deletes the identity file so Omarchy AI stops using it.
    MyApi has no documented programmatic revoke for an ASC device — fully
    cutting access also means removing the device from the MyApi
    dashboard, which the settings panel's copy says explicitly rather than
    implying this alone is enough."""
    MYAPI_IDENTITY_PATH.unlink(missing_ok=True)


def _load_identity() -> dict | None:
    if not MYAPI_IDENTITY_PATH.exists():
        return None
    try:
        with open(MYAPI_IDENTITY_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        log.warning("myapi identity file unreadable", exc_info=True)
        return None


class MyApiClient:
    """Identity is re-read from disk per instance rather than cached
    process-wide: connect-myapi/disconnect-myapi run in a separate,
    short-lived CLI process from the daemon that actually makes calls, so
    there's no shared in-memory state to keep consistent."""

    def __init__(self) -> None:
        self._identity = _load_identity()

    @property
    def connected(self) -> bool:
        return self._identity is not None

    @property
    def scope(self) -> str | None:
        return (self._identity or {}).get("scope")

    @property
    def account(self) -> str | None:
        return (self._identity or {}).get("account")

    # -- connecting ----------------------------------------------------

    def enroll(self, code: str) -> dict:
        """Exchange a one-time Quick Connect code for an approved
        identity. Public, unauthenticated endpoint on MyApi's side —
        minting the code on the dashboard *is* the approval, there's no
        separate human click after this call."""
        code = code.strip()
        if not code:
            raise MyApiError("no code given")

        private_key = Ed25519PrivateKey.generate()
        public_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        private_bytes = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        public_b64 = base64.b64encode(public_bytes).decode()

        resp = self._raw_request(
            "POST", "/agentic/asc/enroll",
            body={"code": code, "public_key": public_b64},
            signed=False,
        )
        if resp.get("status") != "approved":
            raise MyApiError(resp.get("message") or resp.get("error") or f"enroll failed: {resp}")

        identity = {
            "private_key": base64.b64encode(private_bytes).decode(),
            "public_key": public_b64,
            "key_fingerprint": resp.get("key_fingerprint"),
            "scope": resp.get("scope"),
            # Best-effort — not confirmed present on every account/plan;
            # callers must tolerate None.
            "account": resp.get("account") or resp.get("account_label"),
            "connected_at": time.time(),
        }
        _write_json_0600(MYAPI_IDENTITY_PATH, identity)
        self._identity = identity
        return identity

    # -- signed requests -------------------------------------------------

    def _sign_headers(self) -> dict:
        identity = self._identity
        if identity is None:
            raise MyApiError("not connected")
        private_key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(identity["private_key"]))
        timestamp = str(int(time.time()))
        message = f"{timestamp}:{identity['key_fingerprint']}".encode()
        signature = private_key.sign(message)
        return {
            "X-Agent-PublicKey": identity["public_key"],
            "X-Agent-Signature": base64.b64encode(signature).decode(),
            "X-Agent-Timestamp": timestamp,
        }

    def _raw_request(
        self, method: str, path: str, *,
        query: dict | None = None, body: object | None = None, signed: bool = True,
    ) -> dict:
        url = BASE_URL + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        }
        if signed:
            headers.update(self._sign_headers())
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {}
            message = parsed.get("message") or parsed.get("error") or f"HTTP {e.code}"
            data_error = parsed.get("data", {}).get("error", {}) if isinstance(parsed.get("data"), dict) else {}
            if isinstance(data_error, dict) and data_error.get("errors"):
                message = f"{message}: " + "; ".join(map(str, data_error["errors"]))
            raise MyApiError(message, reconnect_needed=e.code in (401, 403), details=parsed) from e
        except urllib.error.URLError as e:
            raise MyApiError(f"could not reach myapiai.com: {e.reason}") from e
        if not raw:
            return {}
        try:
            result = json.loads(raw)
            if isinstance(result, dict) and (result.get("ok") is False or result.get("success") is False):
                raise MyApiError(str(result.get("error") or result.get("message") or "provider request failed"), details=result)
            return result
        except json.JSONDecodeError:
            return {"raw": raw.decode(errors="replace")}

    def request(self, method: str, path: str, *, query: dict | None = None, body: object | None = None) -> dict:
        return self._raw_request(method, path, query=query, body=body, signed=True)

    # -- convenience wrappers over the service gateway --------------------

    def list_services(self) -> dict:
        return self.request("GET", "/services")

    def service_methods(self, service: str) -> dict:
        return self.request("GET", f"/services/{service}/methods")

    def call_service(
        self, service: str, path: str, method: str = "GET",
        query: dict | None = None, body: object | None = None,
    ) -> dict:
        # REST query parameters travel as text. Composio's proxy rejects
        # numeric/bool values instead of applying normal URL encoding.
        parameters = {}
        for key, value in (query or {}).items():
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                raise MyApiError(f"query parameter {key!r} must be a string, number, or boolean")
            parameters[key] = str(value).lower() if isinstance(value, bool) else str(value)
        payload = {"path": path, "method": method, "query": parameters}
        if body is not None:
            payload["body"] = body
        return self.request("POST", f"/services/{service}/proxy", body=payload)
