"""One approval policy for every way a tool call reaches the world.

Voice, chat, the phone, the Task Runtime and the agenda all ask the same
question here: what must happen before this call runs?

    decide(name, args, tainted=..., said=..., risk=...) -> AUTO | VOICE | TAP | DENY

    effect             untainted session            tainted session
    read, reversible   AUTO                         AUTO
    irreversible       AUTO (HIGH risk: TAP,        TAP when a target came from
                       ELEVATED: VOICE)             content (provenance.py), else
                                                    as untainted
    send               VOICE (a confirm card;       TAP when a target came from
                       AUTO with a standing         content; else VOICE, and the
                       approval for that exact      voice yes itself is refused
                       recipient and kind)          (accepts())
    destructive        TAP                          TAP
    BLOCKED risk       DENY                         DENY

- A *send* is any external write: Gmail send and reply, myapi_write,
  myapi_execute and every write the myapi agent prepares (Slack, posts,
  calendar...). It never runs on the call: the tool prepares a confirm card
  with the exact recipients, subject and body (execution/myapi_agent.py), and
  only an approval record for those exact arguments lets it dispatch
  (execution/actions/myapi_tools.py checks one before every send). Jev's
  switchboard can still send a call back (a block), but its judgment is not
  an approval.
- *Destructive* local actions -- deleting files or skills, overwriting an
  existing file, removing packages -- need a human tap.
- VOICE means a spoken (or typed-in-chat) yes counts, but only when
  voice_refusal() finds nothing: the risk is ELEVATED or below, the session
  is untainted, and the speaker is verified. With speaker ID off and no
  identity known, a yes counts as it always did (today's behavior); a
  recognized person who is not the MyApi owner never approves.
- Every approval binds to its arguments (call_key/send_key: a sha256 of the
  canonical JSON), so a changed argument needs a new one, and a one-shot
  approval expires after TTL_SECONDS (10 minutes) if nothing used it. A
  spoken yes to a card older than that is refused too: only a tap still
  answers it.

Standing approvals (off by default, made only by the user): an exact
recipient for one kind of send ("gmail_send" or "gmail_reply") that then
runs without a card, in an untainted session or when the user named that
recipient themself. Stored in STATE_DIR/standing_approvals.json and managed
with `python -m omarchy_ai.core.approvals standing add|remove|list`.

A scheduled action (core/agenda.py) that sends or destroys is approved once,
when it is created: the creation needs the same approval as the step would
now, and the job keeps the step's approval (its send key), so at run time
exactly that step runs unattended and a changed one does not.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Collection, Iterator

AUTO, VOICE, TAP, DENY = "auto", "voice-yes", "human-tap", "deny"
_ORDER = {AUTO: 0, VOICE: 1, TAP: 2, DENY: 3}
TTL_SECONDS = 10 * 60

READ, REVERSIBLE, IRREVERSIBLE, SEND, DESTRUCTIVE = (
    "read",
    "reversible",
    "irreversible",
    "send",
    "destructive",
)
SEND_TOOLS = frozenset(
    {"myapi_write", "myapi_execute", "myapi_gmail_send", "myapi_gmail_reply"}
)
# Risk levels as runtime/permissions.Risk numbers them (not imported: that
# module imports a great deal).
LOW, NORMAL, ELEVATED, HIGH, BLOCKED = range(5)
TAP_BY, VOICE_BY, STANDING_BY = "tap", "voice", "standing"
STANDING_KINDS = {
    "gmail_send": "GMAIL_SEND_EMAIL",
    "gmail_reply": "GMAIL_REPLY_TO_THREAD",
}


@dataclass(frozen=True)
class Verdict:
    level: str
    reason: str = ""

    def at_least(self, level: str) -> bool:
        return _ORDER[self.level] >= _ORDER[level]


def strictest(*verdicts: Verdict) -> Verdict:
    return max(verdicts, key=lambda v: _ORDER[v.level], default=Verdict(AUTO))


# ------------------------------------------------------------- what a call does

# Deleting or removing for good, in a command the assistant runs.
_DESTRUCTIVE_COMMAND = re.compile(
    r"(?:^|[\s;&|(])(?:sudo\s+)?(?:"
    r"rm|rmdir|unlink|shred|trash(?:-put)?|gio\s+(?:remove|trash)"
    r"|(?:pacman|yay|paru)\s+-R\w*"
    r"|(?:pip3?|uv\s+pip|npm|pnpm|yarn|cargo|gem)\s+(?:uninstall|remove|rm)\b"
    r"|flatpak\s+(?:uninstall|remove)|snap\s+remove|adb\s+uninstall"
    r"|find\b[^;&|]*\s-delete|git\s+clean\b|dd\s[^;&|]*\bof=|mkfs\.?\w*|truncate\s"
    r")(?=\s|$)",
)


def destructive_command(command: str) -> bool:
    return bool(_DESTRUCTIVE_COMMAND.search(str(command or "")))


# What runtime/permissions.classify says when a command deletes outside the
# workspace or removes software.
_DESTRUCTIVE_REASON = re.compile(
    r"deletes \S.* outside|deletes system path|removes packages|removes an app|uninstalls|empties the package cache",
)


def destructive_assessment(reasons: list[str]) -> str | None:
    """The reason a classified command is destructive, or None."""
    return next((r for r in reasons or [] if _DESTRUCTIVE_REASON.search(str(r))), None)


def _overwrites(args: dict) -> bool:
    if not args.get("overwrite"):
        return False
    try:
        return Path(str(args.get("path") or "")).expanduser().exists()
    except (OSError, ValueError):
        return True


def _steps(args: dict) -> list[dict]:
    steps = args.get("steps")
    steps = (
        steps if isinstance(steps, list) else [steps] if isinstance(steps, dict) else []
    )
    found = [
        {
            "tool": s.get("tool") or s.get("name"),
            "args": s.get("args") or s.get("arguments") or {},
        }
        for s in steps
        if isinstance(s, dict)
    ]
    if args.get("tool"):
        found.append({"tool": args.get("tool"), "args": args.get("args") or {}})
    return [
        s for s in found if isinstance(s["tool"], str) and isinstance(s["args"], dict)
    ]


def effect(name: str, args: dict | None) -> str:
    """read | reversible | irreversible | send | destructive."""
    args = args if isinstance(args, dict) else {}
    if name in SEND_TOOLS or name.startswith("myapi:"):
        return SEND
    if name == "delete_skill":
        return DESTRUCTIVE
    if name == "write_file" and _overwrites(args):
        return DESTRUCTIVE
    if name == "terminal_task" and destructive_command(args.get("command") or ""):
        # The assistant's own terminal. Text typed into the user's windows is
        # theirs to submit (execution/verified_input.py), so not this.
        return DESTRUCTIVE
    from ..execution import tools

    return tools.effect_class(name)


# -------------------------------------------------------------------- policy


def decide(
    name: str,
    args: dict | None,
    *,
    tainted: bool = False,
    said: str = "",
    risk: int | None = None,
    chosen: Collection[str] = (),
) -> Verdict:
    """What must happen before `name(args)` runs (the module docstring's matrix).
    `chosen`: targets the assistant itself used before the taint arose
    (execution/provenance.py Taint.chose)."""
    args = args if isinstance(args, dict) else {}
    if risk is not None and risk >= BLOCKED:
        return Verdict(DENY, "never allowed")
    if name == "schedule_task":
        # Approved at creation: the schedule needs what its strictest step needs now.
        steps = [
            decide(s["tool"], s["args"], tainted=tainted, said=said, chosen=chosen)
            for s in _steps(args)
        ]
        own = _irreversible(name, args, tainted, said, chosen)
        worst = strictest(own, *steps)
        if worst.level == AUTO:
            return worst
        return Verdict(
            worst.level, f"a scheduled step needs approval now: {worst.reason}"
        )
    kind = effect(name, args)
    if kind == DESTRUCTIVE:
        return Verdict(TAP, f"{name.replace('_', ' ')} deletes or overwrites for good")
    if kind == SEND:
        from_content = _from_content(name, args, tainted, said, chosen)
        if standing_match(name, args) and from_content is None:
            return Verdict(AUTO, "standing approval for this recipient")
        if from_content is not None:
            return Verdict(TAP, from_content)
        return Verdict(
            VOICE, "an external send waits for the user's yes on its confirm card"
        )
    if kind in (READ, REVERSIBLE):
        if name == "myapi_call" and tainted:
            from ..execution import provenance

            why = provenance.read_query_needed(args, said=said)
            if why is not None:
                return Verdict(TAP, why)
        verdict = Verdict(AUTO)
    else:
        verdict = _irreversible(name, args, tainted, said, chosen)
    if risk is not None and risk >= HIGH:
        return strictest(verdict, Verdict(TAP, "HIGH risk"))
    if risk is not None and risk >= ELEVATED:
        return strictest(verdict, Verdict(VOICE, "ELEVATED risk"))
    return verdict


def _from_content(
    name: str, args: dict, tainted: bool, said: str, chosen: Collection[str] = ()
) -> str | None:
    from ..execution import provenance

    return provenance.approval_needed(
        name, args, tainted=tainted, said=said, chosen=chosen
    )


def _irreversible(
    name: str, args: dict, tainted: bool, said: str, chosen: Collection[str] = ()
) -> Verdict:
    why = _from_content(name, args, tainted, said, chosen)
    return Verdict(TAP, why) if why else Verdict(AUTO)


# ------------------------------------------------------------ who may approve


def _speaker_id_enabled() -> bool:
    try:
        from ..config import load_config

        return bool(getattr(load_config(), "speaker_id_enabled", False))
    except Exception:  # noqa: BLE001 -- unknown: treat as on, the safer side
        return True


def speaker_refusal(identity=None) -> str | None:
    """Why the person speaking cannot approve by voice, or None. A known
    person (by phone, typing at this computer, or their voice) approves when
    they are the MyApi owner, or when no owner is set; so does typing or a
    paired phone while no owner is set. An unrecognized voice approves only
    while speaker ID is off -- today's behavior, since then nobody can be
    told apart."""
    from . import identities

    who = identity if identity is not None else identities.current()
    owner = identities.owner()
    if who.person:
        if owner and who.person != owner:
            return f"{identities.name(who.person)} is not {identities.name(owner)}, who approves these"
        return None
    if who.how in ("typed", "phone") and not owner:
        return None  # typing at this computer, or a paired phone: no voice to recognize
    if _speaker_id_enabled():
        return "the speaker's voice was not recognized"
    return None


def voice_refusal(
    *,
    risk: int = ELEVATED,
    tainted: bool = False,
    identity=None,
    asked_at: float | None = None,
    now: float | None = None,
) -> str | None:
    """Why a spoken (or chat) yes does not count here, or None when it does."""
    if risk >= HIGH:
        return "it is HIGH risk"
    if tainted:
        return "untrusted content is in this conversation, and a spoken yes is relayed by the model that read it"
    if (
        asked_at is not None
        and (time.time() if now is None else now) - asked_at > TTL_SECONDS
    ):
        return "it was prepared more than 10 minutes ago"
    return speaker_refusal(identity)


def accepts(verdict: Verdict, by: str, **context) -> str | None:
    """Why `by` (tap | voice) cannot answer a call needing `verdict`, or None."""
    if verdict.level == DENY:
        return verdict.reason or "never allowed"
    if by == TAP_BY or verdict.level == AUTO:
        return None
    if verdict.level == TAP:
        return verdict.reason or "only a tap approves it"
    return voice_refusal(**context)


# --------------------------------------------------- the session a call runs in


@dataclass(frozen=True)
class Session:
    tainted: bool = False
    channel: str = ""


_SESSION: ContextVar[Session] = ContextVar(
    "omarchy_ai_approval_session", default=Session()
)


@contextmanager
def session(tainted: bool, channel: str = "") -> Iterator[None]:
    """The conversation a call (task_respond's yes included) comes from."""
    token = _SESSION.set(Session(bool(tainted), channel))
    try:
        yield
    finally:
        _SESSION.reset(token)


def current_session() -> Session:
    return _SESSION.get()


# ----------------------------------------------------------- approval records


def _canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")
    )


def args_hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def call_key(name: str, args: dict | None) -> str:
    return args_hash({"tool": name, "args": args or {}})


def send_key(service: str, operation: str, arguments: dict | None) -> str:
    """What one external send is, whichever tool or card asks for it."""
    return args_hash({"send": [service, operation], "arguments": arguments or {}})


_lock = threading.Lock()
_grants: dict[str, tuple[float, str]] = {}


def grant(key: str, by: str, now: float | None = None) -> None:
    """A one-shot approval of exactly `key`, valid for TTL_SECONDS."""
    now = time.time() if now is None else now
    with _lock:
        for old in [k for k, (at, _) in _grants.items() if now - at > TTL_SECONDS]:
            del _grants[old]
        _grants[key] = (now, by)


def take(key: str, now: float | None = None) -> str | None:
    """Who approved `key` (and the approval is used up), or None."""
    now = time.time() if now is None else now
    with _lock:
        hit = _grants.pop(key, None)
    if hit is None or now - hit[0] > TTL_SECONDS:
        return None
    return hit[1]


def pending(key: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    with _lock:
        hit = _grants.get(key)
    return hit is not None and now - hit[0] <= TTL_SECONDS


_CALL: ContextVar[tuple[str, str] | None] = ContextVar(
    "omarchy_ai_approved_call", default=None
)


@contextmanager
def approved_call(name: str, args: dict | None, by: str) -> Iterator[None]:
    """The user approved exactly this call (a tap on its card, or a counted
    yes): the tool, and what it calls in turn, may rely on it."""
    token = _CALL.set((call_key(name, args), by))
    try:
        yield
    finally:
        _CALL.reset(token)


def approved_by(name: str, args: dict | None) -> str | None:
    hit = _CALL.get()
    return hit[1] if hit and hit[0] == call_key(name, args) else None


def approved_tool(name: str) -> str | None:
    """Who approved the current call, whatever the tool rewrote of its args
    since (schedule_task fills in steps): only for the tool's own bookkeeping."""
    hit = _CALL.get()
    return hit[1] if hit else None


# ------------------------------------------------ scheduled actions (agenda)

_JOB: ContextVar[dict | None] = ContextVar("omarchy_ai_approved_job", default=None)


@contextmanager
def job_scope(job: dict) -> Iterator[None]:
    """A scheduled action's run: the sends approved when it was created."""
    token = _JOB.set(job)
    try:
        yield
    finally:
        _JOB.reset(token)


def job_allows(key: str) -> str | None:
    job = _JOB.get() or {}
    approved = job.get("approved")
    keys = approved.get("keys") if isinstance(approved, dict) else None
    return STANDING_BY if key in (keys or []) else None


# ------------------------------------------- standing approvals (user-made)


def standing_path() -> Path:
    from .. import config

    return config.STATE_DIR / "standing_approvals.json"


def _standing() -> list[dict]:
    from . import statefile

    data = statefile.read_json(standing_path(), {"approvals": []}, expect=dict)
    rows = data.get("approvals") if isinstance(data.get("approvals"), list) else []
    return [
        r
        for r in rows
        if isinstance(r, dict)
        and r.get("kind") in STANDING_KINDS
        and r.get("recipient")
    ]


def list_standing() -> list[dict]:
    return _standing()


def add_standing(kind: str, recipient: str) -> dict:
    """Only the user makes one (the CLI below); nothing in a conversation can."""
    recipient = str(recipient or "").strip().lower()
    if kind not in STANDING_KINDS:
        raise ValueError(f"kind must be one of {', '.join(sorted(STANDING_KINDS))}")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", recipient):
        raise ValueError("recipient must be one exact email address")
    from . import statefile

    row = {"kind": kind, "recipient": recipient, "created": time.time()}
    with statefile.locked_json(standing_path(), {"approvals": []}, expect=dict) as doc:
        rows = [
            r
            for r in doc.data.get("approvals") or []
            if isinstance(r, dict)
            and (r.get("kind"), r.get("recipient")) != (kind, recipient)
        ]
        doc.data = {"approvals": rows + [row]}
    return row


def remove_standing(kind: str, recipient: str) -> bool:
    recipient = str(recipient or "").strip().lower()
    from . import statefile

    with statefile.locked_json(standing_path(), {"approvals": []}, expect=dict) as doc:
        rows = [r for r in doc.data.get("approvals") or [] if isinstance(r, dict)]
        kept = [
            r for r in rows if (r.get("kind"), r.get("recipient")) != (kind, recipient)
        ]
        doc.data = {"approvals": kept}
    return len(kept) != len(rows)


def _addresses(value: object) -> list[str]:
    values = value if isinstance(value, (list, tuple)) else [value] if value else []
    return [str(v).strip().lower() for v in values if str(v or "").strip()]


def standing_kind(name: str, args: dict | None) -> str | None:
    args = args if isinstance(args, dict) else {}
    operation = {
        "myapi_gmail_send": "GMAIL_SEND_EMAIL",
        "myapi_gmail_reply": "GMAIL_REPLY_TO_THREAD",
    }.get(name)
    if (
        name == "myapi_execute"
        and str(args.get("service") or "").strip().lower() == "gmail"
    ):
        operation = str(args.get("operation") or "")
    if name.startswith("myapi:gmail:"):
        operation = name.split(":", 2)[2]
    return next((k for k, op in STANDING_KINDS.items() if op == operation), None)


def standing_match(name: str, args: dict | None) -> bool:
    """A standing approval covers the call: the exact kind, one recipient
    that is approved, and no Cc or Bcc."""
    kind = standing_kind(name, args)
    if kind is None:
        return False
    args = args if isinstance(args, dict) else {}
    nested = args.get("arguments")
    inner: dict = nested if isinstance(nested, dict) else args
    to = _addresses(inner.get("recipient_email"))
    if len(to) != 1 or _addresses(inner.get("cc")) or _addresses(inner.get("bcc")):
        return False
    try:
        rows = _standing()
    except Exception:  # noqa: BLE001 -- an unreadable file approves nothing
        return False
    return any(r["kind"] == kind and r["recipient"] == to[0] for r in rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m omarchy_ai.core.approvals",
        description="Standing approvals: sends that need no confirm card.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    standing = sub.add_parser("standing").add_subparsers(dest="action", required=True)
    for action in ("add", "remove"):
        p = standing.add_parser(action)
        p.add_argument("kind", choices=sorted(STANDING_KINDS))
        p.add_argument("recipient")
    standing.add_parser("list")
    args = parser.parse_args(argv)
    if args.action == "list":
        print(json.dumps(list_standing(), indent=1))
    elif args.action == "add":
        print(json.dumps(add_standing(args.kind, args.recipient)))
    else:
        print(
            "removed"
            if remove_standing(args.kind, args.recipient)
            else "no such standing approval"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
