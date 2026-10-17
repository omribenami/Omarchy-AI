"""A tool for a worker process that logs, for tests/test_structured_logs.py.

    python -m omarchy_ai.execution.worker_main --actions fakes.log_worker_actions:ACTIONS

`log_env` logs args["say"] and answers with the OMARCHY_LOG_* variables the
worker exported for the call. With OMARCHY_TEST_CANARY set, that value is the
only saved secret the worker's redaction knows (no keyring is read).
"""

from __future__ import annotations

import json
import logging
import os

from omarchy_ai.core import logs
from omarchy_ai.execution import passwords
from omarchy_ai.execution.actions.base import ActionResult

_CANARY = os.environ.get("OMARCHY_TEST_CANARY", "")
passwords.known_secrets = lambda: [_CANARY] if _CANARY else []  # type: ignore[assignment]

log = logging.getLogger("tests.log_worker")


def _log_env(args: dict) -> ActionResult:
    log.info("worker says %s", args.get("say", ""))
    exported = {k: v for k, v in os.environ.items() if k.startswith(logs.ENV_PREFIX)}
    exported.pop(logs.JSONL_ENV, None)
    return ActionResult(True, json.dumps({"env": exported, "pid": os.getpid()}))


ACTIONS = {"log_env": _log_env}
