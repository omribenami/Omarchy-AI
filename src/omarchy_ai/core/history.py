"""Rolling cross-session conversation history.

Each gpt-live-1 session used to start with zero memory of anything said in
a previous wake-word cycle, even seconds earlier — confirmed as a real gap
by the user ("we have no context at the start of each conversation and
that's not good"). This stores a compact transcript of each session and
folds recent ones (within a configurable retention window) into the next
session's instructions.

Distinct from core/memory.py's learned preferences: this is *what was said*
(conversation content), not standing behavioral rules. Stored as JSONL so a
mid-session crash can't corrupt earlier entries, keyed by wall-clock time so
pruning by age is a plain filter. Kept deliberately small — this whole blob
gets folded into a real-time, latency-sensitive voice session's prompt, not
the place for an ever-growing verbatim transcript.
"""

from __future__ import annotations

import json
import logging
import time

from ..config import STATE_DIR

log = logging.getLogger("omarchy_ai.core.history")

HISTORY_PATH = STATE_DIR / "conversation_history.jsonl"


def _load_raw() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    records = []
    try:
        with open(HISTORY_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:  # noqa: BLE001
        log.warning("failed to load conversation history", exc_info=True)
    return records


def append_session(turns: list[dict], retention_hours: float) -> None:
    """Record this session's turns, then drop anything older than
    retention_hours so the file can't grow without bound."""
    if not turns:
        return
    now = time.time()
    records = _load_raw()
    records.append({"ts": now, "turns": turns})
    cutoff = now - retention_hours * 3600
    records = [r for r in records if r.get("ts", 0) >= cutoff]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(HISTORY_PATH, "w") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        log.warning("failed to save conversation history", exc_info=True)


def load_recent_context(retention_hours: float, max_chars: int = 4000) -> str:
    """Render sessions within the retention window as a compact transcript,
    oldest first, truncated from the front (most recent content is more
    useful than older content when it doesn't all fit)."""
    now = time.time()
    cutoff = now - retention_hours * 3600
    records = [r for r in _load_raw() if r.get("ts", 0) >= cutoff]
    if not records:
        return ""
    lines: list[str] = []
    for r in records:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"]))
        lines.append(f"[{when}]")
        for turn in r.get("turns", []):
            text = (turn.get("text") or "").strip()
            if text:
                lines.append(f"{turn.get('role', '?')}: {text}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[-max_chars:]
    return text
