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

## [Unreleased]

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
