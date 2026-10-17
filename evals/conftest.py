"""Importing the ``evals`` package isolates the XDG directories (see
``evals/__init__.py``) before any eval imports ``omarchy_ai``."""

import evals  # noqa: F401
