"""The assistant's dedicated browser.

Split out of the former execution/actions.py; see execution/actions/__init__.py."""

from __future__ import annotations

import json
import logging
import re

from ...config import load_config
from .base import ActionResult

log = logging.getLogger("omarchy_ai.execution.actions")


def open_browser(args: dict) -> ActionResult:
    # Omarchy-ai owns one browser: the dedicated Jev-ultrafast Chromium
    # profile. Do not open the user's normal browser and then drive it with
    # keyboard focus, which is both ambiguous and unsafe.
    try:
        from ..browser_jev import _ensure_dedicated_browser
        _ensure_dedicated_browser()
        from ..browser_jev import _focus_dedicated_window
        _focus_dedicated_window()
        return ActionResult(True, "dedicated Jev browser ready")
    except Exception as exc:
        return ActionResult(False, f"dedicated Jev browser unavailable: {exc}")


def browser_task(args: dict) -> ActionResult:
    """Run jev-ultrafast with Gateway-backed Jev decisions and one Gateway key."""
    try:
        from ..browser_jev import run_browser_task
        result = run_browser_task(args, load_config())
    except Exception as exc:
        log.exception("browser task failed before execution")
        return ActionResult(False, f"browser task unavailable; no browser action executed: {exc}")
    try:
        # The idle about:blank tab it started with kept sitting in front of the
        # task's tab whenever the browser was shown (2026-09-26).
        from ..browser_control import tidy_blank_tabs
        tidy_blank_tabs()
    except Exception:  # noqa: BLE001 -- cosmetic
        log.debug("could not close blank browser tabs", exc_info=True)
    if not result.ok and ("left open" in result.message or "NOT verified" in result.message):
        # Say why, from the tab itself (2026-09-26: signed out of GitHub, and
        # all she could report was "stopped repeated interaction").
        stuck = re.search(r"(?:interaction with|same control \()([^).]+)", result.message)
        from ..browser_inspect import explain_stop
        why = explain_stop(stuck.group(1).strip() if stuck else None)
        if why:
            result = ActionResult(False, result.message + why)
    return result


def inspect_browser(args: dict) -> ActionResult:
    """Read-only: what the assistant browser's task tab shows, and any cause
    the page itself reveals for a stopped task. Never clicks or navigates."""
    from ..browser_inspect import inspect
    try:
        state = inspect()
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"assistant browser not reachable: {exc}")
    return ActionResult(not state.get("error"), json.dumps(state, ensure_ascii=False)[:6000])


def show_browser(args: dict) -> ActionResult:
    """Bring the assistant's own browser to the user's screen with the task's
    tab in front -- not the idle about:blank tab it used to show (2026-09-26)."""
    try:
        from ..browser_jev import _ensure_dedicated_browser
        from .. import browser_control
        _ensure_dedicated_browser()
        pages = browser_control.tabs()
        tab = browser_control._task_tab(pages)
        if tab is None:
            return ActionResult(False, "the assistant browser has no open page")
        browser_control._front(tab)
        browser_control.tidy_blank_tabs()
        return ActionResult(True, f"assistant browser is on the user's screen; {browser_control._state(tab)}")
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"could not show the assistant browser: {exc}")


def browser_control(args: dict) -> ActionResult:
    from ..browser_control import browser_control as control
    return control(args)
