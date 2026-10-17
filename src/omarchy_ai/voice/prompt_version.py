"""Which system prompt a session assembles.

``legacy`` (the default) is the historical persona in ``Config.instructions``
plus the blocks in ``voice/live.py``. ``v2`` is the structured prompt in
``voice/prompt_v2.py``. ``OMARCHY_PROMPT_VERSION`` overrides the config value.
Anything other than ``v2`` stays on the legacy prompt.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar

_ENV = "OMARCHY_PROMPT_VERSION"
_SURFACE: ContextVar[str | None] = ContextVar("omarchy_prompt_surface", default=None)


def resolve_prompt_version(config) -> str:
    raw = os.environ.get(_ENV)
    if raw is None or not str(raw).strip():
        raw = getattr(config, "prompt_version", None) or "legacy"
    value = str(raw).strip().lower()
    if value in {"v2", "2"}:
        return "v2"
    return "legacy"


def current_surface() -> str | None:
    """Surface set by the caller around prompt assembly. None means voice."""
    return _SURFACE.get()


@contextmanager
def prompt_surface(surface: str):
    """Tell v2 which surface is assembling a prompt, without new call arguments.

    Existing callers and test doubles keep ``build_session_config(config)``.
    """
    token = _SURFACE.set(surface)
    try:
        yield
    finally:
        _SURFACE.reset(token)
