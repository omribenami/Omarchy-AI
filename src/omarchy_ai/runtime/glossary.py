"""Terms of this project a worker must not guess at.

2026-09-27: the planner read "a Jev-based Home Assistant solution" as "Jev
(likely referring to a JavaScript/TypeScript or specific lightweight
framework)", and every step was judged against that wrong goal.
"""

PROJECT_TERMS = """PROJECT TERMS (this user's system; never guess what a term means):
- Jev: TypeSafe's typed decision model (an evaluation model that answers choice/boolean/score questions fast and
  accurately; it never writes text). Omarchy AI makes its decisions with it (src/omarchy_ai/core/jev.py, through the
  Vercel AI Gateway). "Jev-based"/"Jev-powered" means the decisions are made by Jev.
- HA-Jev (github.com/AboveColin/HA-Jev): the Home Assistant integration for Jev (typed sensors, actions, and a
  conversation agent for Assist). It needs a Jev API key, entered in Home Assistant's integration setup.
- MyApi (myapiai.com): the user's API gateway: connected services, their machines, and a token vault. Vault tokens
  (API keys, e.g. the Home Assistant token) are for programs: `omarchy-ai-vault list` shows their names;
  `$(omarchy-ai-vault get 'NAME')` passes a value to a command. Never print a value.
- Omarchy AI / Omachy: this voice assistant; its user tools live in ~/.config/omarchy-ai/tools.
If the request uses any other term you do not know, find out what it means on this system or from its source
first (search, read the docs it points to); if it is still unclear, ask the user. Never write "likely X"."""
