"""Injection eval suite (plan 2.6, DoD-4).

Importing this package isolates the XDG directories (through ``evals``) and
installs the no-live-desktop guard before anything touches ``omarchy_ai``,
so neither pytest nor ``python -m evals.injection`` can reach the user's own
config, state or desktop.
"""

import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import evals  # noqa: F401, E402  (isolates the XDG directories before omarchy_ai loads)

_TESTS = str(Path(__file__).resolve().parents[2] / "tests")
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)
import no_live_desktop  # noqa: E402, F401  (no eval runs a real desktop or device command)

from .fixtures import FIXTURES, Fixture  # noqa: E402
from .harness import Outcome, run_fixture  # noqa: E402

__all__ = ["FIXTURES", "Fixture", "Outcome", "run_fixture"]
