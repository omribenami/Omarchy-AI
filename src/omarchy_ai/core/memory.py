"""Cross-session memory: the user's standing rules (plan 2.2).

The model calls remember_preference when the user gives a standing
correction or instruction ("always confirm before muting", "call me X"),
not for one-off requests. That call only PROPOSES a record; it becomes
active (folded into every future session's instructions) only after the
user says yes in the same untainted conversation
(execution/actions/assistant.py holds that policy). Nothing here changes
code or acts on its own.

Storage, two files under one lock:

- learned_preferences.yaml stays exactly what it always was: a YAML list of
  the ACTIVE rules' texts, in order. It is the source of truth for what is
  active, so older versions of this code (a rollback) read every confirmed
  rule unchanged, and a hand edit of the list still works.
- memory.json beside it (schema_version 1) holds each record's metadata
  `{id, text, kind, source_turn, created, last_used, confirmed, provenance}`
  plus the proposals waiting for a yes. A list entry with no metadata (every
  entry on the first day, or one added by hand or by older code) is an
  active record with provenance "user:legacy".

The metadata file is written before the list. A crash between the two
leaves both files valid (each write is atomic); at worst that one change is
lost, since what is active is always read from the list.

Dedupe: texts are compared normalized (Unicode-folded, case and punctuation
ignored), and a near-duplicate (rapidfuzz ratio >= SIMILAR) of an active
rule replaces it once the user confirms instead of adding a second one.
Past MAX_ACTIVE nothing is dropped: a new proposal is refused and the
least-recently-used rules are reported for review.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import logging
import re
import secrets
import time
import unicodedata
from pathlib import Path
from typing import Any, Iterator

from rapidfuzz import fuzz

from ..config import STATE_DIR
from . import statefile

log = logging.getLogger("omarchy_ai.core.memory")

PREFERENCES_PATH = STATE_DIR / "learned_preferences.yaml"
SCHEMA_VERSION = 1
MAX_ACTIVE = 50
MAX_PROPOSED = 10  # proposals nobody answered; the oldest unanswered one goes first
SIMILAR = 85.0  # rapidfuzz ratio of normalized texts that counts as the same rule
REVIEW_COUNT = 3
KINDS = ("preference", "fact", "procedure")
LEGACY = "user:legacy"

_FENCE_TAG = re.compile(r"<\s*(/?)\s*user_memory\b", re.IGNORECASE)


def records_path() -> Path:
    return PREFERENCES_PATH.with_name("memory.json")


def normalize(text: str) -> str:
    """What two texts are compared by: NFKC, case-folded, combining marks
    (Hebrew points, accents) dropped, punctuation and spacing collapsed."""
    folded = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", str(text)).casefold())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return " ".join(re.sub(r"[\W_]+", " ", folded).split())


def similarity(a: str, b: str) -> float:
    return float(fuzz.ratio(normalize(a), normalize(b)))


def fence_text(text: str) -> str:
    """A rule's text made safe inside <user_memory>: it cannot close the fence."""
    return _FENCE_TAG.sub(lambda m: "&lt;" + m.group(1) + "user_memory", str(text))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _legacy_id(text: str) -> str:
    return "m" + hashlib.sha1(normalize(text).encode()).hexdigest()[:8]


def _legacy_time() -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(PREFERENCES_PATH.stat().st_mtime))
    except OSError:
        return ""


def _legacy_record(text: str, stamp: str) -> dict[str, Any]:
    return {
        "id": _legacy_id(text),
        "text": text,
        "kind": "preference",
        "source_turn": "",
        "created": stamp,
        "last_used": stamp,
        "confirmed": True,
        "provenance": LEGACY,
    }


def _assemble(texts: list[str], meta: list) -> list[dict[str, Any]]:
    """Active records in list order (metadata joined by normalized text),
    then the proposals."""
    known = {
        normalize(r.get("text", "")): r
        for r in meta
        if isinstance(r, dict) and r.get("confirmed")
    }
    stamp = ""
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for text in texts:
        found = known.get(normalize(text))
        if found is not None and found.get("id") not in seen:
            record = {**_legacy_record(text, ""), **found, "text": text, "confirmed": True}
        else:
            stamp = stamp or _legacy_time()
            record = _legacy_record(text, stamp)
            if record["id"] in seen:  # the same text twice in a hand-edited list
                record["id"] += "-" + str(len(records))
        seen.add(record["id"])
        records.append(record)
    for r in meta:
        if isinstance(r, dict) and not r.get("confirmed") and r.get("id") and r.get("text"):
            records.append({**r, "confirmed": False})
    return records


def _read_texts() -> list[str]:
    data = statefile.read_yaml(PREFERENCES_PATH, [], expect=list)
    return [str(p) for p in data]


def _read_meta() -> list:
    data = statefile.read_json(
        records_path(), {"records": []}, expect=dict, version=SCHEMA_VERSION, lock=PREFERENCES_PATH
    )
    records = data.get("records")
    return records if isinstance(records, list) else []


def load_records() -> list[dict[str, Any]]:
    """Every record: the active ones first (in prompt order), then proposals."""
    try:
        texts = _read_texts()
    except Exception:  # noqa: BLE001
        log.warning("failed to load learned preferences", exc_info=True)
        return []
    try:
        meta = _read_meta()
    except Exception:  # noqa: BLE001 -- metadata never hides an active rule
        log.warning("failed to load memory metadata", exc_info=True)
        meta = []
    return _assemble(texts, meta)


def load_preferences() -> list[str]:
    """The active rules' texts, exactly as stored: what the prompts inject."""
    try:
        return _read_texts()
    except Exception:  # noqa: BLE001
        log.warning("failed to load learned preferences", exc_info=True)
        return []


@contextmanager
def _edit() -> Iterator[list[dict[str, Any]]]:
    """Locked read-modify-write of both files; change the list in place."""
    with statefile.locked(PREFERENCES_PATH):
        with statefile.locked_yaml(PREFERENCES_PATH, [], expect=list) as listing:
            with statefile.locked_json(
                records_path(), {"records": []}, expect=dict, version=SCHEMA_VERSION
            ) as meta:
                stored = meta.data.get("records")
                records = _assemble(
                    [str(p) for p in listing.data], stored if isinstance(stored, list) else []
                )
                yield records
                meta.data = {"records": records}
            listing.data = [r["text"] for r in records if r.get("confirmed")]


def _active(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if r.get("confirmed")]


def _same(records: list[dict[str, Any]], text: str) -> dict[str, Any] | None:
    norm = normalize(text)
    return next((r for r in _active(records) if normalize(r["text"]) == norm), None)


def _closest(records: list[dict[str, Any]], text: str, skip: str = "") -> dict[str, Any] | None:
    best, score = None, SIMILAR
    for r in _active(records):
        if r["id"] == skip:
            continue
        value = similarity(r["text"], text)
        if value >= score:
            best, score = r, value
    return best


def review(records: list[dict[str, Any]] | None = None, count: int = REVIEW_COUNT) -> list[dict[str, Any]]:
    """The least-recently-used active rules: what to show for review."""
    active = _active(load_records() if records is None else records)
    return sorted(active, key=lambda r: r.get("last_used") or "")[:count]


def propose(
    text: str, *, source_turn: str = "", provenance: str = "user", kind: str = "preference"
) -> dict[str, Any]:
    """A proposed record for `text`, not active until confirm(). Statuses:
    proposed (with `similar`, the active rule it would replace, if any),
    duplicate (already active; its last_used is refreshed), full (nothing
    stored; `review` lists the least-recently-used rules), empty."""
    text = " ".join(str(text or "").split())
    if not normalize(text):
        return {"status": "empty"}
    with _edit() as records:
        same = _same(records, text)
        if same is not None:
            same["last_used"] = _now()
            return {"status": "duplicate", "record": dict(same)}
        similar = _closest(records, text)
        if similar is None and len(_active(records)) >= MAX_ACTIVE:
            return {"status": "full", "review": [dict(r) for r in review(records)]}
        norm = normalize(text)
        records[:] = [r for r in records if r.get("confirmed") or normalize(r["text"]) != norm]
        stamp = _now()
        record = {
            "id": "m" + secrets.token_hex(4),
            "text": text,
            "kind": kind if kind in KINDS else "preference",
            "source_turn": source_turn,
            "created": stamp,
            "last_used": stamp,
            "confirmed": False,
            "provenance": provenance,
        }
        records.append(record)
        proposed = [r for r in records if not r.get("confirmed")]
        for stale in proposed[:-MAX_PROPOSED]:
            records.remove(stale)
        return {"status": "proposed", "record": dict(record), "similar": dict(similar) if similar else None}


def pending(text: str, *, session_prefix: str = "") -> dict[str, Any] | None:
    """The proposal for `text` (exact or near), from turns starting with
    `session_prefix`, newest first."""
    norm = normalize(text)
    candidates = [
        r
        for r in load_records()
        if not r.get("confirmed") and str(r.get("source_turn", "")).startswith(session_prefix)
    ]
    for r in reversed(candidates):
        if normalize(r["text"]) == norm:
            return r
    for r in reversed(candidates):
        if similarity(r["text"], text) >= SIMILAR:
            return r
    return None


def confirm(record_id: str) -> dict[str, Any]:
    """Activate a proposal. Statuses: saved, merged (it replaced the
    near-duplicate `replaced`), duplicate, full (the proposal is kept),
    missing."""
    with _edit() as records:
        record = next((r for r in records if r["id"] == record_id and not r.get("confirmed")), None)
        if record is None:
            return {"status": "missing"}
        stamp = _now()
        same = _same(records, record["text"])
        if same is not None:
            records.remove(record)
            same["last_used"] = stamp
            return {"status": "duplicate", "record": dict(same)}
        similar = _closest(records, record["text"])
        if similar is not None:
            records.remove(record)
            replaced = similar["text"]
            similar.update(
                text=record["text"],
                last_used=stamp,
                source_turn=record["source_turn"],
                provenance=record["provenance"],
            )
            return {"status": "merged", "record": dict(similar), "replaced": replaced}
        if len(_active(records)) >= MAX_ACTIVE:
            return {"status": "full", "review": [dict(r) for r in review(records)]}
        records.remove(record)
        record.update(confirmed=True, last_used=stamp)
        # after the last active record: the list keeps its order
        at = max((i + 1 for i, r in enumerate(records) if r.get("confirmed")), default=0)
        records.insert(at, record)
        return {"status": "saved", "record": dict(record)}


def add_preference(text: str, *, provenance: str = "user:settings") -> dict[str, Any]:
    """A rule the user adds directly (settings, CLI): active at once.
    Statuses: saved, duplicate, full, empty. Nothing is ever dropped."""
    text = " ".join(str(text or "").split())
    if not normalize(text):
        return {"status": "empty"}
    with _edit() as records:
        same = _same(records, text)
        stamp = _now()
        if same is not None:
            same["last_used"] = stamp
            return {"status": "duplicate", "record": dict(same)}
        if len(_active(records)) >= MAX_ACTIVE:
            return {"status": "full", "review": [dict(r) for r in review(records)]}
        record = {
            "id": "m" + secrets.token_hex(4),
            "text": text,
            "kind": "preference",
            "source_turn": "",
            "created": stamp,
            "last_used": stamp,
            "confirmed": True,
            "provenance": provenance,
        }
        at = max((i + 1 for i, r in enumerate(records) if r.get("confirmed")), default=0)
        records.insert(at, record)
        return {"status": "saved", "record": dict(record)}


def forget(record_id: str) -> bool:
    """Remove a record (active or proposed). False if there is none."""
    with _edit() as records:
        before = len(records)
        records[:] = [r for r in records if r["id"] != record_id]
        return len(records) != before


def edit(record_id: str, text: str) -> dict[str, Any]:
    """Change a record's text. Statuses: saved, duplicate (another active
    rule already says it; nothing changed), missing, empty."""
    text = " ".join(str(text or "").split())
    if not normalize(text):
        return {"status": "empty"}
    with _edit() as records:
        record = next((r for r in records if r["id"] == record_id), None)
        if record is None:
            return {"status": "missing"}
        same = _same(records, text)
        if same is not None and same["id"] != record_id:
            return {"status": "duplicate", "record": dict(same)}
        record.update(text=text, last_used=_now())
        return {"status": "saved", "record": dict(record)}
