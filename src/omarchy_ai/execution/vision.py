"""Screenshot-based clarification: a plain, separate API call to a
vision-capable model, decoupled entirely from the gpt-live-1 realtime
session — deliberately not attempting to feed images through the live
session's own event stream, which is unverified/undocumented for this
brand-new API. This uses the standard, stable Responses API instead.
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("omarchy_ai.execution.vision")

RESPONSES_URL = "https://api.openai.com/v1/responses"
_TIMEOUT = 30


def _capture() -> Path | None:
    # "save" mode: just grim + echo the path, no clipboard copy or
    # notification — this capture is for internal use, not the user-facing
    # screenshot action.
    try:
        proc = subprocess.run(
            ["omarchy-capture-screenshot", "fullscreen", "save"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    path_str = (proc.stdout or "").strip().splitlines()
    if not path_str:
        return None
    path = Path(path_str[0])
    return path if path.exists() else None


def describe_screen(question: str, api_key_path: str, model: str = "gpt-5") -> str:
    """Capture the current screen and ask a vision model about it.
    Returns a plain-text answer, or an error message starting with
    'error:' — never raises, this is called from a tool-result path where
    an exception would just look like a crash to the user.
    """
    path = _capture()
    if path is None:
        return "error: could not capture a screenshot"

    try:
        image_bytes = path.read_bytes()
    except OSError as e:
        return f"error: could not read screenshot: {e}"

    b64 = base64.b64encode(image_bytes).decode()
    prompt = question.strip() or "Briefly describe what's on the screen."

    try:
        with open(api_key_path) as f:
            key = f.read().strip()
    except OSError as e:
        return f"error: could not read API key: {e}"

    body = json.dumps(
        {
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {
                            "type": "input_image",
                            "image_url": f"data:image/png;base64,{b64}",
                        },
                    ],
                }
            ],
        }
    ).encode()
    req = urllib.request.Request(
        RESPONSES_URL,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        log.warning("vision request failed: HTTP %s %s", e.code, e.read().decode()[:300])
        return "error: vision request failed"
    except urllib.error.URLError as e:
        log.warning("vision request failed: %s", e)
        return "error: vision request failed"

    for item in result.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                text = c.get("text")
                if text:
                    return text
    return "error: no description returned"
