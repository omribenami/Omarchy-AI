"""Under pytest, everything in tests/chaos/ carries the `chaos` marker
(select with `-m chaos`, skip with `-m "not chaos"`)."""

from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "chaos: fault-injection tests that kill and restart a child process"
    )


def pytest_collection_modifyitems(config, items):
    for item in items:
        if _HERE in Path(str(item.fspath)).resolve().parents:
            item.add_marker(pytest.mark.chaos)
