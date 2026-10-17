"""Entry point of a chaos child process (started by harness.ChaosHarness.run).

argv: <module:function> <json {"args": [...], "kwargs": {...}}>
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

ISOLATION_EXIT_CODE = 3
RESULT_PREFIX = "CHAOS-RESULT "


def _isolated() -> bool:
    root = Path(os.environ.get("OMARCHY_CHAOS_ROOT") or "/nonexistent").resolve()
    if not root.is_dir() or Path.home().resolve() != (root / "home"):
        return False
    from omarchy_ai import config

    dirs = (config.CONFIG_DIR, config.STATE_DIR, config.CACHE_DIR, config.RUNTIME_DIR)
    return all(Path(d).resolve().is_relative_to(root) for d in dirs)


def _use_fake_myapi(url: str) -> None:
    from fakes.myapi_server import fake_identity
    from omarchy_ai.config import MYAPI_IDENTITY_PATH
    from omarchy_ai.myapi import client

    client.BASE_URL = url
    if not MYAPI_IDENTITY_PATH.exists():
        client._write_json_0600(MYAPI_IDENTITY_PATH, fake_identity())


def main() -> None:
    if not _isolated():
        print(
            "chaos child: state/config dirs are not inside OMARCHY_CHAOS_ROOT",
            file=sys.stderr,
        )
        os._exit(ISOLATION_EXIT_CODE)
    import no_live_desktop  # noqa: F401  (no desktop commands or keyring here either)
    from omarchy_ai.core.crashpoints import CRASH_EXIT_CODE, CrashpointHit

    if url := os.environ.get("OMARCHY_CHAOS_MYAPI_URL"):
        _use_fake_myapi(url)
    target, payload = sys.argv[1], json.loads(sys.argv[2])
    module, _, name = target.partition(":")
    func = getattr(importlib.import_module(module), name)
    try:
        result = func(*payload["args"], **payload["kwargs"])
    except CrashpointHit:
        # raise mode: finally blocks above ran; still report it as a crash.
        sys.stdout.flush()
        os._exit(CRASH_EXIT_CODE)
    print(
        RESULT_PREFIX + json.dumps(result, ensure_ascii=False, default=str), flush=True
    )


if __name__ == "__main__":
    main()
