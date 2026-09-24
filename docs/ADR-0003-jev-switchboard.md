# ADR-0003: Jev as the switchboard for every request

Status: accepted, implemented (2026-09-24)
Code: `src/omarchy_ai/voice/switchboard.py`, `src/omarchy_ai/voice/jev_fast.py`,
`GeminiLiveSession._run_call` / `_review` in `src/omarchy_ai/voice/gemini_live.py`.
Tests: `tests/test_switchboard.py`.

## Context

Until now the live model was the switchboard. Gemini heard the request,
picked a tool and ran it; Jev decided only when Gemini chose to call a Jev
tool (`desktop_task`, `browser_task`, `start_task`), plus a fast path for 25
fixed desktop commands. Real sessions showed what that costs:

- "switch to workspace 4" → Gemini switched to 5 (2026-09-23).
- "move the window to workspace 4" → `list_commands` → a mouse-drag binding;
  nothing moved.
- Multi-step jobs done as loose `type_text` chains instead of the Task
  Runtime, with nothing checking them (2026-09-24, STATUS.md).

Decision from the product owner: **Jev is the decision-making switchboard for
every incoming request. Gemini generates the payload; Jev makes the final
routing decision. The first pass is fast, the second is accurate.** Jev
reviews **every** tool call.

## Decision

```
you stop talking
   │
   ├─► PASS 1 (fast, jev_fast.judge, one Jev call, ~0.4s)
   │      command: one of 25 code-owned instant commands → run + verify now
   │      route:   instant | desktop_goal | web | whole_task | terminal |
   │               files | info | conversation      → kept as the hint
   │
   └─► Gemini generates the payload (a tool call)
          │
          ▼
       PASS 2 (accurate, switchboard.review, one Jev call per call, ~0.3s)
          state: the user's latest words, earlier turns, the pass-1 route,
                 the calls already made this turn, the call + what the tool does
          Jev:   route  (execute | reject | ask_user | start_task | desktop_task)
                 matches (does the call do what was asked, right values?)
                 gap    (values | tool | not_asked | ambiguous | none)
          code:  policy below → execute | send back | ask | reroute
```

### Code-owned policy (`switchboard.decide`)

| Verdict | When | What the model gets |
| --- | --- | --- |
| reroute | route is `start_task` p ≥ 0.9, or `desktop_task` p ≥ 0.75 with a confident agreeing pass-1 route (else 0.9); the pass-1 route does not contradict it; the tool is in that reroute's list; no state-changing call has run yet this turn | the other executor's result, prefixed "Jev routed this request to …". Its argument is the user's own words (`{"goal": request}`), never generated text |
| ask | `ask_user` p ≥ 0.85 | "Not run: … ambiguous … ask the user one short question" |
| reject | `matches` < 0.1 (reads: < 0.05), or `reject` p ≥ 0.8 with `matches` < 0.3 (not for reads) | "Not run: Jev checked … and the target or values are wrong …" so Gemini regenerates |
| execute | otherwise | the normal result |

Reroutes allowed per tool: `start_task` from `terminal_task`, `terminal_type`,
`type_text`, `open_terminal`; `desktop_task` from `list_commands`,
`execute_command`. A pass-1 route of `terminal` (typing or relaying dictated
text to a coding agent) never allows a Task Runtime reroute.

### Speed

- Read-only calls (`READ_ONLY`: listings, reads, status) start at the same
  time as the review; the result is used only if Jev lets the call run. Reads
  cost no extra latency.
- Identical `(tool, args, request)` reviews within 30s reuse the decision
  (repeated `read_tile_log` polling costs one Jev call).
- State-changing calls wait for the review: measured median 311ms, max 458ms
  across 24 live Gateway calls.
- A call with no user words (e.g. nothing said yet) runs without a review.
  A call made while announcing a heartbeat result is judged against that
  result's text.

### Failure

If Jev is unreachable (timeout 3s, no retries) the call runs as before and the
log says `Switchboard unavailable, running <tool> unreviewed`. Observed live:
a Gateway HTTP 503 during calibration fell open exactly this way. The
assistant never stops working because the Gateway is down.

## Evidence (live Jev through Vercel AI Gateway, 2026-09-24)

Twelve cases from real sessions, pass 1 then pass 2:

| request | Gemini's call | verdict |
| --- | --- | --- |
| switch to workspace 4 | `workspace_switch 5` | reject (matches 0.06, gap values) |
| switch to workspace 4 | `workspace_switch 4` | execute (0.97) |
| mute the sound | `volume_up` | reject (matches 0.01) |
| move this window to workspace 4 | `list_commands "move window"` | reroute → `desktop_task` (0.83, pass 1 instant 1.0) |
| tell Claude: fix the login bug and add a test | `type_text` of that text | execute (pass 1 terminal 0.99) |
| run ls in the terminal | `press_key Return` after focus + type | execute (0.99) |
| go to the Docker folder | `type_text "cd docker"` | execute |
| open the browser and search for cheap flights to Rome | `browser_task` | execute |
| turn the volume up | `list_windows` | execute (a read) |
| what did the build print? | `read_tile_log` | execute (Gateway 503: fell open) |
| open a terminal | `open_terminal` | execute |
| find what is using port 8080 and stop it | `terminal_task "ss -ltnp"` | execute (Jev judged it a fine first step; a reroute to `start_task` was hoped for) |

11/12 as intended; the miss is the conservative direction (the old behaviour).

Calibration changes from the first run: the `desktop_task` reroute bar with
an agreeing pass 1 (0.81 was below 0.9), and the pass-1 route texts ("tell
Claude: …" had been classed `whole_task` 0.95; now `terminal` 0.99).

## Consequences

- Every Gemini tool call on desktop and phone (both use `_run_call`) is
  reviewed and logged: `Switchboard: call=… name=… decision=… evidence=… (…ms)`.
- About 0.3s is added to each state-changing call; a focus → type → Enter chain
  pays it three times.
- Not covered: the OpenAI Live provider (`voice/live.py`) and the Omarchi-ai
  provider keep their own dispatch; mission sub-steps are not reviewed one by
  one (the `run_mission` call is).
- Thresholds are code constants in `switchboard.py`; retune them from the
  `Switchboard:` log lines, not by feel.
