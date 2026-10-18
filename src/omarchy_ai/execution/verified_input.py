"""Per-conversation keyboard targeting; dispatch success is not task completion."""
import json
import re
import time
import uuid

from ..core import approvals, effects
from . import provenance
from .actions import ActionResult


EXTERNAL_WRITE_TOOLS = frozenset({
    "myapi_write", "myapi_execute", "myapi_gmail_send", "myapi_gmail_reply",
})


def writes_externally(name: str, args: dict) -> bool:
    """An external write now, or one scheduled to run unattended later
    (schedule_task with such a step): both need the user's explicit yes."""
    if name in EXTERNAL_WRITE_TOOLS:
        return True
    if name != "schedule_task" or not isinstance(args, dict):
        return False
    steps = args.get("steps")
    steps = steps if isinstance(steps, list) else [steps] if isinstance(steps, dict) else []
    names = [args.get("tool")] + [s.get("tool") or s.get("name") for s in steps if isinstance(s, dict)]
    return any(n in EXTERNAL_WRITE_TOOLS for n in names)


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
        self._write_key, self._write_at, self._write_stage = None, 0.0, ""
        # The user turn a tool call belongs to: what its side effects are
        # caused by (core/effects.py). Every session calls heard_user() when
        # the user speaks or types, so calls between two of them -- the
        # model's retries included -- are one turn.
        self._session, self._turn = uuid.uuid4().hex[:12], 0
        # Untrusted content seen in this conversation, and the user's own
        # words (execution/provenance.py): the taint check before every call.
        self.taint = provenance.Taint()

    @property
    def turn_origin(self) -> str:
        return f"turn:{self._session}:{self._turn}"

    def assistant_replied(self) -> None:
        """The assistant finished speaking (after a block: it asked)."""
        if self._confirm_stage == "blocked":
            self._confirm_stage = "asked"
        if self._write_stage == "blocked":
            self._write_stage = "asked"

    def heard_user(self, text: str = "") -> None:
        """The user spoke (after the question: they answered). `text` is
        what they said, or a piece of it."""
        self.taint.heard(text)
        self._turn += 1
        if self._confirm_stage == "asked":
            self._confirm_stage = "answered"
        if self._write_stage == "asked":
            self._write_stage = "answered"

    def _external_write_allowed(self, name, args) -> ActionResult | None:
        """None once the user answered the question about this exact call."""
        material = {"tool": name, "args": args}
        key = json.dumps(material, sort_keys=True, default=str)
        if key == self._write_key and self._write_stage == "answered" and \
                time.monotonic() - self._write_at < self.CONFIRM_SECONDS:
            self._write_key, self._write_stage = None, ""
            return None
        self._write_key, self._write_at, self._write_stage = key, time.monotonic(), "blocked"
        description = str(args.get("description") or args.get("title") or name.replace("myapi_", "").replace("_", " "))
        return ActionResult(False, (
            f"NOT run yet: {description}. Ask the user to confirm this exact change and wait for "
            "their answer. Retry the identical call only after an explicit yes; if they change any detail, "
            "submit the changed call and confirm it separately."), harness=True)

    def approve_external_write(self, name: str, args: dict) -> None:
        """Kept for the sessions that call it after the Jev switchboard's
        `confirmed` judgment; it approves nothing any more. A model judge may
        add a block (its reject/ask verdicts) but never stands in for the
        user's decision (core/approvals.py): a send becomes a confirm card,
        and a schedule that sends waits for the user's own answer."""
        return None

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
        turn = provenance.Turn(self.turn_origin, self.taint.tainted, tuple(self.taint.sources),
                               self.taint.utterances)
        with effects.origin(self.turn_origin), provenance.in_turn(turn), approvals.session(self.taint.tainted):
            return self._run(execute, name, args)

    def _run(self, execute, name, args):
        # One policy for every session (core/approvals.py).
        verdict = approvals.decide(name, args, tainted=self.taint.tainted, said=self.taint.said())
        sends = name in approvals.SEND_TOOLS  # the tool itself prepares a confirm card
        if verdict.level == approvals.DENY:
            return ActionResult(False, f"NOT run: {verdict.reason}.", harness=True)
        if verdict.level == approvals.VOICE and not sends:
            refused = approvals.voice_refusal(tainted=self.taint.tainted)
            if refused is not None:
                verdict = approvals.Verdict(approvals.TAP, f"a spoken yes does not count here: {refused}")
        if verdict.level == approvals.TAP:
            from .myapi_agent import ask_tap
            return ask_tap(name, args, verdict.reason, self.taint.sources if self.taint.tainted else [])
        source = provenance.of(name, args)

        def run(tool, tool_args):
            # Only what the tool itself returned can carry content, not a block of ours.
            result = execute(tool, tool_args)
            if tool == name and provenance.untrusted(source) and getattr(result, "message", "") \
                    and not getattr(result, "harness", False):
                self.taint.mark(source)
            return result

        if verdict.level == approvals.VOICE and not sends:
            # A schedule that sends: the identical call again after the user's yes.
            blocked_write = self._external_write_allowed(name, args)
            if blocked_write is not None:
                return blocked_write
            with approvals.approved_call(name, args, approvals.VOICE_BY):
                return self._guarded(run, name, args)
        return self._guarded(run, name, args)

    def _guarded(self, execute, name, args):
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
