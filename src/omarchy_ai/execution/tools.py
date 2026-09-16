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
        "read_tile_log",
        "Read the real text output of a terminal this assistant opened "
        "with open_terminal — everything printed in it, plus what was "
        "typed. Use this instead of describe_screen to check on a "
        "terminal's output or whether a command finished — it's real "
        "text, not a vision call, so it's much faster. Only tracks "
        "terminals opened via open_terminal, not every terminal window "
        "on screen — if nothing matches, the result says so and lists "
        "what is currently tracked.",
        {
            "type": "object",
            "properties": {
                "window": {
                    "type": "string",
                    "description": (
                        "Which terminal, matched fuzzily against its "
                        "window title (e.g. a directory name or running "
                        "command shown in the title). Omit if only one "
                        "tracked terminal is open."
                    ),
                }
            },
            "required": [],
        },
    ),
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
        "show_window_labels",
        "Show a real floating name-label badge on top of each candidate "
        "window, positioned at that window's actual on-screen rectangle. "
        "Use this right before you ask the user 'which window do you "
        "mean?' out loud, scoped to just the ambiguous candidates (by app "
        "or title) — so they can look at the screen and answer instead of "
        "you listing windows in speech. Call hide_window_labels as soon "
        "as you have their answer, whether or not you then act on it.",
        {
            "type": "object",
            "properties": {
                "targets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "App names or title substrings, one per candidate "
                        "window — same fuzzy matching as focus_window's "
                        "target. Omit or leave empty to label every open "
                        "window instead of a specific set."
                    ),
                }
            },
            "required": [],
        },
    ),
    _tool(
        "hide_window_labels",
        "Clear any window name-label badges currently shown by "
        "show_window_labels.",
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
    _tool(
        "start_casting",
        "Start mirroring this desktop's screen and audio to an Android "
        "TV/projector over the local network. Connects to the TV, launches "
        "the receiver app on it if needed, and starts streaming — takes a "
        "few seconds. Safe to call again if already casting (no-op). If "
        "the user named a specific TV, pass it as target. If no target is "
        "given and more than one TV is currently discoverable on the "
        "network, this returns ok=false with the list of candidates in "
        "the message instead of guessing — call list_cast_targets, ask "
        "the user which one, then call this again with their answer as "
        "target. If exactly one TV is discoverable (or none, in which "
        "case it falls back to the one TV already known from before), it "
        "just works with no target needed.",
        {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "Which TV to cast to — a device name as returned "
                        "by list_cast_targets (e.g. 'Living Room TV', "
                        "matched case-insensitively as a substring), or a "
                        "raw IP address. Omit if the user didn't name one "
                        "and there's no ambiguity."
                    ),
                }
            },
            "required": [],
        },
    ),
    _tool(
        "stop_casting",
        "Stop mirroring the screen/audio to the TV/projector. Safe to call "
        "even if nothing is currently casting.",
    ),
    _tool(
        "list_cast_targets",
        "List Android TVs currently discoverable on the local network (via "
        "mDNS — real-time, not a fixed list), each with a name and IP. Call "
        "this when the user asks what TVs/casting targets are available, or "
        "right before asking 'which TV do you mean?' after start_casting "
        "comes back ambiguous — read the names out and let them answer by "
        "name (e.g. 'the Living Room TV' or 'Idol TV'), then call "
        "start_casting again with that name as target. This only finds "
        "TVs that already have the Android TV remote-control service "
        "running — a TV that's never been set up at all won't appear here; "
        "use install_receiver_on_tv for that case instead.",
    ),
    _tool(
        "install_receiver_on_tv",
        "Install or update the receiver app on a TV. Two very different "
        "cases, both handled by this one tool: (1) the TV named by target "
        "(or the only TV currently reachable, if target is omitted) is "
        "already paired and known — this just checks its installed "
        "version against the latest build and reinstalls only if it's "
        "out of date, instantly, no back-and-forth at all; use this for "
        "'update the receiver', 'install the newer version', or similar "
        "on a TV that's already been cast to before. (2) target doesn't "
        "match any TV currently known/reachable — treated as a brand-new "
        "TV that's never been set up, and this becomes a real guided, "
        "multi-step back-and-forth: Android requires the user to "
        "personally enable Developer options and Wireless debugging on "
        "the TV's own screen first, which cannot be done remotely or "
        "skipped. Call this with no pairing_code first: if no TV is "
        "mid-setup yet, it returns the exact steps to narrate to the user "
        "(Settings > About > tap the build entry repeatedly to unlock "
        "Developer options > Developer options > Wireless debugging on > "
        "Pair device with pairing code); once the user says they see a "
        "pairing screen, call it again (still no pairing_code, same "
        "target) to check whether it's now discoverable. Once found, ask "
        "the user to read the code shown on the TV out loud (or type "
        "it), then call this a final time with that code as pairing_code "
        "— it will pair, connect, and install the app, or explain "
        "exactly what went wrong if something fails. Building the APK "
        "first if it isn't already built (or has changed since the last "
        "build) can take a while — say so rather than going quiet.",
        {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "Which TV — a device name as returned by "
                        "list_cast_targets (e.g. 'Living Room TV'), or a "
                        "raw IP address. Omit if the user didn't name one; "
                        "with no target, an already-paired, uniquely "
                        "reachable TV is still detected and updated in "
                        "place, so this is safe to omit for 'update the "
                        "receiver' when there's no ambiguity about which "
                        "TV is meant."
                    ),
                },
                "pairing_code": {
                    "type": "string",
                    "description": (
                        "The pairing code currently shown on the TV's "
                        "screen, read out or typed by the user. Only "
                        "relevant for a genuinely new TV (case 2 above) — "
                        "omit for an update/reinstall on an already-paired "
                        "TV, and omit the first time through a new TV's "
                        "setup too, to just check/discover a "
                        "pairing-in-progress TV and get the narrated steps."
                    ),
                },
            },
            "required": [],
        },
    ),
    _tool(
        "set_reminder",
        "Sets a lightweight desktop notification reminder that pops up "
        "after a number of minutes — Omarchy's own built-in reminder "
        "mechanism (`omarchy reminder`), not a new invention. It only "
        "understands minutes from now, not absolute times or dates: "
        "convert whatever the user said ('in 20 minutes', 'in an hour', "
        "'at 3pm') into a minute count yourself before calling this. "
        "Prefer this over run_omarchy_command for reminders.",
        {
            "type": "object",
            "properties": {
                "minutes": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "How many minutes from now the reminder should fire.",
                },
                "message": {
                    "type": "string",
                    "description": "What the reminder should say. Omit for a plain reminder with no message.",
                },
            },
            "required": ["minutes"],
        },
    ),
    _tool(
        "list_reminders",
        "Lists reminders that are currently set and haven't fired yet, "
        "with their message and how long until each one fires. Call this "
        "if asked what reminders are set, or whether one was actually "
        "set, rather than assuming.",
    ),
    _tool(
        "clear_reminders",
        "Cancels every currently-set reminder. There's no way to cancel "
        "just one — say so if the user only wants one specific reminder "
        "gone and more than one is set (list_reminders first to check).",
    ),
    _tool(
        "run_omarchy_command",
        "Runs an `omarchy` CLI command — the same command-line tool used "
        "for Omarchy desktop customization (themes, bar layout, toggles "
        "like night light/bluetooth). Only a fixed allowlist of safe "
        "command groups can actually run through this (theme, toggle, "
        "reminder, bar, capture) — anything involving packages, system "
        "updates, reinstalling, hooks, plugins, or system power is "
        "refused. Pass args as the full argv after 'omarchy' itself, "
        "e.g. ['theme', 'set', 'catppuccin'], ['toggle', 'nightlight'], "
        "['bar', 'move', 'omarchy.clock', '--section', 'right']. Use "
        "this for anything the user asks for that matches one of those "
        "areas and isn't already covered by a more specific tool — "
        "nightlight_toggle and set_reminder/list_reminders/"
        "clear_reminders already exist and are preferred over this for "
        "those cases.",
        {
            "type": "object",
            "properties": {
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "The omarchy CLI arguments, in order, as separate "
                        "strings — first element is the command group "
                        "(theme/toggle/reminder/bar/capture), the rest are "
                        "whatever that group's subcommand takes."
                    ),
                }
            },
            "required": ["args"],
        },
    ),
]

# --- MyApi (myapiai.com) tools --------------------------------------------
# Kept out of TOOLS deliberately: these are only meaningful once a MyApi
# connection actually exists, so voice/live.py's build_session_config
# spreads this list in conditionally (myapi.is_connected()) instead of
# unconditionally like everything above — an unconnected user should never
# see the model attempt (and fail) a MyApi call it has no way to make
# succeed. myapi_call is GET-only for the same "no confirm/policy layer
# yet" reasoning as run_omarchy_command's own allowlist above; see
# execution/actions.py's myapi_call for the enforcement.
MYAPI_TOOLS: list[dict] = [
    _tool(
        "myapi_list_services",
        "Lists the services connected to the user's MyApi account (Gmail, "
        "Calendar, Drive, Notion, Slack, and whatever else they've "
        "connected at myapiai.com). Call this first if you're not sure "
        "what's available before assuming a request can or can't be "
        "answered through MyApi.",
    ),
    _tool(
        "myapi_service_methods",
        "Looks up what operations are actually callable on one connected "
        "MyApi service — call this before a service's first use in a "
        "conversation so you know the right path/shape to pass to "
        "myapi_call, rather than guessing at a REST path.",
        {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": (
                        "The service's id as returned by myapi_list_services, "
                        "e.g. 'gmail', 'googlecalendar', 'notion'."
                    ),
                }
            },
            "required": ["service"],
        },
    ),
    _tool(
        "myapi_call",
        "Reads data from a connected MyApi service — e.g. checking email, "
        "looking up a calendar, searching Drive/Notion. Prefer this over "
        "opening a browser and using describe_screen whenever the request "
        "is covered by a connected service: it's structured data, no "
        "vision call needed, and much faster. Only GET requests work here "
        "— this cannot send, create, update, or delete anything yet (no "
        "email sending, no calendar-event creation); say so plainly if "
        "asked for that rather than attempting it.",
        {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "The service id, e.g. 'gmail', 'googlecalendar', 'notion' — see myapi_list_services.",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "The service's own REST path to read, e.g. "
                        "'/gmail/v1/users/me/messages' for gmail — see myapi_service_methods "
                        "for what's available on this service."
                    ),
                },
                "query": {
                    "type": "object",
                    "description": "Optional query-string parameters for the request, as key/value pairs.",
                },
            },
            "required": ["service", "path"],
        },
    ),
]
