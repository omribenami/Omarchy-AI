"""Behavior evals for the Omarchy assistant prompts.

Deterministic runs use a prompt-conditioned stand-in model (see ``policy``).
Live-model runs are optional and stay behind ``OMARCHY_EVAL_LIVE=1``.
"""

PROMPT_VERSION = "evals-1"
