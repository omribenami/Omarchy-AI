"""Local file actions (list/read/write/edit) and GitHub attachment upload.

Split out of the former execution/actions.py; see execution/actions/__init__.py."""

from __future__ import annotations

import json
import re

from ...config import load_config
from .. import files as local_files
from .. import sudo_approval
from .base import ActionResult


def github_upload_attachment(args: dict) -> ActionResult:
    from .. import github_upload
    return github_upload.upload(str(args.get("path") or ""), str(args.get("repo") or ""))


# --- local files -----------------------------------------------------------

def list_files(args: dict) -> ActionResult:
    try:
        result = local_files.list_files(args.get("path"), load_config(), bool(args.get("recursive")))
        return ActionResult(True, json.dumps(result))
    except (local_files.FileAccessError, ValueError) as exc:
        return ActionResult(False, str(exc))


def read_file(args: dict) -> ActionResult:
    try:
        result = local_files.read_file(
            args.get("path"), load_config(), args.get("start_line", 1), args.get("max_chars", 4_000),
            system_config=bool(args.get("system_config")),
        )
        if result and _looks_like_script(result, str(args.get("path") or "")):
            result += _SCRIPT_HINT
        return ActionResult(True, result or "(empty file)")
    except (local_files.FileAccessError, ValueError) as exc:
        return ActionResult(False, str(exc))


# Real Gemini Live, commercial_prompt.md (2026-09-23): after read_file it
# ignored run_mission and started doing the steps one tool at a time again,
# which is how narration and step order were lost. The nudge belongs in the
# result the model is reading at that moment, not only in a long prompt.
_SCRIPT_HINT = ("\n\n[assistant note] This file is a multi-step script. If the user asked you to perform it, "
                "call run_mission ONCE now with all of its steps (say + action each, its exact targets and "
                "workspace rule); do not perform the steps yourself one by one.")


# Journal 2026-09-27 08:18-08:20: README.md (a page of "- " bullets) was
# marked a script, and the run_mission guard then blocked her grep and
# git diff twice in the middle of an ordinary README edit.
_NOT_SCRIPTS = re.compile(r"(^|/)(readme|changelog|license|contributing|status|adr-[^/]*)(\.[a-z]+)?$|"
                          r"\.(py|js|ts|sh|json|toml|ya?ml|lua|conf|ini|html|css)$", re.I)


def _looks_like_script(text: str, path: str = "") -> bool:
    if _NOT_SCRIPTS.search(path.strip()):
        return False
    lines = [l.strip().lower() for l in text.splitlines() if l.strip()]
    numbered = sum(1 for l in lines if re.match(r"^(\d+[.)]|step \d+)", l))
    bullets = sum(1 for l in lines if l.startswith("- "))
    # Journal 2026-10-05 16:08:59: a 204-line feature inventory (79 table
    # rows, 49 bullets, the word "Narration" in one cell) was marked a script
    # and her next find was refused. More table rows than steps is a
    # reference document.
    if sum(1 for l in lines if l.startswith("|")) > numbered + bullets:
        return False
    cues = sum(w in text.lower() for w in ("narrat", "demonstrat", "step", "then ", "finally", "workspace"))
    return (numbered >= 3 and cues >= 1) or (numbered + bullets >= 3 and "narrat" in text.lower())


def write_file(args: dict) -> ActionResult:
    try:
        path = local_files.write_file(
            args.get("path"), args.get("content"), load_config(), bool(args.get("overwrite")),
        )
        return ActionResult(True, f"saved {path}")
    except (local_files.FileAccessError, ValueError) as exc:
        return ActionResult(False, str(exc))


def edit_file(args: dict) -> ActionResult:
    try:
        config = load_config()
        privileged = bool(args.get("privileged"))
        password = None
        if privileged:
            from .. import flux_approve
            # With Flux's phone approval on for sudo, the phone approves the
            # edit with a fingerprint and no saved password is used.
            if not flux_approve.sudo_ready():
                if not config.sudo_access_enabled:
                    return ActionResult(False, "persistent Sudo Access is disabled in Assistant Settings")
                password = sudo_approval.retrieve()
        path, count = local_files.edit_file(
            args.get("path"), args.get("old_text"), args.get("new_text"), config,
            expected_replacements=args.get("expected_replacements", 1),
            privileged=privileged, sudo_password=password,
        )
        return ActionResult(True, f"updated and verified {path} ({count} exact replacement(s))")
    except (local_files.FileAccessError, ValueError) as exc:
        return ActionResult(False, str(exc))
