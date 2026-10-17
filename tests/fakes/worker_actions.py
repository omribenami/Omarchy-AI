"""Fake tool adapters for a worker process (execution/worker_main.py --actions).

    python -m omarchy_ai.execution.worker_main --actions fakes.worker_actions:ACTIONS

`hang` starts a child process in the worker's process group, writes the
worker's and the child's pids to args["pids"], and blocks forever;
`stubborn` does the same but ignores SIGTERM, so only SIGKILL ends it.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from omarchy_ai.execution.actions.base import ActionResult


def _hang(args: dict, ignore_term: bool = False) -> ActionResult:
    if ignore_term:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    Path(args["pids"]).write_text(json.dumps([os.getpid(), child.pid]))
    while True:
        time.sleep(1)


ACTIONS = {
    "echo": lambda args: ActionResult(
        True, json.dumps({"args": args, "pid": os.getpid()})
    ),
    "fail": lambda args: ActionResult(False, "no such thing"),
    "boom": lambda args: 1 / 0,
    "die": lambda args: os._exit(3),
    "hang": _hang,
    "stubborn": lambda args: _hang(args, ignore_term=True),
}
