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

Notes (plan 4.4): facts ("the spare key is in the blue drawer") and
procedures ("to back up the laptop: ...") go through the same propose /
confirm flow and the same taint rule, but they are never in the prompt: the
recall tool finds them on demand (core/memory_index.py, an FTS5 index that is
only a cache of them). They live in memory_notes.json (schema_version 1:
`records`, each with a `use_count` that recall bumps, and `settings`), a file
older code never reads or rewrites, so a rollback leaves them untouched and
learned_preferences.yaml stays exactly the list of active preferences.

Conflicts: a new item that says the opposite of an active one of its kind
("never confirm before muting" against "always confirm before muting": the
same words once negations are set aside, one side negated) is proposed with
`conflict`, and the user is asked which wins. Only a yes for the new one
replaces the old; `keep_both` saves it beside a similar one instead. Nothing
is merged without the user's answer.
"""

from __future__ import annotations

from contextlib import contextmanager
import datetime
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
NOTE_KINDS = ("fact", "procedure")  # recalled on demand, never in the prompt
NOTES_SCHEMA_VERSION = 1
MAX_NOTES = 500
STALE_DAYS = 90
CONFLICT_CORE = 90.0  # token_sort_ratio of two texts' negation-free words
LEGACY = "user:legacy"

# Words that flip what a rule says, in the languages the user speaks (after
# normalize(): "don't" is "don t"). "always"/"תמיד" only intensify.
_NEGATIONS = frozenset({
    "never", "not", "no", "don", "doesn", "didn", "won", "shouldn", "cannot", "stop", "avoid",
    "without", "לא", "אל", "בלי", "אסור", "תפסיק",
})
_NEUTRAL = frozenset({"t", "always", "ever", "please", "תמיד", "בבקשה"})

_FENCE_TAG = re.compile(r"<\s*(/?)\s*user_memory\b", re.IGNORECASE)


def records_path() -> Path:
    return PREFERENCES_PATH.with_name("memory.json")


def notes_path() -> Path:
    return PREFERENCES_PATH.with_name("memory_notes.json")


def is_note(record: dict[str, Any]) -> bool:
    return record.get("kind") in NOTE_KINDS


def normalize(text: str) -> str:
    """What two texts are compared by: NFKC, case-folded, combining marks
    (Hebrew points, accents) dropped, punctuation and spacing collapsed."""
    folded = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", str(text)).casefold())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return " ".join(re.sub(r"[\W_]+", " ", folded).split())


def similarity(a: str, b: str) -> float:
    return float(fuzz.ratio(normalize(a), normalize(b)))


def _polarity(text: str) -> tuple[bool, str]:
    """(negated, the words that are left once negations are set aside)."""
    words = normalize(text).split()
    negated = sum(w in _NEGATIONS for w in words) % 2 == 1
    core = " ".join(w for w in words if w not in _NEGATIONS and w not in _NEUTRAL)
    return negated, core


def contradicts(a: str, b: str) -> bool:
    """True when `a` and `b` say the same thing with opposite polarity.
    Deliberately narrow: a missed conflict only means the near-duplicate
    question (or none) is asked, a false one only means the user is asked
    which wins. Neither ever changes memory without their answer."""
    neg_a, core_a = _polarity(a)
    neg_b, core_b = _polarity(b)
    return bool(core_a and core_b) and neg_a != neg_b and fuzz.token_sort_ratio(core_a, core_b) >= CONFLICT_CORE


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


def _notes_of(doc: Any) -> list[dict[str, Any]]:
    stored = doc.get("records") if isinstance(doc, dict) else None
    return [
        {**r, "use_count": int(r.get("use_count") or 0)}
        for r in (stored if isinstance(stored, list) else [])
        if isinstance(r, dict) and r.get("id") and r.get("text") and r.get("kind") in NOTE_KINDS
    ]


def _read_notes_doc() -> dict[str, Any]:
    return statefile.read_json(
        notes_path(), {"records": []}, expect=dict, version=NOTES_SCHEMA_VERSION, lock=PREFERENCES_PATH
    )


def load_notes() -> list[dict[str, Any]]:
    """Facts and procedures, active and proposed (never in the prompt)."""
    try:
        return _notes_of(_read_notes_doc())
    except Exception:  # noqa: BLE001
        log.warning("failed to load memory notes", exc_info=True)
        return []


def load_records() -> list[dict[str, Any]]:
    """Every record: the active preferences first (in prompt order), then
    their proposals, then the notes (facts and procedures)."""
    try:
        texts = _read_texts()
    except Exception:  # noqa: BLE001
        log.warning("failed to load learned preferences", exc_info=True)
        return load_notes()
    try:
        meta = _read_meta()
    except Exception:  # noqa: BLE001 -- metadata never hides an active rule
        log.warning("failed to load memory metadata", exc_info=True)
        meta = []
    return _assemble(texts, meta) + load_notes()


def notes_hint() -> str:
    """One line for the prompt when notes exist (their texts never are)."""
    count = sum(1 for r in load_notes() if r.get("confirmed"))
    if not count:
        return ""
    return (f"The user also has {count} saved note{'s' if count != 1 else ''} (facts and procedures), not "
            "listed here: look one up with the recall tool when it may help.")


def embeddings_enabled() -> bool:
    """The optional Gateway embedding re-rank for recall: off by default."""
    try:
        settings = _read_notes_doc().get("settings")
    except Exception:  # noqa: BLE001
        return False
    return isinstance(settings, dict) and settings.get("embeddings") is True


def set_embeddings(enabled: bool) -> None:
    with statefile.locked(PREFERENCES_PATH):
        with statefile.locked_json(
            notes_path(), {"records": []}, expect=dict, version=NOTES_SCHEMA_VERSION
        ) as doc:
            settings = doc.data.get("settings")
            doc.data["settings"] = {**(settings if isinstance(settings, dict) else {}), "embeddings": bool(enabled)}


def load_preferences() -> list[str]:
    """The active rules' texts, exactly as stored: what the prompts inject."""
    try:
        return _read_texts()
    except Exception:  # noqa: BLE001
        log.warning("failed to load learned preferences", exc_info=True)
        return []


@contextmanager
def _edit() -> Iterator[list[dict[str, Any]]]:
    """Locked read-modify-write of the three files; change the list in
    place. Each file is written only when its own content changed, so a note
    never rewrites learned_preferences.yaml."""
    with statefile.locked(PREFERENCES_PATH):
        with statefile.locked_yaml(PREFERENCES_PATH, [], expect=list) as listing:
            with statefile.locked_json(
                records_path(), {"records": []}, expect=dict, version=SCHEMA_VERSION
            ) as meta:
                with statefile.locked_json(
                    notes_path(), {"records": []}, expect=dict, version=NOTES_SCHEMA_VERSION
                ) as notes:
                    stored = meta.data.get("records")
                    stored = [r for r in (stored if isinstance(stored, list) else [])
                              if not (isinstance(r, dict) and is_note(r))]
                    records = _assemble([str(p) for p in listing.data], stored) + _notes_of(notes.data)
                    yield records
                    preferences = [r for r in records if not is_note(r)]
                    notes.data["records"] = [r for r in records if is_note(r)]
                meta.data = {"records": preferences}
            listing.data = [r["text"] for r in preferences if r.get("confirmed")]


def _kind(record: dict[str, Any]) -> str:
    return str(record.get("kind") or "preference")


def _active(records: list[dict[str, Any]], kind: str = "preference") -> list[dict[str, Any]]:
    """Active records of one kind ("note": facts and procedures together)."""
    if kind == "note":
        return [r for r in records if r.get("confirmed") and is_note(r)]
    return [r for r in records if r.get("confirmed") and _kind(r) == kind]


def _full(records: list[dict[str, Any]], kind: str) -> bool:
    if kind in NOTE_KINDS:
        return len(_active(records, "note")) >= MAX_NOTES
    return len(_active(records)) >= MAX_ACTIVE


def _same(records: list[dict[str, Any]], text: str, kind: str = "preference") -> dict[str, Any] | None:
    norm = normalize(text)
    return next((r for r in _active(records, kind) if normalize(r["text"]) == norm), None)


def _closest(
    records: list[dict[str, Any]], text: str, skip: str = "", kind: str = "preference"
) -> dict[str, Any] | None:
    best, score = None, SIMILAR
    for r in _active(records, kind):
        if r["id"] == skip:
            continue
        value = similarity(r["text"], text)
        if value >= score:
            best, score = r, value
    return best


def _conflict(records: list[dict[str, Any]], text: str, kind: str = "preference") -> dict[str, Any] | None:
    return next((r for r in _active(records, kind) if contradicts(r["text"], text)), None)


def _insert(records: list[dict[str, Any]], record: dict[str, Any]) -> None:
    """A newly active preference goes after the last active one (the list
    keeps its order); a note goes at the end."""
    if is_note(record):
        records.append(record)
        return
    at = max((i + 1 for i, r in enumerate(records) if r.get("confirmed") and not is_note(r)), default=0)
    records.insert(at, record)


def _new_record(text: str, kind: str, *, source_turn: str, provenance: str, confirmed: bool) -> dict[str, Any]:
    stamp = _now()
    record: dict[str, Any] = {
        "id": "m" + secrets.token_hex(4),
        "text": text,
        "kind": kind,
        "source_turn": source_turn,
        "created": stamp,
        "last_used": stamp,
        "confirmed": confirmed,
        "provenance": provenance,
    }
    if kind in NOTE_KINDS:
        record["use_count"] = 0
    return record


def review(records: list[dict[str, Any]] | None = None, count: int = REVIEW_COUNT) -> list[dict[str, Any]]:
    """The least-recently-used active rules: what to show for review."""
    active = _active(load_records() if records is None else records)
    return sorted(active, key=lambda r: r.get("last_used") or "")[:count]


def _idle_days(stamp: str, now: datetime.datetime) -> float | None:
    try:
        then = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError):
        return None
    return (now - then).total_seconds() / 86400


def stale(records: list[dict[str, Any]] | None = None, days: int = STALE_DAYS) -> list[dict[str, Any]]:
    """Active notes not recalled (or said again, or edited) for `days` days,
    longest idle first, each with `idle_days`: offered for review, never
    dropped. Preferences are left out: they are in every prompt, so a
    last_used date says nothing about whether one still matters (the
    review past MAX_ACTIVE covers them)."""
    now = datetime.datetime.now().astimezone()
    found = []
    for r in load_records() if records is None else records:
        idle = _idle_days(str(r.get("last_used") or ""), now) if r.get("confirmed") and is_note(r) else None
        if idle is not None and idle >= days:
            found.append({**r, "idle_days": int(idle)})
    return sorted(found, key=lambda r: -r["idle_days"])


def touch(record_ids: list[str]) -> list[dict[str, Any]]:
    """Recall hits: bump last_used and use_count on those active notes."""
    wanted = set(record_ids)
    if not wanted:
        return []
    stamp = _now()
    with _edit() as records:
        hit = []
        for r in records:
            if r["id"] in wanted and is_note(r) and r.get("confirmed"):
                r.update(last_used=stamp, use_count=int(r.get("use_count") or 0) + 1)
                hit.append(dict(r))
        return hit


def propose(
    text: str, *, source_turn: str = "", provenance: str = "user", kind: str = "preference"
) -> dict[str, Any]:
    """A proposed record for `text`, not active until confirm(). Statuses:
    proposed (with `similar`, the active rule it would replace, if any),
    duplicate (already active; its last_used is refreshed), full (nothing
    stored; `review` lists the least-recently-used rules), empty."""
    text = " ".join(str(text or "").split())
    kind = kind if kind in KINDS else "preference"
    if not normalize(text):
        return {"status": "empty"}
    with _edit() as records:
        same = _same(records, text, kind)
        if same is not None:
            same["last_used"] = _now()
            return {"status": "duplicate", "record": dict(same)}
        conflict = _conflict(records, text, kind)
        similar = None if conflict else _closest(records, text, kind=kind)
        if conflict is None and similar is None and _full(records, kind):
            return {"status": "full", "review": [dict(r) for r in review(records)]}
        norm = normalize(text)
        records[:] = [r for r in records if r.get("confirmed") or normalize(r["text"]) != norm]
        record = _new_record(text, kind, source_turn=source_turn, provenance=provenance, confirmed=False)
        if conflict is not None:
            record["conflicts_with"] = conflict["id"]
        records.append(record)
        proposed = [r for r in records if not r.get("confirmed")]
        for old in proposed[:-MAX_PROPOSED]:
            records.remove(old)
        return {
            "status": "proposed",
            "record": dict(record),
            "similar": dict(similar) if similar else None,
            "conflict": dict(conflict) if conflict else None,
        }


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


def confirm(record_id: str, *, keep_both: bool = False) -> dict[str, Any]:
    """Activate a proposal. Statuses: saved, merged (it replaced the
    contradicted or near-duplicate record `replaced`), duplicate, full (the
    proposal is kept), missing. keep_both saves it beside them instead."""
    with _edit() as records:
        record = next((r for r in records if r["id"] == record_id and not r.get("confirmed")), None)
        if record is None:
            return {"status": "missing"}
        kind = _kind(record)
        stamp = _now()
        same = _same(records, record["text"], kind)
        if same is not None:
            records.remove(record)
            same["last_used"] = stamp
            return {"status": "duplicate", "record": dict(same)}
        conflict_id = record.pop("conflicts_with", "")
        if not keep_both:
            target = next((r for r in _active(records, kind) if r["id"] == conflict_id), None)
            target = target or _closest(records, record["text"], kind=kind)
            if target is not None:
                records.remove(record)
                replaced = target["text"]
                target.update(
                    text=record["text"],
                    last_used=stamp,
                    source_turn=record["source_turn"],
                    provenance=record["provenance"],
                )
                return {"status": "merged", "record": dict(target), "replaced": replaced}
        if _full(records, kind):
            if conflict_id:
                record["conflicts_with"] = conflict_id
            return {"status": "full", "review": [dict(r) for r in review(records)]}
        records.remove(record)
        record.update(confirmed=True, last_used=stamp)
        _insert(records, record)
        return {"status": "saved", "record": dict(record)}


def add_preference(text: str, *, provenance: str = "user:settings", kind: str = "preference") -> dict[str, Any]:
    """A rule (or, with `kind`, a fact or procedure) the user adds directly
    (settings, CLI): active at once. Statuses: saved, duplicate, full,
    empty. Nothing is ever dropped."""
    text = " ".join(str(text or "").split())
    kind = kind if kind in KINDS else "preference"
    if not normalize(text):
        return {"status": "empty"}
    with _edit() as records:
        same = _same(records, text, kind)
        if same is not None:
            same["last_used"] = _now()
            return {"status": "duplicate", "record": dict(same)}
        if _full(records, kind):
            return {"status": "full", "review": [dict(r) for r in review(records)]}
        record = _new_record(text, kind, source_turn="", provenance=provenance, confirmed=True)
        _insert(records, record)
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
        same = _same(records, text, _kind(record))
        if same is not None and same["id"] != record_id:
            return {"status": "duplicate", "record": dict(same)}
        record.update(text=text, last_used=_now())
        return {"status": "saved", "record": dict(record)}
