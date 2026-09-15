"""Cross-session learned preferences — the self-improvement mechanism's v1
scope (user explicitly chose "learn from corrections" over log-based
self-diagnosis or self-editing code, which stay out of scope for now).

The model calls the remember_preference tool when the user gives a standing
correction/instruction ("always confirm before muting", "call me X"), not
for one-off task requests. Saved here as plain text, folded into every
future session's instructions by live.py. No code changes, no autonomy
beyond remembering text the user themselves said.
"""

from __future__ import annotations

import logging

import yaml

from ..config import STATE_DIR

log = logging.getLogger("omarchy_ai.core.memory")

PREFERENCES_PATH = STATE_DIR / "learned_preferences.yaml"
_MAX_PREFERENCES = 50


def load_preferences() -> list[str]:
    if not PREFERENCES_PATH.exists():
        return []
    try:
        data = yaml.safe_load(PREFERENCES_PATH.read_text()) or []
    except Exception:  # noqa: BLE001
        log.warning("failed to load learned preferences", exc_info=True)
        return []
    if not isinstance(data, list):
        return []
    return [str(p) for p in data]


def add_preference(text: str) -> None:
    prefs = load_preferences()
    if text in prefs:
        return
    prefs.append(text)
    prefs = prefs[-_MAX_PREFERENCES:]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PREFERENCES_PATH.write_text(yaml.safe_dump(prefs, allow_unicode=True, sort_keys=False))
