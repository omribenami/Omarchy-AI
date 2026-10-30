"""Which one of a few named things (a TV, a camera) the user meant.

Journal 2026-10-22..26: "Door", "הדלת" and "הדלת, הרינג" each failed with
"no camera named ..." while one camera was called "front door"; start_casting
target "Living Room TV" failed while that TV was listed under its mDNS name.
The model then retried, asked the user, or gave up. A spoken name is matched
here by its words (English, a few common Hebrew/loan words for rooms and
places, the article dropped), then by spelling; the one candidate that fits
is used, and only a real tie is sent back as a question.
"""
from __future__ import annotations

import difflib
import re

# Words that say what kind of thing it is, not which one.
_FILLER = frozenset({
    "the", "a", "an", "my", "our", "in", "on", "at", "of", "to", "from",
    "camera", "cameras", "cam", "cctv", "tv", "television", "telly", "screen",
    "please", "one", "device",
    "מצלמה", "מצלמת", "המצלמה", "טלוויזיה", "הטלוויזיה", "טלויזיה", "הטלויזיה", "של", "את", "ב", "ה",
})

# Phrases first (longest first), then single words. Generic place words only.
_PHRASES = {
    "חדר שינה": "bedroom", "חדר השינה": "bedroom", "חדר ילדים": "kids room", "חדר הילדים": "kids room",
    "חדר תינוק": "nursery", "חדר התינוק": "nursery", "חדר עבודה": "office", "חדר אוכל": "dining room",
    "חדר משחקים": "playroom", "דלת כניסה": "front door", "דלת הכניסה": "front door",
    "lounge room": "living room", "family room": "living room", "sitting room": "living room",
    "front room": "living room", "main room": "living room", "salon": "living room",
}
_WORDS = {
    "סלון": "living room", "מטבח": "kitchen", "דלת": "door", "כניסה": "entrance front door",
    "חניה": "driveway parking", "חנייה": "driveway parking", "שביל": "driveway path", "חצר": "yard",
    "גינה": "garden yard", "מרפסת": "balcony porch", "משרד": "office", "מוסך": "garage", "שער": "gate",
    "מחסן": "shed storage", "פרגולה": "patio", "בריכה": "pool", "מדרגות": "stairs", "מסדרון": "hallway",
    "תינוק": "nursery baby", "ילדים": "kids", "שינה": "bedroom", "אמבטיה": "bathroom", "גג": "roof",
    "רחוב": "street", "אחורית": "back", "קדמית": "front", "אחורי": "back", "קדמי": "front",
    "lounge": "living", "backyard": "back yard", "frontdoor": "front door", "doorbell": "door",
    "entry": "entrance", "kid": "kids", "baby": "nursery",
}


def _translate_word(word: str) -> str:
    if word in _WORDS:
        return _WORDS[word]
    # Hebrew article/conjunction/preposition prefixes: הדלת, והחניה, בסלון.
    for cut in (2, 1):
        if len(word) > cut + 1 and all(c in "הובלמ" for c in word[:cut]) and word[cut:] in _WORDS:
            return _WORDS[word[cut:]]
    return word


def words(text: str) -> list[str]:
    """The identifying words of a name, translated and singular-ish."""
    text = " ".join(re.sub(r"[^\w]+", " ", str(text).casefold()).split())
    for phrase in sorted(_PHRASES, key=len, reverse=True):
        text = re.sub(rf"(?<!\w)[הוב]?{re.escape(phrase)}(?!\w)", f" {_PHRASES[phrase]} ", text)
    out = []
    for word in text.split():
        for part in _translate_word(word).split():
            if part not in _FILLER and part not in out:
                out.append(part)
    return out


def _covers(said: str, name: str) -> bool:
    return said == name or (min(len(said), len(name)) >= 3 and (name.startswith(said) or said.startswith(name)))


def match(said: str, candidates: dict[str, list[str]]) -> tuple[str | None, list[str]]:
    """(the key `said` means, None) or (None, the keys it could mean).

    `candidates` maps each thing's key to its names (the first is the one to
    say; the rest are other names it goes by). An empty list back means
    nothing fits at all."""
    said_clean = " ".join(str(said).casefold().split())
    if not said_clean:
        return None, []
    exact = [k for k, names in candidates.items() if any(said_clean == " ".join(n.casefold().split()) for n in names)]
    if len(exact) == 1:
        return exact[0], []
    said_words = words(said_clean)
    if not said_words:
        return None, list(candidates)
    tiers: dict[int, set[str]] = {}
    for key, names in candidates.items():
        best = 0
        for name in names:
            name_words = words(name)
            if not name_words:
                continue
            if said_words == name_words:
                score = 4
            else:
                hit = [w for w in said_words if any(_covers(w, n) for n in name_words)]
                if len(hit) == len(said_words):
                    score = 3          # every word said is in the name ("door" -> "front door")
                elif all(any(_covers(w, n) for w in said_words) for n in name_words):
                    score = 3          # the whole name is in what was said ("the front door, the ring one")
                elif hit:
                    score = 1 + (len(hit) / len(said_words) >= .5)
                else:
                    score = 0
            best = max(best, score)
        if best:
            tiers.setdefault(best, set()).add(key)
    if tiers:
        top = sorted(tiers[max(tiers)], key=list(candidates).index)
        return (top[0], []) if len(top) == 1 else (None, top)
    # Spelling: "drivway", "kitchn", a garbled transcript.
    joined = " ".join(said_words)
    scored = sorted(((max(difflib.SequenceMatcher(None, joined, " ".join(words(n)) or n.casefold()).ratio()
                          for n in names), key) for key, names in candidates.items()), reverse=True)
    if scored and scored[0][0] >= .75 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= .1):
        return scored[0][1], []
    close = [key for ratio, key in scored if ratio >= .75]
    return None, close
