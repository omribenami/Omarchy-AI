"""What each tool call actually runs on the OS.

Ported from jarvisd (~/Git/jarvisd/src/jarvisd/actions.py), which proved
these out against this exact machine. Deliberately excludes logout/reboot/
shutdown — those are Level 3/4 per ADR-0001's policy table and need a real
confirm/policy layer before they're reachable by voice at all, not just a
description telling the model to ask first.

Every action shells out through an explicit argv list — never shell=True —
because arguments here can originate from a speech transcript by way of the
model's tool-call arguments, and that is untrusted input same as any other.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import base64
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ... import myapi
from ...config import load_config
from ...myapi import usage as myapi_usage
from .. import files as local_files
from .. import sudo_approval
from ..desktop_env import desktop_env

from .base import (  # noqa: F401
    ActionResult,
    _TIMEOUT,
    _clamp_percent,
    _desktop_env,
    _hyprctl_dispatch,
    _run,
    _run_detached,
)
from .media import (  # noqa: F401
    NO_MEDIA_PLAYER,
    _MPRIS,
    _mpris,
    _mpris_status,
    media_next,
    media_play_pause,
    media_prev,
    mic_mute_toggle,
    volume_down,
    volume_mute_toggle,
    volume_set,
    volume_up,
)
from .files import (  # noqa: F401
    _NOT_SCRIPTS,
    _SCRIPT_HINT,
    _looks_like_script,
    edit_file,
    github_upload_attachment,
    list_files,
    read_file,
    write_file,
)
from .browser import (  # noqa: F401
    browser_control,
    browser_task,
    inspect_browser,
    open_browser,
    show_browser,
)
from .terminal import (  # noqa: F401
    open_terminal,
    read_tile_log,
    terminal_close,
    terminal_hide,
    terminal_read,
    terminal_show,
    terminal_sudo,
    terminal_task,
    terminal_type,
)
from .desktop import (  # noqa: F401
    SCREEN_LOCKED,
    _OMARCHY_BLOCKED_SUBCOMMANDS,
    _OMARCHY_SAFE_GROUPS,
    _TERMINAL_CLASSES,
    _WTYPE_MODIFIERS,
    _describe_screen_raw,
    _gateway_key_configured,
    _keyboard_blocked,
    _paste_text,
    _screen_context,
    _screen_locked,
    _terminal_text_hint,
    battery_status,
    bluetooth_toggle,
    brightness_down,
    brightness_set,
    brightness_up,
    clear_reminders,
    close_bar_panel,
    describe_screen,
    execute_command,
    list_bar_icons,
    list_commands,
    list_reminders,
    lock_screen,
    nightlight_toggle,
    open_bar_panel,
    open_editor,
    open_files,
    press_key,
    run_omarchy_command,
    screenshot,
    set_reminder,
    submit_sudo_password,
    type_text,
    unlock_screen,
    unlock_screen_for_paired_phone,
)
from .windows import (  # noqa: F401
    _SSH_VALUE_FLAGS,
    _THIS_WINDOW,
    _WINDOW_KINDS,
    _WRAPPER_COMMS,
    _jev_window,
    _matching_windows,
    _process_children,
    _rank_windows,
    _remote_session,
    _running_program,
    _ssh_destination,
    _with_programs,
    close_window,
    focus_window,
    hide_window_labels,
    list_windows,
    move_window_to_workspace,
    show_window_labels,
    window_fullscreen_toggle,
    workspace_next,
    workspace_prev,
    workspace_switch,
)
from .assistant import (  # noqa: F401
    _SKILL_READ,
    _resolve_scheduled,
    cancel_scheduled_task,
    check_assistant_updates,
    delete_skill,
    find_skill,
    get_release_notes,
    get_update_status,
    hide_routines_hud,
    hide_tasks_hud,
    list_scheduled_tasks,
    list_skills,
    load_skill,
    remember_preference,
    report_issue,
    run_mission,
    run_scheduled_task_now,
    run_skill,
    save_skill,
    schedule_task,
    show_routines_hud,
    show_tasks_hud,
    update_assistant,
    watch_camera,
)
from .casting import (  # noqa: F401
    _APK_BUILD_MARKER,
    _APK_PATH,
    _REPO_ROOT,
    _SIGNALING_PORT,
    _VENV_PYTHON,
    _aapt_path,
    _discover_androidtv_devices,
    _ensure_receiver_apk_built,
    _ensure_receiver_current,
    _install_or_update_receiver,
    _installed_receiver_version,
    _local_apk_version,
    _looks_like_address,
    _pending_pair_target,
    _port_listening,
    _reachable_target_for_install,
    _resolve_cast_target,
    _source_fingerprint,
    _start_casting,
    install_receiver_on_tv,
    list_cast_targets,
    start_casting,
    stop_casting,
)
from .myapi_tools import (  # noqa: F401
    _GMAIL_DIGEST_CHARS,
    _download_url,
    _find_base64_payload,
    _find_download_url,
    _gmail_digest,
    _gmail_execute,
    _gmail_fallback_queries,
    _gmail_messages,
    _gmail_search_with_fallback,
    _myapi_execute,
    _safe_download_name,
    _walk_gmail_attachments,
    myapi_call,
    myapi_execute,
    myapi_gmail_download_attachment,
    myapi_gmail_reply,
    myapi_gmail_search,
    myapi_gmail_search_attachments,
    myapi_gmail_send,
    myapi_list_services,
    myapi_service_methods,
    myapi_vault_list,
    myapi_write,
)

log = logging.getLogger("omarchy_ai.execution.actions")


ACTIONS = {
    "check_assistant_updates": check_assistant_updates,
    "update_assistant": update_assistant,
    "get_update_status": get_update_status,
    "get_release_notes": get_release_notes,
    "schedule_task": schedule_task,
    "watch_camera": watch_camera,
    "list_scheduled_tasks": list_scheduled_tasks,
    "show_tasks_hud": show_tasks_hud,
    "hide_tasks_hud": hide_tasks_hud,
    "show_routines_hud": show_routines_hud,
    "hide_routines_hud": hide_routines_hud,
    "cancel_scheduled_task": cancel_scheduled_task,
    "run_scheduled_task_now": run_scheduled_task_now,
    "find_skill": find_skill,
    "load_skill": load_skill,
    "run_skill": run_skill,
    "save_skill": save_skill,
    "list_skills": list_skills,
    "delete_skill": delete_skill,
    "report_issue": report_issue,
    "list_bar_icons": list_bar_icons,
    "open_bar_panel": open_bar_panel,
    "close_bar_panel": close_bar_panel,
    "volume_up": volume_up,
    "volume_down": volume_down,
    "volume_mute_toggle": volume_mute_toggle,
    "volume_set": volume_set,
    "mic_mute_toggle": mic_mute_toggle,
    "brightness_up": brightness_up,
    "brightness_down": brightness_down,
    "brightness_set": brightness_set,
    "screenshot": screenshot,
    "lock_screen": lock_screen,
    "unlock_screen": unlock_screen,
    "github_upload_attachment": github_upload_attachment,
    "open_terminal": open_terminal,
    "read_tile_log": read_tile_log,
    "open_browser": open_browser,
    "browser_task": browser_task,
    "inspect_browser": inspect_browser,
    "show_browser": show_browser,
    "browser_control": browser_control,
    "open_files": open_files,
    "open_editor": open_editor,
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "workspace_switch": workspace_switch,
    "workspace_next": workspace_next,
    "workspace_prev": workspace_prev,
    "close_window": close_window,
    "window_fullscreen_toggle": window_fullscreen_toggle,
    "list_windows": list_windows,
    "focus_window": focus_window,
    "move_window_to_workspace": move_window_to_workspace,
    "run_mission": run_mission,
    "terminal_task": terminal_task,
    "terminal_read": terminal_read,
    "terminal_type": terminal_type,
    "terminal_show": terminal_show,
    "terminal_hide": terminal_hide,
    "terminal_close": terminal_close,
    "terminal_sudo": terminal_sudo,
    "show_window_labels": show_window_labels,
    "hide_window_labels": hide_window_labels,
    "describe_screen": describe_screen,
    "remember_preference": remember_preference,
    "list_commands": list_commands,
    "execute_command": execute_command,
    "type_text": type_text,
    "press_key": press_key,
    "submit_sudo_password": submit_sudo_password,
    "bluetooth_toggle": bluetooth_toggle,
    "nightlight_toggle": nightlight_toggle,
    "battery_status": battery_status,
    "media_play_pause": media_play_pause,
    "media_next": media_next,
    "media_prev": media_prev,
    "start_casting": start_casting,
    "stop_casting": stop_casting,
    "list_cast_targets": list_cast_targets,
    "install_receiver_on_tv": install_receiver_on_tv,
    "run_omarchy_command": run_omarchy_command,
    "set_reminder": set_reminder,
    "list_reminders": list_reminders,
    "clear_reminders": clear_reminders,
    "myapi_list_services": myapi_list_services,
    "myapi_vault_list": myapi_vault_list,
    "myapi_service_methods": myapi_service_methods,
    "myapi_call": myapi_call,
    "myapi_write": myapi_write,
    "myapi_execute": myapi_execute,
    "myapi": lambda args: __import__("omarchy_ai.execution.myapi_agent", fromlist=["run"]).run(args),
    "myapi_gmail_send": myapi_gmail_send,
    "myapi_gmail_reply": myapi_gmail_reply,
    "myapi_gmail_search": myapi_gmail_search,
    "myapi_gmail_search_attachments": myapi_gmail_search_attachments,
    "myapi_gmail_download_attachment": myapi_gmail_download_attachment,
}


def desktop_task(args: dict) -> ActionResult:
    from ..desktop_jev import desktop_task as execute
    return execute(args)


def search_os_knowledge(args: dict) -> ActionResult:
    from ..os_knowledge import search_os_knowledge as search
    return search(args)


def _task_tool(name: str):
    def call(args: dict) -> ActionResult:
        from ...runtime import service
        return getattr(service, name)(args)
    return call


ACTIONS.update(desktop_task=desktop_task, search_os_knowledge=search_os_knowledge,
               start_task=_task_tool("start_task"), task_status=_task_tool("task_status"),
               task_respond=_task_tool("task_respond"))


def run_action(name: str, args: dict) -> ActionResult:
    fn = ACTIONS.get(name)
    if fn is None:
        from .. import user_tools
        if user_tools.exists(name):
            return user_tools.run(name, args)
        log.error("no such action: %s", name)
        return ActionResult(False, f"unknown action '{name}'")
    if name == "myapi" or name.startswith("myapi_"):
        # The owner's account (core/identities.py), checked at each call: the
        # voice speaking can change in the middle of a conversation.
        from ...core import identities
        refusal = identities.myapi_refusal()
        if refusal:
            log.info("refused %s for %s", name, identities.current())
            return ActionResult(False, refusal)
    try:
        return fn(args)
    except Exception:  # noqa: BLE001
        log.exception("action %s raised", name)
        return ActionResult(False, f"{name} crashed")
