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
import subprocess
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from .actions import ActionResult

log = logging.getLogger("omarchy_ai.execution.browser_jev")
_browser_process = None
_cancel_generation = 0


def cancel_browser_tasks() -> None:
    """Invalidate delegated work, including threads waiting for a model."""
    global _cancel_generation
    _cancel_generation += 1
    from .desktop_jev import cancel_desktop_tasks
    cancel_desktop_tasks()


def _guard_browser_action(original, generation):
    recent = []

    def guarded(action, page, text=None):
        if generation != _cancel_generation:
            raise RuntimeError("Browser task cancelled because the assistant stopped")
        key = (action.get("kind"), action.get("label"), page.get("url"))
        # Fingerprints change when a menu toggles, so fingerprint-only
        # progress detection does not catch repeated Times-button clicks.
        if action.get("kind") != "wait" and recent[-6:].count(key) >= 3:
            raise RuntimeError(f"Stopped repeated interaction with {action.get('label', 'control')}")
        recent.append(key)
        return original(action, page, text=text)
    return guarded


def _focus_dedicated_window() -> None:
    """Raise the Jev Chromium window on the current workspace."""
    try:
        from .actions import _desktop_env
        env = _desktop_env()
        clients = json.loads(subprocess.check_output(["hyprctl", "clients", "-j"], env=env, text=True))
        current = json.loads(subprocess.check_output(["hyprctl", "activeworkspace", "-j"], env=env, text=True))
        candidates = [c for c in clients if str(c.get("class", "")).lower() in {"chromium", "google-chrome"}]
        same_workspace = [c for c in candidates if c.get("workspace", {}).get("id") == current.get("id")]
        target = same_workspace[-1] if same_workspace else (candidates[-1] if candidates else None)
        if target and target.get("address"):
            subprocess.run(["hyprctl", "dispatch", "focuswindow", f"address:{target['address']}"], env=env, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.info("focused dedicated browser window: %s", target.get("title", "Chromium"))
    except Exception:
        log.debug("could not focus dedicated browser window", exc_info=True)


def _ensure_dedicated_browser() -> None:
    """Start the browser-use-only Chromium profile with a stable CDP port."""
    global _browser_process
    endpoint = "http://127.0.0.1:9229"
    try:
        urllib.request.urlopen(endpoint + "/json/version", timeout=0.3).close()
        os.environ["BU_CDP_URL"] = endpoint
        return
    except Exception:
        # The process may have crashed while BU_CDP_URL remained in the
        # daemon environment. Clear stale routing so the next request can
        # relaunch Chromium instead of waiting 30s for a dead endpoint.
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


def run_browser_task(args: dict, config) -> ActionResult:
    generation = _cancel_generation
    url = str(args.get("url") or "").strip()
    goal = str(args.get("goal") or "").strip()
    if not url.startswith(("https://", "http://")) or not goal:
        return ActionResult(False, "browser_task requires an http(s) URL and a complete goal")

    _ensure_dedicated_browser()
    from jev_ultrafast import Agent
    from jev_ultrafast import agent as agent_module
    from jev_ultrafast import model as policy
    from ..voice.omarchy import GatewayClient

    client = GatewayClient(config)

    def gateway_evaluate(_url, _key, body):
        request_body = json.dumps({
            "state": body["state"],
            "questions": body["questions"],
            "model": config.omarchy_jev_model,
        }).encode()
        headers = {
            "ai-evaluation-model-specification-version": "4",
            "ai-model-id": config.omarchy_jev_model,
        }
        response = None
        for attempt in range(2):
            try:
                response = client._request("/ai/evaluation-model", request_body, "application/json", headers)
                break
            except Exception:
                if attempt == 1:
                    raise
                log.warning("Jev browser evaluation timed out/failed; retrying")
                time.sleep(0.35)
        assert response is not None
        result = json.loads(response)
        result.setdefault("model", config.omarchy_jev_model)
        result.setdefault("usage", {})
        # Gateway evaluation answers expose probabilities; jev-ultrafast's
        # validator also expects the explicit confidence field used by its
        # direct API. Derive it from the selected probability.
        for answer in (result.get("answers") or {}).values():
            if "confidence" not in answer:
                answer["confidence"] = (answer.get("probabilities") or {}).get(answer.get("choice"), 0.0)
        return result

    def gateway_text(_context):
        value = client.text_value(_context)
        return value, {"model": config.omarchy_text_model, "latency_ms": 0, "usage": {}}

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
    agent = None
    try:
        agent = Agent(url, goal, screenshots=False)
        agent.browser.act = _guard_browser_action(agent.browser.act, generation)
        # Browser() creates its owned tab in the background.  This assistant
        # has a dedicated profile, so make that tab visible and active.  Keep
        # the harness startup target intact: its daemon is attached to that
        # target and closing it would disconnect the CDP socket.
        try:
            from browser_harness.helpers import cdp
            cdp("Target.activateTarget", targetId=agent.browser.target)
            _focus_dedicated_window()
            log.info("dedicated browser target active: %s", agent.browser.target)
        except Exception:
            log.exception("could not activate dedicated browser target")
        final = None
        recovery_attempts = 0
        for state in agent.run():
            if generation != _cancel_generation:
                return ActionResult(False, "Browser task cancelled because the assistant stopped.")
            final = state
            if state.get("status") in {"done", "blocked"}:
                if state.get("status") == "blocked":
                    decisions = state.get("decisions") or []
                    last_choice = decisions[-1].get("choice") if decisions else None
                    history = state.get("history") or []
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
                    if (heuristic_block or premature_block) and recovery_attempts < 3:
                        recovery_attempts += 1
                        log.warning(
                            "browser block after %d actions; choice=%s heuristic=%s recovering (%d/3), url=%s, actions=%s",
                            len(history), last_choice, heuristic_block, recovery_attempts,
                            (state.get("page") or {}).get("url"),
                            [item.get("action") for item in repeated],
                        )
                        # run() yields a snapshot, not the mutable agent state.
                        # Allow asynchronous results to settle and observe again
                        # before resuming the generator's terminal-state check.
                        time.sleep(1)
                        agent.state["page"] = agent.browser.observe(screenshot=False)
                        agent.state["decision"] = None
                        agent.state["status"] = "ready"
                        continue
                break
        if not final:
            return ActionResult(False, "browser agent produced no result")
        status = final.get("status")
        history = final.get("history") or []
        if status == "done":
            return ActionResult(True, f"Browser task visibly completed after {len(history)} verified action(s).")
        last_actions = ", ".join(item.get("action", "?") for item in history[-3:])
        page_url = (final.get("page") or {}).get("url", "unknown")
        return ActionResult(False, f"Browser task blocked after {len(history)} verified action(s) at {page_url}; last actions: {last_actions}. No completion evidence was found.")
    except Exception as error:
        log.exception("browser task stopped; retaining its tab for inspection")
        count = len(agent.state.get("history", [])) if agent else 0
        return ActionResult(False, f"Browser task stopped after {count} actions: {error}. The task tab has been left open; completion was not verified.")
    finally:
        # Keep the owned tab visible for inspection after completion/failure.
        # Upstream close() closes the tab, which looks like a browser crash.
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
