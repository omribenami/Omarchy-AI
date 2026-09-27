"""Read-only look at the assistant browser's task tab, and why a task stopped.

Real failure, 2026-09-26 22:56: asked to open an issue on Omarchy's GitHub,
browser_task clicked "New issue" three times and stopped with "Stopped
repeated interaction with New issue". The tab it left open showed the cause
at a glance -- the dedicated browser profile was signed out of GitHub, and
the button does nothing while signed out -- but nothing looked. Asked "try
to understand why you failed", she called no tool at all.

inspect() reads the tab over the dedicated Chromium's DevTools port (the
same one browser_jev drives) and never clicks, types or navigates.
diagnose() turns what is on the page into a cause she can say, from
evidence only: a sign-in wall, a CAPTCHA, an open dialog, an error banner,
a disabled control.
"""
from __future__ import annotations

import asyncio
import json
import re
import urllib.request
from urllib.parse import urlparse

from .browser_jev import _TARGET_FILE

CDP_PORT = 9229

_PAGE_STATE = r"""(() => {
  const vis = e => !!(e && (e.offsetWidth || e.offsetHeight || e.getClientRects().length));
  const text = e => (e.innerText || e.textContent || e.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
  const all = sel => [...document.querySelectorAll(sel)].filter(vis);
  const login = document.querySelector('meta[name="user-login"]');
  const signInLinks = all('a,button').map(text).filter(t => /^(sign in|log in|login|sign up|sign in to|continue with)/i.test(t)).slice(0, 5);
  return {
    url: location.href, title: document.title,
    signed_in_user: login ? login.content : null,
    sign_in_prompts: signInLinks,
    password_field: all('input[type=password]').length > 0,
    captcha: !!document.querySelector('iframe[src*="captcha"], iframe[src*="challenge"], .g-recaptcha, #challenge-form, [data-sitekey]'),
    dialogs: all('[role=dialog], dialog[open], [aria-modal=true]').map(d => text(d).slice(0, 300)).slice(0, 3),
    alerts: all('[role=alert], .flash-error, .error, .alert-danger, [aria-live=assertive]').map(text).filter(Boolean).map(t => t.slice(0, 200)).slice(0, 4),
    controls: all('button, a[role=button], input[type=submit]').map(e => ({label: text(e).slice(0, 60), disabled: !!(e.disabled || e.getAttribute('aria-disabled') === 'true')})).filter(c => c.label).slice(0, 40),
    excerpt: (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').slice(0, 1500),
  };
})()"""


def _tabs() -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=3) as response:
        return [t for t in json.loads(response.read()) if t.get("type") == "page"]


def _task_tab(tabs: list[dict]) -> dict | None:
    try:
        target = _TARGET_FILE.read_text().strip()
    except OSError:
        target = ""
    owned = next((t for t in tabs if t.get("id") == target), None)
    return owned or next((t for t in tabs if not t.get("url", "").startswith(("about:", "chrome:"))), None)


async def _evaluate(ws_url: str, expression: str) -> dict:
    import websockets
    async with websockets.connect(ws_url, max_size=2 ** 24, open_timeout=5) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": expression, "returnByValue": True}}))
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if message.get("id") == 1:
                return (message.get("result") or {}).get("result", {}).get("value") or {}


def inspect() -> dict:
    """What the task tab shows right now. Raises OSError if the browser is not running."""
    tab = _task_tab(_tabs())
    if tab is None:
        return {"error": "the assistant browser has no open page"}
    try:
        asyncio.get_running_loop()
        running = True
    except RuntimeError:
        running = False
    if running:  # called from inside an event loop: evaluate on a private one
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            state = pool.submit(asyncio.run, _evaluate(tab["webSocketDebuggerUrl"], _PAGE_STATE)).result(20)
    else:
        state = asyncio.run(_evaluate(tab["webSocketDebuggerUrl"], _PAGE_STATE))
    state["diagnosis"] = diagnose(state)
    return state


def diagnose(state: dict, stuck_on: str | None = None) -> list[str]:
    """Causes supported by what is on the page, most likely first."""
    host = urlparse(state.get("url") or "").hostname or "this site"
    causes = []
    signed_out = (not state.get("signed_in_user") and bool(state.get("sign_in_prompts"))) or state.get("password_field")
    if signed_out:
        what = f"'{stuck_on}' and anything else that needs an account" if stuck_on else "actions that need an account"
        prompts = state.get("sign_in_prompts") or []
        providers = [p for p in prompts if re.search(r"\b(google|apple|microsoft|github|facebook)\b", p, re.I)]
        # 2026-09-27 10:24: asked to sign in to GitHub, she read "until the
        # user signs in" as "only the user may", and handed it back.
        if providers:
            fix = (f"If the user asked you to sign in, do it: browser_task with the goal \"Click '{providers[0]}' "
                   "and choose the user's account\" (an account the provider already offers). Never type a "
                   "password or 2FA code: if one is asked, call show_browser and ask the user to type it there.")
        else:
            fix = ("Signing in here needs the user's password: call show_browser and ask the user to sign in "
                   "there (never type a password yourself), or do the job another way that is already signed in.")
        causes.append(f"The assistant's browser is not signed in to {host} (the page shows "
                      f"{', '.join(repr(p) for p in prompts[:2]) or 'a password field'}). "
                      f"{what[0].upper() + what[1:]} will not work until it is signed in. {fix}")
    if state.get("captcha"):
        causes.append("The page is showing a CAPTCHA / verification challenge that needs a human.")
    for dialog in state.get("dialogs") or []:
        causes.append(f"A dialog is open over the page: {dialog[:200]!r}.")
    for alert in state.get("alerts") or []:
        causes.append(f"The page shows an error or alert: {alert!r}.")
    if stuck_on:
        for control in state.get("controls") or []:
            if control.get("label", "").lower() == stuck_on.lower() and control.get("disabled"):
                causes.append(f"'{stuck_on}' is disabled on this page.")
                break
    return causes


def explain_stop(stuck_on: str | None = None) -> str:
    """One sentence for a failed task's result; empty when nothing is certain."""
    try:
        state = inspect()
    except Exception:  # noqa: BLE001 -- diagnosis is a bonus, never a new failure
        return ""
    if state.get("error"):
        return ""
    causes = diagnose(state, stuck_on)
    where = f" Page: {state.get('title') or state.get('url')}."
    if not causes:
        return where + " Nothing on the page explains it; use inspect_browser to look closer."
    return where + " Likely cause (from the page itself): " + " ".join(causes[:2])
