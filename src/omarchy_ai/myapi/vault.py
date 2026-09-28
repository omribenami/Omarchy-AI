"""The MyApi token vault, for tasks and user tools -- never for the model.

2026-09-28: asked to use the Home Assistant token from the vault, she could
not: Omarchy AI had no vault access at all (only connected services), and a
task worked around it by borrowing the Home Assistant owner's login session.

- `labels()` is what the model may see: names, services and URLs, no values.
- `get(name)` returns a value for a program (a user tool's `run`, a task
  command through `omarchy-ai-vault get NAME`). Every value handed out is
  recorded by id (never by value) in the private runtime dir; the harness
  re-reads those values and masks them in command output, task files, the
  agenda and logs (execution/passwords.py `known_secrets`), so a worker that
  prints one does not put it in front of a model or on disk.

MyApi quirk (confirmed 2026-09-28): /vault/tokens lists tokens from every
workspace of the account, but /vault/tokens/{id}/reveal only finds tokens in
the workspace this client's identity belongs to ("Token not found").
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


def get(name: str, client: MyApiClient | None = None) -> str:
    client = client or MyApiClient()
    token = find(name, client)
    cached = _cache.get(token["id"])
    if cached and time.time() - cached[0] < _CACHE_SECONDS:
        return cached[1]
    try:
        data = client.request("GET", f"/vault/tokens/{token['id']}/reveal")
    except MyApiError as exc:
        if "not found" in str(exc).lower():
            where = token.get("workspaceId")
            try:
                where = next((w.get("name") for w in client.request("GET", "/workspaces").get("workspaces", [])
                              if w.get("id") == where), where)
            except MyApiError:
                pass
            raise MyApiError(f"the vault token {token.get('label')!r} is in the MyApi workspace {where!r}, which Omarchy "
                             "AI's MyApi login cannot read; move it to Omarchy AI's workspace at myapiai.com") from exc
        raise
    value = (data.get("data") or data).get("token")
    if not value:
        raise MyApiError(f"MyApi returned no value for {token.get('label')!r}")
    _cache[token["id"]] = (time.time(), value)
    _record(token["id"])
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
    """`omarchy-ai-vault list` | `omarchy-ai-vault get NAME` (value on stdout,
    for a program: `curl -H "Authorization: Bearer $(omarchy-ai-vault get
    'home assistant')" ...`)."""
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv[:1] == ["list"]:
            print(json.dumps(labels(), ensure_ascii=False, indent=1))
            return 0
        if argv[:1] == ["get"] and len(argv) == 2:
            sys.stdout.write(get(argv[1]))
            return 0
    except MyApiError as exc:
        print(f"omarchy-ai-vault: {exc}", file=sys.stderr)
        return 1
    print("usage: omarchy-ai-vault list | get NAME", file=sys.stderr)
    return 2
