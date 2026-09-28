"""Keep MyApi tool results small with one fast Jev selection pass.

The old path asked a general model to write an answer and then asked Jev to
fact-check it. That added two remote calls after MyApi had already answered.
The live model needs a compact, relevant payload, not another prose model.
"""
from __future__ import annotations

import json
import logging

from ..core.jev import Jev, JevError, choice

log = logging.getLogger("omarchy_ai.voice.myapi_refinement")

MAX_REFINEMENT_SOURCE_CHARS = 80_000
FAST_RESULT_CHARS = 16_000
MAX_CANDIDATES = 30
MAX_CANDIDATE_CHARS = 1_200
KEEP_CANDIDATES = 8


def _bounded_source(result: str) -> str:
    if len(result) <= MAX_REFINEMENT_SOURCE_CHARS:
        return result
    marker = f"\n...[MyApi result shortened from {len(result):,} characters]...\n"
    available = MAX_REFINEMENT_SOURCE_CHARS - len(marker)
    head = available * 3 // 4
    return result[:head] + marker + result[-(available - head):]


def _candidate_lists(value: object, path: str = "$") -> list[tuple[str, list]]:
    found: list[tuple[str, list]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if isinstance(child, list) and child:
                found.append((child_path, child))
            if isinstance(child, (dict, list)):
                found.extend(_candidate_lists(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value[:10]):
            if isinstance(child, (dict, list)):
                found.extend(_candidate_lists(child, f"{path}[{index}]"))
    return found


def _compact(value: object, limit: int = MAX_CANDIDATE_CHARS) -> str:
    rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return rendered if len(rendered) <= limit else rendered[:limit] + "…"


def refine_myapi_result(config, request: str, result: str) -> str:
    """Return small results unchanged; rank records in oversized JSON once."""
    del config
    request = request.strip()
    if not request or not result or len(result) <= FAST_RESULT_CHARS:
        return result
    source = _bounded_source(result)
    try:
        parsed = json.loads(source)
    except (json.JSONDecodeError, TypeError):
        return source
    lists = _candidate_lists(parsed)
    if not lists:
        return source
    path, records = max(lists, key=lambda item: (
        sum(isinstance(row, dict) for row in item[1][:MAX_CANDIDATES]),
        min(len(item[1]), MAX_CANDIDATES),
    ))
    records = records[:MAX_CANDIDATES]
    options = {f"r{i}": _compact(record) for i, record in enumerate(records)}
    options["none"] = "None of these records helps answer the request."
    try:
        answer = Jev().ask(
            {"request": request[:800]},
            {"records": choice(
                "Which record best helps answer request? Use probabilities to rank all relevant records.",
                options,
            )},
            timeout=2.5,
            retries=0,
            fail_fast=True,
        )["records"]
        probabilities = answer.get("probabilities") or {}
        ranked = sorted(
            ((int(name[1:]), score) for name, score in probabilities.items()
             if name.startswith("r") and name[1:].isdigit()),
            key=lambda item: item[1], reverse=True,
        )
        selected = [records[index] for index, _score in ranked[:KEEP_CANDIDATES]]
        if not selected:
            return source
    except (JevError, KeyError, TypeError, ValueError) as exc:
        log.info("MyApi Jev selection unavailable: %s", str(exc)[:160])
        return source
    return json.dumps({
        "jev_selected": True,
        "request": request,
        "source_path": path,
        "selected_records": selected,
        "records_considered": len(records),
        "original_chars": len(result),
    }, ensure_ascii=False)
