# ADR-0002: Task Runtime — Jev as control plane, a harness as execution plane

Status: accepted, first slice implemented (2026-09-23)
Code: `src/omarchy_ai/runtime/`, `src/omarchy_ai/cli/task.py`, tests in
`tests/test_task_runtime.py`.

## Context

Until now Omarchy AI was a voice assistant with tools. The live model
(Gemini Live / gpt-live) chained tool calls itself, Jev made narrow typed
decisions (desktop loop, browser loop, heartbeat watches, skills), and the
shell was reachable only through `terminal_task` (a tmux terminal read back as
screen text), with no permission layer (ADR-0001, "Risks worth naming now").
Multi-step work lived entirely in one model's context, with nothing that
could pause for approval, survive a restart, verify its own results, or hand
code work to Claude Code/Codex and check it.

The goal: one assistant to the user; inside, a persistent, multi-agent
operating-system agent. **Jev is the control plane** (who works next, what
happens next, whether the evidence is enough). **The harness is the
execution plane** (state, permissions, execution, evidence). Workers
(internal agents, system tools, Claude Code, Codex) do the heavy work.

## Decision

```
voice / CLI ─► start_task ─► TaskRuntime (persistent task record)
                  │  plan: worker model writes objective + acceptance criteria
                  ▼
        Jev ROUTE ─► executor ─► WorkContext (permission-checked capabilities)
                  │               run_command · launch · desktop · screen · action · run_external
                  ▼
        harness observes: every command + exit code, workspace git diff, tests it runs itself
                  ▼
        Jev DIRECT ─► CONTINUE | RETRY | CHANGE_EXECUTOR | SPAWN_SUBAGENT | REQUEST_REVIEW
                      RUN_TESTS | REQUEST_MORE_TESTS | ROLLBACK | ASK_USER | FAIL | CERTIFY
                  ▼
        code evidence gates ─► Jev CERTIFY ─► certified / unverified / failed
```

### Jev's four jobs, each one typed Jev call over a bounded brief

| Job | Question(s) | Code owns |
| --- | --- | --- |
| ROUTE | choice over *available* executors (+ASK_USER); boolean P(needs a source change) | which executors are available at all |
| DIRECT | choice over the directives *possible right now*; choice of next executor | the allowed set (e.g. CERTIFY only when the evidence gates pass; RUN_TESTS only with a validated plan not yet run; RETRY limit; ROLLBACK only with a checkpoint) |
| VALIDATE | boolean: "would at least one of these commands fail if the behavior were still broken?" | rejecting exit-code masking (`; echo $?`, `\|\| true`) before asking |
| CERTIFY | boolean: every criterion met, judged from harness evidence (claims say *what* is claimed, not whether it is true); choice: the main gap | the gates: last step done, no pending approval, fresh harness evidence after the last change, and for changed files validated tests run after the change (or a passing independent review after 2 failed plan attempts), no failed test/review; a rejected certification needs *new* evidence before it can be asked again |

Jev never sees a raw transcript: `control.brief()` is ≤14 000 chars (MiniMax's
verifier limit) of goal, criteria, step outcomes with executor claims marked
`claim_UNTRUSTED`, harness evidence (head and tail of long output), tests,
reviews, notes and budget. If Jev is unreachable the runtime falls back to
RETRY-once or FAIL and **never certifies**.

### Executors (`runtime/executors/`)

One abstraction (`Executor.available()`, `Executor.run(Assignment, WorkContext) -> Report`),
one registry (`default_executors()`). Vendor knowledge stays in one small
subclass each.

| Executor | Kind | What it is |
| --- | --- | --- |
| DIRECT_TOOL | direct | the existing Jev desktop loop, then one existing assistant tool picked by the worker model and schema-checked |
| SYSTEM_AGENT | internal | Linux/Omarchy operator loop: `run` (any installed CLI, ≤4 per step), `find_tools` (PATH + apropos), `help` (local --help/man/tldr for the *installed* version), `launch` (detached GUI), `desktop` (Jev desktop loop), `screen` (vision), `finish` |
| TEST_AGENT | internal | the System agent with a verification role: proposes assertion-style test commands; the *runtime* runs them |
| REVIEW_AGENT | internal | read-only diff review by the worker model when no external reviewer is available |
| CLAUDE_CODE | external | `claude -p --output-format json`, prompt on stdin; write mode = `acceptEdits` plus an allowlist of test/git commands; review mode denies Edit/Write |
| CODEX | external | `codex exec -s workspace-write\|read-only -o last-message -` |

Discovery (`ExternalCodingAgent.detect`) checks installed → runs (`--version`)
→ logged in (`claude auth status`, `codex login status`), cached 10 min.
Neither is required; if both are missing the internal agents still work.
Reviews go to an agent that did not write the change.

### Task Runtime (`runtime/runtime.py`, `runtime/task.py`)

One JSON file per task in `~/.local/state/omarchy-ai/tasks/`, saved atomically
after every change: goal, objective, criteria, steps (executor, role,
directive, outcome, claim, findings), every command (risk, decision, exit
code, output tail, full-output path), evidence (source `harness`, `executor`,
`reviewer` or `vision`), Jev decisions with probabilities, test plan and
results, reviews, git checkpoints, grants/declines, pending approval,
question, answers, certification, budget. Agents receive an `Assignment` with
only their instructions plus a bounded context (criteria, last 3 steps, recent
harness evidence, files, notes); no model holds the whole task.

Waits are real pauses. On `needs_approval` / `needs_user` the thread ends and
the task is saved with the step to re-dispatch; `respond()` resumes it (the
agent gets its pre-pause progress and the approval or answer). A task left
`running` by a dead process is marked `interrupted` and can be resumed.

Git checkpoints: at task start and before every coding-agent write,
`git stash create` (kept reachable under `refs/omarchy-ai/tasks/…`) plus the
untracked-file list. The workspace diff against the baseline is observed after
every step and is the harness's own record of what changed. ROLLBACK restores
tracked files from the checkpoint and removes files the task created.

### Permissions (`runtime/permissions.py`): enforced by the harness, not a prompt

`classify(command)` → LOW / NORMAL / ELEVATED / HIGH / BLOCKED;
`decide()` → allow / ask / deny. Auto-approval (`task_auto_approve`, default
NORMAL) is capped at ELEVATED: HIGH always asks, BLOCKED never runs, a user
grant covers exactly one command, and a user decline is remembered and
auto-denied for the rest of the task. Coding-agent invocations are checked too
(`coding_agent_risk`: read-only LOW; writing in a git repo under home/tmp
NORMAL; no git ELEVATED; config dirs ELEVATED; elsewhere HIGH). Rollback is
ELEVATED. Subprocesses get an environment with credential variables removed.

Approval channels: the desktop notification's Approve/Deny buttons (a real
human click the model cannot fake), `omarchy-ai-task approve|deny`, or voice
(`task_respond`) for ELEVATED only: **HIGH cannot be approved by voice**,
because a voice approval is relayed by the live model.

Examples (all in the tests): `wpctl status`, `journalctl --user -u X -n 50`,
`ss -ltnp | grep 8080`, `pacman -Qi` → LOW; `ffmpeg … out.mp4`, `pytest` →
NORMAL; `yay -S`, `systemctl --user restart myapp`, `git push`, `rm -rf build`,
`sed -i` on `~/.config` → ELEVATED; `systemctl --user restart pipewire` (a
live voice session depends on it), `sudo …`, `pacman -Rns`, `git push --force`,
reading `~/.ssh/id_*`, `curl … | bash`, writing `/etc` → HIGH; `rm -rf ~`,
`mkfs`, `/dev/tcp` reverse shells, `base64 -d | sh`, `echo $(rm -rf ~)` →
BLOCKED.

### Surfaces

- Voice/live model: `start_task`, `task_status`, `task_respond` in
  `execution/tools.py`; `start_task` returns at once (the work runs in a
  runtime thread), so nothing under `voice/` changed.
- The daemon forwards task events to an open conversation through
  `session.announce` (from runtime threads via `loop.call_soon_threadsafe`);
  every event also raises a desktop notification.
- CLI `omarchy-ai-task run|list|show|approve|deny|answer|resume|cancel|executors|classify`.
- Config: `task_runtime_enabled`, `task_auto_approve`, `task_agent_model`
  (default `anthropic/claude-sonnet-5` via the Gateway), `task_max_steps`,
  `task_max_minutes`, `task_coding_agent_timeout`.

## MiniMax Code: what was reused, adapted, or done differently

Studied at `github.com/MiniMax-AI/minimax-code` @ a914a30 (MIT first-party;
`third_party/sandbox-runtime` Apache-2.0, not used). It is a TypeScript
monorepo, so reuse means porting patterns and data, not importing code.

| Area | MiniMax | Here |
| --- | --- | --- |
| Command risk | `agent-modules/permission`: HARD_BLOCKED / SOFT_RISK registries, quote-aware split, wrapper unwrap, redirect write targets, SIGKILL demotion, then a cloud LLM classifier for soft risks | **Adapted**: registries (Linux-relevant entries), split/unwrap/redirect/SIGKILL ported to Python. **Different**: graded 5-level risk instead of allow/ask plus an LLM classifier; soft risks go to the user, no LLM decides permissions |
| Env scrubbing | `agent-core/bash-subprocess-env.ts` SENSITIVE_ENV_NAME_RE | **Reused** (same keyword set) |
| Shell execution | pi-mono `bash-executor`: tail truncation + full-output file, ANSI strip, kill on abort | **Adapted** (`runtime/shell.py`), plus process-group kill on timeout |
| Goal/verification | `agent-modules/goal`: host-owned status; worker may only *propose* completion; bounded evidence brief (14k); independent verifier; one retry on unparseable verdict; `VERDICT:` grammar | **Adapted**: `Task` status is runtime-owned, claims are untrusted, `brief()` fitting, the review `VERDICT:` grammar, worker-model JSON retry. **Different**: the verifier is Jev (typed booleans), gated by code-owned evidence checks, and tests are run by the harness itself |
| Runaway guard | `agent-modules/runaway-guard`: exact-action repeat, same error family, ABAB, polling | **Adapted** (subset): exact repeat (warn at 3, stop at 5) and same error family inside the System agent; step/op/time budgets in the runtime |
| Background tasks / sessions | `background-task` manager, SQLite session store, resume | **Different**: one JSON file per task, thread per task, `interrupted` on restart; simpler and enough at this scale |
| Subagents | read-only verifier subagent profile | **Adapted**: TEST_AGENT, REVIEW_AGENT, read-only external reviews |
| Planning/execution split | plan mode | **Adapted**: a planner writes objective + acceptance criteria only; workers execute |
| MCP, skills/plugins, TUI, context compaction | full implementations | **Not taken now**: Omarchy already has skills (`core/skills.py`); MCP and compaction are future work (see Roadmap) |

## Evidence: live runs on this machine (2026-09-23)

Real Jev, real Gateway worker model, real Codex/Claude Code. Every fix below
came from reading the task record, not from guessing.

1. "Which process is listening on TCP 8080?" → SYSTEM_AGENT ran `ss` (LOW).
   **Unverified**: after a rejected certification Jev picked CERTIFY three
   more times on unchanged evidence, and the planner invented a
   "save the findings to a file" criterion. Fix: CERTIFY needs new
   evidence after a rejection; the planner may not add deliverables.
2. Same → TEST_AGENT proposed `sudo -n lsof`; the harness stopped it as
   HIGH and asked. Deny exposed three bugs: the paused step stayed
   `waiting_approval`; the declined command could be asked for again; and
   `grep` with no match (exit 1, no output) counted as a failure ("unfixed
   failure" gap). All fixed and tested.
3. Same → test plans all ended `; echo EXIT:$?`, which always exits 0
   (Jev correctly refused them). Fix: TEST role requires assertion-style
   checks; the harness rejects exit-code masking itself.
4. Same → certification p 0.64 → 0.70 → 0.79 (threshold 0.8, same bar as
   `browser_jev`'s goal check). The brief clipped the `ss` listing mid-line.
   Fix: evidence keeps head and tail. Next run: **certified, p=0.87, 1 step.**
5. "Fix add() in calc.py; tests must pass" (scratch repo) → routed to CODEX
   (p=0.97), which made the correct 1-line fix; CLAUDE_CODE reviewed it
   independently, read-only (PASS, $0.17). But VALIDATE refused every sound
   test plan (≈0.25) and CONTINUE→TEST_AGENT looped 9× to the step budget.
   Probing the live Jev on the saved task: the compound question
   ("claim true AND every criterion AND real behavior") scored 0.26; "is it
   meaningful, not trivial" passed a bare `ss -ltnp` (0.94); the
   counterfactual "would at least one of these fail if the behavior were
   still broken?" gave good plans 0.95/0.80 and compile/cat/import-only/
   listing plans 0.32–0.39 (it misses `; echo $?`, now caught by code).
   Fix: that question; test/review subagents reachable only through their
   own directives. Next run: **certified in 2 steps**: CODEX fix, then a
   validated plan (p=0.95), 3/3 harness-run checks passed, certify p=0.80.
   `__pycache__` files then showed up as "changes"; build artifacts are now
   ignored.

## Consequences / risks

- Worker quality depends on `task_agent_model`; the loop, permissions and
  certification do not.
- Jev calibration is question-sensitive (run 5). New Jev questions should be
  probed against real saved tasks before use; the evidence above shows how.
- Claude Code's headless tool allowlist uses the `Bash(cmd *)` form; if a
  future CLI changes that syntax, write-mode Claude falls back to editing
  without running tests (the harness still runs them).
- Tasks started by voice run in the daemon; approving one from the CLI
  resumes it in the CLI process (the state is in the file, so either works).

## Roadmap (not in this slice)

1. Live voice acceptance: exercise `start_task` / announcements /
   notification approvals in a real voice session (needs a daemon restart;
   deliberately not done while the voice service is live).
2. Stream task progress into the Watch Dogs overlay / bar.
3. Resume an interrupted coding-agent session (`claude --resume`,
   `codex exec resume`) instead of re-dispatching from scratch.
4. MCP tools as another executor surface (MiniMax `agent-modules/mcp`).
5. Scheduled tasks (`core/agenda.py`) able to start runtime tasks.
6. Per-task sandboxing for the System agent (MiniMax's sandbox-runtime is
   Apache-2.0 and bubblewrap-based; worth evaluating for NORMAL commands).
