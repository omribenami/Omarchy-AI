"""Tell the user when a provider's credits, quota or token limit run out.

Before this, running out was silent. Real logs:
- 2026-09-14 23:28 (OpenAI): `credit_balance_exhausted` hung up the
  conversation, and every wake after that failed within 0.3s. The user heard
  and saw nothing.
- 2026-09-19 12:04 (Gemini Live): `1011 Resource has been exhausted (e.g.
  check quota)` crashed the session mid-conversation.

An alert is three things at once:
- Screen: red dollar signs popping over every monitor with the provider's
  name (Quickshell plugin omarchy-ai.quota-alert, IPC target `quotaAlert`).
- Notification: a critical desktop notification with the billing link, which
  stays until dismissed.
- Voice: if a conversation is open on a different, working provider, it says
  the warning in the user's language (`speaker`, set by the daemon).
  Otherwise a pre-rendered clip in the assistant's own voice plays locally
  (voice/alerts/, made by scripts/make_quota_clips.py). The out-of-credit
  provider cannot be asked to speak about itself.

Background failures (Jev, vision, heartbeat) alert at most once per provider
every ALERT_EVERY seconds. A failure the user just caused, such as saying the
wake word to a provider that is out, alerts every time, with a short
debounce.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import subprocess
import threading
import time

log = logging.getLogger("omarchy_ai.core.quota")

CLIP_DIR = Path(__file__).resolve().parent.parent / "voice" / "alerts"
ALERT_EVERY = 600
USER_DEBOUNCE = 20

PROVIDERS = {
    "openai": ("OpenAI", "credits used up", "https://platform.openai.com/settings/organization/billing/"),
    "gemini": ("Gemini", "quota used up", "https://aistudio.google.com/usage"),
    "vercel": ("Vercel AI Gateway", "credits used up", "https://vercel.com/dashboard/ai-gateway"),
}

# Spoken text; the clips are rendered from exactly these strings. Hebrew is
# phrased gender-neutrally.
MESSAGES = {
    ("openai", "en"): "Heads up: your OpenAI credits have run out. Add credits on the OpenAI billing page to keep "
                      "using it.",
    ("gemini", "en"): "Heads up: your Gemini quota has run out. It works again when the quota resets, or after you "
                      "raise it.",
    ("vercel", "en"): "Heads up: your Vercel AI Gateway credits have run out. Jev and the Gateway features are "
                      "paused until you add credits.",
    ("openai", "he"): "לתשומת הלב: נגמר הקרדיט ב-OpenAI. כדי להמשיך, צריך להוסיף קרדיט בדף החיוב של OpenAI.",
    ("gemini", "he"): "לתשומת הלב: נגמרה המכסה של Gemini. זה יחזור לעבוד כשהמכסה תתאפס, או אחרי שמגדילים אותה.",
    ("vercel", "he"): "לתשומת הלב: נגמר הקרדיט ב-Vercel AI Gateway. Jev והיכולות של ה-Gateway מושהים עד שמוסיפים "
                      "קרדיט.",
}

# Wording the providers really use for "out of credit/quota" (see the
# module docstring), not plain rate limiting or outages: a transient 429 or
# 503 is retried elsewhere and must not cry wolf.
_QUOTA = re.compile(
    r"insufficient[_ ]quota|credit[_ ]balance|no credits|out of credits|insufficient[_ ]funds|"
    r"resource[_ ]has[_ ]been[_ ]exhausted|resource_exhausted|quota|billing|payment required|"
    r"exceeded your current|token limit|tokens? per (day|minute)", re.I)

# The daemon points this at the open conversation while one is running:
# speaker(provider, text) -> True when it will say it (False when the
# conversation runs on the very provider that is out).
speaker = None

_lock = threading.Lock()
_last: dict[str, float] = {}


def is_quota_error(text: str, status: int | None = None) -> bool:
    return status == 402 or bool(_QUOTA.search(str(text or "")))


def report(provider: str, detail: str = "", *, user_initiated: bool = False) -> bool:
    """Alert once (see the module docstring). Returns whether it alerted.
    Never raises: an alert must not break the code path that hit the limit."""
    if provider not in PROVIDERS:
        return False
    now = time.monotonic()
    with _lock:
        gap = USER_DEBOUNCE if user_initiated else ALERT_EVERY
        if now - _last.get(provider, -1e9) < gap:
            return False
        _last[provider] = now
    log.warning("Quota alert: provider=%s user_initiated=%s detail=%s", provider, user_initiated,
                " ".join(str(detail).split())[:300])
    threading.Thread(target=_alert, args=(provider,), daemon=True, name=f"quota-{provider}").start()
    return True


def _alert(provider: str) -> None:
    name, what, url = PROVIDERS[provider]
    _run(["omarchy-shell", "-q", "quotaAlert", "show", json.dumps({"provider": name, "message": what})])
    _run(["notify-send", "-u", "critical", "-a", "Omarchy AI", f"$ {name} {what}",
          f"Omarchy AI can't use {name} until this is fixed.\n{url}"])
    lang = language()
    said = False
    if speaker is not None:
        try:
            said = bool(speaker(provider, f"{name} {what}. " + MESSAGES[(provider, lang)]))
        except Exception:  # noqa: BLE001
            log.debug("live speaker failed", exc_info=True)
    if not said:
        clip = CLIP_DIR / f"quota-{provider}-{lang}.ogg"
        if not clip.exists():
            clip = CLIP_DIR / f"quota-{provider}-en.ogg"
        _run(["pw-play", str(clip)], timeout=20)


def language() -> str:
    """The user's language from their recent words: Hebrew or English."""
    try:
        from ..config import STATE_DIR
        lines = (STATE_DIR / "conversation_history.jsonl").read_text().splitlines()[-3:]
        text = " ".join(t["text"] for line in lines for t in json.loads(line).get("turns", [])
                        if t.get("role") == "user")
    except (OSError, ValueError, KeyError, TypeError):
        return "en"
    hebrew = sum(1 for c in text if "֐" <= c <= "׿")
    latin = sum(1 for c in text if c.isascii() and c.isalpha())
    return "he" if hebrew > latin else "en"


def _run(argv: list[str], timeout: float = 5) -> None:
    try:
        subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        log.debug("quota alert step failed: %s", argv[0], exc_info=True)
