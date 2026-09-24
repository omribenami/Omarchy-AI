"""JSON-only text model access for internal workers (System agent, planner,
internal reviewer). Jev never generates text; these workers do, and their
output is always parsed and checked by code before anything runs.

Uses the same Vercel AI Gateway key and chat-completions endpoint as the
rest of the assistant (voice/omarchy.py GatewayClient), with a configurable
worker model and a bounded schema-retry (MiniMax's verifier retries once
with "the previous reply had no parseable verdict"; same idea here).
"""
from __future__ import annotations

import json
import logging
import re
import time

log = logging.getLogger(__name__)

_TRANSIENT = re.compile(r"HTTP (429|500|502|503|504|529)\b|timed out|temporarily unavailable|unavailable", re.I)


class WorkerModelError(RuntimeError):
    pass


class WorkerModel:
    """complete(system, user) -> dict. `transport(payload, timeout) -> raw
    chat-completions JSON` is injectable for tests."""

    def __init__(self, model: str | None = None, transport=None, *, max_tokens: int = 2500):
        self._model = model
        self._transport = transport
        self.max_tokens = max_tokens

    def _setup(self):
        if self._transport is None:
            from ..config import load_config
            from ..voice.omarchy import GatewayClient
            config = load_config()
            client = GatewayClient(config)
            self._model = self._model or getattr(config, "task_agent_model", None) or client._text_model()

            def transport(payload, timeout):
                return json.loads(client._request("/chat/completions", json.dumps(payload).encode(),
                                                  "application/json", timeout=timeout))
            self._transport = transport
        return self._transport

    @property
    def model(self) -> str:
        self._setup()
        return self._model or "unknown"

    def complete(self, system: str, user, *, timeout: float = 60, retries: int = 2) -> dict:
        transport = self._setup()
        content = user if isinstance(user, str) else json.dumps(user, ensure_ascii=False)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
        last_error = ""
        for attempt in range(retries + 1):
            payload = {"model": self._model, "temperature": 0, "max_tokens": self.max_tokens,
                       "response_format": {"type": "json_object"}, "messages": messages}
            try:
                raw = transport(payload, timeout)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                if attempt < retries and _TRANSIENT.search(last_error):
                    time.sleep(0.8 * 2 ** attempt)
                    continue
                raise WorkerModelError(last_error) from exc
            value = parse_json_object(_content(raw))
            if value is not None:
                return value
            last_error = "reply was not a JSON object"
            messages = messages + [{"role": "user", "content":
                                    "Your previous reply was not a single valid JSON object. Reply again with only the JSON object."}]
        raise WorkerModelError(last_error)


def _content(raw) -> str:
    choices = (raw or {}).get("choices") or []
    content = (choices[0].get("message") or {}).get("content") if choices else ""
    if isinstance(content, list):
        content = "".join(str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content)
    return str(content or "")


def parse_json_object(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    try:
        value = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(text[start:end + 1])
        except ValueError:
            return None
    return value if isinstance(value, dict) else None
