# Task reliability audit — 2026-10-02

The October expense task exposed failures in shared execution paths. This
change applies to all internal worker tasks, not a Concur-specific recipe.

## Findings and changes

- Worker logs repeatedly show a 90-second Claude timeout followed by a
  90-second Codex timeout before API fallback, on every action. The timeout
  now covers the whole model call, split between candidate backends. Schema
  and transport retries share that deadline. Failed CLI backends sit out for
  five minutes; quota failures retain their one-hour cooldown. Expiry makes
  the preferred backend eligible again.
- Restart recovery relaunched assignments with only short findings, losing
  detailed worker history. Workers now persist bounded results after actions
  and record the proposed action before dispatch. Recovery supplies both to
  the next worker. An in-flight operation is explicitly uncertain and must
  be inspected before repeating an external change. This is not an exactly-once
  transaction guarantee: a process can die after a remote write but before
  recording its result.
- Interrupted assignments remained labeled running in historical steps.
  Recovery now closes those steps as interrupted before starting another.
- Shell repetition had a guard, while browser and API action loops did not.
  Identical non-shell action/result pairs now warn after three repetitions
  and return control to the coordinator after five.
- Planning replaced the actual deliverable with prerequisites and invented
  unattended authentication requirements. Planner instructions now preserve
  the final deliverable, permit legitimate authentication/approval pauses,
  and distinguish a saved procedure from a missing executable capability.
- Follow-up routing treated a timeout as a reason to select a coding worker.
  Jev now receives explicit guidance to select capabilities for the remaining
  work. It still makes the selection; no workflow executor is forced.
- Every progress heartbeat also entered the spoken-result listener, and
  deduplicated results were recreated as generic announcements. That listener
  now accepts only meaningful outcomes and drops duplicates. Progress still
  reaches the conversation indicator and task HUD independently.

## Verification and limits

Regression tests cover fallback deadlines and cooldown recovery, persisted
action history and uncertain in-flight work, and quiet progress delivery.
Existing permission, certification, cancellation, and restart tests remain
applicable. Actual receipt retrieval, authentication, and final report
contents require separate live verification; a worker's completion claim
does not establish that the report exists or is correct.

Future work listed here on 2026-10-02, and where it landed since:

- Provider health persistence across daemon restarts: done.
  `core/provider_health.py` keeps worker-backend, coding-agent, quota,
  voice-model and Gateway-breaker cooldowns in
  `STATE_DIR/provider_health.json` until they expire.
- Per-tool idempotency keys: done. `core/effects.py` journals every external
  or irreversible change with a key from its origin (turn, pending action,
  agenda step, task step), tool and arguments. A retry of a confirmed key
  returns the stored result; a key that was dispatched with no answer is
  `uncertain` and is not sent again. Every tool declares an effect class in
  `execution/tools.py`, and `core/retry.py` retries by it. This is still not
  exactly-once at the provider: an uncertain send is reported to the user
  (`core/pending_actions.py` recovery), not reconciled.
- Cancellation and deadlines through browser and API adapters: done for the
  live paths. `execution/runner.py` runs every live, phone and Task Runtime
  tool call with a per-tool deadline and a `core/cancel.py` token. The
  MyApi and Gmail reads run in a worker process group that is killed at the
  deadline. Other synchronous adapters still cannot be interrupted
  forcibly: they are cancelled through their own canceller where one exists
  (browser, desktop), or abandoned at the deadline with the result dropped
  and any dispatched effect marked `uncertain`.
