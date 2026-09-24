"""Per-conversation keyboard targeting; dispatch success is not task completion."""
import json
import time

from .actions import ActionResult


# Guards WHERE input lands (verified focus), never WHAT is typed. A former
# "this looks like a conversational request" text heuristic was removed on
# purpose: relaying the user's prompts into claude/codex terminals is the
# point (STATUS.md, 2026-09-22), and a phrasing guess cannot tell a prompt
# for another agent from a mistake.
class InputGuard:
    # Live probe 2026-09-24: type_text, press_key Return, press_key Return --
    # at an ssh password prompt the second Return sends an empty password.
    DOUBLE_RETURN_SECONDS = 3.0

    def __init__(self):
        self.address = None
        self._last_return = None  # window of a Return with nothing typed since
        self._return_at = 0.0

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
            self.address = self.focused(execute)
            if not self.address:
                return ActionResult(False, "Focus could not be verified; input is blocked. Retry focus_window.")
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
                                  "before reporting the outcome.")
        return result
