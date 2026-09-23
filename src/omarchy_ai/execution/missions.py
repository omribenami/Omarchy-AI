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
from pathlib import Path

from .actions import ActionResult, _run, run_action

# Actions a mission step may use, with the arguments each requires.
ACTIONS = {
    "say": (),                                # narration only
    "browser_task": ("url", "goal"),
    "terminal_run": ("command",),             # her own terminal, typed via tmux
    "start_casting": (),                      # target optional: any discoverable TV
    "stop_casting": (),
    "workspace_switch": ("number",),
    "move_window_to_workspace": ("number",),
    "open_browser": (),
    "desktop_task": ("goal",),
    "demo_file": ("content",),                # write/edit a demo document
    "show_windows": (),                       # read-only: what is open and focused
    "describe_screen": (),
    "wait": ("seconds",),
}
FIELDS = ("url", "goal", "steps", "command", "target", "number", "seconds", "content")
MAX_STEPS = 12
DEMO_FILE = "~/Omarchy-AI-demo.txt"

PLANNER_SYSTEM = (
    "You complete a narrated desktop demo plan. For each incomplete step, return the action and the exact "
    "arguments it needs, taken from the SCRIPT (commands, sites, device names). Actions and fields: "
    "browser_task(url, goal), terminal_run(command), start_casting(target optional), desktop_task(goal), "
    "demo_file(content: a short text document to write and then edit), show_windows(), describe_screen(), "
    "workspace_switch(number), say(). Prefer demo_file for 'edit/modify a document', show_windows for "
    "'check the focused window', start_casting without target for 'any TV'. Never invent destructive "
    "commands. Return JSON: {\"steps\": [{\"index\": <n>, \"action\": ..., <fields>}]}."
)


def _flatten(step: dict) -> dict:
    """Gemini fills flat named fields far more reliably than a nested args
    object: real 2026-09-23 plan sent every action step with no args."""
    args = dict(step.get("args")) if isinstance(step.get("args"), dict) else {}
    for field in FIELDS:
        if step.get(field) not in (None, "") and field not in args:
            args[field] = step[field]
    return {"say": str(step.get("say") or "").strip(), "action": str(step.get("action") or "say").strip(),
            "args": args}


def _missing(step: dict) -> list[str]:
    if step["action"] not in ACTIONS:
        return ["action"]
    return [k for k in ACTIONS[step["action"]] if step["args"].get(k) in (None, "")]


def _repair(raw: list, workspace) -> list[dict]:
    """Fix the sloppy-but-obvious plans real Gemini produced for the
    commercial script (2026-09-23): a separate open_terminal/open_browser
    step before the step that already opens it, workspace_switch without a
    number, wait without seconds, browser_task with steps but no goal, and
    action steps with no arguments at all."""
    steps = []
    for step in raw:
        if not isinstance(step, dict):
            continue
        step = _flatten(step)
        a, say, text = step["args"], step["say"], step["say"].lower()
        if step["action"] in ("workspace_switch", "move_window_to_workspace") and not a.get("number") and workspace:
            a["number"] = workspace
        if step["action"] == "wait" and a.get("seconds") in (None, ""):
            step["action"] = "say"
        if step["action"] == "open_browser" and any(w in text for w in ("search", "google", "navigat", "website")):
            # Real Gemini plan (2026-09-23): only open_browser for "search
            # Google for Omarchy and open the first result" -- the search
            # itself was never planned. The narration says what to do.
            step["action"] = "browser_task"
        if step["action"] == "browser_task":
            if not a.get("goal"):
                a["goal"] = "; ".join(str(x) for x in a["steps"]) if isinstance(a.get("steps"), list) else say
            if not a.get("url") and ("google" in text or "search" in text or "google" in str(a.get("goal")).lower()):
                a["url"] = "https://www.google.com"
        if step["action"] == "desktop_task" and not a.get("goal") and ("focused window" in text or "window" in text):
            step["action"] = "show_windows"
        if step["action"] in ("desktop_task", "say") and not a.get("goal") and any(
                w in text for w in ("editing a document", "edit a document", "modify files", "modifying a file")):
            step["action"] = "demo_file"
        if step["action"] == "say" and ("mirror my screen" in text or "cast" in text) and "tv" in text:
            step["action"] = "start_casting"
        steps.append(step)
    merged = []
    for i, step in enumerate(steps):
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        opener = {"open_terminal": "terminal_run", "open_browser": "browser_task"}
        if nxt and opener.get(step["action"]) == nxt["action"]:
            nxt["say"] = " ".join(x for x in (step["say"], nxt["say"]) if x)  # the next step opens it anyway
            continue
        if step["action"] == "open_terminal":
            step["action"] = "say"  # opening an idle terminal alone does nothing useful
        merged.append(step)
    return merged


def validate(args: dict, script: str | None = None, planner=None) -> tuple[list[dict], int | None, list[str]]:
    """(steps, workspace, notes). Never rejects a plan for missing details:
    repairs, asks the planner (with the script text) once for every step
    still incomplete, and narrates whatever still cannot be resolved."""
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
    steps = _repair(raw, workspace)
    incomplete = [i for i, st in enumerate(steps) if _missing(st)]
    notes = []
    if incomplete and planner is not None:
        try:
            answer = planner(PLANNER_SYSTEM, {"script": (script or "")[:6000], "steps": [
                {"index": i, "say": steps[i]["say"], "action": steps[i]["action"], "have": steps[i]["args"]}
                for i in incomplete]})
            for fix in answer.get("steps") or []:
                i = fix.get("index")
                if isinstance(i, int) and 0 <= i < len(steps) and fix.get("action") in ACTIONS:
                    steps[i]["action"] = fix["action"]
                    steps[i]["args"].update({k: fix[k] for k in FIELDS if fix.get(k) not in (None, "")})
        except Exception as exc:  # planner is best-effort
            notes.append(f"planner unavailable: {str(exc)[:80]}")
    for i, st in enumerate(steps, 1):
        missing = _missing(st)
        if missing:
            notes.append(f"step {i} ({st['action']}) had no {', '.join(missing)}: narrated only")
            st["action"], st["args"] = "say", {}
    steps = [st for st in steps if st["action"] != "say" or st["say"]]
    if not steps:
        raise ValueError("no steps left to run")
    return steps, workspace, notes


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


def demo_file(content: str) -> ActionResult:
    """Write a small demo document, then edit it, and prove both."""
    from .actions import edit_file, read_file, write_file
    path = str(Path(DEMO_FILE).expanduser())
    written = write_file({"path": path, "content": content.strip() + "\n", "overwrite": True})
    if not written.ok:
        return written
    edited = edit_file({"path": path, "old_text": content.strip().splitlines()[0],
                        "new_text": content.strip().splitlines()[0] + " (edited by Omarchy AI)"})
    if not edited.ok:
        return edited
    after = read_file({"path": path})
    return ActionResult(after.ok, f"wrote and edited {path}: {after.message[:200]}")


def run_step(step: dict) -> ActionResult:
    action, args = step["action"], step["args"]
    if action == "say":
        return ActionResult(True, "narration only")
    if action == "wait":
        time.sleep(min(max(float(args["seconds"]), 0), 30))
        return ActionResult(True, "waited")
    if action == "terminal_run":
        # A mission is a demonstration: always on screen, typed via tmux.
        from .actions import terminal_task
        return terminal_task({"command": str(args["command"]), "name": "mission", "show": "yes"})
    if action == "demo_file":
        return demo_file(str(args["content"]))
    if action == "show_windows":
        return run_action("list_windows", {})
    if action == "workspace_switch":
        run_action(action, {"number": int(args["number"])})
        return ensure_workspace(int(args["number"]))
    if action == "browser_task":
        # A demo is watched: never the co-pilot background mode. Real
        # 2026-09-23 run: "browser task running in the background while the
        # user works", so the page never appeared on screen.
        args = {**args, "show": "yes"}
    return run_action(action, args)
