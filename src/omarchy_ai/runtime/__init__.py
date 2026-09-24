"""Task Runtime: the persistent execution plane under the Jev control plane.

    user / voice -> Jev (route, direct, validate, certify)
                 -> TaskRuntime (owns state, permissions, evidence)
                 -> executors (direct tools, System agent, Claude Code, Codex)

See docs/ADR-0002-task-runtime.md for the design and what was adapted from
MiniMax Code.
"""
