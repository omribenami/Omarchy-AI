"""Per-conversation keyboard targeting; dispatch success is not task completion."""
import json

from .actions import ActionResult


class InputGuard:
    def __init__(self):
        self.address = None

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

    @staticmethod
    def _conversation_pasted_into_terminal(window, args):
        if str(window.get("app") or "").casefold() not in {"foot", "kitty", "alacritty", "ghostty", "wezterm"}:
            return False
        text = str(args.get("text") or "").strip().casefold()
        starts = ("please ", "can you ", "could you ", "would you ", "i want ", "i need ",
                  "the omarchy assistant ", "make sure ", "help me ")
        return text.startswith(starts)

    def run(self, execute, name, args):
        keyboard = name in {"type_text", "press_key", "submit_sudo_password"}
        window = self.focused_window(execute) if keyboard and self.address else None
        if keyboard and (not self.address or not window or window.get("address") != self.address):
            self.address = None
            return ActionResult(False, "Input NOT sent: focus is unverified or changed. "
                                "Call focus_window for the intended target successfully before retrying.")
        if name == "type_text" and self._conversation_pasted_into_terminal(window, args):
            return ActionResult(False, "Input NOT sent: this looks like the user's conversational request, "
                                "not a shell command. Do not type requests to the assistant into a terminal.")
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
