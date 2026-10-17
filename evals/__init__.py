"""Behavior evals for the Omarchy assistant prompts.

Deterministic runs use a prompt-conditioned stand-in model (see ``policy``).
Live-model runs are optional and stay behind ``OMARCHY_EVAL_LIVE=1``.

Importing this package before ``omarchy_ai`` points the XDG directories at a
throwaway folder, so neither ``python -m evals`` nor ``pytest evals`` can read
or write the user's own config and state. Under the full pytest run,
``tests/conftest.py`` has already done the same.
"""

import os
import sys
import tempfile
from pathlib import Path

if "omarchy_ai" not in sys.modules:
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

PROMPT_VERSION = "evals-1"
