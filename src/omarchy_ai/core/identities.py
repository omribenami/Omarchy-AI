"""The people of the household, as the assistant tells them apart.

Each person has a key (lowercase; also their voice folder under
~/.config/omarchy-ai/speakers/<key>/ and their entry in people.yaml), a
display name, the Flux phones that are theirs, and at most one of them owns
the MyApi account. A request from an attached phone is that person without
any voice match; a voice is matched by voice/speaker_id.py.

Stored in ~/.config/omarchy-ai/identities.json (0600), written by the
settings panel (cli/settings.py identity-* commands):

    {"people": {"<key>": {"name": "...", "phones": ["<flux device id>"]}},
     "owner": "<key>" | null}

people.yaml stays the user's own hand-written file; it is only read here.
"""
from __future__ import annotations

import json
import os
import re
import threading
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path

from ..config import CONFIG_DIR

PATH = CONFIG_DIR / "identities.json"
KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")

_lock = threading.RLock()


@dataclass(frozen=True)
class Identity:
    """Who a request comes from, and how that is known."""
    person: str | None = None
    how: str | None = None  # "phone" | "voice" | "typed" | None


CURRENT_IDENTITY: ContextVar[Identity] = ContextVar("omarchy_ai_identity", default=Identity())


def current() -> Identity:
    return CURRENT_IDENTITY.get()


def set_current(identity: Identity) -> Token:
    return CURRENT_IDENTITY.set(identity)


def reset_current(token: Token) -> None:
    CURRENT_IDENTITY.reset(token)


def _load() -> dict:
    try:
        data = json.loads(PATH.read_text())
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    people = data.get("people") if isinstance(data.get("people"), dict) else {}
    clean = {}
    for key, entry in people.items():
        if isinstance(key, str) and KEY.fullmatch(key) and isinstance(entry, dict):
            phones = [p for p in entry.get("phones") or [] if isinstance(p, str) and p]
            clean[key] = {"name": str(entry.get("name") or key.capitalize()), "phones": phones}
    owner = data.get("owner")
    return {"people": clean, "owner": owner if owner in clean else None}


def _write(data: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PATH)


def _person(data: dict, key: str) -> dict:
    if key not in data["people"]:
        raise ValueError(f"no person '{key}'")
    return data["people"][key]


def people() -> list[dict]:
    """[{"key", "name", "phones", "owner"}] in key order."""
    data = _load()
    return [{"key": key, "name": entry["name"], "phones": list(entry["phones"]), "owner": key == data["owner"]}
            for key, entry in sorted(data["people"].items())]


def add(key: str, name: str | None = None) -> None:
    key = str(key or "").strip().lower()
    if not KEY.fullmatch(key):
        raise ValueError("a person's key is 1-32 lowercase letters, digits, - or _ (their voice folder name)")
    with _lock:
        data = _load()
        if key in data["people"]:
            raise ValueError(f"'{key}' already exists")
        data["people"][key] = {"name": (name or "").strip() or key.capitalize(), "phones": []}
        _write(data)


def rename(key: str, name: str) -> None:
    name = str(name or "").strip()
    if not name:
        raise ValueError("a name is required")
    with _lock:
        data = _load()
        _person(data, key)["name"] = name[:80]
        _write(data)


def remove(key: str) -> None:
    with _lock:
        data = _load()
        _person(data, key)
        del data["people"][key]
        if data["owner"] == key:
            data["owner"] = None
        _write(data)


def attach_phone(key: str, device_id: str) -> None:
    """A phone belongs to one person: attaching moves it from anyone else."""
    if not isinstance(device_id, str) or not device_id:
        raise ValueError("a device id is required")
    with _lock:
        data = _load()
        _person(data, key)
        for entry in data["people"].values():
            entry["phones"] = [p for p in entry["phones"] if p != device_id]
        data["people"][key]["phones"].append(device_id)
        _write(data)


def detach_phone(device_id: str) -> None:
    with _lock:
        data = _load()
        for entry in data["people"].values():
            entry["phones"] = [p for p in entry["phones"] if p != device_id]
        _write(data)


def set_owner(key: str | None) -> None:
    with _lock:
        data = _load()
        if key is not None:
            _person(data, key)
        data["owner"] = key
        _write(data)


def owner() -> str | None:
    return _load()["owner"]


def person_for_device(device_id: str | None) -> str | None:
    if not device_id:
        return None
    for key, entry in _load()["people"].items():
        if device_id in entry["phones"]:
            return key
    return None


def name(key: str | None) -> str:
    """A person's display name ("" for nobody)."""
    if not key:
        return ""
    entry = _load()["people"].get(key)
    return entry["name"] if entry else key.capitalize()


def phones(key: str | None) -> list[str]:
    """The Flux device ids attached to a person."""
    entry = _load()["people"].get(key) if key else None
    return list(entry["phones"]) if entry else []


def myapi_refusal() -> str | None:
    """Why the current identity may not use MyApi, or None when it may.
    With no owner set nothing is private; with one, only the owner may."""
    data = _load()
    owner_key = data["owner"]
    if owner_key is None or current().person == owner_key:
        return None
    return (f"MyApi is private to {data['people'][owner_key]['name']}. Tell the user that in one sentence; "
            "do not try it another way.")


def person_for_session(record: dict | None) -> str | None:
    """A phone session's person (phone/server.py session records): the one
    a QR pairing was made for, else whoever its Flux device is attached to."""
    if not record:
        return None
    person = record.get("person")
    if person and person in _load()["people"]:
        return person
    return person_for_device(record.get("device"))


def describe(identity: Identity) -> str:
    """One line for a prompt: who is speaking and how certain that is."""
    if not identity.person:
        return ""
    how = {"phone": "on their own phone (certain)", "typed": "typing at this computer (its owner)",
           "voice": "recognized by their voice"}.get(identity.how or "", "")
    return f"The person talking to you is {name(identity.person)}" + (f", {how}" if how else "") + "."


def _flux_devices() -> dict[str, str]:
    from ..phone import flux_pairing
    return {d["id"]: str(d.get("name") or d["id"]) for d in flux_pairing.devices()}


def _sessions() -> list[dict]:
    from ..phone import server
    return server.load_sessions()


def _voice(key: str, enabled: bool, enrolled: set[str], speakers_dir: Path) -> dict:
    folder = speakers_dir / key
    clips = len(list(folder.glob("*.wav"))) if folder.is_dir() else 0
    if not enabled:
        state, text = "disabled", f"Voice recognition is off ({clips} recording{'' if clips == 1 else 's'})"
    elif key in enrolled:
        state, text = "enrolled", f"Voice enrolled ({clips} recording{'' if clips == 1 else 's'})"
    elif clips:
        state, text = "recordings", f"{clips} recording{'' if clips == 1 else 's'}, not indexed yet"
    else:
        state, text = "none", f"No voice: add WAV recordings to {folder}"
    return {"state": state, "clips": clips, "text": text}


def status(config=None) -> dict:
    """Everything the settings panel shows, without loading the voice model."""
    from ..voice import speaker_id
    if config is None:
        from ..config import load_config
        config = load_config()
    enabled = bool(getattr(config, "speaker_id_enabled", False))
    try:
        enrolled = set(json.loads(speaker_id.EMBEDDINGS_PATH.read_text()))
    except (OSError, ValueError, TypeError):
        enrolled = set()
    household = speaker_id.people()
    devices = _flux_devices()
    sessions = _sessions()
    data = _load()
    attached = {p for entry in data["people"].values() for p in entry["phones"]}
    out = []
    for key, entry in sorted(data["people"].items()):
        info = household.get(key) or {}
        out.append({
            "key": key, "name": entry["name"], "owner": key == data["owner"],
            "household_name": str(info.get("name") or ""), "relation": str(info.get("relation") or ""),
            "voice": _voice(key, enabled, enrolled, speaker_id.SPEAKERS_DIR),
            "phones": [{"id": p, "name": devices.get(p, p), "paired": p in devices} for p in entry["phones"]],
            "qr_phones": sum(1 for s in sessions if s.get("person") == key and not s.get("device")),
        })
    folders = {p.name for p in speaker_id.SPEAKERS_DIR.iterdir() if p.is_dir()} if speaker_id.SPEAKERS_DIR.is_dir() else set()
    unlisted = sorted((set(household) | folders) - set(data["people"]))
    warnings = []
    if data["owner"] and not enabled:
        warnings.append("Voice recognition is off, so the MyApi owner is recognized only by an attached phone.")
    return {
        "people": out,
        "owner": data["owner"],
        "speaker_id_enabled": enabled,
        "unlisted": [{"key": k, "name": str((household.get(k) or {}).get("name") or k.capitalize())} for k in unlisted if KEY.fullmatch(k)],
        "unattached_phones": [{"id": i, "name": n} for i, n in sorted(devices.items(), key=lambda d: d[1]) if i not in attached],
        "anonymous_sessions": sum(1 for s in sessions if not s.get("device") and not s.get("person")),
        "warnings": warnings,
    }
