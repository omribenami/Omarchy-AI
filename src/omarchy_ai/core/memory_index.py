"""Recall over saved notes: the facts and procedures in core/memory.py (plan 4.4).

Notes are never in the prompt. The recall tool asks this module for the few
that match a query, and each hit's use_count and last_used are bumped
(memory.touch), which is what the staleness review reads.

The index is a cache: memory_index.sqlite3 beside learned_preferences.yaml,
one FTS5 table over the active notes' normalized text (memory.normalize,
so case, accents and Hebrew points do not matter), ranked by bm25. It is
rebuilt whenever the notes' fingerprint (ids, kinds and texts) changes, and
deleted and rebuilt when it does not open. Without FTS5, or when the
database fails, a rapidfuzz ranking over the notes answers instead.

Optional, off by default (`omarchy-ai-settings memory-embeddings on`): the
notes and the query are embedded through the Vercel AI Gateway and the
cosine ranking is fused with the bm25 one (reciprocal rank), so "where is
the spare key" also finds "the extra house key is in the blue drawer". Any
Gateway failure falls back to the FTS ranking alone. The Gateway key is
read from the configured key file and never logged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import urllib.request
from contextlib import closing
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from . import memory

log = logging.getLogger("omarchy_ai.core.memory_index")

INDEX_NAME = "memory_index.sqlite3"
LIMIT = 5
FUZZY_MIN = 60.0  # rapidfuzz fallback: token_set_ratio a hit needs
EMBED_URL = "https://ai-gateway.vercel.sh/v1/embeddings"
EMBED_MODEL = "openai/text-embedding-3-small"
EMBED_TIMEOUT = 5.0
EMBED_MIN = 0.35  # cosine a semantic-only hit needs
RRF_K = 60

# Words a query is made of that say nothing about which note it wants.
_STOP = frozenset(
    "a an the is are was were be to of in on at for and or my me i you your it its "
    "what where when how who which do does did please about that this with from tell "
    "remember recall saved note notes there".split()
)


def index_path() -> Path:
    return memory.PREFERENCES_PATH.with_name(INDEX_NAME)


def _terms(query: str) -> list[str]:
    words = memory.normalize(query).split()
    return [w for w in words if w not in _STOP] or words


def _match(terms: list[str]) -> str:
    # normalize() leaves word characters only, so a term cannot break the quotes
    return " OR ".join(f'"{t}"*' for t in terms)


def _fingerprint(notes: list[dict[str, Any]]) -> str:
    rows = sorted((n["id"], n.get("kind", ""), n["text"]) for n in notes)
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _sync(conn: sqlite3.Connection, notes: list[dict[str, Any]]) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5("
        "id UNINDEXED, body, tokenize = 'unicode61 remove_diacritics 2')"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS vectors (id TEXT PRIMARY KEY, digest TEXT, vector TEXT)"
    )
    fingerprint = _fingerprint(notes)
    row = conn.execute("SELECT value FROM meta WHERE key = 'fingerprint'").fetchone()
    if row and row[0] == fingerprint:
        return
    with conn:
        conn.execute("DELETE FROM notes_fts")
        conn.executemany(
            "INSERT INTO notes_fts (id, body) VALUES (?, ?)",
            [(n["id"], memory.normalize(n["text"])) for n in notes],
        )
        conn.execute("DELETE FROM vectors WHERE id NOT IN (SELECT id FROM notes_fts)")
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('fingerprint', ?)",
            (fingerprint,),
        )


def _fts_ranking(conn: sqlite3.Connection, terms: list[str], limit: int) -> list[str]:
    rows = conn.execute(
        "SELECT id FROM notes_fts WHERE notes_fts MATCH ? ORDER BY bm25(notes_fts) LIMIT ?",
        (_match(terms), limit),
    ).fetchall()
    return [r[0] for r in rows]


def _fuzzy_ranking(
    notes: list[dict[str, Any]], terms: list[str], limit: int
) -> list[str]:
    query = " ".join(terms)
    scored = [
        (fuzz.token_set_ratio(query, memory.normalize(n["text"])), i, n["id"])
        for i, n in enumerate(notes)
    ]
    hits = sorted((s for s in scored if s[0] >= FUZZY_MIN), key=lambda s: (-s[0], s[1]))
    return [s[2] for s in hits[:limit]]


def _gateway_key() -> str:
    from ..config import load_config

    return Path(load_config().vercel_gateway_api_key_path).read_text().strip()


def _embed(texts: list[str]) -> list[list[float]]:
    """One Gateway embeddings request (OpenAI-compatible)."""
    request = urllib.request.Request(
        EMBED_URL,
        data=json.dumps({"model": EMBED_MODEL, "input": texts}).encode(),
        headers={
            "Authorization": "Bearer " + _gateway_key(),
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=EMBED_TIMEOUT) as response:
        data = json.loads(response.read())
    rows = sorted(data["data"], key=lambda r: r["index"])
    return [[float(x) for x in r["embedding"]] for r in rows]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _semantic_ranking(
    conn: sqlite3.Connection, notes: list[dict[str, Any]], query: str, limit: int
) -> list[str]:
    stored = {
        row[0]: (row[1], row[2])
        for row in conn.execute("SELECT id, digest, vector FROM vectors").fetchall()
    }
    missing = [n for n in notes if stored.get(n["id"], ("",))[0] != _digest(n["text"])]
    vectors = _embed([query] + [n["text"] for n in missing])
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO vectors (id, digest, vector) VALUES (?, ?, ?)",
            [
                (n["id"], _digest(n["text"]), json.dumps(v))
                for n, v in zip(missing, vectors[1:])
            ],
        )
    known = {n["id"]: v for n, v in zip(missing, vectors[1:])}
    for n in notes:
        if n["id"] not in known and n["id"] in stored:
            known[n["id"]] = json.loads(stored[n["id"]][1])
    scored = sorted(
        ((_cosine(vectors[0], v), i) for i, v in known.items()), reverse=True
    )
    return [i for score, i in scored if score >= EMBED_MIN][:limit]


def _fuse(*rankings: list[str]) -> list[str]:
    score: dict[str, float] = {}
    for ranking in rankings:
        for rank, record_id in enumerate(ranking):
            score[record_id] = score.get(record_id, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(score, key=lambda i: -score[i])


def _rank(notes: list[dict[str, Any]], query: str, limit: int) -> list[str]:
    terms = _terms(query)
    if not terms:
        return []
    for attempt in (1, 2):
        try:
            with closing(sqlite3.connect(index_path(), timeout=5.0)) as conn:
                _sync(conn, notes)
                ranking = _fts_ranking(conn, terms, limit)
                if memory.embeddings_enabled():
                    try:
                        ranking = _fuse(
                            ranking, _semantic_ranking(conn, notes, query, limit)
                        )
                    except Exception as exc:  # noqa: BLE001 -- the Gateway only adds to FTS
                        log.warning(
                            "memory embeddings unavailable: %s", type(exc).__name__
                        )
                return ranking[:limit]
        except sqlite3.DatabaseError:
            if attempt == 1:  # a cache: drop a broken one and build it again
                log.warning("memory index unreadable; rebuilding", exc_info=True)
                index_path().unlink(missing_ok=True)
                continue
            log.warning("memory index unavailable; fuzzy recall", exc_info=True)
    return _fuzzy_ranking(notes, terms, limit)


def search(query: str, limit: int = LIMIT) -> list[dict[str, Any]]:
    """The active notes that best match `query`, best first; each hit's
    use_count and last_used are bumped."""
    notes = [n for n in memory.load_notes() if n.get("confirmed")]
    if not notes or not str(query or "").strip():
        return []
    ranking = _rank(notes, str(query), limit)
    if not ranking:
        return []
    touched = {r["id"]: r for r in memory.touch(ranking)}
    return [touched[i] for i in ranking if i in touched]
