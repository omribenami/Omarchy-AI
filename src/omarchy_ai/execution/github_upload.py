"""Upload a file to GitHub as an attachment and return its URL.

A video that plays in a README is a GitHub attachment
(https://github.com/user-attachments/assets/<id>), never a repo file:
GitHub strips `<video src=...>` and `![](x.mp4)` that point into the repo
(checked 2026-09-27, see knowledge/arch-operations.md). git and the gh CLI
cannot create an attachment; only the web editor's upload does. On
2026-09-27 the assistant and five Task Runtime runs failed at exactly this
step: they searched the repo for "omarchy.mp4", rewrote the README with repo
paths, and one copied the mp4 into the repo root to push it.

This drives the assistant's own signed-in Chromium (the dedicated browser,
CDP on port 9229) in a throwaway tab: the repo's new-issue form, whose
comment box uploads a dropped/chosen file and inserts its attachment URL.
The issue is never submitted. The file is read from disk by the browser
itself and pasted into the comment box (see _upload), so no OS dialog opens
and nothing is typed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from .actions import ActionResult

log = logging.getLogger(__name__)

ENDPOINT = "http://127.0.0.1:9229"
ATTACHMENT = re.compile(r"https://github\.com/user-attachments/(?:assets|files)/[0-9A-Za-z./_-]+")
_ADD_FILES = re.compile(r"click to add files|attach files", re.I)  # present once the form is ready
UPLOAD_SECONDS = 300
_ERROR_TEXT = re.compile(r"(too big|too large|exceeds|not supported|try again|failed to upload|something went wrong)",
                         re.I)


def repo_slug(repo: str) -> str | None:
    """'owner/name' from 'owner/name', a github.com URL, or a local checkout."""
    repo = str(repo or "").strip()
    path = Path(repo).expanduser()
    if repo and path.is_dir():
        try:
            repo = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"], capture_output=True,
                                  text=True, timeout=5, check=False).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return None
    m = re.search(r"(?:github\.com[:/])?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", repo)
    return f"{m.group(1)}/{m.group(2)}" if m else None


class _Cdp:
    def __init__(self, ws):
        self.ws, self.n, self.events = ws, 0, []

    async def call(self, method: str, params: dict | None = None, timeout: float = 20) -> dict:
        self.n += 1
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), max(0.1, deadline - time.monotonic())))
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result") or {}
            if msg.get("method"):
                self.events.append(msg)

    async def event(self, method: str, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            for i, e in enumerate(self.events):
                if e["method"] == method:
                    return self.events.pop(i)["params"]
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), max(0.1, deadline - time.monotonic())))
            if msg.get("method"):
                self.events.append(msg)

    async def js(self, expression: str):
        r = await self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        return (r.get("result") or {}).get("value")


async def _upload(ws_url: str, path: str) -> str:
    import websockets
    async with websockets.connect(ws_url, max_size=2 ** 24, open_timeout=5) as ws:
        cdp = _Cdp(ws)
        await cdp.call("Page.enable")
        for _ in range(40):  # the form renders client-side
            await asyncio.sleep(0.5)
            state = await cdp.js("JSON.stringify({user: document.querySelector('meta[name=\"user-login\"]')?.content "
                                 "|| '', ready: [...document.querySelectorAll('button,[role=button]')].some(b => "
                                 f"/{_ADD_FILES.pattern}/i.test(b.innerText||''))}})")
            state = json.loads(state or "{}")
            if state.get("ready"):
                break
        else:
            raise RuntimeError("GitHub's comment box did not load")
        if not state.get("user"):
            raise RuntimeError("the assistant browser is not signed in to GitHub (sign in there first)")
        # The form's own file chooser never reached CDP's interception (its
        # React input is created on click; a trusted CDP click opened nothing,
        # tried 2026-09-27). Instead: a hidden input of our own gets the real
        # file from disk (DOM.setFileInputFiles needs no gesture), and that File
        # is pasted into the comment box -- the same as pasting a file by hand.
        await cdp.call("DOM.enable")
        await cdp.js("(() => { const i = document.createElement('input'); i.type = 'file'; i.id = 'oai-upload'; "
                     "i.style.display = 'none'; document.body.appendChild(i) })()")
        root = await cdp.call("DOM.getDocument", {"depth": 0})
        node = await cdp.call("DOM.querySelector", {"nodeId": root["root"]["nodeId"], "selector": "#oai-upload"})
        await cdp.call("DOM.setFileInputFiles", {"files": [path], "nodeId": node["nodeId"]})
        await cdp.js("(() => { const f = document.getElementById('oai-upload').files[0]; const dt = new DataTransfer(); "
                     "dt.items.add(f); const t = document.querySelector('textarea'); t.focus(); "
                     "t.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true})) "
                     "})()")
        deadline = time.monotonic() + UPLOAD_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            value = await cdp.js("document.querySelector('textarea')?.value || ''") or ""
            found = ATTACHMENT.search(value)
            if found:
                return found.group(0).rstrip(").")
            alert = await cdp.js("[...document.querySelectorAll('[role=alert],.flash-error,[class*=error]')]"
                                 ".map(e => e.innerText).join(' ').slice(0, 300)") or ""
            if _ERROR_TEXT.search(alert):
                raise RuntimeError(f"GitHub refused the upload: {alert.strip()}")
        raise RuntimeError(f"no attachment URL after {UPLOAD_SECONDS}s (the upload may still be running)")


def _describe(exc: Exception) -> str:
    return str(exc) or type(exc).__name__


def upload(path: str, repo: str) -> ActionResult:
    file = Path(str(path or "")).expanduser()
    if not file.is_file():
        return ActionResult(False, f"no such file: {file}")
    slug = repo_slug(repo)
    if not slug:
        return ActionResult(False, "repo must be owner/name, a github.com URL or a local checkout with an origin")
    try:
        from .browser_jev import _ensure_dedicated_browser
        _ensure_dedicated_browser()
        req = urllib.request.Request(f"{ENDPOINT}/json/new?https://github.com/{slug}/issues/new", method="PUT")
        tab = json.loads(urllib.request.urlopen(req, timeout=10).read())
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"could not open the assistant browser: {exc}")
    try:
        url = asyncio.run(_upload(tab["webSocketDebuggerUrl"], str(file.resolve())))
    except Exception as exc:  # noqa: BLE001
        log.warning("GitHub upload of %s failed: %s", file, exc)
        return ActionResult(False, f"upload failed: {_describe(exc)}")
    finally:
        try:  # the draft issue is never submitted
            urllib.request.urlopen(f"{ENDPOINT}/json/close/{tab['id']}", timeout=5)
        except Exception:  # noqa: BLE001
            pass
    log.info("GitHub upload: %s -> %s", file, url)
    return ActionResult(True, json.dumps({
        "url": url, "file": str(file),
        "note": ("Put this URL on its own line in the README (a bare line plays as a video) in place of the old "
                 "one, commit and push; do not add the file to the repo."),
    }))


def main(argv: list[str] | None = None) -> int:
    """`python -m omarchy_ai.execution.github_upload <file> <repo>`: for the
    Task Runtime's workers, which run shell commands. Prints the URL."""
    import sys
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: python -m omarchy_ai.execution.github_upload <file> <owner/repo | checkout dir>", file=sys.stderr)
        return 2
    result = upload(*args)
    print(json.loads(result.message)["url"] if result.ok else result.message, file=sys.stdout if result.ok else sys.stderr)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
