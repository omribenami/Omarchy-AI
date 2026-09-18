"""Per-conversation keyboard targeting; dispatch success is not task completion."""
import json

from .actions import ActionResult


class InputGuard:
    def __init__(self):
        self.address = None

    @staticmethod
    def focused(execute):
        result = execute("list_windows", {})
        if result.ok:
            try:
                return next((w.get("address") for w in json.loads(result.message)
                             if w.get("focused")), None)
            except (ValueError, TypeError, AttributeError):
                pass
        return None

    def run(self, execute, name, args):
        keyboard = name in {"type_text", "press_key", "submit_sudo_password"}
        if keyboard and (not self.address or self.focused(execute) != self.address):
            self.address = None
            return ActionResult(False, "Input NOT sent: focus is unverified or changed. "
                                "Call focus_window for the intended target successfully before retrying.")
        if name == "focus_window":
            self.address = None
        result = execute(name, args)
        if name == "focus_window" and result.ok:
            self.address = self.focused(execute)
            if not self.address:
                return ActionResult(False, "Focus could not be verified; input is blocked. Retry focus_window.")
        if name in {"open_terminal", "open_browser", "open_editor", "open_files", "close_window", "desktop_task"}:
            self.address = None
        if keyboard and result.ok:
            result = ActionResult(True, "Input command sent to verified window " + self.address +
                                  ". Application acceptance and task completion are NOT verified. "
                                  "Read fresh output or inspect the screen before reporting the outcome.")
        return result
