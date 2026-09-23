"""Jev fast path: simple desktop commands run the moment the user stops talking.

With Gemini as the live provider every decision used to go through Gemini;
Jev ran only if Gemini chose to call it. A plain "switch to workspace 4"
therefore waited on Gemini's turn, and on 2026-09-23 Gemini switched to 5
instead, while "move the focused terminal to workspace 4" stalled in a
command search.

Here Jev (TypeSafe's intent-routing pattern: one Choice over a closed set of
intents plus a gate) reads the transcribed request as soon as the user
pauses. Only an unambiguous single command from a small, code-owned action
set is executed, then verified. Everything else, including anything
conditional, negated, multi-part or uncertain, goes to Gemini as before.
"""
from __future__ import annotations

import json
import logging
import time

from ..core.jev import Jev, JevError, boolean, choice
from ..execution.actions import ActionResult, _run, run_action

log = logging.getLogger("omarchy_ai.voice.jev_fast")

# Code-owned action space: every option maps to fixed arguments.
ACTIONS: dict[str, tuple[str, dict, str]] = {}
for _n in range(1, 11):
    ACTIONS[f"workspace_{_n}"] = ("workspace_switch", {"number": _n}, f"Switch the view to workspace {_n}.")
    ACTIONS[f"move_window_{_n}"] = ("move_window_to_workspace", {"number": _n},
                                    f"Move the focused (current) window to workspace {_n}.")
ACTIONS.update({
    "volume_up": ("volume_up", {}, "Turn the output volume up."),
    "volume_down": ("volume_down", {}, "Turn the output volume down."),
    "volume_mute_toggle": ("volume_mute_toggle", {}, "Mute or unmute the sound."),
    "media_play_pause": ("media_play_pause", {}, "Pause or resume the music or media that is playing."),
    "fullscreen": ("window_fullscreen_toggle", {}, "Make the focused window fullscreen, or leave fullscreen."),
})

THRESHOLD = 0.95        # desktop_jev's mutation bar
SINGLE_THRESHOLD = 0.9


def questions() -> dict:
    return {
        "command": choice(
            "Which single desktop command does the user's latest `request` ask for? Any language, including "
            "Hebrew. Choose other for questions, conversation, anything negated or conditional, more than one "
            "task, a different window than the focused one, or anything not listed.",
            {"other": "None of these, or not exactly one of these.",
             **{key: text for key, (_, _, text) in ACTIONS.items()}}),
        # Wording from a real probe: "no question" made Jev reject "Can you
        # switch to workspace 4?" (0.23) although its command choice was 0.99.
        "single": boolean(
            "Does `request` ask the assistant to do exactly one simple desktop action right now? A polite "
            "request phrased as a question ('can you switch to...?') counts. It does not count if it asks for "
            "information, adds a second task, sets a condition, or says not to do it."),
    }


def decide(text: str, jev: Jev | None = None) -> tuple[str, dict, dict] | None:
    """(tool, args, evidence) for a clear fast command, else None."""
    text = " ".join(str(text or "").split())
    if not 2 <= len(text) <= 300:
        return None
    try:
        answers = (jev or Jev()).ask({"request": text}, questions(), timeout=3, retries=1)
    except JevError as exc:
        log.info("Jev fast path unavailable: %s", str(exc)[:120])
        return None
    command, single = answers["command"], answers["single"]["p"]
    evidence = {"choice": command["choice"], "p": round(command["p"], 2), "single": round(single, 2)}
    if command["choice"] == "other" or command["p"] < THRESHOLD or single < SINGLE_THRESHOLD:
        return None
    tool, args, _ = ACTIONS[command["choice"]]
    return tool, dict(args), evidence


def _active_workspace():
    r = _run(["hyprctl", "activeworkspace", "-j"])
    try:
        return json.loads(r.message).get("id") if r.ok else None
    except ValueError:
        return None


def execute(tool: str, args: dict) -> ActionResult:
    result = run_action(tool, args)
    if tool == "workspace_switch" and result.ok:
        # The dispatch only returns "ok"; check the view really moved.
        for _ in range(10):
            if _active_workspace() == args["number"]:
                return ActionResult(True, f"verified: now on workspace {args['number']}")
            time.sleep(0.03)
        return ActionResult(False, f"dispatched but not verified on workspace {args['number']}")
    return result
