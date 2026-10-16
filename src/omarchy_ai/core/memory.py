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

from ..config import STATE_DIR
from . import statefile

log = logging.getLogger("omarchy_ai.core.memory")

PREFERENCES_PATH = STATE_DIR / "learned_preferences.yaml"
_MAX_PREFERENCES = 50


def load_preferences() -> list[str]:
    try:
        data = statefile.read_yaml(PREFERENCES_PATH, [], expect=list)
    except Exception:  # noqa: BLE001
        log.warning("failed to load learned preferences", exc_info=True)
        return []
    return [str(p) for p in data]


def add_preference(text: str) -> None:
    # Locked read-modify-write (core/statefile.py): two processes adding at
    # once both keep theirs, and a file that does not parse is kept aside
    # instead of being overwritten by this one preference.
    with statefile.locked_yaml(PREFERENCES_PATH, [], expect=list) as doc:
        prefs = [str(p) for p in doc.data]
        if text in prefs:
            return
        prefs.append(text)
        doc.data = prefs[-_MAX_PREFERENCES:]
