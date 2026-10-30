"""Signing in to a site in the assistant's browser as the user.

2026-10-19 21:15: asked to open ChatGPT, make sure she was signed in with the
user's account and talk to it, she answered "I'm not signed in, please sign
in", and "sign in for me" got the same; the user had to do it by hand. The
user's rule since: when asked to sign in on their behalf she does it, and the
check is their fingerprint.

- The login (username and password) is kept per site in the user's MyApi
  vault ("Login: <site>"; their rule: "always store provided credentials in
  the myApi vault"), else GNOME Keyring when the vault refuses it; never in
  a file, a log or the model's context. The first time, the user gives
  the password in the chat ("password: X" is captured and masked by
  execution/passwords.py) and the username as an argument; after a sign-in
  that worked it is saved for that site.
- Every use of a saved or given password first asks the phone for a
  fingerprint (execution/flux_approve.py): a valid signature over this
  request, or nothing is typed.
- The password goes into the page by CDP Input.insertText into the focused
  password field of the assistant browser's task tab, on the site it was
  saved for: a page on another host never gets it.
"""

from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlsplit

from .actions.base import ActionResult

log = logging.getLogger(__name__)

_ATTRIBUTES = ("application", "omarchy-ai", "purpose", "site-login")
_PAGES = 4  # email page, password page, and a chooser or two in between
_SETTLE = 2.5

# The visible login fields and the button that sends them, marked so CDP can
# click them by position. Nothing typed here: only where things are.
_FIND = r"""(() => {
  const shown = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && !e.disabled; };
  const inputs = [...document.querySelectorAll('input')].filter(shown);
  const pass = inputs.find(e => e.type === 'password');
  const user = inputs.find(e => e.autocomplete && /username|email/.test(e.autocomplete))
    || inputs.find(e => e.type === 'email')
    || inputs.find(e => /user|email|login|identifier/i.test((e.name || '') + ' ' + (e.id || '')) && e.type !== 'hidden');
  const code = inputs.find(e => e.autocomplete === 'one-time-code' || /otp|code|2fa|totp/i.test((e.name || '') + (e.id || '')));
  const form = (pass || user || {}).form;
  const buttons = [...(form || document).querySelectorAll('button, input[type=submit]')].filter(shown);
  const submit = buttons.find(b => b.type === 'submit') || buttons.find(b => /continue|next|log ?in|sign ?in|המשך|התחבר/i.test(b.innerText || b.value || ''));
  const mark = (e, k) => { if (!e) return null; e.setAttribute('data-omarchy-login', k); return k; };
  return {url: location.href, user: mark(user, 'user'), userValue: user ? user.value : '', pass: mark(pass, 'pass'),
          code: mark(code, 'code'), submit: mark(submit, 'submit')};
})()"""

_CENTER = r"""((k) => { const e = document.querySelector(`[data-omarchy-login="${k}"]`); if (!e) return null;
  e.scrollIntoView({block: 'center'}); const r = e.getBoundingClientRect();
  return {x: r.left + r.width / 2, y: r.top + r.height / 2}; })"""


def _host(url: str) -> str:
    host = (urlsplit(url if "//" in url else "https://" + url).hostname or "").lower()
    return host.removeprefix("www.")


def _site(host: str) -> str:
    """The registrable part a login belongs to: auth.openai.com and
    chatgpt.com sign in through each other, so the user names the site and
    the login is kept under the host of the login form they reach."""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _secret(args: list[str], value: str | None = None):
    from . import sudo_approval
    return sudo_approval._run(args, password=value)


def _vault_name(site: str) -> str:
    return f"Login: {site}"


def _parse(value) -> dict | None:
    try:
        login = json.loads(value) if value else None
    except ValueError:
        return None
    return login if isinstance(login, dict) and login.get("password") else None


def saved(site: str) -> dict | None:
    try:
        from ..myapi import vault
        login = _parse(vault.get(_vault_name(site)))
        if login:
            return login
    except Exception:  # noqa: BLE001 -- not in the vault (or it is unreachable): the keyring
        pass
    try:
        result = _secret(["lookup", *_ATTRIBUTES, "site", site])
    except OSError:
        return None
    return _parse(result.stdout if result.returncode == 0 else "")


def save(site: str, username: str, password: str) -> bool:
    value = json.dumps({"username": username, "password": password})
    try:
        from ..myapi.client import MyApiClient
        MyApiClient().request("POST", "/vault/tokens", body={
            "name": _vault_name(site), "token": value, "service": site, "websiteUrl": f"https://{site}",
            "description": "Saved by Omarchy AI's sign_in; used only after a fingerprint on the phone."})
        return True
    except Exception as exc:  # noqa: BLE001 -- e.g. not a master token: keep it in the keyring
        log.warning("could not save the %s login in the MyApi vault (%s); keeping it in the keyring",
                    site, str(exc)[:120])
    try:
        result = _secret(["store", f"--label=Omarchy AI login for {site}", *_ATTRIBUTES, "site", site], value)
    except OSError:
        return False
    return result.returncode == 0


def saved_passwords() -> list[str]:
    """Every saved site password, for masking (execution/passwords.py): the
    keyring's, and those inside login values read from the vault (the vault
    masks the whole value, not the password in it)."""
    values = []
    try:
        from ..myapi import vault
        values += [str(login["password"]) for v in vault.revealed_values() if (login := _parse(v))]
    except Exception:  # noqa: BLE001
        pass
    try:
        result = _secret(["search", "--all", "--unlock", *_ATTRIBUTES])
    except OSError:
        return values
    for line in (result.stdout or "").splitlines():
        if line.startswith("secret = "):
            try:
                values.append(str(json.loads(line[len("secret = "):]).get("password") or ""))
            except (ValueError, AttributeError):
                continue
    return [v for v in values if v]


def _click(tab, key: str) -> bool:
    from .browser_control import _eval, _run, _session
    at = _eval(tab, f"{_CENTER}({json.dumps(key)})")
    if not at:
        return False
    x, y = at["x"], at["y"]
    _run(_session(tab["ws"], [
        ("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1}),
        ("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1}),
    ]))
    return True


def _type_into(tab, key: str, text: str) -> None:
    from .browser_control import _eval, _run, _session
    _click(tab, key)
    # Select what is there, so a prefilled field is replaced, not appended to.
    _eval(tab, f"(() => {{ const e = document.querySelector('[data-omarchy-login=\"{key}\"]'); "
               "if (e) { e.focus(); e.select && e.select(); } })()")
    _run(_session(tab["ws"], [("Input.insertText", {"text": text})]))


def _submit(tab, found: dict) -> None:
    from .browser_control import _run, _session
    if found.get("submit") and _click(tab, "submit"):
        return
    _run(_session(tab["ws"], [
        ("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13,
                                    "text": "\r"}),
        ("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13}),
    ]))


def _approve(site: str, username: str) -> tuple[bool, str]:
    from . import flux_approve
    ok, reason = flux_approve.request(f"Sign in to {site} as you", by="omarchy",
                                      why=f"as {username}" if username else "")
    if ok:
        return True, reason
    if ok is False:
        return False, f"Not signed in: the user declined the fingerprint request ({reason})."
    return False, (f"Not signed in: the fingerprint approval could not be had ({reason}). Tell the user that, and "
                   "offer to try again or to show them the browser.")


def _task_tab():
    from . import browser_control
    from .browser_jev import _ensure_dedicated_browser
    _ensure_dedicated_browser()
    return browser_control._task_tab(browser_control.tabs())


def sign_in(args: dict, *, approve=_approve) -> ActionResult:
    from .browser_control import _eval
    from . import passwords
    tab = _task_tab()
    if tab is None:
        return ActionResult(False, "the assistant browser has no open page: open the site's sign-in page with "
                                   "browser_task first", harness=True)
    found = _eval(tab, _FIND) or {}
    host = _host(found.get("url") or tab.get("url") or "")
    if not found.get("user") and not found.get("pass"):
        return ActionResult(False, f"No sign-in form on the page ({host or 'no page'}). First open the site's sign-in "
                                   "form with browser_task (click its 'Log in' button, or 'Continue with Google' "
                                   "when the user signs in with Google), then call sign_in again.", harness=True)
    site = _site(host)
    login = saved(site)
    given = str(args.get("username") or "").strip()
    username = given or (login or {}).get("username") or found.get("userValue") or ""
    password = (login or {}).get("password") if not args.get("use_given_password") else None
    password = password or passwords.spoken_password()
    if not password or (found.get("user") and not username):
        missing = " and ".join(w for w, gone in (("the username or email", not username),
                                                ("the password", not password)) if gone)
        return ActionResult(False, f"No saved login for {site}. Ask the user for {missing} (they can type the "
                                   "password in the chat as 'password: ...'; it is kept out of every log and "
                                   "your context). Then call sign_in again with username=, and "
                                   "use_given_password=true.", harness=True)
    ok, why = approve(site, username)
    if not ok:
        return ActionResult(False, why, harness=True)
    pages = 0
    sent_password = False
    while pages < _PAGES:
        pages += 1
        if _site(_host(found.get("url") or "")) != site:
            break  # the form moved to another site: never send it there
        if found.get("user") and username and found.get("userValue") != username:
            _type_into(tab, "user", username)
        if found.get("pass"):
            _type_into(tab, "pass", password)
            sent_password = True
        _submit(tab, found)
        time.sleep(_SETTLE)
        found = _eval(tab, _FIND) or {}
        if sent_password or not (found.get("user") or found.get("pass")):
            break
    from .browser_inspect import inspect
    try:
        state = inspect()
    except Exception as exc:  # noqa: BLE001
        state = {"error": str(exc)}
    still = bool(found.get("pass"))
    code = bool(found.get("code"))
    if sent_password and not still and (given or not login or args.get("use_given_password")):
        save(site, username, password)
    summary = {k: state.get(k) for k in ("url", "title", "signed_in_user", "sign_in_prompts", "captcha", "alerts")}
    if code:
        note = ("The site now asks for a verification code. If it was sent to the user's email or phone, read it "
                "yourself (myapi: Gmail) and type it with browser_control; otherwise ask the user for it.")
    elif still:
        note = "The password form is still there: the password may be wrong. Read the page's alert and tell the user."
    elif not sent_password:
        note = "No password field appeared; look at the page (inspect_browser) and carry on from there."
    else:
        note = "Signed in, approved by fingerprint. Carry on with the user's request."
    return ActionResult(not still and sent_password and not code,
                        f"{note} ({why}.) Page: {json.dumps(summary, ensure_ascii=False)[:1200]}")
