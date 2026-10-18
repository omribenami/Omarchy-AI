"""Event triggers: a scheduled job that runs when something happens, not at a time.

A job with `on` (core/agenda.py) is polled on the heartbeat at its own pace
and runs its kind (a reminder, an action, a command...) when its event is
seen. Code owns every check; no model decides whether an event happened.

    file_changed    a file or folder changed (stat polling, no inotify)
    email           a new Gmail message matches a query (MyApi, read-only)
    process_exited  a process matching a name or pid is no longer running
    device_online   a host answers a ping, or a check command exits 0
    calendar        a calendar event starts within `lead_minutes` (MyApi)

Every trigger is:
- deduped: what was already there when it was made never fires (a file's
  first state, the messages already matching); an email or a calendar event
  fires once, by its id, kept in triggers.json (core/statefile.py);
- debounced: events seen within `debounce_minutes` of each other come as one
  firing once the source has been quiet that long (a file being written
  fires once it settles, a flapping device once);
- quiet-hours aware: during `quiet_hours` ('22:00-07:00') events are held and
  fire when they end (`quiet` 'hold', the default) or are dropped ('drop');
- rate-limited: MyApi polls are at least POLL_FLOOR_MYAPI minutes apart per
  trigger and MYAPI_MIN_GAP seconds apart across all of them.

A firing is at most once: the trigger's state is saved before the job runs.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any, Callable

from ..config import STATE_DIR
from . import statefile

STATE_PATH = STATE_DIR / "triggers.json"
STATE_VERSION = 1
EVENTS = ("file_changed", "email", "process_exited", "device_online", "calendar")
# Minutes between checks, by event. MyApi ones never faster than the floor.
POLL_MINUTES = {
    "file_changed": 1.0,
    "email": 5.0,
    "process_exited": 1.0,
    "device_online": 2.0,
    "calendar": 5.0,
}
POLL_FLOOR_MYAPI = 5.0
MYAPI_MIN_GAP = 30.0  # seconds between any two trigger polls of MyApi, in this process
DEBOUNCE_MINUTES = {
    "file_changed": 1.0,
    "email": 0.0,
    "process_exited": 0.0,
    "device_online": 2.0,
    "calendar": 0.0,
}
# Without `repeat`, these stop after their first firing ("tell me when the
# build exits"); the others keep going until cancelled.
ONCE_BY_DEFAULT = frozenset({"process_exited", "device_online"})
CALENDAR_LEAD_MINUTES = 10.0
CALENDAR_LATE_SECONDS = (
    15 * 60
)  # an event that started this long ago is no longer "starting"
EMAIL_MAX = 10
SEEN_MAX = 500
PENDING_MAX = 20
PROBE_TIMEOUT = 30
MYAPI_EVENTS = frozenset({"email", "calendar"})
_QUIET = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")

_myapi_lock = threading.Lock()
_myapi_last = [0.0]  # monotonic time of the last MyApi poll


class Unavailable(Exception):
    """The source could not be checked this time (not an event)."""


# ---------------------------------------------------------------- spec


def _clean(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        raise ValueError(f"`on` value longer than {limit} characters")
    return text


def _minutes(value: Any, name: str, low: float, high: float) -> float:
    try:
        minutes = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"on.{name} must be a number of minutes") from None
    if not low <= minutes <= high:
        raise ValueError(f"on.{name} must be between {low:g} and {high:g} minutes")
    return minutes


def quiet_window(text: str) -> tuple[int, int]:
    """'22:00-07:00' as (start, end) minutes after midnight."""
    match = _QUIET.match(text or "")
    if not match:
        raise ValueError("on.quiet_hours must look like '22:00-07:00'")
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise ValueError("on.quiet_hours has a time out of range")
    return h1 * 60 + m1, h2 * 60 + m2


def parse(raw: Any) -> dict:
    """The validated trigger for a job's `on`, from what the model sent."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise ValueError(
                'on must be an object like {"event": "email", "query": "from:x"}'
            ) from None
    if not isinstance(raw, dict):
        raise ValueError("on must be an object with `event`")
    event = _clean(raw.get("event"), 40)
    if event not in EVENTS:
        raise ValueError(f"on.event must be one of {', '.join(EVENTS)}")
    spec: dict = {"event": event}
    if event == "file_changed":
        spec["path"] = _clean(raw.get("path"), 1000)
        if not spec["path"]:
            raise ValueError("on.path is required for file_changed")
    elif event == "email":
        spec["query"] = _clean(raw.get("query"), 500)
        if not spec["query"]:
            raise ValueError("on.query (a Gmail search) is required for email")
    elif event == "process_exited":
        spec["process"] = _clean(raw.get("process"), 200)
        if not spec["process"]:
            raise ValueError(
                "on.process (a name or pid) is required for process_exited"
            )
    elif event == "device_online":
        host, check = _clean(raw.get("host"), 255), _clean(raw.get("check"), 1000)
        if bool(host) == bool(check):
            raise ValueError(
                "device_online needs exactly one of on.host (pinged) or on.check (a command; exit 0 = online)"
            )
        if host and not re.fullmatch(r"[A-Za-z0-9._:-]+", host):
            raise ValueError("on.host must be a host name or address")
        spec["host" if host else "check"] = host or check
    elif event == "calendar":
        spec["lead_minutes"] = _minutes(
            raw.get("lead_minutes", CALENDAR_LEAD_MINUTES), "lead_minutes", 0, 24 * 60
        )
    if raw.get("debounce_minutes") not in (None, ""):
        spec["debounce_minutes"] = _minutes(
            raw["debounce_minutes"], "debounce_minutes", 0, 24 * 60
        )
    if raw.get("quiet_hours") not in (None, ""):
        spec["quiet_hours"] = _clean(raw["quiet_hours"], 20)
        quiet_window(spec["quiet_hours"])  # validates
        quiet = _clean(raw.get("quiet") or "hold", 10)
        if quiet not in ("hold", "drop"):
            raise ValueError(
                "on.quiet must be 'hold' (fire when quiet hours end) or 'drop'"
            )
        spec["quiet"] = quiet
    return spec


def poll_minutes(spec: dict, asked: Any = None) -> float:
    """Minutes between checks: the asked pace, never under a MyApi floor."""
    minutes = (
        POLL_MINUTES[spec["event"]] if asked in (None, "") else max(float(asked), 1.0)
    )
    if spec["event"] in MYAPI_EVENTS:
        minutes = max(minutes, POLL_FLOOR_MYAPI)
    return minutes


def once_by_default(spec: dict) -> bool:
    return spec["event"] in ONCE_BY_DEFAULT


def untrusted(spec: dict) -> bool:
    """Whether a firing's text came from someone else (an email, an event)."""
    return spec["event"] in MYAPI_EVENTS


def describe(spec: dict) -> str:
    event = spec["event"]
    what = {
        "file_changed": lambda: f"when {spec['path']} changes",
        "email": lambda: f"when a new email matches {spec['query']!r}",
        "process_exited": lambda: f"when process {spec['process']!r} exits",
        "device_online": lambda: (
            f"when {spec.get('host') or 'the check command'} comes online"
        ),
        "calendar": lambda: (
            f"{spec['lead_minutes']:g} min before each calendar event starts"
        ),
    }[event]()
    if spec.get("quiet_hours"):
        what += f"; quiet hours {spec['quiet_hours']} ({'held until they end' if spec['quiet'] == 'hold' else 'dropped'})"
    return what


def in_quiet_hours(spec: dict, now: float) -> bool:
    if not spec.get("quiet_hours"):
        return False
    start, end = quiet_window(spec["quiet_hours"])
    local = time.localtime(now)
    minute = local.tm_hour * 60 + local.tm_min
    if start == end:
        return False
    return start <= minute < end if start < end else minute >= start or minute < end


def debounce_seconds(spec: dict) -> float:
    return 60 * float(spec.get("debounce_minutes", DEBOUNCE_MINUTES[spec["event"]]))


# ---------------------------------------------------------------- observers
# Each takes (spec, state, now), may update state, and returns the events
# seen now (short lines of text). Unavailable means "could not look".


def _file_signature(path: Path) -> str:
    try:
        info = path.stat()
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        raise Unavailable(str(exc)) from exc
    if not path.is_dir():
        return f"{info.st_mtime_ns}:{info.st_size}"
    try:
        entries = sorted(
            (e.name, e.stat(follow_symlinks=False).st_mtime_ns)
            for e in os.scandir(path)
        )
    except OSError as exc:
        raise Unavailable(str(exc)) from exc
    return "dir:" + hashlib.sha256(json.dumps(entries).encode()).hexdigest()


def _observe_file(spec: dict, state: dict, now: float) -> list[str]:
    signature = _file_signature(Path(os.path.expanduser(spec["path"])))
    before = state.get("sig")
    state["sig"] = signature
    if before is None or before == signature:
        return []  # its first state is the baseline
    if signature == "missing":
        return [f"{spec['path']} was removed"]
    return [
        f"{spec['path']} was created"
        if before == "missing"
        else f"{spec['path']} changed"
    ]


def _running(target: str) -> bool:
    """Whether a process matching `target` (a pid, or a name in its command
    line) runs now, from /proc. This process never counts."""
    me = os.getpid()
    if target.isdigit():
        return int(target) != me and Path(f"/proc/{target}").exists()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            comm = (entry / "comm").read_text().strip()
            cmdline = (
                (entry / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode(errors="replace")
            )
        except OSError:
            continue  # gone meanwhile, or not ours to read
        if comm == target or target in cmdline:
            return True
    return False


def _observe_process(spec: dict, state: dict, now: float) -> list[str]:
    running = _running(spec["process"])
    before = state.get("running")
    state["running"] = running
    if running:
        return []
    if before is None:
        return [
            f"process {spec['process']!r} is not running"
        ]  # already exited when it was made
    return [f"process {spec['process']!r} exited"] if before else []


def _probe(spec: dict) -> bool:
    """One online check: a ping, or the user's check command (exit 0)."""
    command = (
        ["ping", "-c", "1", "-W", "2", spec["host"]]
        if spec.get("host")
        else ["bash", "-lc", spec["check"]]
    )
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            timeout=PROBE_TIMEOUT,
            check=False,
            stdin=subprocess.DEVNULL,
            cwd=str(Path.home()),
        )
    except subprocess.TimeoutExpired:
        return False
    except OSError as exc:
        raise Unavailable(f"could not run the check: {exc}") from exc
    return proc.returncode == 0


def _observe_device(spec: dict, state: dict, now: float) -> list[str]:
    online = _probe(spec)
    before = state.get("online")
    state["online"] = online
    name = spec.get("host") or "the device"
    if not online:
        return []
    if before is None:
        return [f"{name} is online"]
    return [f"{name} came online"] if not before else []


def _myapi_turn() -> None:
    """Rate limit across every MyApi trigger in this process."""
    from .. import myapi

    if not myapi.is_connected():
        raise Unavailable("MyApi is not connected")
    with _myapi_lock:
        now = time.monotonic()
        if now - _myapi_last[0] < MYAPI_MIN_GAP:
            raise Unavailable("rate-limited; next check soon")
        _myapi_last[0] = now


def _remember(state: dict, keys: list[str]) -> None:
    seen = state.setdefault("seen", [])
    seen.extend(k for k in keys if k not in seen)
    del seen[:-SEEN_MAX]


def _observe_email(spec: dict, state: dict, now: float) -> list[str]:
    _myapi_turn()
    from .. import myapi
    from ..execution.actions.myapi_tools import (
        _gmail_digest,
        _gmail_execute,
        _gmail_messages,
    )

    try:
        # The exact query, never the broadened fallbacks myapi_gmail_search tries.
        result = _gmail_execute(
            "GMAIL_FETCH_EMAILS",
            {
                "query": spec["query"],
                "max_results": EMAIL_MAX,
                "include_payload": False,
            },
        )
    except myapi.MyApiError as exc:
        raise Unavailable(f"Gmail search failed: {exc}") from exc
    messages = [
        m for m in _gmail_digest(_gmail_messages(result)) if m.get("message_id")
    ]
    baseline = "seen" not in state
    seen = set(state.get("seen") or [])
    new = [m for m in messages if str(m["message_id"]) not in seen]
    _remember(state, [str(m["message_id"]) for m in messages])
    if baseline:
        return []  # what already matched when the trigger was made is not new
    return [
        f"email from {m.get('from') or 'someone'}: {str(m.get('subject') or '(no subject)')[:150]}"
        for m in reversed(new)
    ]  # oldest first


def _event_start(item: dict) -> float | None:
    start = item.get("start")
    text = start.get("dateTime") if isinstance(start, dict) else None
    if not isinstance(text, str):
        return None  # all-day events have no start time to be told about
    from datetime import datetime

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _observe_calendar(spec: dict, state: dict, now: float) -> list[str]:
    _myapi_turn()
    from datetime import datetime, timezone
    from .. import myapi

    def stamp(t: float) -> str:
        return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lead = 60 * float(spec["lead_minutes"])
    try:
        result = myapi.MyApiClient().call_service(
            "googlecalendar",
            "/calendar/v3/calendars/primary/events",
            "GET",
            query={
                "timeMin": stamp(now - CALENDAR_LATE_SECONDS),
                "timeMax": stamp(now + lead + 60),
                "singleEvents": True,
                "orderBy": "startTime",
                "maxResults": 20,
            },
        )
    except myapi.MyApiError as exc:
        raise Unavailable(f"calendar read failed: {exc}") from exc
    items = _calendar_items(result)
    seen = set(state.get("seen") or [])
    events, keys = [], []
    for item in items:
        start = _event_start(item)
        if start is None or not now - CALENDAR_LATE_SECONDS <= start <= now + lead:
            continue
        key = f"{item.get('id')}@{start:.0f}"  # a moved event is a new one
        if key in seen:
            continue
        keys.append(key)
        events.append(
            f"{str(item.get('summary') or 'an event')[:150]} starts at "
            f"{time.strftime('%H:%M', time.localtime(start))}"
        )
    _remember(state, keys)
    return events


def _calendar_items(value: object) -> list[dict]:
    if isinstance(value, dict):
        if isinstance(value.get("items"), list):
            return [i for i in value["items"] if isinstance(i, dict)]
        for child in value.values():
            found = _calendar_items(child)
            if found:
                return found
    return []


OBSERVERS: dict[str, Callable[[dict, dict, float], list[str]]] = {
    "file_changed": _observe_file,
    "email": _observe_email,
    "process_exited": _observe_process,
    "device_online": _observe_device,
    "calendar": _observe_calendar,
}


# ---------------------------------------------------------------- checking


def _state(job_id: str) -> dict:
    data = statefile.read_json(STATE_PATH, {}, expect=dict, version=STATE_VERSION)
    jobs = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
    state = jobs.get(job_id)
    return dict(state) if isinstance(state, dict) else {}


def _store(job_id: str, state: dict | None) -> None:
    with statefile.locked_json(
        STATE_PATH, {}, expect=dict, version=STATE_VERSION
    ) as doc:
        jobs = doc.data.get("jobs")
        if not isinstance(jobs, dict):
            jobs = doc.data["jobs"] = {}
        if state is None:
            jobs.pop(job_id, None)
        else:
            jobs[job_id] = state


def forget(job_id: str) -> None:
    """Drop a finished or cancelled trigger's state."""
    try:
        _store(job_id, None)
    except (OSError, ValueError):
        pass


def check(job: dict, now: float) -> tuple[list[str] | None, str]:
    """(the events to fire on now, or None; a short note for last_result).

    Called under the job's lease (core/agenda.py), so this job's state has
    one writer; the file lock covers the other jobs' entries."""
    spec = job["on"]
    state = _state(job["id"])
    try:
        events = OBSERVERS[spec["event"]](spec, state, now)
        note = "checked"
    except Unavailable as exc:
        events, note = [], f"source unavailable: {str(exc)[:200]}"
    quiet = in_quiet_hours(spec, now)
    if events:
        if quiet and spec.get("quiet") == "drop":
            note = f"{len(events)} event(s) in quiet hours, dropped"
            events = []
        else:
            pending = state.setdefault("pending", [])
            pending.extend(events)
            del pending[:-PENDING_MAX]
            state["last_event"] = now
    fire = None
    pending = state.get("pending") or []
    if pending:
        if now - float(state.get("last_event") or now) < debounce_seconds(spec):
            note = f"{len(pending)} event(s), waiting for it to settle"
        elif quiet:
            note = f"{len(pending)} event(s) held for quiet hours"
        else:
            fire, state["pending"], state["fired_at"] = list(pending), [], now
            note = "fired"
    _store(job["id"], state)  # before the job runs: a firing happens at most once
    return fire, note
