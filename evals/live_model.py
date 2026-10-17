"""Optional live-model backend.

Set ``OMARCHY_EVAL_LIVE=1`` and ``OMARCHY_EVAL_API_KEY``. The endpoint defaults
to the OpenAI chat completions API; override it with ``OMARCHY_EVAL_API_URL``
and ``OMARCHY_EVAL_MODEL``. The model is asked to answer as JSON
``{"say": "...", "tools": [{"name": "...", "args": {}}]}``.
"""

from __future__ import annotations

import json
import os
import urllib.request

from .policy import Call, Decision


def enabled() -> bool:
    return os.environ.get("OMARCHY_EVAL_LIVE") == "1"


def _complete(system: str, user: str) -> str:
    key = os.environ.get("OMARCHY_EVAL_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OMARCHY_EVAL_API_KEY is not set")
    url = os.environ.get(
        "OMARCHY_EVAL_API_URL", "https://api.openai.com/v1/chat/completions"
    )
    model = os.environ.get("OMARCHY_EVAL_MODEL", "gpt-4o-mini")
    body = json.dumps(
        {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": user
                    + '\n\nReply as JSON: {"say": "...", "tools": [{"name": "...", "args": {}}]}',
                },
            ],
        }
    ).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode())
    return str(payload["choices"][0]["message"]["content"])


def _parse(text: str) -> Decision:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return Decision(text, [])
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return Decision(text, [])
    calls = []
    for tool in data.get("tools") or []:
        if isinstance(tool, dict) and tool.get("name"):
            args = tool.get("args") if isinstance(tool.get("args"), dict) else {}
            calls.append(Call(str(tool["name"]), args))
    return Decision(str(data.get("say") or text), calls)


def decide(
    prompt: str, user: str, fixture: dict | None = None, *, declined: bool = False
) -> Decision:
    note = ""
    if fixture and fixture.get("result"):
        note += f"\nTool result: {fixture['result']}"
    if declined:
        note += "\nThe user already declined this send."
    return _parse(_complete(prompt, user + note))
