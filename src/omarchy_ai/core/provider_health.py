"""Provider health that survives a restart: cooldowns, breakers, quota.

Each module that benches a provider keeps its own in-memory state, exactly
as before (runtime/llm.py worker-backend cooldowns, coding_agents.py usage
limits, core/quota.py alert state, voice/fallback.py failed voice models,
core/jev.py's Gateway breaker). Before this file, a restart forgot all of
it: the first call after it went straight back to a backend that had just
failed, and paid its timeout again.

Now each of them also reports a change here as one entry,
`{provider, kind, state, until, reason, since}` with `until` a wall-clock
epoch, and this module writes them to STATE_DIR/provider_health.json
(core/statefile.py: flock, atomic write, corrupt files quarantined).

- Writes never block the caller: `record()`/`clear()` only queue the change
  and arm a short timer; one background write per burst merges the queued
  changes into the file under its lock, so another process's entries are
  kept.
- Nothing is read or written until the daemon calls `enable()` at startup.
  Tests and CLIs that touch the same modules stay in memory.
- `enable()` loads the file, drops expired entries, and hands each owner
  its entries (`register(kind, restore)`), whenever the owner is imported.
- `snapshot()` is the read API for doctor.
"""

from __future__ import annotations

import atexit
import logging
from pathlib import Path
import threading
import time
from typing import Any, Callable, TypedDict

from . import statefile

log = logging.getLogger("omarchy_ai.core.provider_health")

FILE_NAME = "provider_health.json"
VERSION = 1
# Changes within this window go out in one write.
COALESCE_SECONDS = 0.5


class Entry(TypedDict):
    provider: str
    kind: str
    state: str
    until: float
    reason: str
    since: float


Restore = Callable[[list[Entry]], None]

_lock = threading.Lock()
_enabled = False
_path: Path | None = None
_loaded: dict[str, Entry] = {}
_restorers: dict[str, Restore] = {}
# key -> the entry to write, or the time it was cleared (a float).
_pending: dict[str, Entry | float] = {}
_timer: threading.Timer | None = None


def path() -> Path:
    from .. import config

    return _path or config.STATE_DIR / FILE_NAME


def _key(kind: str, provider: str) -> str:
    return f"{kind}:{provider}"


def to_monotonic(until: float) -> float:
    """A wall-clock `until` on this process's monotonic clock."""
    return time.monotonic() + (until - time.time())


def to_wall(monotonic_until: float) -> float:
    return time.time() + (monotonic_until - time.monotonic())


def _valid(entry: Any, now: float) -> bool:
    if not isinstance(entry, dict):
        return False
    try:
        return (
            isinstance(entry.get("provider"), str)
            and isinstance(entry.get("kind"), str)
            and float(entry["until"]) > now
        )
    except (KeyError, TypeError, ValueError):
        return False


def _entry(raw: dict) -> Entry:
    return Entry(
        provider=str(raw["provider"]),
        kind=str(raw["kind"]),
        state=str(raw.get("state") or ""),
        until=float(raw["until"]),
        reason=str(raw.get("reason") or ""),
        since=float(raw.get("since") or 0),
    )


def _read(file: Path) -> dict[str, Entry]:
    """Live entries on disk; {} for a missing, quarantined or newer file."""
    try:
        data = statefile.read_json(file, expect=dict, version=VERSION)
    except statefile.StateFileError as exc:
        log.warning("provider health not loaded: %s", exc)
        return {}
    now = time.time()
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}
    return {k: _entry(v) for k, v in entries.items() if _valid(v, now)}


# ------------------------------------------------------------------- owners


def register(kind: str, restore: Restore) -> None:
    """Called by an owner module at import. If the daemon has already
    loaded the file, `restore` gets this kind's live entries now."""
    with _lock:
        _restorers[kind] = restore
        entries = [e for e in _loaded.values() if e["kind"] == kind] if _enabled else []
    if entries:
        _restore(kind, restore, entries)


def _restore(kind: str, restore: Restore, entries: list[Entry]) -> None:
    now = time.time()
    entries = [e for e in entries if e["until"] > now]
    if not entries:
        return
    try:
        restore(entries)
    except Exception:  # noqa: BLE001 -- a bad entry must not stop the daemon
        log.warning("provider health for %s not restored", kind, exc_info=True)
        return
    for e in entries:
        log.info(
            "provider health restored: %s %s %s for %.0fs more (%s)",
            e["kind"],
            e["provider"],
            e["state"],
            e["until"] - now,
            e["reason"][:120],
        )


def enable(file: Path | None = None) -> None:
    """Start persisting (the daemon, once at startup): load the file and
    restore every registered owner's live entries."""
    global _enabled, _path, _loaded
    with _lock:
        _path = Path(file) if file is not None else None
        _loaded = _read(path())
        _enabled = True
        loaded = dict(_loaded)
        restorers = dict(_restorers)
    for kind, restore in restorers.items():
        _restore(kind, restore, [e for e in loaded.values() if e["kind"] == kind])


def disable() -> None:
    """Stop persisting (tests): pending changes are dropped, not written."""
    global _enabled, _path, _timer, _loaded
    with _lock:
        if _timer is not None:
            _timer.cancel()
        _enabled, _path, _timer, _loaded = False, None, None, {}
        _pending.clear()


def enabled() -> bool:
    return _enabled


def record(
    kind: str, provider: str, state: str, until: float, reason: str = ""
) -> None:
    """`provider` is benched (`state`) until the wall-clock epoch `until`.
    Returns at once; the write happens on a background timer."""
    if not _enabled:
        return
    entry = Entry(
        provider=provider,
        kind=kind,
        state=state,
        until=float(until),
        reason=" ".join(str(reason).split())[:300],
        since=time.time(),
    )
    _queue(_key(kind, provider), entry)


def clear(kind: str, provider: str) -> None:
    """`provider` is healthy again."""
    if not _enabled:
        return
    _queue(_key(kind, provider), time.time())


def _queue(key: str, change: Entry | float) -> None:
    global _timer
    with _lock:
        if not _enabled:
            return
        _pending[key] = change
        if _timer is None:
            _timer = threading.Timer(COALESCE_SECONDS, flush)
            _timer.daemon = True
            _timer.name = "provider-health"
            _timer.start()


def flush() -> None:
    """Write every queued change now (the timer, exit, tests)."""
    global _timer
    with _lock:
        _timer = None
        if not _enabled or not _pending:
            return
        changes = dict(_pending)
        _pending.clear()
        file = path()
    try:
        with statefile.locked_json(file, expect=dict, version=VERSION) as doc:
            now = time.time()
            stored = doc.data.get("entries")
            entries = {
                k: v
                for k, v in (stored if isinstance(stored, dict) else {}).items()
                if _valid(v, now)
            }
            for key, change in changes.items():
                if isinstance(change, dict):
                    entries[key] = dict(change)
                elif key in entries and float(entries[key].get("since") or 0) <= change:
                    # Not one another process recorded after we cleared it.
                    del entries[key]
            doc.data = {"entries": entries}
    except (OSError, statefile.StateFileError):
        log.warning("provider health not saved", exc_info=True)


atexit.register(flush)


# ------------------------------------------------------------------- doctor


def snapshot(file: Path | None = None) -> list[dict[str, Any]]:
    """Every provider currently benched, soonest to recover first:
    [{provider, kind, state, until, reason}]. Reads the file without a lock
    (writes are atomic), plus this process's not-yet-written changes."""
    target = Path(file) if file is not None else path()
    entries: dict[str, Any] = dict(_read(target))
    with _lock:
        if file is None or target == path():
            for key, change in _pending.items():
                if isinstance(change, dict):
                    entries[key] = change
                else:
                    entries.pop(key, None)
    now = time.time()
    rows = [
        {k: e[k] for k in ("provider", "kind", "state", "until", "reason")}
        for e in entries.values()
        if e["until"] > now
    ]
    return sorted(rows, key=lambda r: (r["until"], r["kind"], r["provider"]))
