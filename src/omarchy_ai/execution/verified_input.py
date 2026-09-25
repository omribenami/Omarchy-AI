"""Per-conversation keyboard targeting; dispatch success is not task completion."""
import json
import re
import time

from .actions import ActionResult


# Guards WHERE input lands (verified focus), never WHAT is typed. A former
# "this looks like a conversational request" text heuristic was removed on
# purpose: relaying the user's prompts into claude/codex terminals is the
# point (STATUS.md, 2026-09-22), and a phrasing guess cannot tell a prompt
# for another agent from a mistake.
#
# One exact exception: input that ENDS the session in the window. Real
# session 2026-09-24 22:44: mid-sentence ("... to write in the terminal to
# close", the user was explaining, not asking) the model typed `exit` +
# Return into the user's Claude Code window and ended it. The switchboard
# passed it (p=0.95): a sentence fragment reads like a request. Ending a
# shell, an ssh connection or an agent session cannot be undone, so it
# needs the user's answer to a question the assistant actually asked.
_SESSION_END = re.compile(r"\s*/?(exit|logout|quit)(\s+\d+)?\s*;?\s*|\s*(exit|quit)\(\)\s*", re.IGNORECASE)


def ends_session(name: str, args: dict) -> bool:
    if name == "type_text":
        return bool(_SESSION_END.fullmatch(str(args.get("text") or "")))
    if name == "press_key":
        mods = {str(m).lower() for m in args.get("modifiers") or []}
        return str(args.get("key", "")).lower() == "d" and mods == {"ctrl"}
    return False


def where(window: dict | None) -> str:
    """Which machine input to this window runs on, for tool results."""
    machine = (window or {}).get("machine")
    if not machine:
        return ""
    if machine.startswith("REMOTE"):
        return (f" WHERE IT RUNS: {machine}, NOT on this computer. list_files, read_file, write_file, "
                "edit_file and terminal_task only reach this computer. Name the machine when you report.")
    return f" WHERE IT RUNS: {machine}, not a remote server."


class InputGuard:
    # Live probe 2026-09-24: type_text, press_key Return, press_key Return --
    # at an ssh password prompt the second Return sends an empty password.
    DOUBLE_RETURN_SECONDS = 3.0

    CONFIRM_SECONDS = 90.0

    def __init__(self):
        self.address = None
        self._last_return = None  # window of a Return with nothing typed since
        self._return_at = 0.0
        # Session-ending input waiting for the user's answer: its key, when
        # it was blocked, and blocked -> asked -> answered.
        self._confirm_key, self._confirm_at, self._confirm_stage = None, 0.0, ""

    def assistant_replied(self) -> None:
        """The assistant finished speaking (after a block: it asked)."""
        if self._confirm_stage == "blocked":
            self._confirm_stage = "asked"

    def heard_user(self) -> None:
        """The user spoke (after the question: they answered)."""
        if self._confirm_stage == "asked":
            self._confirm_stage = "answered"

    def _session_end_allowed(self, name, args, window) -> ActionResult | None:
        """None to send it; otherwise the block, the first time around."""
        key = (self.address, name, json.dumps(args, sort_keys=True, default=str))
        if key == self._confirm_key and self._confirm_stage == "answered" and \
                time.monotonic() - self._confirm_at < self.CONFIRM_SECONDS:
            self._confirm_key, self._confirm_stage = None, ""
            return None
        self._confirm_key, self._confirm_at, self._confirm_stage = key, time.monotonic(), "blocked"
        what = (window or {}).get("running") or "shell"
        title = (window or {}).get("title") or self.address
        return ActionResult(False, (
            f"Input NOT sent: this would END the {what} session in window {title!r} "
            f"({(window or {}).get('machine') or 'unknown machine'}) and close it. Only do this if the user "
            "explicitly asked to exit THIS session. Ask them one short question naming the window, e.g. "
            f"'End the {what} session in {title}?', and wait for their answer. Retry the same call only if they "
            "say yes; a sentence that got cut off, or the user explaining something, is not a yes."))

    @staticmethod
    def focused_window(execute):
        result = execute("list_windows", {})
        if result.ok:
            try:
                return next((w for w in json.loads(result.message) if w.get("focused")), None)
            except (ValueError, TypeError, AttributeError):
                pass
        return None

    @classmethod
    def focused(cls, execute):
        window = cls.focused_window(execute)
        return window.get("address") if window else None

    def run(self, execute, name, args):
        keyboard = name in {"type_text", "press_key", "submit_sudo_password"}
        window = self.focused_window(execute) if keyboard and self.address else None
        if keyboard and (not self.address or not window or window.get("address") != self.address):
            self.address = None
            return ActionResult(False, "Input NOT sent: focus is unverified or changed. "
                                "Call focus_window for the intended target successfully before retrying.")
        if name == "focus_window":
            self.address = None
        if keyboard and ends_session(name, args):
            blocked = self._session_end_allowed(name, args, window)
            if blocked is not None:
                return blocked
        is_return = name == "press_key" and str(args.get("key", "")).lower() in {"return", "enter"}
        if is_return and self._last_return == self.address and \
                time.monotonic() - self._return_at < self.DOUBLE_RETURN_SECONDS:
            return ActionResult(True, "Not pressed: Return was already pressed in this window after the last "
                                "typed text. Read the window's log instead.")
        if keyboard:
            # submit_sudo_password ends with its own Return.
            ended = is_return or name == "submit_sudo_password"
            self._last_return, self._return_at = (self.address, time.monotonic()) if ended else (None, 0.0)
        result = execute(name, args)
        if name == "focus_window" and result.ok:
            window = self.focused_window(execute)
            self.address = window.get("address") if window else None
            if not self.address:
                return ActionResult(False, "Focus could not be verified; input is blocked. Retry focus_window.")
            result = ActionResult(True, result.message + where(window))
        if name in {"open_terminal", "open_browser", "open_editor", "open_files", "close_window", "desktop_task"}:
            self.address = None
        if keyboard and result.ok:
            # 2026-09-24 00:30: typed 'ls' ten times without Return ("lslsls:
            # command not found"), and read a different terminal with the
            # same title instead of the one it typed into.
            pending = (" The text is typed but NOT run: press_key Return to submit it."
                       if name == "type_text" else "")
            result = ActionResult(True, "Input command sent to verified window " + self.address +
                                  "." + pending + " Application acceptance and task completion are NOT verified. "
                                  "Read fresh output with read_tile_log window='" + self.address +
                                  "' (the address: several terminals can share a title) or inspect the screen "
                                  "before reporting the outcome." + where(window))
        return result
