"""Point the side-effect journal (core/effects.py) at a throwaway directory.

Under pytest, tests/conftest.py already gives every test its own STATE_DIR.
Under `python -m unittest discover -s tests` nothing does, so a test module
whose code reaches a journaled tool (write_file, a MyApi write, an agenda
step, an ELEVATED task command) sets it up for the whole module:

    from fakes.effects_journal import isolate_journal
    setUpModule, tearDownModule = isolate_journal()

The paths are swapped by hand rather than with mock.patch: several test
modules call mock.patch.stopall() in tearDown, which would undo it.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path


def isolate_journal():
    saved: list = []

    def set_up_module() -> None:
        from omarchy_ai.core import effects

        root = Path(tempfile.mkdtemp(prefix="omarchy-effects-"))
        saved[:] = [effects.journal_path, effects.index_path, root]
        effects.journal_path = lambda: root / effects.JOURNAL_NAME  # type: ignore[assignment]
        effects.index_path = lambda: root / effects.INDEX_NAME  # type: ignore[assignment]

    def tear_down_module() -> None:
        from omarchy_ai.core import effects

        if saved:
            effects.journal_path, effects.index_path, root = saved
            shutil.rmtree(root, ignore_errors=True)
            saved.clear()

    return set_up_module, tear_down_module
