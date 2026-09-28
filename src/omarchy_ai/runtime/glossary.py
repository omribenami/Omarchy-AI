"""Terms a worker must not guess at.

2026-09-27: the planner read a request naming Jev as "Jev (likely referring to
a JavaScript/TypeScript or specific lightweight framework)", and every step was
judged against that wrong goal.

Only terms of Omarchy AI itself live here. The user's own setup (their devices,
services, integrations, vault token names) belongs in their local, untracked
~/.config/omarchy-ai/glossary.md, which is appended when it exists: personal
details never go into this repository (the user's rule, 2026-09-28).
"""
from pathlib import Path

from ..config import CONFIG_DIR

LOCAL_GLOSSARY = Path(CONFIG_DIR) / "glossary.md"

_TERMS = """PROJECT TERMS (never guess what a term means):
- Jev: TypeSafe's typed decision model (an evaluation model that answers choice/boolean/score questions fast and
  accurately; it never writes text). Omarchy AI makes its decisions with it (src/omarchy_ai/core/jev.py, through the
  Vercel AI Gateway). "Jev-based"/"Jev-powered" means the decisions are made by Jev.
- MyApi (myapiai.com): the user's API gateway: connected services, their machines, and a token vault. Vault tokens
  (API keys) are for programs: `omarchy-ai-vault list` shows their names; `$(omarchy-ai-vault get 'NAME')` passes a
  value to a command. Never print a value.
- Omarchy AI / Omachy: this voice assistant; the user's own tools live in ~/.config/omarchy-ai/tools and are
  personal: never copy them, or anything about the user's own setup, into a repository."""

_RULE = """If the request uses any other term you do not know, find out what it means on this system or from its source
first (search, read the docs it points to); if it is still unclear, ask the user. Never write "likely X"."""


def _local_terms() -> str:
    try:
        text = LOCAL_GLOSSARY.read_text().strip()
    except OSError:
        return ""
    return f"\n\nTHE USER'S OWN SETUP (from {LOCAL_GLOSSARY}):\n{text[:4000]}" if text else ""


PROJECT_TERMS = _TERMS + _local_terms() + "\n" + _RULE
