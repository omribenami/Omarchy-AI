"""Tool schemas exposed to the model — one entry per function in actions.py.

Kept separate from actions.py: this is what the model sees and reasons
about (names, descriptions, typed parameters), actions.py is what actually
runs. Only Level 1 (read-only) and Level 2 (reversible) actions per
ADR-0001's policy table are exposed here — nothing sensitive/dangerous
(logout, reboot, shutdown, package changes) until there's a real
confirm/policy layer, not just a hopeful description.
"""

from __future__ import annotations

_NO_ARGS = {"type": "object", "properties": {}, "required": []}


def _tool(name: str, description: str, parameters: dict = _NO_ARGS) -> dict:
    return {"type": "function", "name": name, "description": description, "parameters": parameters}


TOOLS: list[dict] = [
    _tool("volume_up", "Raise the system output volume by a small step."),
    _tool("volume_down", "Lower the system output volume by a small step."),
    _tool("volume_mute_toggle", "Toggle mute on the system output."),
    _tool(
        "volume_set",
        "Set the system output volume to an exact percentage.",
        {
            "type": "object",
            "properties": {
                "percent": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                    "description": "Target volume, 0-100.",
                }
            },
            "required": ["percent"],
        },
    ),
    _tool("mic_mute_toggle", "Toggle mute on the microphone."),
    _tool("brightness_up", "Raise screen brightness by a small step."),
    _tool("brightness_down", "Lower screen brightness by a small step."),
    _tool(
        "brightness_set",
        "Set screen brightness to an exact percentage.",
        {
            "type": "object",
            "properties": {
                "percent": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                    "description": "Target brightness, 0-100.",
                }
            },
            "required": ["percent"],
        },
    ),
    _tool("screenshot", "Take a fullscreen screenshot and save it."),
    _tool("lock_screen", "Lock the screen. Reversible (unlock with the password), safe to run without asking."),
    _tool("open_terminal", "Open a new terminal window."),
    _tool("open_browser", "Open the default web browser."),
    _tool("open_files", "Open the file manager."),
    _tool("open_editor", "Open the default text editor."),
    _tool(
        "workspace_switch",
        "Switch to a specific numbered workspace.",
        {
            "type": "object",
            "properties": {
                "number": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 99,
                    "description": "The workspace number to switch to.",
                }
            },
            "required": ["number"],
        },
    ),
    _tool("workspace_next", "Switch to the next workspace."),
    _tool("workspace_prev", "Switch to the previous workspace."),
    _tool("close_window", "Close the currently focused window."),
    _tool(
        "window_fullscreen_toggle",
        "Toggle fullscreen on the currently focused window/tile — makes it "
        "fill the whole screen, or restores it if it's already fullscreen. "
        "Only affects whichever window is currently focused — if that "
        "might not be the one the user means, call list_windows first and "
        "focus_window to target the right one before calling this.",
    ),
    _tool(
        "list_windows",
        "List every open window/tile: which app, its title, which "
        "workspace it's on, whether it's fullscreen, and which one is "
        "currently focused. Call this whenever it's not certain which "
        "window the user is referring to, or before an action that only "
        "affects the focused window (fullscreen, close) if the user "
        "named a specific app rather than just saying 'this'.",
    ),
    _tool(
        "list_commands",
        "Search Omarchy's full keybinding/command list (228 commands — "
        "app launchers, system menus, capture tools, window/workspace "
        "actions, theme/clipboard/emoji pickers, and more) by what the "
        "user described. Use this instead of guessing when they ask for "
        "something outside your other specific tools — most things "
        "Omarchy can do have a bound command. Returns matching titles; "
        "call execute_command with the exact title you want to run.",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What the user described, e.g. 'screenshot', 'clipboard', 'theme'.",
                }
            },
            "required": ["query"],
        },
    ),
    _tool(
        "execute_command",
        "Run one of Omarchy's bound commands by its exact title, as "
        "returned by list_commands. Always call list_commands first to "
        "get the exact title — don't guess one.",
        {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "The exact command title from list_commands.",
                }
            },
            "required": ["title"],
        },
    ),
    _tool(
        "type_text",
        "Type literal text into whichever window is currently focused — "
        "a terminal, a browser address/search bar, a text field, "
        "anywhere. Focus the right window first with focus_window if "
        "needed. Does not press Enter afterward — call press_key with "
        "'Return' separately if the text should be submitted/run.",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The exact text to type."}
            },
            "required": ["text"],
        },
    ),
    _tool(
        "press_key",
        "Press a key, optionally with modifiers, in the focused window — "
        "e.g. 'Return' alone to submit/run what was typed, or ctrl+l to "
        "focus a browser's address bar before typing a URL (window focus "
        "alone does not focus the address bar specifically — confirmed "
        "live that typing without this lands nowhere on a fresh new-tab "
        "page). Other useful combos: ctrl+t (new tab), ctrl+w (close "
        "tab), ctrl+c/ctrl+v (copy/paste), alt+Tab (switch window).",
        {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Key name, e.g. Return, Tab, Escape, BackSpace, l, t, w, c, v.",
                },
                "modifiers": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["shift", "ctrl", "alt", "super"]},
                    "description": "Modifiers to hold while pressing the key, e.g. ['ctrl'] for ctrl+l.",
                },
            },
            "required": ["key"],
        },
    ),
    _tool(
        "get_recent_actions",
        "Recall what actions you've already taken this conversation, "
        "optionally filtered to a specific window/app — use this to avoid "
        "repeating yourself or to answer 'what did you just do' / 'what "
        "have you done to this window' style questions.",
        {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "App name or window title to filter to. Omit for "
                        "everything done this conversation."
                    ),
                }
            },
            "required": [],
        },
    ),
    _tool(
        "describe_screen",
        "Take a screenshot and get a description of what's actually on "
        "screen right now, answering a specific question about it if "
        "given. Only use this when list_windows and the conversation so "
        "far genuinely aren't enough to tell what the user means — it's "
        "slower than the other tools, so don't reach for it by default.",
        {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "What to look for or ask about the screen. Empty "
                        "for a general description."
                    ),
                }
            },
            "required": [],
        },
    ),
    _tool(
        "focus_window",
        "Switch focus to a specific window so subsequent actions (like "
        "fullscreen or close) apply to it. Target by app/class name or "
        "part of the window title (e.g. 'chromium', 'terminal') — matches "
        "case-insensitively against either.",
        {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "App name or part of the window title to focus.",
                }
            },
            "required": ["target"],
        },
    ),
    _tool(
        "remember_preference",
        "Save a standing preference or correction about how you should "
        "behave in future conversations, not just this one — e.g. 'always "
        "confirm before muting', 'call me by my first name', 'don't use "
        "metric units'. Only for an explicit, general instruction about "
        "your own behavior; not for one-off task requests like 'open the "
        "browser'.",
        {
            "type": "object",
            "properties": {
                "preference": {
                    "type": "string",
                    "description": "The standing instruction to remember, written as a clear rule.",
                }
            },
            "required": ["preference"],
        },
    ),
    _tool("bluetooth_toggle", "Turn Bluetooth on if it's off, or off if it's on."),
    _tool("nightlight_toggle", "Turn the night light (warmer screen color) on or off."),
    _tool("battery_status", "Read the current battery charge percentage and charging state."),
    _tool("media_play_pause", "Toggle play/pause on the current media player."),
    _tool("media_next", "Skip to the next track."),
    _tool("media_prev", "Go back to the previous track."),
]
