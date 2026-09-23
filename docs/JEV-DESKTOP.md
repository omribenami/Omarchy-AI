# Jev as an Omarchy desktop worker

Research and implementation: 2026-09-18. The primary conversational provider
remains Gemini Live on this machine. This integration also exposes the worker
through the shared OpenAI live tool set; it does not select the turn-based
Omarchi-ai speech provider.

## Model capabilities and boundaries

TypeSafe documents Jev as an evaluator of text state with typed questions:
Choice selects a supplied option, Score evaluates an ordered rubric, and Noul
estimates a proposition. Questions can share one request but are evaluated
independently. Use narrow questions and compose their results in code. Jev
does not generate free text. The desktop worker supplies no images: visual
interpretation, speech, new text and complex planning stay with the live model
or its existing specialist tools.

Sources: https://docs.typesafe.ai/introduction and
https://docs.typesafe.ai/primitives/choice

A valid type is not proof of a correct decision. TypeSafe's confidence statistic
is distinct from the probability of the selected option. Correction
(2026-09-22): Gateway *does* return it, just not inside the answer object; it
arrives as `providerMetadata.typesafe.confidence.<question>`. The desktop worker
still reads only `answers`, so it still records confidence as absent. That keeps
its behaviour unchanged. Reading it would activate the `confidence >= 0.8` gate
below for the first time. `core/jev.py` (heartbeat and skills) reads it. See
STATUS.md 2026-09-22 for the probe. Mutation and target decisions require
selected probability >=0.95; when confidence is supplied it must also be >=0.8.
These are conservative initial thresholds, not calibrated desktop reliability
claims. Uncertainty returns control to the live model. Handoff itself never
needs a high confidence score because it cannot mutate the desktop.

Source: https://docs.typesafe.ai/confidence

## Browser pattern adapted to native state

The existing browser adapter uses browser-use/jev-ultrafast. Its central pattern
is an observed, indexed action space, batched operation/target questions,
validated execution, and separate text generation when needed. The independently
published jkudish/jev-browser similarly puts budgets, progress and stopping in
code. Their published browser timings do not establish desktop speed or accuracy.

Sources: https://github.com/browser-use/jev-ultrafast and
https://github.com/jkudish/jev-browser

The native worker follows:

1. The live model calls `desktop_task` with the current complete goal and constraints.
2. Parallel native reads gather windows, active workspace, output audio state and
   device, brightness and device, installed themes, and discovered bar IDs.
3. Jev chooses an operation and compatible target/value in one evaluation call.
   All questions remain within Choice's 255-option limit.
4. Code validates distributions and thresholds, checks cancellation/time limits,
   then re-reads action prerequisites. Stale targets or devices cause a handoff.
5. Existing actions execute with code-owned arguments. There is no model-generated
   shell command, Python, selector, coordinate, file path or arbitrary CLI argv.
6. A fresh state read checks the operation's actual postcondition. Jev can judge
   the remaining goal on the next step; DONE also requires verified steps and a
   goal probability >=0.95.
7. Results include status, operation probabilities, optional confidence, decision
   latency, dispatched arguments and independently verified steps. The live model
   reads these before describing success or continuing remaining work.

Supported: numbered workspaces 1–99, focusing discovered windows, output volume
and mute, brightness, installed theme selection, and discovered bar panel IDs.
Bar open/close returns a handoff after dispatch because shell acceptance does not
prove visual rendering. No arbitrary app controls, keyboard input, new text,
file edits, package operations, power actions or window closing are exposed in
this worker. The live model retains its existing tools for unsupported work.

The loop allows eight decisions and a 25-second budget, checked between bounded
operations; an in-flight operation can finish after that budget. Evaluation
requests time out after eight seconds. Repeated identical actions are blocked.
Cancellation is checked again after model waits and freshness reads. An OS action
already executing cannot be recalled; its result still needs inspection.

## Knowledge base

The package includes copies of the user's Downloads/OMARCHY_EXPERT.md and
Downloads/omarchy_capabilities.json. The latter records 456 dispatcher routes,
pinned to Omarchy quattro commit 9c5482c58dbe4974de337450754885083c91eada. It is
reference data, never a registry of automatically executable permissions.

`search_os_knowledge` performs bounded local lexical retrieval. The Jev loop
receives three relevant chunks; the live model can request up to five. The
packaged Arch notes add audio/service references and the key lessons from the
2026-09-17 local action audit. Installed help and fresh state always take priority.
No private audit transcript was copied into the package. This is retrieved
context, not fine-tuning or a complete ArchWiki mirror. ArchWiki access was
blocked during research; official Arch manual pages were accessible.

Sources: https://man.archlinux.org/man/wpctl.1.en and
https://man.archlinux.org/man/systemctl.1.en

## Validation and limits

134 Python tests pass, including typed-protocol wiring, stale-state rejection,
missing/invalid/uncertain probabilities, cancellation, budgets, repeated actions,
postcondition failure, panel handoff, device changes, and retained dispatch traces
when verification fails. A wheel build includes all three knowledge assets.

Real Gateway dry-runs correctly resolved workspace three in English and Hebrew
and the installed Nord theme. Visual and negated requests handed back. Recorded
Jev calls were approximately 0.37–0.98 seconds, not a general latency benchmark.
Some volume/brightness/panel decisions were below the execution threshold and
also handed back. Do not claim full autonomous coverage of routine desktop tasks.

A constrained real brightness probe selected 51%, executed and independently
verified it. The next DONE choice had probability 1 but goal-met probability
0.86, so the worker correctly returned a handoff with the verified step rather
than claiming completion. The original 50% brightness was restored and verified.
This checks a real action path, not end-to-end spoken delegation. Voice-driven
Jev task completion has not yet been tested with the user.

Reproduce a read-only model probe (uses the configured Gateway key and sends
current structured desktop state; performs no desktop mutation):

```sh
.venv/bin/python scripts/probe_desktop_jev.py 'Switch to workspace three'
```

## Phone endpoint repair discovered during this work

The running voice daemon reported listening but HTTPS port 8766 timed out and
its accept queue was full. The old server wrapped its listening socket in TLS,
so a stalled handshake could block the single accept loop before a worker started.
TLS wrapping now occurs in each client worker, with a five-second handshake
limit and a 30-second request read timeout. A real local TLS/socket regression
holds an idle connection open while another HTTPS request succeeds and verifies
that the idle connection expires. Pairing and authentication are unchanged.

After activation, the running HTTPS endpoint also passed the idle-client test:
a second HTTPS request returned 200 and the stalled socket expired. The assistant
control socket reported listening/Gemini/no error. Gemini Live accepted the
updated session/tool schema in a separate connection without microphone capture
or audio transmission. Spoken task routing remains to be validated in use.
