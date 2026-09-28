# Changelog

This file is read by the assistant itself. When a new version is published,
the updater fetches this file from the release tag (or, for historical
`dist/` bundles, the commit) it would install (`core/updates.py`), and the
assistant uses the `### Highlights` of every version newer than the
installed one to answer "what's new?".

Rules for every release:

- One `## [x.y.z] - YYYY-MM-DD` section per published version, newest first.
  `scripts/build-install-package.sh` refuses to build a bundle whose version
  has no section here.
- `### Highlights` is required: short, user-facing bullets. They are spoken
  aloud, so write for a listener. Say what changed for the user, not which
  file changed.
- `### Fixes` and `### Under the hood` are optional, for detail the user can
  ask about.
- Work lands under `## [Unreleased]` first. Rename it to the version when
  releasing.

## [0.10.1] - 2026-09-28

### Highlights

- She can see the names of the tokens in your MyApi vault, and tasks and tools
  can use a token without it ever being shown to her or saved anywhere.
- When a task fails, she tells you why and what it still had to do, instead
  of repeating what the worker claimed.
- Tasks know what Jev and MyApi are, and look up any term they don't
  know instead of guessing.

## [0.10.0] - 2026-09-27

### Highlights

- Background tasks are now done by Codex or Claude Code. The paid API worker
  is only used when both are unavailable or out of quota, and only after you
  approve it for that task.
- Ask for a specific agent, like "open Codex with yolo and ask it to ...",
  and that agent does every step. You can also say "move that task to Codex".
- When you tell her to SSH to a machine, she does it. For password logins
  she types the password you gave her, or your saved one, without ever
  putting it in a command.
- Passwords you say or type are kept in your keyring and masked everywhere
  else: conversation history, task files and logs.
- She now passes your answers to a task that asked you a question, instead
  of telling you to do it yourself.

### Fixes

- Codex and Claude Code were unavailable to every task when started from the
  service (a mise wrapper hung), so jobs fell back to paid API models.
- A misheard "No ssh to it" (really "Now ssh to it") no longer blocks the SSH.
- A task done only by a coding agent can be verified and finished.

### Under the hood

- Codex with yolo runs without its sandbox after one high-risk approval.
- A coding agent that hits its usage limit is skipped until its reset time.
- The task list she reads is compact, with what to do for each waiting task.

## [0.9.0] - 2026-09-27

### Highlights

- You can approve a waiting task from your paired phone. The phone page shows
  what the task wants to do and why it needs approval; type your approval PIN
  and tap Approve, or tap Deny. Set the PIN in Assistant Settings under Phone
  Bridge. It is not your login password.

### Under the hood

- The PIN is stored only as a salted hash. Five wrong tries lock phone
  approvals for 15 minutes. An approval only applies to the exact request
  the phone showed.

## [0.8.0] - 2026-09-27

### Highlights

- When a background task needs your approval, the assistant tells you right
  away, even if no conversation is open, and explains what the change is and
  why it needs approval. Ask what it changes and it shows you the actual
  diff.
- A waiting approval also floats as an envelope at the top right of the
  screen. Click it to approve or deny.
- One job, one task: asking again about something already being worked on
  adds to that task instead of starting another, and the assistant tells
  you how it is going. It can also correct a task that goes the wrong way.
- Videos in a GitHub README can now be replaced by the assistant itself: it
  uploads the file through its signed-in browser and puts the new link in
  place of the old one.
- From your paired phone you can unlock the computer, by asking or with the
  new Unlock button. The room microphone can never unlock it.
- While a phone is connected, the computer does not lock or turn off the
  screen, so the phone mirror keeps working with the lid closed.
- Asked to sign in to a website with Google or another provider, the
  assistant clicks the button itself. Passwords and codes are left to you.

### Fixes

- Repeated tool calls that returned the same request for missing details no
  longer loop dozens of times; missing details are filled in from what you
  asked.
- Terminal output is read in full after the command finishes, and idle
  terminals are reused instead of opening a new window for every command.
- Asking to unlock the screen no longer locks it again.
- After you approve a command, the task runs exactly that command instead
  of redoing its work and asking again.
- A finished task is no longer reported as failed because of a wrongly
  worded check.
- Project documents such as the README are no longer mistaken for scripts
  to perform step by step.

### Under the hood

- Stale approvals for cancelled tasks are no longer announced.
- Background workers are told how to prove a README video plays and how to
  word their checks.
- A test fails on any README video that is not a GitHub attachment link.

## [0.7.0] - 2026-09-27

### Highlights

- When something keeps failing, she no longer gives up. She hands the job
  to a background worker that can dig deeper, and tells you it did.
- She can file GitHub issues on any repository, including Omarchy itself,
  using your GitHub login. She reads the title back and asks before posting.
- She can now write her own tools. Nothing is added without your approval,
  and once you approve one she can use it straight away.
- She picks the right tool much faster. Her less common tools are chosen for
  her in a fraction of a second instead of her searching through them all.
- While your screen is mirrored to a TV, she listens through the TV's
  microphone, and her voice plays on the TV.
- Her voice no longer crackles when the computer is busy, for example while
  recording the screen or casting.
- You can chat with her by typing, on the desktop and on your phone, without
  her speaking out loud.
- She tells you about background tasks that finished while you were away,
  and can say which model is doing the work.

### Fixes

- Mirroring to a TV no longer freezes the picture when a microphone is
  plugged into the TV.
- The TV now gets the computer's sound instead of its microphone.
- The on-screen visualizer no longer uses most of a processor core.
- Screen recordings she starts are full screen and include her voice.
- She checks what she heard before acting when speech recognition garbles
  Hebrew into another language.
- She can switch, open and close browser tabs, and explains why a browser
  task failed, for example when a site is not signed in.

### Under the hood

- Background workers use Claude Code, then Codex, then the Gateway API, and
  the worker model is chosen automatically by a qualification exam.
- Installing a tool she wrote, reading a stored login token, and uploading
  data with your credentials now always ask first.

## [0.6.1] - 2026-09-25

### Highlights

- Talking through your phone no longer freezes for a minute or more when
  there is background noise. She answers within about two seconds of you
  finishing, the same as on the computer.
- She now knows which machine each terminal is on. A terminal connected to
  a server over SSH is labelled with that server, and she says which machine
  a result came from instead of mixing up the server and this computer.
- She no longer types exit, logout or quit into a terminal on her own. If
  it would end a session, she asks you first and waits for your answer.

### Fixes

- The phone bridge now sends its audio through the stuck-turn guard, with
  its own threshold for phone audio (it used to go straight to Gemini).

## [0.6.0] - 2026-09-24

### Highlights

- Every request now goes through Jev, the assistant's fast decision model,
  twice. The moment you stop talking it runs simple commands and works out
  what kind of request it is. Then, before any action runs, it checks that
  the action really matches what you asked. A wrong one, like switching to
  workspace 5 when you said 4, is caught and corrected instead of done.
- Jobs are handed to the right helper automatically: a desktop request goes
  to the fast desktop loop, and a job with several steps goes to the task
  runner that checks its own work.
- When OpenAI, Gemini or Vercel runs out of credit, you find out right away.
  Red dollar signs pop up on the screen, a notification links to the
  billing page, and you hear a warning. Before, the assistant just went
  quiet, and the wake word stopped working with no explanation.

### Under the hood

- Jev switchboard (`voice/switchboard.py`, docs/ADR-0003-jev-switchboard.md):
  pass 1 (`jev_fast.judge`) adds a route to the existing fast-path call;
  pass 2 reviews every Gemini tool call (desktop and phone) with a
  code-owned policy: execute, send back, ask, or reroute to `desktop_task` /
  `start_task` with the user's own words. Read-only calls start in parallel
  with the review; identical reviews are cached for 30s; Jev outages fall
  open with a log line. Live calibration: 11/12 cases as intended, median
  311ms per review.
- Quota alerts (`core/quota.py`, plugin `omarchy-ai.quota-alert`, clips in
  `voice/alerts/` made by `scripts/make_quota_clips.py`): detected in the
  Gateway client, OpenAI Live (error events and session creation), OpenAI
  vision, the phone bridge (OpenAI and Gemini) and the daemon's Gemini crash
  handler. `omarchy-ai-settings test-quota-alert <provider>` shows it.

## [0.5.0] - 2026-09-24

### Highlights

- You can always talk to her. Slow actions (looking at the screen, casting,
  email and other connected services, the command search, update checks) run
  in the background. She keeps listening and answering, and tells you the
  result when it is ready.
- The phone bridge connects about five seconds faster.
- She finishes her sentences. Her own voice leaking back through the
  microphone can no longer cut her off; you can still interrupt her by
  speaking up.
- Scripted demos always show the browser on screen, on the right tab.
- Scripted demos run as planned even when details are missing: she fills them
  in from the script file (the command, the site, which document to edit)
  instead of giving up and improvising.
- While you are talking to her, her work is shown on screen. It only moves to
  the background if you are actively using the keyboard or mouse at that
  moment.
- She can take on whole tasks, not just single actions: "why does my
  Bluetooth keep disconnecting", "find what is using port 8080", "fix this
  bug and test it". She works in the background with the right helper
  (her own Linux expert, or Claude Code and Codex for code), checks the
  result herself, and tells you when it is verified done.
- Anything risky (installing, restarting services, deleting, root) waits for
  your OK first. Root-level actions need a click on the notification, not
  just a spoken yes.
- She no longer freezes on "thinking" for up to a minute and a half when
  there is a TV or people talking in the background. Once you stop
  talking, she answers within about two seconds.
- Quick actions are quiet: she no longer says "switching to workspace 4"
  and then "I've switched to workspace 4". She just does it and says
  "done".
- When you ask her to use your password for ssh, scp or sudo in your own
  terminal, she does it instead of asking you to type it.
- Names don't have to be exact. Ask for the "Docker" folder and she finds
  "docker", or asks which one if there are several.
- When she says she'll tell you when something finishes, she really sets up
  a watch on it and wakes up to tell you, and to do the next step you asked
  for.
- She keeps working in the terminal you're using, including over ssh on
  another machine. She checks which machine she is on before running
  anything, and copies real settings instead of making them up.
- For jobs with several steps she says the plan and carries it through,
  and only says it's done once she has seen it work.

### Fixes

- Stuck turns: background speech-like sound kept Gemini's end-of-speech
  detection from ever closing your turn (repeats piled up and were answered
  together, 40-108s later). She now sends a short silence once your words
  stop and no reply has started.
- She read the wrong terminal when two had the same title; she now reads
  the one she typed into (by window address).
- A second Enter right after the first, with nothing typed in between, is
  no longer sent (at an ssh password prompt it sent an empty password).
- Watches on your own terminal windows follow the window even after ssh
  changes its title.
- Terminals opened before a restart or update could no longer be read (their
  logs were deleted at startup while still in use). They now stay readable.
- She can read her own background terminals with the same terminal-reading
  tool, and is pointed to the terminal's text instead of screenshots.

### Under the hood

- New Task Runtime (`src/omarchy_ai/runtime/`, docs/ADR-0002-task-runtime.md):
  Jev routes, directs, validates and certifies; a persistent harness owns
  state, permission levels and evidence; executors are the System agent,
  test and review subagents, direct desktop tools, Claude Code and Codex.
  New `omarchy-ai-task` command and `task_*` settings.

## [0.4.1] - 2026-09-23

### Highlights

- Co-pilot mode: she works alongside you. Installs, commands and long jobs
  run in her own terminal and never type into your windows. When you are not
  using the computer, you see her work on your screen. While you are working,
  she carries on in the background and hands the work over to your screen
  once you stop for about 30 seconds. Say "show me" to watch, or "in the
  background" to keep it out of the way.
- She tells you when a check finishes, even after you said goodbye. When one
  of Jev's watches fires ("let me know when Claude is done"), she wakes up on
  her own and tells you. If you answer, it is done. If you are away, she
  catches you up the next time you talk.

## [0.4.0] - 2026-09-23

### Highlights

- Terminal relay: she now sends whatever you ask into a terminal or a coding
  agent (Claude Code, Codex, aider), including URLs, markdown and multi-line
  prompts. Multi-line text is pasted as one block instead of being submitted
  line by line.
- Scheduled tasks and a heartbeat, decided by Jev: recurring or one-off
  reminders, watches ("tell me when the build in that terminal finishes", "let
  me know when Claude is waiting for me"), scheduled desktop goals and
  background commands. They keep running between conversations. Their results
  arrive as desktop notifications and are summarised in the next conversation.
- Skills that improve over time: she can save a procedure that worked as a
  named skill, update it when it turns out wrong, and Jev picks the matching
  skill when a similar request comes up again.
- Release highlights: when an update is available she tells you, offers to go
  through what's new, and can actually do it from this changelog.
- Browser tasks that finish what they start. She only reports success after
  Jev confirms the page really shows the goal done. Multi-step web tasks go
  step by step, in order. Everything stays in one tab.
- She stays on your workspace. Asking her to type into "the terminal" uses the
  terminal where you are, not one on another workspace, and the browser comes
  to you instead of pulling you to it.
- Faster, more accurate command and window matching. Jev picks the right
  Omarchy command by meaning, in any language including Hebrew, and she can
  find "the Claude terminal" by what is running in it.
- The assistant overlay shows every state: connecting while she starts up,
  listening, thinking whenever she is busy (a MyApi call, a background task,
  or working out her answer), and speaking. Tools that succeed light it
  green, and tools that fail light it red.
- Less crackling in her voice: her speech no longer runs dry between audio
  chunks. On a busy computer some clicks remain; that part comes from echo
  cancellation and is being worked on.
- Instant desktop commands: Jev handles "switch to workspace 4", "move this
  window to workspace 3", volume and play/pause the moment you stop talking,
  without waiting for the conversation model.
- Scripted demos: ask her to perform a file of steps and she narrates each
  step while doing it, keeps to the workspace it names, and stops to ask
  instead of improvising when something is missing.
- Smarter web tasks: she breaks your request into simple steps for the
  browser (searching "eggs", not "a pack of eggs") and asks you when
  something is missing, like which store.

### Fixes

- Removed the "looks like a conversational request" filter that refused to
  type prompts into a terminal running an editor.
- Clicking a Google result now opens it. Before, the click landed on an
  overlay, nothing happened, and the task gave up after repeating the click.
- Browser tasks no longer report "done" on a results page or on the wrong
  article.
- A dropped browser connection heals itself instead of failing every browser
  task until a restart.
- A busy browser is no longer mistaken for a crashed one.
- A button that changes its own label (a cart's "81 added", "82 added")
  can no longer be clicked dozens of times in a row.
- Slow, heavy shopping sites no longer time out the browser connection.

## [0.3.10] - 2026-09-22

### Highlights

- Restarting the assistant from the Settings panel no longer cuts off a
  conversation that is in progress. The panel waits and tells you why.

### Under the hood

- The installer build refuses to package a stale `uv.lock`.
- New `scripts/diagnose-update.sh` for "it still says an update is available
  after updating" reports.

## [0.3.9] - 2026-09-22

### Highlights

- Self-updates to 0.3.8 failed on every machine because of a stale lock file.
  That is fixed.
- When a self-update fails, a GitHub issue can be filed automatically
  (requires a GitHub token on the machine).

## [0.3.8] - 2026-09-21

### Highlights

- She keeps talking and listening while longer desktop and browser tasks run,
  instead of going silent until they finish.

## [0.3.7] - 2026-09-20

### Highlights

- Fixed a crash when typing text into windows.
- She says what she is doing while she works, instead of staying silent.

### Fixes

- The installer no longer checks `uv` through pacman, and it refreshes a stale
  `uv.lock`.
- New `--skip-android` build option.

## [0.3.6] - 2026-09-19

### Highlights

- Browser tasks that search for several items at once work reliably.

## [0.3.5] - 2026-09-18

### Highlights

- The Jev browser agent is bundled with the installer, so browser tasks work
  out of the box.

## [0.3.4] - 2026-09-18

### Highlights

- Saving the Gateway key works on clean installs.

## [0.3.3] - 2026-09-18

### Highlights

- Refreshed Settings panel. It fixes upgrades and exposes the Jev Gateway key.

## [0.3.2] - 2026-09-18

### Highlights

- Jev desktop worker: fast, verified workspace, window, volume, brightness,
  theme and bar-panel actions.
- More stable live audio.

## [0.3.1] - 2026-09-18

### Highlights

- Omarchy AI gateway mode and browser automation updates.

## [0.3.0] - 2026-09-17

### Highlights

- Gemini desktop and phone audio, echo suppression, and a portable installer.
