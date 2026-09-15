"""The one real, persistent device registry for casting — the single
source of truth `execution/actions.py`'s cast tools and the
omarchy-ai.tv-discovery overlay both read and write, per the user's own
explicit requirement: no separate device list/discovery state for the UI
vs. the voice agent.

Module-level state, same lifetime as the daemon process — matching the
existing module-level-state pattern `execution/actions.py` already uses
for `_cast_process`/`_pending_pair_target`, not a class instance threaded
through call sites.

Status values: "online" (seen in the most recent discovery pass),
"offline" (previously known, not seen in the most recent pass — kept
visible, never dropped, per the task's own "keep known offline devices
visible" ask), "unknown" (never confirmed via a real discovery pass — the
hardcoded fallback TV before its first refresh), "connecting"/"connected"
(an active cast attempt/session against that device — set by
execution/actions.py's start_casting, never downgraded back to "online"
by a concurrent refresh() just because a discovery pass happened to run).
"""

from __future__ import annotations

import time

from . import discovery

_ACTIVE_STATUSES = {"connecting", "connected"}

# {address: {"name", "address", "status", "last_seen"}}
_devices: dict[str, dict] = {}
_last_refresh: float = 0.0


def _fallback_address() -> str:
    return discovery.TV_ADB_ADDR.split(":")[0]


def refresh() -> list[dict]:
    """Runs a real discovery pass and merges it into the registry -- the
    one function that actually talks to the network. Never raises
    (discovery.discover_androidtv_devices() already guarantees that)."""
    global _last_refresh
    seen = discovery.discover_androidtv_devices()
    seen_addresses = {d["address"] for d in seen}

    now = time.time()
    for d in seen:
        existing = _devices.get(d["address"])
        if existing is not None and existing["status"] in _ACTIVE_STATUSES:
            # Don't let a concurrent mDNS pass flicker an active
            # connect/session back to "online" -- just refresh the name in
            # case it changed (e.g. the user renamed the device).
            existing["name"] = d["name"]
            existing["last_seen"] = now
            continue
        _devices[d["address"]] = {
            "name": d["name"], "address": d["address"], "status": "online", "last_seen": now,
        }

    for addr, dev in _devices.items():
        if addr not in seen_addresses and dev["status"] not in _ACTIVE_STATUSES:
            dev["status"] = "offline"

    fallback_addr = _fallback_address()
    if fallback_addr not in _devices:
        _devices[fallback_addr] = {
            "name": "previously paired TV", "address": fallback_addr,
            "status": "unknown", "last_seen": 0.0,
        }

    _last_refresh = now
    return snapshot()


def snapshot() -> list[dict]:
    """Current registry state, no network call -- what the overlay and any
    read-only tool call bind to between refreshes."""
    return sorted(_devices.values(), key=lambda d: d["name"].lower())


def get_or_refresh(max_age: float = 10.0) -> list[dict]:
    """Read path for the voice tool-call hot path: reuse a recent refresh
    instead of paying a fresh multi-second avahi-browse on every single
    call within one conversation, without ever serving state older than
    max_age seconds."""
    if not _devices or (time.time() - _last_refresh) > max_age:
        return refresh()
    return snapshot()


def _set_status(address: str, status: str) -> None:
    dev = _devices.get(address)
    if dev is not None:
        dev["status"] = status
        dev["last_seen"] = time.time()
    else:
        # A target resolved via raw IP (never seen by mDNS discovery) --
        # still worth tracking once it's actually being cast to.
        _devices[address] = {
            "name": address, "address": address, "status": status, "last_seen": time.time(),
        }


def mark_connecting(address: str) -> None:
    _set_status(address, "connecting")


def mark_connected(address: str) -> None:
    _set_status(address, "connected")


def mark_disconnected(address: str) -> None:
    # Back to "online" rather than forcing a re-discovery just to know --
    # it was reachable a moment ago; the next refresh() will correct this
    # to "offline" on its own if it's genuinely no longer around.
    _set_status(address, "online")


def mark_failed(address: str) -> None:
    _set_status(address, "offline")
