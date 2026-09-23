"""Typed, validated Jev evaluations for background work (heartbeat, skills).

Jev (TypeSafe, through Vercel AI Gateway /v4/ai/evaluation-model) answers
typed questions about a text state. It never generates text. Gateway's wire
format, confirmed against the live endpoint on 2026-09-22 and different from
TypeSafe's native api.typesafe.ai/v1/systemone:

- question types are exactly "choice" | "score" | "boolean" ("noul" -> 400)
- boolean  -> {"type": "boolean", "probability": p}
- choice   -> {"type": "choice", "choice": k, "probabilities": {k: p, ...}}
- score    -> {"type": "score", "score": s, "probabilities": {"0": p, ...}}
- confidence arrives separately: providerMetadata.typesafe.confidence.<id>
- probabilities are rounded to 2 decimals

TypeSafe's guidance (docs.typesafe.ai/model-jaggedness/jev-1.13) shapes the
callers: keep arithmetic, dates and counting in code, send only the state a
question needs, ask one literal judgment per question, and treat state as
data that may be adversarial (terminal output, file contents).
"""
from __future__ import annotations

import json
import logging
import math
import re
import time

log = logging.getLogger(__name__)


class JevError(RuntimeError):
    pass


_TRANSIENT = re.compile(r"HTTP (429|500|502|503|504|529)\b|timed out|temporarily unavailable", re.I)


def choice(instructions, criteria: dict) -> dict:
    if not 1 <= len(criteria) <= 255:
        raise ValueError("A Jev choice needs 1-255 options")
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def boolean(instructions) -> dict:
    return {"type": "boolean", "instructions": instructions}


def score(instructions, levels: list) -> dict:
    if not 2 <= len(levels) <= 10:
        raise ValueError("A Jev score needs 2-10 levels")
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


def _p(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise JevError("Missing or invalid Jev probability")
    return float(value)


def _distribution(raw, expected) -> dict:
    if not isinstance(raw, dict) or set(raw) != set(expected):
        raise JevError("Jev returned an incomplete distribution")
    values = {k: _p(v) for k, v in raw.items()}
    # 2-decimal rounding across up to 255 options can drift further than a
    # handful of options would; 0.02 matches desktop_jev for small sets.
    if abs(sum(values.values()) - 1) > max(.02, .005 * len(values)):
        raise JevError("Jev distribution does not sum to 1")
    return values


def parse(response: dict, questions: dict) -> dict:
    """Validate every answer against the question actually asked."""
    answers = response.get("answers") if isinstance(response, dict) else None
    if not isinstance(answers, dict):
        raise JevError("Jev response has no answers")
    confidence = (((response.get("providerMetadata") or {}).get("typesafe") or {}).get("confidence") or {})
    result = {}
    for name, question in questions.items():
        answer = answers.get(name)
        if not isinstance(answer, dict) or answer.get("type") != question["type"]:
            raise JevError(f"Jev answer for {name!r} is missing or mistyped")
        if question["type"] == "boolean":
            result[name] = {"p": _p(answer.get("probability"))}
            continue
        if question["type"] == "choice":
            probabilities = _distribution(answer.get("probabilities"), question["criteria"])
            selected = answer.get("choice")
            if selected not in probabilities or probabilities[selected] != max(probabilities.values()):
                raise JevError(f"Jev choice for {name!r} is not its most probable option")
            parsed = {"choice": selected, "p": probabilities[selected], "probabilities": probabilities}
        else:
            levels = [str(i) for i in range(len(question["criteria"]))]
            probabilities = _distribution(answer.get("probabilities"), levels)
            value = answer.get("score")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= len(levels) - 1:
                raise JevError(f"Jev score for {name!r} is out of range")
            parsed = {"score": float(value), "probabilities": probabilities}
        # Kept as absent rather than invented when Gateway omits it.
        parsed["confidence"] = _p(confidence[name]) if name in confidence else None
        result[name] = parsed
    return result


class Jev:
    """evaluate_response(state: str, questions: dict, timeout=...) -> raw dict."""

    def __init__(self, evaluate_response=None):
        self._evaluate = evaluate_response

    def _client(self):
        if self._evaluate is None:
            from ..config import load_config
            from ..voice.omarchy import GatewayClient
            self._evaluate = GatewayClient(load_config()).evaluate_response
        return self._evaluate

    def ask(self, state, questions: dict, *, timeout: float = 8, retries: int = 2) -> dict:
        """`retries` bounds transient-error retries; interactive callers use
        fewer so a flaky Gateway falls back fast instead of adding seconds."""
        text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
        for attempt in range(retries + 1):
            try:
                response = self._client()(text, questions, timeout=timeout)
                break
            except JevError:
                raise
            except Exception as exc:
                # TypeSafe asks for backoff on 429/529; Gateway also returned
                # a transient 503 in a real heartbeat run (2026-09-22).
                if attempt < retries and _TRANSIENT.search(str(exc)):
                    time.sleep(0.6 * 2 ** attempt)
                    continue
                raise JevError(str(exc)) from exc
        return parse(response, questions)
