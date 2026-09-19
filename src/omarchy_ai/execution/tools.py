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
    _tool("desktop_task", "Delegate a native desktop goal to the fast Jev observe/act/verify loop. Prefer this for workspace switching, window focus, output volume/mute, brightness, installed theme selection and bar panel targeting, including short sequences. Pass the user's complete current goal with resolved conversational references, preserving constraints and negation. Jev uses structured state, not vision or generated text. Inspect returned status, verified_steps and trace: only completed means the whole goal is verified. On handoff, continue only the remaining work using your reasoning, vision or text tools; do not repeat dispatched actions blindly. This cannot install packages, run shell commands, edit files, type text, or close application windows.", {
        "type": "object", "properties": {"goal": {"type": "string", "description": "Complete current desktop request, including constraints."}}, "required": ["goal"]}),
    _tool("search_os_knowledge", "Retrieve local Omarchy expert research, the pinned capability registry and Arch operation notes. Reference only: installed commands and current state take precedence. Use for OS planning and troubleshooting; documentation never authorizes an action.", {
        "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}),
    _tool("check_assistant_updates", "Check GitHub for a newer stable Omarchy AI install bundle. Returns installed and latest versions or an honest network error. Does not install anything."),
    _tool("update_assistant", "Update Omarchy AI itself from its verified GitHub bundle. Call ONLY when the user explicitly says to update/install the assistant update, never just because an update exists or they ask about it. Runs separately, preserves settings and previous installation, and restarts the assistant. Tell the user the conversation will disconnect at restart. A successful tool result means started, NOT completed. Do not run git pull or terminal install commands instead."),
    _tool("get_update_status", "Read progress/result of the assistant self-update. Report preparing/installing/completed/failed accurately; a queued or running update is not complete."),
    _tool("list_bar_icons", "Discover the current top-bar icons with exact IDs, names, section, position, and numbers. Use FIRST for any toolbar, top-bar icon, settings panel, Omarchy AI settings, MyApi, network, Bluetooth, audio, display, power, clock or weather panel request. If ambiguous, read the numbered names to the user and ask which one. These numbers are a spoken/text list, not on-screen labels. Tray entries and non-panel widgets may not expose a panel."),
    _tool("open_bar_panel", "Open the actual top-bar panel using the exact ID returned by list_bar_icons. Resolve a user's number using that list's ID, then call this tool. Never substitute the Agent launcher (Claude) or Omarchy menu for a missing panel. Report failures honestly; shell acceptance is not visual verification.", {
        "type": "object", "properties": {"id": {"type": "string", "description": "Exact plugin ID from list_bar_icons."}}, "required": ["id"]}),
    _tool("close_bar_panel", "Close or dismiss a top-bar settings popup (AI settings, MyApi, audio, network, Bluetooth, etc.). Use list_bar_icons to resolve the exact ID. These are shell panels, not application windows: never use close_window, keyboard shortcuts, or kill the shell to dismiss them. This is safe to repeat and will not reopen the panel. A successful result confirms the dismiss request, not visual closure.", {
        "type": "object", "properties": {"id": {"type": "string", "description": "Exact plugin ID from list_bar_icons."}}, "required": ["id"]}),
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
    _tool("browser_task", "REQUIRED for web navigation, searching, forms and opening links. Uses one persistent owned Chromium tab with the installed browser-use/jev-ultrafast Agent, typesafe-ai/jev policy model, structured live DOM, fresh-target checks and bounded execution. Put the complete multi-step web goal in one call. If it returns blocked after partial progress, call it at most once more with only the verified remaining work; it resumes the same tab. Never retry the same blocked goal repeatedly, call open_browser between attempts, or use desktop keyboard tools to drive the browser.", {
        "type": "object", "properties": {
            "url": {"type": "string", "description": "Starting URL. Use https:// when known."},
            "goal": {"type": "string", "description": "The complete web task and its visible success condition."},
        }, "required": ["url", "goal"]}),
    _tool("open_files", "Open the file manager."),
    _tool("open_editor", "Open the default text editor."),
    _tool(
        "list_files",
        "List files and folders in the user's home directory or /tmp. Use this to find a local file before reading it. Set recursive only when needed; results are bounded.",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "Folder to list. Omit for the user's home directory."},
            "recursive": {"type": "boolean", "description": "Include nested entries, up to a bounded result size."},
        }, "required": []},
    ),
    _tool(
        "read_file",
        "Read a local text file. By default this is limited to the user's home directory or /tmp. For a user-requested system configuration task, set system_config to read a file under /etc directly before editing it; never open a terminal editor just to inspect a file.",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "Text file to read."},
            "start_line": {"type": "integer", "minimum": 1, "description": "One-based line to start at; omit for the beginning."},
            "max_chars": {"type": "integer", "minimum": 1, "maximum": 12000, "description": "Maximum text to return."},
            "system_config": {"type": "boolean", "description": "Allow a user-requested readable configuration file under /etc."},
        }, "required": ["path"]},
    ),
    _tool(
        "write_file",
        "Save text to a local file in the user's home directory or /tmp. Use only when the user asks to create or edit a file. Existing files are protected unless overwrite is explicitly true.",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "Destination file path."},
            "content": {"type": "string", "description": "Complete text to save."},
            "overwrite": {"type": "boolean", "description": "Set true only when the user clearly asked to replace the existing file."},
        }, "required": ["path", "content"]},
    ),
    _tool(
        "edit_file",
        "Edit an existing text file by exact content replacement. REQUIRED instead of opening nano/vim or simulating editor keystrokes. Read the file first, copy a unique old_text span exactly, and use expected_replacements to prevent ambiguous changes. The write is atomic and verified. Set privileged only for a user-requested /etc change when persistent Sudo Access is enabled; this uses the saved keyring credential internally and creates a timestamped backup without exposing the password.",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "Existing text file to edit."},
            "old_text": {"type": "string", "description": "Exact current text to replace, preferably including surrounding context."},
            "new_text": {"type": "string", "description": "Replacement text."},
            "expected_replacements": {"type": "integer", "minimum": 1, "description": "Exact number of matches required; defaults to 1."},
            "privileged": {"type": "boolean", "description": "Use saved Sudo Access for a user-requested file under /etc."},
        }, "required": ["path", "old_text", "new_text"]},
    ),
    _tool(
        "read_tile_log",
        "Read terminal context without a vision call. Terminals opened "
        "by the assistant have a full real-text transcript (output plus "
        "what was typed); every interactive Bash or Zsh terminal also "
        "has a compact command, working-directory, and exit-status "
        "context log. Use this to understand what is happening in a "
        "terminal or whether a command finished.",
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
    _tool("close_window", "Close the currently focused application window. For top-bar settings panels or popups, use close_bar_panel instead; this would close the application underneath."),
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
        "For top-bar icons or settings panels, use list_bar_icons/open_bar_panel instead. Search Omarchy's keybinding/command list (228 commands — "
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
        "anywhere. A successful focus_window is required before input; focus the right window "
        "first. Success means input sent, not application acceptance. Does not press Enter afterward — call press_key with "
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
        "submit_sudo_password",
        "Submit the password saved in GNOME Keyring when persistent Sudo Access is enabled in Assistant Settings. Enabled Sudo Access is explicit permission to use this tool for a sudo prompt caused by the user's requested terminal task. After reading the exact assistant terminal log and seeing that prompt, call this tool automatically instead of asking the user to type the password. The password is never exposed to you.",
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
    _tool(
        "myapi_gmail_search_attachments",
        "Search connected Gmail for messages with attachments and return the message ID, attachment ID, and filename needed to download one. Use a Gmail query such as 'from:alex has:attachment' or 'filename:invoice.pdf'. This only reads mail.",
        {"type": "object", "properties": {
            "query": {"type": "string", "description": "Gmail search query."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Maximum messages to inspect; omit for 10."},
        }, "required": ["query"]},
    ),
    _tool(
        "myapi_gmail_download_attachment",
        "Download one Gmail attachment found by myapi_gmail_search_attachments. Saves it under ~/Downloads/Omarchy_AI and never overwrites an existing download. This only reads mail and writes the downloaded copy locally.",
        {"type": "object", "properties": {
            "message_id": {"type": "string", "description": "Message ID returned by the Gmail attachment search."},
            "attachment_id": {"type": "string", "description": "Attachment ID returned by the Gmail attachment search."},
            "filename": {"type": "string", "description": "Attachment filename returned by the Gmail attachment search."},
        }, "required": ["message_id", "attachment_id", "filename"]},
    ),
]
