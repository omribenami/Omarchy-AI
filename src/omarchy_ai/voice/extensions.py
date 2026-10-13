"""The user's own voice extensions.

Python files in ~/.config/omarchy-ai/extensions/ are imported once, when the
first conversation needs them. A file may define either of:

    live_connect(config, model, live_config)
        An `async with` session to hold this conversation with instead of
        `model` (the session interface of voice/gateway_live.py), or None to
        leave the choice alone. If that session cannot be entered, the
        conversation is held with `model` after all.

    heard(session, text)
        Called with every piece of the user's transcribed speech and the
        GeminiLiveSession it belongs to; may be async. True means the
        extension took these words itself. Raising fallback.Reconnect makes
        the conversation connect again, which asks live_connect() anew.
        Not every conversation can: `session.restart_stalled` is True where
        it reconnects (the desktop, the phone) and False where it does not
        (the satellite speaker); there a Reconnect ends the conversation,
        and the next one asks live_connect().

    typed(session, text)
        Called with every message the user types to her (the phone's chat,
        the desktop chat) before it goes to the model, with the
        GeminiLiveSession of that chat; may be async. True means the
        extension took the message: it is not sent to the model and not
        kept in the transcript. What the extension answers it shows itself,
        as a model's message through session.on_message. fallback.Reconnect
        works as in heard(); a chat that cannot reconnect ends its session,
        and the next message opens a new one, which asks live_connect().

They are local to this machine, like user tools (~/.config/omarchy-ai/tools),
and never part of this repository. A file that does not import, and a hook
that raises anything but fallback.Reconnect, is logged and skipped: an
extension must not end a conversation by accident, and its own failures are
never taken for the voice model's.
"""
from __future__ import annotations

import importlib.util
import inspect
import logging
import os
import threading
from contextlib import AsyncExitStack, asynccontextmanager

from ..config import CONFIG_DIR
from . import fallback

log = logging.getLogger("omarchy_ai.voice.extensions")

EXTENSIONS_DIR = CONFIG_DIR / "extensions"

_lock = threading.Lock()
_modules: list | None = None


class SessionFailed(RuntimeError):
    """An extension's own session failed: the conversation ends, and no voice
    model is marked as failing for it."""


def modules() -> list:
    global _modules
    with _lock:
        if _modules is None:
            found = []
            for path in sorted(EXTENSIONS_DIR.glob("*.py")) if EXTENSIONS_DIR.is_dir() else []:
                if path.name.startswith("_"):
                    continue
                try:
                    status = path.stat()
                    if status.st_uid != os.getuid() or status.st_mode & 0o022:
                        # Code that runs as the user: only the user's own, and writable by nobody else.
                        log.warning("Voice extension %s skipped: it must be owned by you and writable only by "
                                    "you (chmod go-w)", path.name)
                        continue
                    spec = importlib.util.spec_from_file_location("omarchy_ai_extension_" + path.stem, path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                except Exception:  # noqa: BLE001
                    log.exception("Voice extension %s could not be loaded", path.name)
                    continue
                found.append(module)
                log.info("Voice extension loaded: %s", path.name)
            _modules = found
        return _modules


def live_connect(config, model: str, live_config: dict):
    for module in modules():
        hook = getattr(module, "live_connect", None)
        try:
            session = hook(config, model, live_config) if hook else None
        except Exception:  # noqa: BLE001
            log.exception("Voice extension %s failed in live_connect", module.__name__)
            continue
        if session is not None:
            return session
    return None


@asynccontextmanager
async def _guarded(override, regular):
    async with AsyncExitStack() as stack:
        try:
            session = await stack.enter_async_context(override)
        except Exception:  # noqa: BLE001
            log.exception("A voice extension's session could not start; using the regular connection")
            override = None
            session = await stack.enter_async_context(regular())
        try:
            yield session
        except fallback.Reconnect:
            raise
        except Exception as error:  # noqa: BLE001
            if override is None:
                raise
            log.exception("A voice extension's session failed")
            raise SessionFailed("A voice extension's session failed") from error


def live_session(config, model: str, live_config: dict, regular):
    """`async with` an extension's session if one takes this conversation,
    else (or if that one cannot be entered) the one `regular()` gives."""
    override = live_connect(config, model, live_config)
    return regular() if override is None else _guarded(override, regular)


async def heard(session, text: str) -> bool:
    taken = False
    for module in modules():
        hook = getattr(module, "heard", None)
        try:
            result = hook(session, text) if hook else None
            if inspect.isawaitable(result):
                result = await result
        except fallback.Reconnect:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Voice extension %s failed in heard", module.__name__)
            continue
        taken = taken or result is True
    return taken


async def typed(session, text: str) -> bool:
    try:
        for module in modules():
            hook = getattr(module, "typed", None)
            try:
                result = hook(session, text) if hook else None
                if inspect.isawaitable(result):
                    result = await result
            except fallback.Reconnect:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Voice extension %s failed in typed", module.__name__)
                continue
            if result is True:
                return True
    except fallback.Reconnect:
        if session.restart_stalled:
            raise
        session._hangup.set()  # this chat cannot reconnect: its next message opens a new session
        return True
    return False
