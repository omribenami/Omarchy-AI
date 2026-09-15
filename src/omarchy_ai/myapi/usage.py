"""Local usage log for MyApi tool calls — powers the terminal dashboard
(cli/dashboard.py's "which service, how much" view).

Modeled directly on core/history.py's JSONL-with-retention shape rather
than inventing a new store: JSONL specifically because a mid-write crash
can't corrupt earlier lines, rewritten-whole on each append (not true
append-only) so pruning by age is a plain filter. This is local-only and
independent of anything MyApi's own dashboard tracks — it answers "what
has *this machine's* Omarchy AI actually called", not a account-wide
audit log.
"""

from __future__ import annotations

import json
import logging
import time

from ..config import MYAPI_USAGE_PATH, STATE_DIR

log = logging.getLogger("omarchy_ai.myapi.usage")

# Only "recent" matters for a live dashboard — no reason to let this grow
# forever.
RETENTION_HOURS = 24 * 14  # 2 weeks


def _load_raw() -> list[dict]:
    if not MYAPI_USAGE_PATH.exists():
        return []
    records: list[dict] = []
    try:
        with open(MYAPI_USAGE_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        log.warning("failed to read myapi usage log", exc_info=True)
    return records


def record(*, service: str, path: str, method: str, ok: bool, duration_ms: float) -> None:
    now = time.time()
    records = _load_raw()
    records.append(
        {
            "ts": now,
            "service": service,
            "path": path,
            "method": method,
            "ok": ok,
            "duration_ms": round(duration_ms, 1),
        }
    )
    cutoff = now - RETENTION_HOURS * 3600
    records = [r for r in records if r.get("ts", 0) >= cutoff]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(MYAPI_USAGE_PATH, "w") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        log.warning("failed to save myapi usage log", exc_info=True)


def recent(hours: float = RETENTION_HOURS) -> list[dict]:
    cutoff = time.time() - hours * 3600
    return [r for r in _load_raw() if r.get("ts", 0) >= cutoff]


def aggregate() -> dict[str, dict]:
    """Per-service call counts/success rate for the dashboard's services
    table: {"gmail": {"calls_total": N, "calls_today": N, "ok_total": N,
    "last_ts": epoch}, ...}."""
    all_records = _load_raw()
    now = time.time()
    today_cutoff = now - 24 * 3600
    by_service: dict[str, dict] = {}
    for r in all_records:
        service = r.get("service") or "?"
        stats = by_service.setdefault(
            service, {"calls_total": 0, "calls_today": 0, "ok_total": 0, "last_ts": 0.0}
        )
        stats["calls_total"] += 1
        if r.get("ts", 0) >= today_cutoff:
            stats["calls_today"] += 1
        if r.get("ok"):
            stats["ok_total"] += 1
        stats["last_ts"] = max(stats["last_ts"], r.get("ts", 0))
    return by_service
