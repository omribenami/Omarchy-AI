"""Point the XDG directories at a throwaway folder before anything imports omarchy_ai.

Module-level paths (config.STATE_DIR, conversations.ATTACHMENTS_DIR, agenda.JOBS_PATH, ...)
are computed from the XDG variables at import time, so this has to run first:

- under pytest, tests/conftest.py imports this package before anything else;
- under ``python -m unittest discover -s tests``, discovery walks tests/ in sorted
  order and imports every package it meets, and ``_isolated_dirs`` sorts before
  ``chaos`` and every ``test_*.py``.

XDG_RUNTIME_DIR is redirected too, so no test can reach the live daemon's socket.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="omarchy-ai-tests-"))
atexit.register(shutil.rmtree, ROOT, ignore_errors=True)
for _var in (
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
):
    (ROOT / _var).mkdir(mode=0o700)
    os.environ[_var] = str(ROOT / _var)

from omarchy_ai import config  # noqa: E402

for _path in (
    config.CONFIG_DIR,
    config.STATE_DIR,
    config.CACHE_DIR,
    config.RUNTIME_DIR,
):
    assert _path.is_relative_to(ROOT), (
        f"{_path} is outside the test folder: omarchy_ai was imported before "
        "tests/_isolated_dirs isolated the XDG directories"
    )
