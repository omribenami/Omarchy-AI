"""Bounded local retrieval; documentation never becomes executable actions."""
from functools import lru_cache
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1] / "knowledge"


@lru_cache(maxsize=1)
def documents():
    chunks = []
    for name in ("OMARCHY_EXPERT.md", "arch-operations.md"):
        for part in re.split(r"\n(?=## )", (ROOT / name).read_text()):
            chunks.append({"source": name, "text": part[:5000]})
    registry = json.loads((ROOT / "omarchy_capabilities.json").read_text())
    for command in registry["commands"]:
        chunks.append({"source": "omarchy_capabilities.json", "text": json.dumps({
            key: command.get(key) for key in
            ("route", "summary", "args", "risk", "verification_hint")
        }, ensure_ascii=False)})
    return chunks


def search(query: str, limit: int = 5):
    terms = set(re.findall(r"[\w-]+", query.lower()))
    ranked = sorted(documents(), key=lambda d: sum(
        min(d["text"].lower().count(term), 3) for term in terms if len(term) > 2
    ), reverse=True)
    return ranked[:max(1, min(limit, 5))]


def search_os_knowledge(args):
    from .actions import ActionResult
    query = args.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        return ActionResult(False, "Supply a knowledge query of 1–2000 characters.")
    return ActionResult(True, json.dumps({"reference_only": True,
        "authority": "Installed help and live state override this pinned reference.",
        "matches": search(query)}, ensure_ascii=False))
