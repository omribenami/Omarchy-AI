"""A tool worker process: runs the calls execution/runner.py sends it.

    python -m omarchy_ai.execution.worker_main [--actions module:attr]

One JSON request per line on stdin, {"name", "args", "origin",
"conversation"}; one reply per line, {"ok", "message"}, on the original
stdout. Anything a tool prints goes to stderr (the daemon's log) instead, so
it cannot corrupt a reply. The runner starts it in its own process group and
kills that group at a deadline or cancel; it holds no state the daemon needs.

The caller has already checked who may use the tool (MyApi's owner check in
actions.run_action). `--actions` names another {name: function} table, for
tests.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
from typing import Any, Callable

log = logging.getLogger("omarchy_ai.execution.worker")


def _actions(spec: str | None) -> dict[str, Callable[[dict], Any]]:
    if not spec:
        from .actions import ACTIONS

        return ACTIONS
    module, _, attr = spec.partition(":")
    table = getattr(importlib.import_module(module), attr)
    return dict(table)


def _call(table: dict[str, Callable[[dict], Any]], request: dict) -> dict:
    from ..core import conversations, effects

    name = str(request.get("name") or "")
    args = request.get("args")
    fn = table.get(name)
    if fn is None or not isinstance(args, dict):
        return {"ok": False, "message": f"unknown action '{name}'"}
    if request.get("conversation"):
        conversations.CURRENT.set(request["conversation"])
    try:
        with effects.origin(str(request.get("origin") or "")):
            result = fn(args)
    except Exception:  # noqa: BLE001 -- same wording as actions.run_action
        log.exception("action %s raised", name)
        return {"ok": False, "message": f"{name} crashed"}
    return {"ok": bool(result.ok), "message": str(result.message)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omarchy-ai-tool-worker")
    parser.add_argument("--actions")
    options = parser.parse_args(argv)
    replies = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1, encoding="utf-8")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    logging.basicConfig(
        level=os.environ.get("OMARCHY_WORKER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s[worker %(process)d] %(levelname)s %(message)s",
    )
    from .passwords import RedactingFilter

    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactingFilter())
    table = _actions(options.actions)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except ValueError:
            reply = {"ok": False, "message": "unreadable request"}
        else:
            reply = (
                _call(table, request)
                if isinstance(request, dict)
                else {"ok": False, "message": "unreadable request"}
            )
        replies.write(json.dumps(reply, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
