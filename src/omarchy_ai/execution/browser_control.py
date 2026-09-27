"""Direct control of the assistant's browser, one user request at a time.

Real gap, 2026-09-26 23:04: the user asked to see the GitHub tab a browser
task had left open. The assistant browser showed its idle "about:blank" tab
in front (browser tasks work in a background tab so they never pull the user
over), and she had no way to switch tabs -- she said she couldn't.

browser_task stays the way to reach a goal ("open an issue ..."). This is
for the user steering by hand through her: switch to the GitHub tab, scroll
down, click "New issue", type into the title field, go back.

Code finds what is really on the page; Jev only chooses among those real
elements (or tabs) when the user's words match more than one or none
exactly -- a choice over existing options, never a generated selector.
Clicks and keys are real input events (CDP Input.*), not element.click().
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
import urllib.request

from .actions import ActionResult

log = logging.getLogger(__name__)

CDP = "http://127.0.0.1:9229"
JEV_MIN_P = 0.5
MAX_CANDIDATES = 80

# Visible elements the user could mean, tagged so a later call can find the
# same element again. `kind` "field" collects inputs; "control" clickables.
_COLLECT = r"""((kind) => {
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const label = e => (e.getAttribute('aria-label') || e.innerText || e.value || e.getAttribute('title')
    || e.getAttribute('placeholder') || e.getAttribute('name') || e.getAttribute('alt') || '').trim().replace(/\s+/g, ' ');
  const sel = kind === 'field'
    ? 'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=checkbox]):not([type=radio]), textarea, [contenteditable=true], [contenteditable=""]'
    : 'a[href], button, [role=button], [role=link], [role=tab], [role=menuitem], [role=option], [role=checkbox], [role=switch], input[type=submit], input[type=button], input[type=checkbox], input[type=radio], summary, label[for], [onclick]';
  const out = [];
  document.querySelectorAll('[data-omarchy-el]').forEach(e => e.removeAttribute('data-omarchy-el'));
  for (const e of document.querySelectorAll(sel)) {
    if (!vis(e)) continue;
    let text = label(e);
    if (kind === 'field') {
      const lab = e.id && document.querySelector(`label[for="${CSS.escape(e.id)}"]`);
      text = ((lab && lab.innerText.trim()) || e.getAttribute('aria-label') || e.getAttribute('placeholder') || e.getAttribute('name') || text || e.tagName.toLowerCase()).replace(/\s+/g, ' ');
    }
    if (!text) continue;
    const id = 'e' + out.length;
    e.setAttribute('data-omarchy-el', id);
    out.push({id, text: text.slice(0, 90), disabled: !!(e.disabled || e.getAttribute('aria-disabled') === 'true')});
    if (out.length >= %d) break;
  }
  return out;
})"""  % MAX_CANDIDATES

_CENTER = r"""((id) => { const e = document.querySelector(`[data-omarchy-el="${id}"]`); if (!e) return null;
  e.scrollIntoView({block: 'center', inline: 'center'}); const r = e.getBoundingClientRect();
  return {x: r.left + r.width / 2, y: r.top + r.height / 2}; })"""


# ------------------------------------------------------------------ plumbing
def _http(path: str, method: str = "GET"):
    request = urllib.request.Request(CDP + path, method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        body = response.read()
    try:
        return json.loads(body)
    except ValueError:
        return body.decode(errors="replace")


def tabs() -> list[dict]:
    pages = [t for t in _http("/json/list") if t.get("type") == "page"]
    return [{"n": i + 1, "id": t["id"], "title": t.get("title") or "", "url": t.get("url") or "",
             "ws": t.get("webSocketDebuggerUrl")} for i, t in enumerate(pages)]


def _run(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, coro).result(30)


async def _session(ws_url: str, calls: list[tuple[str, dict]]) -> list:
    """Run CDP calls in order on one tab; each result, or its error."""
    import websockets
    results = []
    async with websockets.connect(ws_url, max_size=2 ** 24, open_timeout=5) as ws:
        for n, (method, params) in enumerate(calls, 1):
            await ws.send(json.dumps({"id": n, "method": method, "params": params}))
            while True:
                message = json.loads(await asyncio.wait_for(ws.recv(), 15))
                if message.get("id") == n:
                    results.append(message.get("result", {"error": message.get("error")}))
                    break
    return results


def _eval(tab: dict, expression: str):
    [result] = _run(_session(tab["ws"], [("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                              "awaitPromise": True})]))
    return (result.get("result") or {}).get("value")


def _front(tab: dict) -> None:
    """Show this tab to the user: the browser window, with the tab in front.
    The window moves to the user's workspace first -- activating a tab
    before that pulled the user over to the browser's workspace
    (browser_jev._focus_dedicated_window, 2026-09-22)."""
    try:
        from .browser_jev import _focus_dedicated_window
        _focus_dedicated_window()
    except Exception:  # noqa: BLE001 -- still switch the tab
        log.debug("could not focus the assistant browser window", exc_info=True)
    _http(f"/json/activate/{tab['id']}")


def tidy_blank_tabs() -> int:
    """Close idle about:blank tabs once a real page is open (the blank tab
    kept ending up in front of the task tab)."""
    pages = tabs()
    if not any(not t["url"].startswith(("about:", "chrome:")) for t in pages):
        return 0
    closed = 0
    for t in pages:
        if t["url"] == "about:blank":
            _http(f"/json/close/{t['id']}")
            closed += 1
    return closed


def _task_tab(pages: list[dict]) -> dict | None:
    from .browser_jev import _TARGET_FILE
    try:
        target = _TARGET_FILE.read_text().strip()
    except OSError:
        target = ""
    return (next((t for t in pages if t["id"] == target), None)
            or next((t for t in pages if not t["url"].startswith(("about:", "chrome:"))), None)
            or (pages[0] if pages else None))


def _window_title() -> str | None:
    """Hyprland's title for the assistant browser window: "<front tab> - Chromium"."""
    try:
        import subprocess
        from .actions import _desktop_env
        from .browser_jev import _dedicated_window
        clients = json.loads(subprocess.check_output(["hyprctl", "clients", "-j"], env=_desktop_env(), text=True,
                                                     timeout=5))
        window = _dedicated_window(clients)
    except Exception:  # noqa: BLE001
        return None
    title = (window or {}).get("title") or ""
    for suffix in (" - Chromium", " - Google Chrome", " – Chromium"):
        if title.endswith(suffix):
            return title[: -len(suffix)]
    return title or None


def _current(pages: list[dict]) -> dict | None:
    """The tab the user is looking at. CDP cannot say (every tab reports
    visibilityState "visible" in this browser, and /json/list listed the
    GitHub tab first while about:blank was in front, 2026-09-26), but the
    window title always names the front tab -- even after the user clicks
    a tab themselves."""
    title = _window_title()
    if title:
        exact = [t for t in pages if (t["title"] or t["url"]) == title]
        if len(exact) == 1:
            return exact[0]
    return _task_tab(pages)


# ------------------------------------------------------------ choosing (Jev)
def _pick(words: str, options: dict[str, str], what: str, jev=None) -> tuple[str | None, str]:
    """(key, how). Exact label, then a unique partial match; otherwise Jev
    chooses among the real options. None when nothing fits well enough."""
    lowered = words.strip().lower()
    exact = [k for k, v in options.items() if v.lower() == lowered]
    if len(exact) == 1:
        return exact[0], "exact"
    partial = [k for k, v in options.items() if lowered and lowered in v.lower()]
    if len(partial) == 1:
        return partial[0], "match"
    if not options:
        return None, "nothing to choose from"
    from ..core.jev import Jev, JevError, choice
    try:
        answer = (jev or Jev()).ask({"user_asked_for": words, "what": what},
                                    {"target": choice(f"The user asked for the {what} they called {words!r}. "
                                                      f"Which of these {what}s on the screen do they mean?",
                                                      dict(options))}, timeout=6, retries=1)["target"]
    except (JevError, KeyError) as exc:
        return None, f"Jev unavailable ({str(exc)[:80]})"
    if answer["p"] < JEV_MIN_P:
        return None, f"Jev was unsure (p={answer['p']:.2f})"
    return answer["choice"], f"Jev p={answer['p']:.2f}"


def _state(tab: dict) -> str:
    """Title and URL from the page itself once it has loaded: /json/list
    titles lag a navigation (live test 2026-09-26: after clicking "Pull
    requests" it still said "Issues")."""
    info = previous = None
    for _ in range(20):
        try:
            info = _eval(tab, "({title: document.title, url: location.href, ready: document.readyState})")
        except Exception:  # noqa: BLE001 -- mid-navigation the page context can vanish
            info = None
        if info and info.get("ready") == "complete" and previous == info:
            break  # loaded, and the title stopped changing (single-page apps set it late)
        previous = info
        time.sleep(0.25)
    info = info or {"title": tab.get("title", ""), "url": tab.get("url", "")}
    return f"now on {info.get('title', '')[:80]!r} ({info.get('url', '')[:120]})"


# ------------------------------------------------------------------ actions
def list_tabs(pages, args, jev) -> ActionResult:
    task = _task_tab(pages)
    rows = [{"n": t["n"], "title": t["title"][:90], "url": t["url"][:160],
             **({"task_tab": True} if task and t["id"] == task["id"] else {})} for t in pages]
    return ActionResult(True, json.dumps(rows, ensure_ascii=False))


def switch_tab(pages, args, jev) -> ActionResult:
    wanted = str(args.get("tab") or args.get("target") or "").strip()
    if wanted.isdigit() and 1 <= int(wanted) <= len(pages):
        tab, how = pages[int(wanted) - 1], "number"
    else:
        options = {f"t{t['n']}": f"{t['title']} — {t['url'][:80]}" for t in pages}
        key, how = _pick(wanted, options, "browser tab", jev)
        if key is None:
            return ActionResult(False, f"no tab matches {wanted!r} ({how}); open tabs: "
                                       + "; ".join(f"{t['n']}. {t['title'][:50]}" for t in pages))
        tab = pages[int(key[1:]) - 1]
    _front(tab)
    closed = tidy_blank_tabs() if tab["url"] != "about:blank" else 0
    return ActionResult(True, f"switched ({how}); {_state(tab)}" + (f"; closed {closed} blank tab(s)" if closed else ""))


def close_tab(pages, args, jev) -> ActionResult:
    wanted = str(args.get("tab") or "").strip()
    if not wanted:
        tab = _current(pages)
    elif wanted.isdigit() and 1 <= int(wanted) <= len(pages):
        tab = pages[int(wanted) - 1]
    else:
        key, how = _pick(wanted, {f"t{t['n']}": t["title"] or t["url"] for t in pages}, "browser tab", jev)
        tab = pages[int(key[1:]) - 1] if key else None
    if tab is None:
        return ActionResult(False, f"no tab matches {wanted!r}")
    _http(f"/json/close/{tab['id']}")
    return ActionResult(True, f"closed {tab['title'][:80]!r}")


def open_url(pages, args, jev) -> ActionResult:
    url = str(args.get("url") or "").strip()
    if not url:
        return ActionResult(False, "open needs a url")
    if "://" not in url:
        url = "https://" + url
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        return ActionResult(False, "only http(s) pages can be opened")
    tab = _http("/json/new?" + urllib.parse.quote(url, safe=":/?&=#%"), method="PUT")
    tab = {"id": tab["id"], "title": tab.get("title", ""), "url": url, "ws": tab.get("webSocketDebuggerUrl")}
    _front(tab)
    tidy_blank_tabs()
    time.sleep(1.5)
    return ActionResult(True, f"opened; {_state(tab)}")


def _on_page(action):
    """Run on the tab the user is looking at (bringing it to the front)."""
    def run(pages, args, jev):
        tab = _current(pages)
        if tab is None:
            return ActionResult(False, "the assistant browser has no open page")
        _front(tab)
        return action(tab, args, jev)
    return run


@_on_page
def navigate(tab, args, jev) -> ActionResult:
    where = str(args.get("action"))
    expression = {"back": "history.back()", "forward": "history.forward()", "reload": "location.reload()"}[where]
    _eval(tab, expression)
    time.sleep(0.5)
    return ActionResult(True, f"{where}; {_state(tab)}")


@_on_page
def scroll(tab, args, jev) -> ActionResult:
    direction = str(args.get("direction") or "down").lower()
    to_text = str(args.get("to") or "").strip()
    if to_text:
        # Visible text only: live test matched the word inside a <script>
        # JSON blob on GitHub.
        found = _eval(tab, "((t) => { const skip = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE']); "
                           "const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT); let n; "
                           "while ((n = w.nextNode())) { const e = n.parentElement; "
                           "if (!e || skip.has(e.tagName) || !e.getClientRects().length) continue; "
                           "if (n.textContent.toLowerCase().includes(t)) { e.scrollIntoView({block: 'center', behavior: 'instant'}); "
                           "return (e.innerText || n.textContent).trim().slice(0, 120); } } return null; })("
                      + json.dumps(to_text.lower()) + ")")
        return (ActionResult(True, f"scrolled to {found!r}") if found
                else ActionResult(False, f"{to_text!r} is not on this page"))
    pages_count = max(0.25, min(float(args.get("amount") or 1), 10))
    # behavior 'instant': sites with smooth scrolling (GitHub) otherwise
    # report the old position (live test: "0%" right after scrolling down).
    target = {"top": "0", "bottom": "document.documentElement.scrollHeight",
              "up": f"scrollY - innerHeight * 0.8 * {pages_count}",
              "down": f"scrollY + innerHeight * 0.8 * {pages_count}"}.get(direction)
    expression = None if target is None else f"window.scrollTo({{top: {target}, behavior: 'instant'}})"
    if expression is None:
        return ActionResult(False, "direction must be up, down, top or bottom")
    position = _eval(tab, expression + "; Math.round(100 * scrollY / "
                          "Math.max(1, document.documentElement.scrollHeight - innerHeight))")
    return ActionResult(True, f"scrolled {direction}; {position}% down the page")


def _element(tab, words, kind, jev):
    found = _eval(tab, f"{_COLLECT}({json.dumps(kind)})") or []
    options = {e["id"]: e["text"] + (" (disabled)" if e["disabled"] else "") for e in found}
    key, how = _pick(words, options, "field" if kind == "field" else "button or link", jev)
    return key, how, found


def _click_at(tab, key) -> dict | None:
    point = _eval(tab, f"{_CENTER}({json.dumps(key)})")
    if not point:
        return None
    x, y = point["x"], point["y"]
    _run(_session(tab["ws"], [
        ("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}),
        ("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1}),
        ("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1}),
    ]))
    return point


@_on_page
def click(tab, args, jev) -> ActionResult:
    words = str(args.get("target") or args.get("label") or "").strip()
    if not words:
        return ActionResult(False, "click needs the label of what to click")
    key, how, found = _element(tab, words, "control", jev)
    if key is None:
        shown = ", ".join(repr(e["text"][:40]) for e in found[:25])
        return ActionResult(False, f"nothing clickable matches {words!r} ({how}). Visible: {shown}")
    chosen = next(e for e in found if e["id"] == key)
    if chosen["disabled"]:
        return ActionResult(False, f"{chosen['text']!r} is disabled on this page")
    probe = "[location.href, document.title, document.body.innerText.length].join('|')"
    before = _eval(tab, probe)
    if not _click_at(tab, key):
        return ActionResult(False, f"{chosen['text']!r} disappeared before it could be clicked")
    # Single-page sites (GitHub) fetch for a moment before the URL changes:
    # wait for a change, up to 3s, before reporting where we are.
    after = before
    for _ in range(12):
        time.sleep(0.25)
        try:
            after = _eval(tab, probe)
        except Exception:  # noqa: BLE001 -- a navigation replaced the page context
            after = None
        if after != before and after is not None and after.split("|")[:2] != before.split("|")[:2]:
            break
    changed = "the page changed" if after != before else "NO visible change on the page (inspect_browser can tell why)"
    return ActionResult(True, f"clicked {chosen['text']!r} ({how}); {changed}; {_state(tab)}")


@_on_page
def type_text(tab, args, jev) -> ActionResult:
    text = str(args.get("text") or "")
    if not text:
        return ActionResult(False, "type needs text")
    field = str(args.get("target") or args.get("field") or "").strip()
    if field:
        key, how, found = _element(tab, field, "field", jev)
        if key is None:
            return ActionResult(False, f"no field matches {field!r} ({how}). Fields: "
                                       + ", ".join(repr(e["text"][:40]) for e in found[:20]))
        _click_at(tab, key)
        label = next(e["text"] for e in found if e["id"] == key)
    else:
        label, how = "the focused field", "focus"
    calls = [("Input.insertText", {"text": text})]
    if args.get("submit"):
        calls += [("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter",
                                              "windowsVirtualKeyCode": 13, "text": "\r"}),
                  ("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter",
                                              "windowsVirtualKeyCode": 13})]
    _run(_session(tab["ws"], calls))
    time.sleep(0.8 if args.get("submit") else 0.2)
    return ActionResult(True, f"typed into {label!r} ({how})" + ("; pressed Enter" if args.get("submit") else "")
                        + f"; {_state(tab)}")


ACTIONS = {"list_tabs": list_tabs, "switch_tab": switch_tab, "close_tab": close_tab, "open": open_url,
           "back": navigate, "forward": navigate, "reload": navigate, "scroll": scroll, "click": click,
           "type": type_text}


def browser_control(args: dict, jev=None) -> ActionResult:
    action = str(args.get("action") or "").strip()
    if action not in ACTIONS:
        return ActionResult(False, f"action must be one of {', '.join(ACTIONS)}")
    try:
        from .browser_jev import _ensure_dedicated_browser
        _ensure_dedicated_browser()
        pages = tabs()
        return ACTIONS[action](pages, args, jev)
    except Exception as exc:  # noqa: BLE001
        log.exception("browser_control %s failed", action)
        return ActionResult(False, f"browser {action} failed: {exc}")
