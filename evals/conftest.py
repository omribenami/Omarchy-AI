"""Importing the ``evals`` package isolates the XDG directories (see
``evals/__init__.py``) before any eval imports ``omarchy_ai``. Then the
tests' live-desktop guard (tests/no_live_desktop.py) is installed, so no
eval can run a real desktop or device command, also when ``pytest evals``
runs on its own."""

import sys
from pathlib import Path

import evals  # noqa: F401

_TESTS = str(Path(__file__).resolve().parent.parent / "tests")
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

import no_live_desktop  # noqa: E402, F401
