"""Keep eval collection from reading the user's Omarchy config or state.

``tests/conftest.py`` does this for the unit suite. If that already imported
``omarchy_ai``, the environment it set is left alone.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

if "omarchy_ai" not in sys.modules and not os.environ.get("XDG_STATE_HOME"):
    _root = Path(tempfile.mkdtemp(prefix="omarchy-ai-evals-"))
    for _var in (
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
    ):
        (_root / _var).mkdir(mode=0o700)
        os.environ[_var] = str(_root / _var)
