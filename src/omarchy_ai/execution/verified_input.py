"""Per-conversation keyboard targeting; dispatch success is not task completion."""
import json
import subprocess
from pathlib import Path

from .actions import ActionResult

# Terminal-hosted editors where stray keystrokes silently corrupt buffer
# content (or, in modal editors, get interpreted as editing commands)
# instead of just failing with "command not found" at a shell prompt.
_EDITOR_COMMS = {"vim", "nvim", "vi", "nano", "emacs", "micro", "joe", "ne", "kak", "hx", "helix", "pico"}


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
    def _pid_for_address(address):
        try:
            clients = json.loads(subprocess.run(
                ["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=5,
            ).stdout)
            return next((c.get("pid") for c in clients if c.get("address") == address), None)
        except (subprocess.SubprocessError, ValueError, OSError):
            return None

    @staticmethod
    def _editor_running(root_pid):
        """Walk the terminal's process tree for a foreground text editor."""
        if not root_pid:
            return False
        children = {}
        try:
            pids = [int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit()]
        except OSError:
            return False
        for pid in pids:
            try:
                stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                children.setdefault(int(stat[1]), []).append(pid)
            except (OSError, ValueError, IndexError):
                continue
        stack, seen = [root_pid], set()
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            try:
                comm = Path(f"/proc/{pid}/comm").read_text().strip()
            except OSError:
                comm = ""
            if comm in _EDITOR_COMMS:
                return True
            stack.extend(children.get(pid, []))
        return False

    @classmethod
    def _conversation_pasted_into_terminal(cls, window, args):
        if str(window.get("app") or "").casefold() not in {"foot", "kitty", "alacritty", "ghostty", "wezterm"}:
            return False
        if not cls._editor_running(cls._pid_for_address(window.get("address"))):
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
