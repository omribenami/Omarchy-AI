"""Missions: a scripted sequence of narrated actions, run by code.

Real session (2026-09-23 00:40, commercial_prompt.md): asked to "narrate in
parallel to doing the actions" through four demos, the live model dropped the
narration, typed `ls` into a different terminal than the one it opened, and
when the named projector (HY300 Pro) was not found it improvised with another
TV. Every tool except desktop/browser tasks makes Gemini wait silently, so
talking *while* acting was impossible, and a long chain of tool calls was
easy to lose.

Now the live model only plans: it turns the file into ordered steps, each a
line to say plus one action. The session (voice/gemini_live.py) runs them:
it asks Gemini to speak the line and starts the action at the same moment,
verifies the result, and stops at the first failure instead of improvising.
"""
from __future__ import annotations

import json
import re
import time

from .actions import ActionResult, _run, run_action

# Actions a mission step may use, with the arguments each requires.
ACTIONS = {
    "say": (),                                # narration only
    "browser_task": ("url", "goal"),
    "terminal_run": ("command",),             # open, focus, type, Return, read output
    "start_casting": ("target",),
    "stop_casting": (),
    "workspace_switch": ("number",),
    "move_window_to_workspace": ("number",),
    "open_browser": (),
    "desktop_task": ("goal",),
    "wait": ("seconds",),
}
MAX_STEPS = 12


def _repair(raw: list, workspace) -> list[dict]:
    """Fix the sloppy-but-obvious plans real Gemini produced for the
    commercial script (2026-09-23): a separate open_terminal/open_browser
    step before the step that already opens it, workspace_switch without a
    number, wait without seconds, browser_task with steps but no goal."""
    steps = []
    for step in raw:
        if not isinstance(step, dict):
            steps.append(step)
            continue
        step = {"say": str(step.get("say") or "").strip(), "action": str(step.get("action") or "say").strip(),
                "args": dict(step.get("args")) if isinstance(step.get("args"), dict) else {}}
        a = step["args"]
        if step["action"] in ("workspace_switch", "move_window_to_workspace") and not a.get("number") and workspace:
            a["number"] = workspace
        if step["action"] == "wait" and a.get("seconds") in (None, ""):
            step["action"] = "say"
        if step["action"] == "browser_task" and not a.get("goal") and isinstance(a.get("steps"), list):
            a["goal"] = "; ".join(str(x) for x in a["steps"])
        steps.append(step)
    merged = []
    for i, step in enumerate(steps):
        nxt = steps[i + 1] if i + 1 < len(steps) and isinstance(steps[i + 1], dict) else None
        opener = {"open_terminal": "terminal_run", "open_browser": "browser_task"}
        if isinstance(step, dict) and nxt and opener.get(step["action"]) == nxt["action"]:
            nxt["say"] = " ".join(x for x in (step["say"], nxt["say"]) if x)  # the next step opens it anyway
            continue
        if isinstance(step, dict) and step["action"] == "open_terminal":
            step["action"] = "say"  # opening an idle terminal alone does nothing useful
        merged.append(step)
    return merged


def validate(args: dict) -> tuple[list[dict], int | None]:
    workspace = args.get("workspace")
    if workspace in (None, "", 0):
        workspace = None
    else:
        workspace = int(workspace)
        if not 1 <= workspace <= 99:
            raise ValueError("workspace must be 1-99")
    raw = args.get("steps")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_STEPS:
        raise ValueError(f"steps must be a list of 1-{MAX_STEPS} steps")
    clean, problems = [], []
    for i, step in enumerate(_repair(raw, workspace), 1):
        if not isinstance(step, dict):
            problems.append(f"step {i} is not an object")
            continue
        action, step_args, say = step["action"], step["args"], step["say"][:600]
        if action not in ACTIONS:
            problems.append(f"step {i}: unknown action {action!r} (allowed: {', '.join(ACTIONS)})")
            continue
        missing = [k for k in ACTIONS[action] if step_args.get(k) in (None, "")]
        if missing:
            problems.append(f"step {i} ({action}) needs {', '.join(missing)}")
            continue
        if action == "say" and not say:
            continue  # nothing to say or do: drop it
        clean.append({"say": say, "action": action, "args": step_args})
    if problems:
        # All at once, so one retry fixes every step.
        raise ValueError("; ".join(problems) + ". Fix these and call run_mission again with the full step list.")
    if not clean:
        raise ValueError("no steps left to run")
    return clean, workspace


def active_workspace():
    w = _run(["hyprctl", "activeworkspace", "-j"])
    try:
        return json.loads(w.message).get("id") if w.ok else None
    except ValueError:
        return None


def ensure_workspace(number) -> ActionResult:
    if number is None or active_workspace() == number:
        return ActionResult(True, "on the mission workspace")
    run_action("workspace_switch", {"number": number})
    for _ in range(10):
        if active_workspace() == number:
            return ActionResult(True, f"switched back to workspace {number}")
        time.sleep(0.05)
    return ActionResult(False, f"could not get to workspace {number}")


def terminal_run(command: str) -> ActionResult:
    """Open a tracked terminal and run one command in THAT terminal."""
    opened = run_action("open_terminal", {})
    if not opened.ok:
        return opened
    title = re.search(r"opened (.+?);", opened.message)
    target = title.group(1).strip() if title else None
    if not target:
        return ActionResult(False, "terminal opened but its title is unknown; not typing blindly")
    focused = ActionResult(False, "not focused")
    for _ in range(40):  # the window appears asynchronously
        focused = run_action("focus_window", {"target": target})
        if focused.ok:
            break
        time.sleep(0.1)
    if not focused.ok:
        return ActionResult(False, f"new terminal {target!r} never took focus: {focused.message}")
    typed = run_action("type_text", {"text": command})
    if not typed.ok:
        return typed
    entered = run_action("press_key", {"key": "Return"})
    if not entered.ok:
        return entered
    time.sleep(1.2)
    output = run_action("read_tile_log", {"window": target})
    tail = output.message[-600:] if output.ok else "(output not readable)"
    return ActionResult(True, f"ran {command!r} in {target}; output tail: {tail}")


def run_step(step: dict) -> ActionResult:
    action, args = step["action"], step["args"]
    if action == "say":
        return ActionResult(True, "narration only")
    if action == "wait":
        time.sleep(min(max(float(args["seconds"]), 0), 30))
        return ActionResult(True, "waited")
    if action == "terminal_run":
        return terminal_run(str(args["command"]))
    if action == "workspace_switch":
        run_action(action, {"number": int(args["number"])})
        return ensure_workspace(int(args["number"]))
    return run_action(action, args)
