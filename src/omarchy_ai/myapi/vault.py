"""The MyApi token vault, for tasks and user tools -- never for the model.

2026-09-28: asked to use a token from the vault, she could not: Omarchy AI
had no vault access at all (only connected services), and a task worked
around it by borrowing another service's login session.

- `labels()` is what the model may see: names, services and URLs, no values.
- `get(name)` returns a value for a program (a user tool's `run`, a task
  command through `omarchy-ai-vault get NAME`). Every value handed out is
  recorded by id (never by value) in the private runtime dir; the harness
  re-reads those values and masks them in command output, task files, the
  agenda and logs (execution/passwords.py `known_secrets`), so a worker that
  prints one does not put it in front of a model or on disk.

MyApi (2026-09-28): reveal only searched the active workspace while the list
showed all of them; fixed in MyApi d1140eb3. Reveal still answers "Token not
found" for a stored value it cannot decrypt.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .client import MyApiClient, MyApiError

_CACHE_SECONDS = 300
_cache: dict[str, tuple[float, str]] = {}
# GNOME Keyring cache across processes (a user tool is a new process per call):
# 2026-09-28 a user tool's call took 2.7 s, 0.8 s of it the MyApi round
# trip, against 11 ms for the service it called. Refreshed after this long, or
# at once with `get --fresh` (the tool does that when a service answers 401).
KEYRING_SECONDS = 12 * 3600
_KEYRING = ("application", "omarchy-ai", "purpose", "vault-cache")


def _keyring_get(name: str) -> str | None:
    import subprocess
    try:
        out = subprocess.run(["secret-tool", "search", "--unlock", *_KEYRING, "name", _norm(name)],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    at = re.search(r"^attribute\.at = (\d+)$", out.stderr + out.stdout, re.M)  # secret-tool prints attributes on stderr
    secret = re.search(r"^secret = (.*)$", out.stdout, re.M)
    if not at or not secret or time.time() - int(at[1]) > KEYRING_SECONDS:
        return None
    return secret[1]


def _keyring_put(name: str, value: str) -> None:
    import subprocess
    try:
        subprocess.run(["secret-tool", "clear", *_KEYRING, "name", _norm(name)], capture_output=True, timeout=5)
        subprocess.run(["secret-tool", "store", "--label", f"Omarchy AI vault cache: {name}", *_KEYRING,
                        "name", _norm(name), "at", str(int(time.time()))], input=value, capture_output=True,
                       text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _revealed_path() -> Path:
    from ..config import RUNTIME_DIR
    return Path(RUNTIME_DIR) / "vault_revealed.json"


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def tokens(client: MyApiClient | None = None) -> list[dict]:
    data = (client or MyApiClient()).request("GET", "/vault/tokens")
    return data.get("data") or data.get("tokens") or []


def labels(client: MyApiClient | None = None) -> list[dict]:
    """Safe for the model: no values, no previews."""
    client = client or MyApiClient()
    names = {}
    try:
        names = {w["id"]: w.get("name") for w in client.request("GET", "/workspaces").get("workspaces", [])}
    except MyApiError:
        pass
    return [{"name": t.get("label") or t.get("name"), "service": t.get("service"),
             "url": t.get("discoveredApiUrl") or t.get("websiteUrl"),
             "workspace": names.get(t.get("workspaceId"), t.get("workspaceId"))} for t in tokens(client)]


def find(name: str, client: MyApiClient | None = None) -> dict:
    wanted = _norm(name)
    items = tokens(client)
    exact = [t for t in items if t.get("id") == name or _norm(t.get("label")) == wanted or _norm(t.get("name")) == wanted]
    loose = exact or [t for t in items if wanted and (wanted in _norm(t.get("label")) or wanted in _norm(t.get("service")))]
    if not loose:
        raise MyApiError(f"no vault token matches {name!r}; available: "
                         + ", ".join(sorted({str(t.get('label')) for t in items})))
    if len(loose) > 1 and not exact:
        raise MyApiError(f"{name!r} matches several vault tokens: " + ", ".join(str(t.get("label")) for t in loose))
    return loose[0]


def get(name: str, client: MyApiClient | None = None, *, fresh: bool = False) -> str:
    if not fresh and not name.startswith("vt_"):
        cached = _keyring_get(name)
        if cached:
            return cached
    client = client or MyApiClient()
    token = find(name, client)
    cached = _cache.get(token["id"])
    if cached and time.time() - cached[0] < _CACHE_SECONDS:
        return cached[1]
    try:
        data = client.request("GET", f"/vault/tokens/{token['id']}/reveal")
    except MyApiError as exc:
        if "not found" in str(exc).lower():
            # MyApi answers "Token not found" both for a missing row and for a
            # stored value it cannot decrypt (2026-09-28: an older token
            # failed while a new token in the same workspace
            # revealed fine, after the workspace fix was deployed).
            raise MyApiError(f"MyApi listed the vault token {token.get('label')!r} but cannot return its value "
                             "(its stored value cannot be decrypted): delete it and add it again at myapiai.com") from exc
        raise
    value = (data.get("data") or data).get("token")
    if not value:
        raise MyApiError(f"MyApi returned no value for {token.get('label')!r}")
    _cache[token["id"]] = (time.time(), value)
    _record(token["id"])
    if not name.startswith("vt_"):
        _keyring_put(name, value)
    return value


def _record(token_id: str) -> None:
    path = _revealed_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        ids = set(json.loads(path.read_text())) if path.exists() else set()
        if token_id not in ids:
            ids.add(token_id)
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(sorted(ids), f)
            os.replace(tmp, path)
    except (OSError, ValueError):
        pass


def revealed_values() -> list[str]:
    """Values handed out on this machine (by any process), for masking."""
    try:
        ids = json.loads(_revealed_path().read_text())
    except (OSError, ValueError):
        return []
    values = []
    for token_id in ids:
        try:
            values.append(get(token_id))
        except MyApiError:
            continue
    return values


def main(argv: list[str] | None = None) -> int:
    """`omarchy-ai-vault list` | `omarchy-ai-vault get [--fresh] NAME` (value on stdout,
    for a program: `curl -H "Authorization: Bearer $(omarchy-ai-vault get
    'github')" ...`)."""
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv[:1] == ["list"]:
            print(json.dumps(labels(), ensure_ascii=False, indent=1))
            return 0
        if argv[:1] == ["get"] and len(argv) == 2:
            sys.stdout.write(get(argv[1]))
            return 0
        if argv[:2] == ["get", "--fresh"] and len(argv) == 3:
            sys.stdout.write(get(argv[2], fresh=True))
            return 0
    except MyApiError as exc:
        print(f"omarchy-ai-vault: {exc}", file=sys.stderr)
        return 1
    print("usage: omarchy-ai-vault list | get [--fresh] NAME", file=sys.stderr)
    return 2
