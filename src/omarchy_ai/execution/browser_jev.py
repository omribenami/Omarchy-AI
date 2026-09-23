"""Gateway adapter for browser-use/jev-ultrafast.

The upstream agent is deliberately reused for its observed-DOM executor and
freshness guards. Its two model HTTP helpers are replaced at runtime: Jev
evaluation and optional field text generation both use the configured Vercel
AI Gateway key, while the selected Omarchy text model supplies only text
values. No TYPESAFE_API_KEY or OpenRouter key is read.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from .actions import ActionResult

log = logging.getLogger("omarchy_ai.execution.browser_jev")
_browser_process = None
_owned_browser = None
_task_lock = threading.Lock()
_cancel_generation = 0
_MAX_ACTIONS = 50
_MAX_SECONDS = 90
_TARGET_FILE = Path.home() / ".cache" / "omarchy-ai" / "jev-browser-target"
# Completion and step checks ride along in the decision request (no extra
# round trip). Real probe, 2026-09-22: without them upstream accepted DONE at
# p=0.50 on a Google results page after clicking a video card that never
# navigated, and at p=0.65 on the wrong Wikipedia article.
_CHECK_THRESHOLD = 0.85
# Completion: real completions scored >= 0.81, incomplete pages <= 0.25
# across the labelled sets with the final-step wording below.
_GOAL_THRESHOLD = 0.8
_MAX_DONE_REJECTIONS = 2
_MAX_STEPS = 8
# Heavy shop pages (H-E-B) exceed browser-harness's 5s CDP reply limit on
# this CPU: "Runtime.evaluate timed out after 5s waiting for the daemon"
# (2026-09-22 23:32). Raised for the duration of a task only.
_CDP_TIMEOUT = 15.0
# Gateway 503 bursts: four in ~2.3s killed a live task (23:33). Back off
# long enough to ride out a burst, still inside the 90s task budget.
_JEV_BACKOFF = (0.5, 1.0, 2.0, 3.0, 4.0)
# A dead DevTools connection surfaced as "no close frame received or sent"
# and then failed every later task instantly (4 in 7s, 2026-09-21).
_CONNECTION_ERRORS = ("no close frame", "connection closed", "connectionclosed", "broken pipe",
                      "no session with given id", "session with given id not found", "target closed",
                      "waiting for the daemon", "connection refused", "not attached")


def cancel_browser_tasks() -> None:
    """Invalidate delegated work, including threads waiting for a model."""
    global _cancel_generation
    _cancel_generation += 1
    from .desktop_jev import cancel_desktop_tasks
    cancel_desktop_tasks()


# Same control, any counter in its label: at most this many uses per task.
# Real incident (2026-09-22): an H-E-B add button relabelled itself on every
# click ("81 added", "82 added", ...), so the exact-label guard below never
# matched and the agent clicked it 45 times until the 50-action budget.
_MAX_SAME_CONTROL = 6


def _control_key(action, page):
    label = re.sub(r"\d+", "#", str(action.get("label") or "")).strip().casefold()
    return (action.get("kind"), label, urlsplit(str(page.get("url") or "")).path)


def _guard_browser_action(original, generation):
    recent = []
    uses: dict = {}

    def guarded(action, page, text=None):
        if generation != _cancel_generation:
            raise RuntimeError("Browser task cancelled because the assistant stopped")
        if action.get("kind") not in ("wait", "scroll"):
            control = _control_key(action, page)
            uses[control] = uses.get(control, 0) + 1
            if uses[control] > _MAX_SAME_CONTROL:
                raise RuntimeError(f"Stopped: used the same control ({action.get('label', 'control')!s:.60}) "
                                   f"{_MAX_SAME_CONTROL} times in one task; check the page (e.g. a cart "
                                   "quantity) before continuing")
        # Reusing a search field is expected for multi-item tasks. Include the
        # generated value so distinct queries do not look like a stuck loop,
        # while still stopping repeated attempts with the same value.
        key = (action.get("kind"), action.get("label"), page.get("url"), text)
        # Fingerprints change when a menu toggles, so fingerprint-only
        # progress detection does not catch repeated Times-button clicks.
        if action.get("kind") != "wait" and recent[-6:].count(key) >= 3:
            raise RuntimeError(f"Stopped repeated interaction with {action.get('label', 'control')}")
        recent.append(key)
        return original(action, page, text=text)
    return guarded


def _dedicated_window(clients):
    """The automation Chromium's window, by the omarchy-ai-browser unit's PID.

    By class alone it could be the user's own browser (Omarchy's default is
    Chromium too), and the class here is "chromium-browser", which the old
    {"chromium", "google-chrome"} check never matched."""
    try:
        shown = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value",
                                "omarchy-ai-browser.service"], capture_output=True, text=True, timeout=5)
        pid = int((shown.stdout or "0").strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        pid = 0
    mine = [c for c in clients if pid and c.get("pid") == pid and c.get("mapped", True)]
    return min(mine, key=lambda c: c.get("focusHistoryID", 1_000)) if mine else None


def _focus_dedicated_window() -> None:
    """Show the Jev Chromium on the user's CURRENT workspace.

    Real bug (2026-09-22): the window stays wherever it was first opened
    (workspace 1 here), and with Hyprland's misc:focus_on_activate=true the
    CDP Target.activateTarget of every browser_task pulled the user over to
    it. Moving the window here first (silently) keeps the user in place.
    Call this BEFORE activating the target."""
    try:
        from .actions import _desktop_env, _hyprctl_dispatch
        env = _desktop_env()
        clients = json.loads(subprocess.check_output(["hyprctl", "clients", "-j"], env=env, text=True))
        current = json.loads(subprocess.check_output(["hyprctl", "activeworkspace", "-j"], env=env, text=True)).get("id")
        target = _dedicated_window(clients)
        if not target or not target.get("address"):
            return
        address = target["address"]
        if current is not None and (target.get("workspace") or {}).get("id") != current:
            _hyprctl_dispatch(f'hl.dsp.window.move({{ workspace = {int(current)}, window = "address:{address}", follow = false }})',
                              ["movetoworkspacesilent", f"{int(current)},address:{address}"])
            log.info("moved dedicated browser window to the current workspace %s", current)
        _hyprctl_dispatch(f'hl.dsp.focus({{ window = "address:{address}" }})', ["focuswindow", f"address:{address}"])
        log.info("focused dedicated browser window: %s", target.get("title", "Chromium"))
    except Exception:
        log.debug("could not focus dedicated browser window", exc_info=True)


def _ensure_dedicated_browser() -> None:
    """Start the browser-use-only Chromium profile with a stable CDP port."""
    global _browser_process
    endpoint = "http://127.0.0.1:9229"
    # A 0.3s probe misread a busy Chromium (heavy ad page) as dead, then the
    # relaunch collided with the still-running unit and the task failed
    # before any action (2026-09-22). Probe patiently, and while the unit is
    # alive wait for it rather than launching a second one.
    unit_active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", "omarchy-ai-browser.service"],
                                 check=False).returncode == 0
    for _ in range(4 if unit_active else 1):
        try:
            urllib.request.urlopen(endpoint + "/json/version", timeout=2).close()
            os.environ["BU_CDP_URL"] = endpoint
            return
        except Exception:
            time.sleep(0.5)
    if unit_active:
        log.warning("dedicated Chromium unit is running but CDP is unresponsive; restarting it")
        subprocess.run(["systemctl", "--user", "stop", "omarchy-ai-browser.service"], check=False, timeout=20)
    # The process may have crashed while BU_CDP_URL remained in the daemon
    # environment. Clear stale routing so the next request can relaunch
    # Chromium instead of waiting 30s for a dead endpoint.
    os.environ.pop("BU_CDP_URL", None)
    os.environ.pop("BU_CDP_WS", None)
    # Use an explicit argument set for the automation profile.
    binary = "/usr/lib/chromium/chromium"
    if not Path(binary).exists():
        raise RuntimeError("dedicated Chromium binary /usr/lib/chromium/chromium is unavailable")
    profile = Path.home() / ".cache" / "omarchy-ai" / "jev-browser"
    profile.mkdir(parents=True, exist_ok=True)
    # User services may start before the graphical session exports these.
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    if not os.environ.get("WAYLAND_DISPLAY"):
        sockets = sorted(runtime.glob("wayland-*"))
        if sockets:
            os.environ["WAYLAND_DISPLAY"] = sockets[0].name
    os.environ.setdefault("DISPLAY", ":0")
    browser_argv = [
        binary, "--remote-debugging-port=9229", f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check", "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
        "--ozone-platform=wayland", "about:blank",
    ]
    # Keep Chromium's GPU/renderer children out of omarchy-ai.service.
    # Chromium can move its main PID to a desktop scope while its children
    # remain in the launching service, whose restart then kills them.
    launch = ["systemd-run", "--user", "--collect",
              "--unit=omarchy-ai-browser", "--property=Type=exec"]
    for variable in ("WAYLAND_DISPLAY", "DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        if os.environ.get(variable):
            launch.append(f"--setenv={variable}={os.environ[variable]}")
    # A failed previous unit keeps its name reserved; clear it first.
    subprocess.run(["systemctl", "--user", "reset-failed", "omarchy-ai-browser.service"],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(launch + ["--", *browser_argv], check=True,
                   capture_output=True, text=True, timeout=10)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(endpoint + "/json/version", timeout=0.5).close()
            os.environ["BU_CDP_URL"] = endpoint
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("dedicated Chromium did not expose CDP on port 9229")


def _same_site(first: str, second: str) -> bool:
    try:
        return urlsplit(first).scheme in {"http", "https"} and urlsplit(first).netloc == urlsplit(second).netloc
    except (TypeError, ValueError):
        return False


def _adopt_existing_tab():
    """Attach to the one surviving automation page after daemon restarts."""
    try:
        from browser_harness.helpers import cdp
        from jev_ultrafast.browser import Browser
        targets = cdp("Target.getTargets").get("targetInfos", [])
        pages = [t for t in targets if t.get("type") == "page"]
        try:
            remembered = _TARGET_FILE.read_text().strip()
        except OSError:
            remembered = ""
        # The tab used last time wins, even if it sits on about:blank:
        # creating a fresh tab per daemon restart is how tabs piled up.
        target = next((t for t in pages if t.get("targetId") == remembered), None)
        if target is None:
            web = [t for t in pages if str(t.get("url") or "").startswith(("http://", "https://"))]
            if not web:
                return None
            target = web[-1]
        browser = Browser.__new__(Browser)
        browser.target = target["targetId"]
        browser.session = cdp(
            "Target.attachToTarget", targetId=browser.target, flatten=True,
        )["sessionId"]
        browser.call(
            "Emulation.setDeviceMetricsOverride", width=1120, height=780,
            deviceScaleFactor=1, mobile=False,
        )
        browser.call("Emulation.setFocusEmulationEnabled", enabled=True)
        _remember_target(browser.target)
        log.info("adopted existing dedicated browser target: %s", browser.target)
        return browser
    except Exception:
        log.debug("no existing browser target could be adopted", exc_info=True)
        return None


def _remember_target(target_id) -> None:
    try:
        _TARGET_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TARGET_FILE.write_text(str(target_id or ""))
    except OSError:
        pass


def _connection_error(error) -> bool:
    text = str(error).casefold()
    return any(marker in text for marker in _CONNECTION_ERRORS)


def _reset_connection() -> None:
    """Drop the cached tab session and let the harness self-heal its daemon."""
    global _owned_browser
    _owned_browser = None
    try:
        from browser_harness.admin import ensure_daemon
        ensure_daemon()
    except Exception:
        log.warning("browser harness could not be healed", exc_info=True)


def _owned_browser_alive() -> bool:
    if _owned_browser is None:
        return False
    try:
        _owned_browser.call("Runtime.evaluate", expression="1", returnByValue=True)
        return True
    except Exception:
        return False


# First point (over the element's own boxes and its children's) whose
# hit-test really lands inside the element. Upstream clicks the geometric
# centre, which on Google results is covered by an overlay span: Jev chose
# the right result at p=1.00 four times and nothing navigated (2026-09-22).
_CLICK_POINT = """(n => { const e = window.__jevFast?.nodes.get(n);
  if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
      !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
  const boxes = [...e.getClientRects(), ...[...e.querySelectorAll('*')].slice(0, 40).flatMap(c => [...c.getClientRects()])];
  for (const r of boxes) { if (!r.width || !r.height) continue;
    for (const [fx, fy] of [[.5,.5],[.2,.5],[.8,.5],[.5,.25],[.5,.75]]) {
      const x = r.x + r.width*fx, y = r.y + r.height*fy;
      if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
      const hit = document.elementFromPoint(x, y);
      if (hit && e.contains(hit)) return {x, y}; } }
  return null; })"""


def _click_on_element(original):
    """Wrap jev_ultrafast.browser.browser_operation: clicks land on a point
    the element really owns; everything else is upstream's code path."""
    def operation(request):
        from browser_harness.helpers import cdp
        action = request.get("action") or {}
        if request.get("operation") != "act" or action.get("kind") != "click" or type(action.get("node")) is not int:
            return original(request)
        session = request["session"]
        found = cdp("Runtime.evaluate", session_id=session, expression=_CLICK_POINT + f"({action['node']})",
                    returnByValue=True).get("result", {}).get("value")
        if not found:
            return original(request)  # upstream decides: covered/stale
        for event in ("mouseMoved", "mousePressed", "mouseReleased"):
            cdp("Input.dispatchMouseEvent", session_id=session, type=event, x=found["x"], y=found["y"],
                button="none" if event == "mouseMoved" else "left", clickCount=1)
        return {"executed": action["id"]}
    return operation


def _page_targets():
    from browser_harness.helpers import cdp
    return [t for t in cdp("Target.getTargets").get("targetInfos", []) if t.get("type") == "page"]


def _absorb_new_tabs(browser, known: set) -> bool:
    """Keep ONE tab: a click that opened a new tab (target=_blank,
    window.open) is folded back into the owned tab. Without this the agent
    keeps observing the unchanged original page, clicks the same link again
    and trips the repeated-interaction stop."""
    from browser_harness.helpers import cdp
    try:
        fresh = [t for t in _page_targets() if t["targetId"] not in known and t["targetId"] != browser.target
                 and (t.get("openerId") in (None, browser.target))]
    except Exception:
        return False
    if not fresh:
        return False
    url = ""
    deadline = time.monotonic() + 2.5
    while time.monotonic() < deadline:  # popups start on about:blank
        try:
            current = {t["targetId"]: t for t in _page_targets()}
        except Exception:
            break
        urls = [str(current.get(t["targetId"], {}).get("url") or "") for t in fresh]
        url = next((u for u in reversed(urls) if u.startswith(("http://", "https://"))), "")
        if url:
            break
        time.sleep(0.1)
    for target in fresh:
        try:
            cdp("Target.closeTarget", targetId=target["targetId"])
        except Exception:
            pass
        known.add(target["targetId"])
    if url:
        browser.call("Page.navigate", url=url)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if browser.evaluate("document.readyState") == "complete":
                    break
            except Exception:
                pass
            time.sleep(0.05)
        log.info("folded a new tab back into the owned tab: %s", url)
    return bool(url)


def _settle(browser, timeout: float = 6.0):
    """Wait for a loading page to hold still before Jev judges it. Jev chose
    BLOCKED twice at 0 actions on heb.com while the app was still loading."""
    deadline = time.monotonic() + timeout
    page = browser.observe(screenshot=False)
    while time.monotonic() < deadline:
        time.sleep(0.6)
        try:
            again = browser.observe(screenshot=False)
        except Exception:
            continue
        if again.get("fingerprint") == page.get("fingerprint") and again.get("actions"):
            return again
        page = again
    return page


class _Progress:
    """Ordered steps plus the Jev checks carried by every decision request.

    Finishing is not Jev's DONE choice: DONE is withheld from the operation
    question and granted only when the independent goal check passes."""

    def __init__(self, goal: str, steps: list[str] | None):
        self.goal = goal
        self.steps = steps or [goal]
        self.index = 0
        self.checks: dict | None = None
        self.rejections = 0

    def render(self) -> str:
        scroll = (" If the element needed next is not among the offered elements, use SCROLL_DOWN "
                  "to reveal more of the page instead of searching again.")
        if len(self.steps) == 1:
            return self.goal + scroll
        lines = [f"Overall goal: {self.goal}", "Steps, strictly in this order:"]
        for i, step in enumerate(self.steps):
            mark = "done" if i < self.index else "CURRENT" if i == self.index else "later"
            lines.append(f"{i + 1}. [{mark}] {step}")
        lines.append(f"Work only on step {self.index + 1} now. Never repeat a done step or start a later one early."
                     + scroll)
        return "\n".join(lines)

    def questions(self) -> dict:
        # One literal yes/no per step (TypeSafe: "ask each decision one way").
        # A page often proves a later step but not an earlier one (an article
        # page does not show the search that led to it: 0.61 vs 0.88 in a
        # real probe), so progress follows the furthest confirmed step.
        questions = {}
        for i, step in enumerate(self.steps[:-1]):
            questions[f"omarchy_step_{i}"] = {"type": "boolean", "instructions": {
                "step": step,
                # Wording picked on a labelled set of real pages (STATUS.md
                # 2026-09-22): "does the page show `step` completed?" scored
                # correct steps as low as 0.05.
                "question": "Given the page the browser is on now (`page.url`, `page.title`), has `step` "
                            "already been accomplished? A search is accomplished once its results, or a page "
                            "reached from them, are showing. Opening a target is accomplished only when that "
                            "target page itself is open."}}
        # Completion judges only the FINAL step: a destination page cannot
        # prove the earlier steps (those are the tracker's job). Judged on the
        # whole goal, a generic "Package manager" article passed at 0.88.
        questions["omarchy_goal_done"] = {"type": "boolean", "instructions": {
            "final_step": self.steps[-1],
            # Picked on labelled real pages (STATUS.md 2026-09-22): "a search
            # ends on its results page" alone made Jev reject omarchy.us for
            # "search Google ... and navigate to the first result" (0.35);
            # judging only the LAST requested thing fixed it (0.81) while the
            # incomplete pages stayed <= 0.09.
            "question": "Look at the LAST thing `final_step` asks for. If that last thing is a search, its "
                        "results page completes it. If it is opening, visiting or navigating to something, "
                        "only that exact page itself completes it: not a results page, not a broader or "
                        "related page, not a page that only links to it. Is the page now open (`page.url`, "
                        "`page.title`) what completes `final_step`? `recent_actions` shows how the browser "
                        "got here."}}
        return questions

    def read(self, answers: dict) -> None:
        def p(name):
            value = (answers.pop(name, None) or {}).get("probability")
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1 else None
        steps = [p(f"omarchy_step_{i}") for i in range(len(self.steps) - 1)]
        self.checks = {"steps": steps, "goal": p("omarchy_goal_done")}

    def advance(self) -> bool:
        confirmed = [i for i, value in enumerate((self.checks or {}).get("steps") or [])
                     if value is not None and value >= _CHECK_THRESHOLD]
        if self.goal_verified():
            confirmed.append(len(self.steps) - 1)
        target = min(max(confirmed) + 1, len(self.steps) - 1) if confirmed else self.index
        if target > self.index:  # never move backwards
            self.index = target
            return True
        return False

    def note_page(self, url: str) -> None:
        if self.goal_verified():
            self.verified_url = url

    def verified_here(self, url: str) -> bool:
        verified = getattr(self, "verified_url", None)
        return bool(verified) and _same_page(verified, url)

    def goal_verified(self) -> bool:
        goal = (self.checks or {}).get("goal")
        return goal is not None and goal >= _GOAL_THRESHOLD

    def summary(self) -> str:
        if len(self.steps) == 1:
            return ""
        done = len(self.steps) if self.goal_verified() else self.index
        return (f" Steps verified: {done}/{len(self.steps)}"
                + (f"; next unfinished: {self.steps[done]!r}." if done < len(self.steps) else "."))


def _same_page(first: str, second: str) -> bool:
    try:
        a, b = urlsplit(first), urlsplit(second)
        return (a.netloc, a.path.rstrip("/"), a.query) == (b.netloc, b.path.rstrip("/"), b.query)
    except (TypeError, ValueError):
        return False


def _agent_with_reused_tab(Agent, url: str, goal: str, resume: bool = False):
    """Create fresh task state while retaining one browser target and cart."""
    global _owned_browser
    if _owned_browser is None or not getattr(_owned_browser, "target", None):
        _owned_browser = _adopt_existing_tab()
    if _owned_browser is None:
        agent = Agent(url, goal, screenshots=False)
        _owned_browser = agent.browser
        _remember_target(agent.browser.target)
        return agent

    browser = _owned_browser
    page = browser.observe(screenshot=False)
    current_url = str(page.get("url") or "")
    # Only an explicit resume keeps same-site progress (a half-filled form,
    # a search in progress). A NEW task on the same site used to inherit the
    # previous task's page and block with 0 actions (Hyprland after an Arch
    # Linux article, 2026-09-22), so it now starts from its own URL.
    keep = _same_site(current_url, url) if resume else _same_page(current_url, url)
    if not keep:
        browser.call("Page.navigate", url=url)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if browser.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)
        page = browser.observe(screenshot=False)

    agent = Agent.__new__(Agent)
    agent.pending_text = None
    agent.browser = browser
    agent.record_dir = None
    agent.screenshots = False
    agent.state = dict(
        browser=browser, goal=goal, page=page, decision=None, history=[],
        status="ready", plan=[goal], plan_index=0, decisions=[], text_calls=[],
        elapsed_ms=0, started_at=None, record=False,
    )
    return agent


_QUOTED = re.compile(r'"([^"]{1,200})"|“([^”]{1,200})”')


def _quoted_value(step: str) -> str | None:
    """The single quoted value in a step (`Search for "eggs"` -> eggs)."""
    values = [a or b for a, b in _QUOTED.findall(step or "")]
    return values[0].strip() if len(values) == 1 and values[0].strip() else None


def _steps_from(args) -> list[str] | None:
    raw = args.get("steps")
    if not isinstance(raw, list):
        return None
    steps = [str(step).strip()[:300] for step in raw if str(step).strip()][:_MAX_STEPS]
    # One step with a quoted value still matters: it carries the exact text.
    return steps if len(steps) > 1 or (steps and _quoted_value(steps[0])) else None


def run_browser_task(args: dict, config) -> ActionResult:
    generation = _cancel_generation
    started = time.monotonic()
    url = str(args.get("url") or "").strip()
    goal = str(args.get("goal") or "").strip()
    if not url.startswith(("https://", "http://")) or not goal:
        return ActionResult(False, "browser_task requires an http(s) URL and a complete goal")
    progress = _Progress(goal, _steps_from(args))

    _ensure_dedicated_browser()
    from jev_ultrafast import Agent
    from jev_ultrafast import agent as agent_module
    from jev_ultrafast import model as policy
    from ..voice.omarchy import GatewayClient

    client = GatewayClient(config)

    def gateway_evaluate(_url, _key, body):
        # The step/goal checks share this request: same page state, no
        # extra round trip (Jev evaluates every question independently).
        questions = {**body["questions"], **progress.questions()}
        operation = dict(questions["operation"])
        operation["criteria"] = {k: v for k, v in operation["criteria"].items() if k != "DONE"}
        questions["operation"] = operation
        request_body = json.dumps({
            "state": body["state"],
            "questions": questions,
            "model": config.omarchy_jev_model,
        }).encode()
        headers = {
            "ai-evaluation-model-specification-version": "4",
            "ai-model-id": config.omarchy_jev_model,
        }
        response = None
        for attempt in range(len(_JEV_BACKOFF) + 1):
            try:
                request_started = time.monotonic()
                response = client._request(
                    "/ai/evaluation-model", request_body, "application/json", headers, timeout=6
                )
                log.info(
                    "Jev browser decision: model=%s latency_ms=%d",
                    config.omarchy_jev_model, (time.monotonic() - request_started) * 1000,
                )
                break
            except Exception as error:
                if attempt == len(_JEV_BACKOFF) or generation != _cancel_generation:
                    raise
                log.warning("Jev browser evaluation failed; retrying: %s", str(error)[:160])
                time.sleep(_JEV_BACKOFF[attempt])
        assert response is not None
        result = json.loads(response)
        result.setdefault("model", config.omarchy_jev_model)
        result.setdefault("usage", {})
        answers = result.get("answers") or {}
        progress.read(answers)
        progress.note_page(str((body["state"].get("page") or {}).get("url") or ""))
        op = answers.get("operation")
        if isinstance(op, dict) and isinstance(op.get("probabilities"), dict):
            # Upstream validates against its full option set, DONE included.
            if progress.goal_verified():
                op["probabilities"] = {k: 0.0 for k in op["probabilities"]} | {"DONE": 1.0}
                op["choice"] = "DONE"
            else:
                op["probabilities"]["DONE"] = 0.0
        # Gateway puts TypeSafe's confidence in providerMetadata, not in the
        # answer objects jev-ultrafast validates. Use the real value when
        # present; otherwise fall back to the selected probability.
        confidence = ((result.get("providerMetadata") or {}).get("typesafe") or {}).get("confidence") or {}
        for name, answer in (result.get("answers") or {}).items():
            if "confidence" not in answer:
                answer["confidence"] = confidence.get(name, (answer.get("probabilities") or {}).get(answer.get("choice"), 0.0))
        return result

    def gateway_text(_context):
        # The live model already decided the exact words: a quoted value in
        # the current step is typed verbatim, no second model guessing. Real
        # failure (2026-09-22): the text helper typed "pack of eggs" from the
        # whole goal sentence, which found nothing useful on heb.com.
        quoted = _quoted_value(progress.steps[progress.index])
        if quoted:
            return quoted, {"model": "live-model step (verbatim)", "latency_ms": 0, "usage": {}}
        value = client.text_value(_context)
        return value, {"model": config.omarchy_text_model, "latency_ms": 0, "usage": {}}

    from jev_ultrafast import browser as browser_module
    import browser_harness.helpers as harness_helpers
    old_cdp = harness_helpers.cdp
    old_browser_cdp = browser_module.cdp

    def patient_cdp(method, session_id=None, _response_timeout=_CDP_TIMEOUT, **params):
        return old_cdp(method, session_id=session_id, _response_timeout=max(_response_timeout, _CDP_TIMEOUT), **params)

    harness_helpers.cdp = browser_module.cdp = patient_cdp
    old_operation = browser_module.browser_operation
    browser_module.browser_operation = _click_on_element(old_operation)
    old_post, old_field = policy.post_json, policy.field_text
    old_choose, old_agent_field = agent_module.choose, agent_module.field_text
    policy.post_json, policy.field_text = gateway_evaluate, gateway_text
    old_typesafe_key = os.environ.get("TYPESAFE_API_KEY")
    old_typesafe_model = os.environ.get("TYPESAFE_MODEL")
    # Upstream choose() reads these names before invoking post_json; the
    # adapter consumes the value locally and never sends it to api.typesafe.ai.
    os.environ["TYPESAFE_API_KEY"] = client.key
    os.environ["TYPESAFE_MODEL"] = config.omarchy_jev_model
    # Agent imported these symbols directly, so patch its local references too.
    agent_module.choose, agent_module.field_text = policy.choose, gateway_text
    old_command_choose = Agent.command.__globals__.get("choose")
    old_command_field = Agent.command.__globals__.get("field_text")
    Agent.command.__globals__["choose"] = policy.choose
    Agent.command.__globals__["field_text"] = gateway_text
    if not _task_lock.acquire(blocking=False):
        harness_helpers.cdp, browser_module.cdp = old_cdp, old_browser_cdp
        browser_module.browser_operation = old_operation
        policy.post_json, policy.field_text = old_post, old_field
        agent_module.choose, agent_module.field_text = old_choose, old_agent_field
        Agent.command.__globals__["choose"] = old_command_choose
        Agent.command.__globals__["field_text"] = old_command_field
        return ActionResult(False, "another browser task is already running; wait for its result instead of opening another")
    try:
        # A cached tab whose DevTools session died used to fail every
        # following task instantly; heal first, then retry once when a
        # connection error happens before any action ran.
        if not _owned_browser_alive():
            _reset_connection()
        for attempt in range(2):
            outcome, executed, error = _run_agent(Agent, url, args, progress, generation, started, config)
            if error is None:
                return outcome
            if attempt == 0 and executed == 0 and _connection_error(error):
                log.warning("browser connection lost before any action; reconnecting once: %s", error)
                _reset_connection()
                continue
            text = str(error)
            if "HTTP 503" in text or "HTTP 529" in text or "HTTP 429" in text:
                # Not the raw 600-char JSON: something she can say.
                text = "the Jev decision service (Vercel Gateway) kept answering 'temporarily unavailable'"
            return ActionResult(False, f"Browser task stopped after {executed} actions: {text[:300]}. "
                                       "The task tab has been left open; completion was not verified.")
    finally:
        # Keep the owned tab visible for inspection after completion/failure.
        # Upstream close() closes the tab, which looks like a browser crash.
        harness_helpers.cdp, browser_module.cdp = old_cdp, old_browser_cdp
        browser_module.browser_operation = old_operation
        policy.post_json, policy.field_text = old_post, old_field
        agent_module.choose, agent_module.field_text = old_choose, old_agent_field
        Agent.command.__globals__["choose"] = old_command_choose
        Agent.command.__globals__["field_text"] = old_command_field
        if old_typesafe_key is None:
            os.environ.pop("TYPESAFE_API_KEY", None)
        else:
            os.environ["TYPESAFE_API_KEY"] = old_typesafe_key
        if old_typesafe_model is None:
            os.environ.pop("TYPESAFE_MODEL", None)
        else:
            os.environ["TYPESAFE_MODEL"] = old_typesafe_model
        _task_lock.release()


def _where(state) -> str:
    page = (state or {}).get("page") or {}
    return f"{page.get('url', 'unknown')} ({str(page.get('title') or '').strip()[:80]!r})"


def _run_agent(Agent, url, args, progress, generation, started, config):
    """One attempt. Returns (result, executed_actions, error_or_None)."""
    agent = None
    try:
        agent = _agent_with_reused_tab(Agent, url, progress.render(), resume=bool(args.get("resume")))
        agent.browser.act = _guard_browser_action(agent.browser.act, generation)
        try:
            known = {t["targetId"] for t in _page_targets()}
        except Exception:
            known = set()
        # Browser() creates its owned tab in the background.  This assistant
        # has a dedicated profile, so make that tab visible and active.  Keep
        # the harness startup target intact: its daemon is attached to that
        # target and closing it would disconnect the CDP socket.
        try:
            from browser_harness.helpers import cdp
            _focus_dedicated_window()  # first: bring it to this workspace
            cdp("Target.activateTarget", targetId=agent.browser.target)
            log.info("dedicated browser target active: %s", agent.browser.target)
        except Exception:
            log.exception("could not activate dedicated browser target")
        agent.state["page"] = _settle(agent.browser)
        final = None
        recovery_attempts = 0
        for state in agent.run():
            if generation != _cancel_generation:
                return ActionResult(False, "Browser task cancelled because the assistant stopped."), 0, None
            final = state
            history = state.get("history") or []
            elapsed = time.monotonic() - started
            if _absorb_new_tabs(agent.browser, known):
                agent.state["page"] = agent.browser.observe(screenshot=False)
                agent.state["decision"] = None
                if agent.state["status"] == "blocked":
                    agent.state["status"] = "ready"
                continue
            if state.get("status") != "done" and progress.verified_here(str((state.get("page") or {}).get("url") or "")):
                # The goal passed its check on this very URL; a self-updating
                # page then failed upstream's unchanged-page recheck and the
                # agent wandered off (omarchy.us -> #home, 2026-09-22).
                final = {**state, "status": "done"}
                break
            if progress.advance():
                log.info("browser step verified; now step %d/%d", progress.index + 1, len(progress.steps))
                agent.state["goal"] = progress.render()
            if len(history) >= _MAX_ACTIONS or elapsed >= _MAX_SECONDS:
                log.warning(
                    "Jev browser budget reached: actions=%d elapsed_ms=%d model=%s",
                    len(history), elapsed * 1000, config.omarchy_jev_model,
                )
                return ActionResult(
                    False,
                    f"Browser task stopped at its speed budget after {len(history)} actions "
                    f"and {elapsed:.1f}s at {_where(state)}.{progress.summary()} Completion was not verified.",
                ), len(history), None
            if state.get("status") == "done" and not progress.goal_verified():
                # Jev's own DONE is not evidence. The independent goal check
                # from the same request must agree, or the agent continues.
                progress.rejections += 1
                if progress.rejections > _MAX_DONE_REJECTIONS:
                    break
                log.warning("browser DONE rejected (goal check p=%s) at %s",
                            (progress.checks or {}).get("goal"), _where(state))
                agent.state["history"].append({
                    "step": len(history) + 1, "action": "DONE rejected: the page does not yet show the goal complete",
                    "kind": "wait", "text": None, "page_changed": None,
                })
                agent.state["status"] = "ready"
                agent.state["decision"] = None
                continue
            if state.get("status") in {"done", "blocked"}:
                if state.get("status") == "blocked":
                    decisions = state.get("decisions") or []
                    last_choice = decisions[-1].get("choice") if decisions else None
                    repeated = history[-3:]
                    # Jev's executor can mark a page blocked after three
                    # unchanged fingerprints, or make a premature BLOCKED
                    # choice while a dynamic search page still has actions.
                    # Permit a few fresh decisions while there are actions;
                    # the cap prevents an infinite loop on a truly stuck page.
                    heuristic_block = (
                        len(repeated) == 3
                        and all(not item.get("page_changed") and item.get("kind") != "wait" for item in repeated)
                    )
                    page_actions = (state.get("page") or {}).get("actions") or []
                    premature_block = last_choice == "BLOCKED" and bool(page_actions)
                    recovery_limit = 4
                    if (heuristic_block or premature_block) and recovery_attempts < recovery_limit:
                        recovery_attempts += 1
                        log.warning(
                            "browser block after %d actions; choice=%s heuristic=%s recovering, url=%s, actions=%s",
                            len(history), last_choice, heuristic_block,
                            (state.get("page") or {}).get("url"),
                            [item.get("action") for item in repeated],
                        )
                        # run() yields a snapshot, not the mutable agent state.
                        # Allow asynchronous results to settle and observe again
                        # before resuming the generator's terminal-state check.
                        agent.state["page"] = _settle(agent.browser, timeout=4.0)
                        agent.state["decision"] = None
                        agent.state["status"] = "ready"
                        continue
                break
        if not final:
            return ActionResult(False, "browser agent produced no result"), 0, None
        status = final.get("status")
        history = [h for h in (final.get("history") or []) if not str(h.get("action", "")).startswith("DONE rejected")]
        if status == "done" and (progress.goal_verified() or getattr(progress, "verified_url", None)):
            log.info(
                "Jev browser completed: actions=%d elapsed_ms=%d goal_p=%s model=%s implementation=jev-ultrafast",
                len(history), (time.monotonic() - started) * 1000, (progress.checks or {}).get("goal"), config.omarchy_jev_model,
            )
            return ActionResult(True, f"Browser task completed and verified by an independent Jev check "
                                      f"after {len(history)} action(s). "
                                      f"Now at {_where(final)}."), len(history), None
        last_actions = ", ".join(str(item.get("action", "?"))[:60] for item in history[-3:])
        reason = ("Jev chose DONE but the independent completion check disagreed"
                  if status == "done" or progress.rejections else "blocked")
        return ActionResult(False, f"Browser task NOT verified ({reason}) after {len(history)} action(s); "
                                   f"now at {_where(final)}; last actions: {last_actions}.{progress.summary()} "
                                   "Inspect the page (describe_screen) before reporting anything as done."), len(history), None
    except Exception as error:
        log.exception("browser task stopped; retaining its tab for inspection")
        count = len(agent.state.get("history", [])) if agent else 0
        return None, count, error
