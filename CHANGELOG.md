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

## [0.13.1] - 2026-10-03

### Highlights

- Pairing a phone keeps the pairing link off the command line, so another
  person on this computer cannot read it while the QR code is showing.
- Tasks show their progress in the chat and on the bar, including after a
  restart, and a cancelled or interrupted task is reported instead of going
  quiet.
- A repeated timeout question stops, and a browser step counts only when the
  page actually changed.

### Fixes

- The phone pairing QR is drawn from the link on standard input. If that
  step fails, the message does not include the link.

## [0.13.0] - 2026-09-30

### Highlights

- Routines and timed requests now really happen. Ask me to turn something on
  at 6:30, or to set the volume every morning, and I do exactly that at that
  time, even with no conversation open, then tell you it's done or what went
  wrong.
- If your computer was asleep when something was due, I tell you I missed it
  instead of doing it hours late.
- Changing the time of something I scheduled now moves it in one step, and I
  only say a time has passed when it really has.
- Every chat, typed or spoken, is saved as a conversation your phone can
  reopen, and a task's approvals and results come back into the chat it
  started in.
- MyApi requests are faster and safer: I read right away, and anything that
  sends, changes or deletes waits for your yes in that chat.

### Fixes

- An approval typed in one chat no longer answers another chat's request.
- Approval requests nobody answers expire after a few hours, and saying
  "approve" to a high-risk step sends the fingerprint prompt to your phone.
- Gmail searches are several times faster, and a phone chat that reconnects
  remembers what you were talking about.
- A smart-home command that answers with an error now counts as failed.

### Under the hood

- A new scheduled-task kind, 'action', stores the exact tool calls when you
  ask and runs them through the same tools a conversation uses, at most once,
  with retries for anything but toggles.
- The heartbeat sleeps until the next job is due instead of ticking every
  minute.

## [0.12.2] - 2026-09-30

### Highlights

- Your phone gets far fewer approval requests. I only ask for things that
  really matter, like pushing code, running as root or touching your keys,
  and each one reaches your phone once.
- Tapping one of my notifications in Flux now stays in the Flux app.
- I hand fewer jobs off to a background task by mistake. A locked screen,
  a slow Gmail answer or a hiccup reading the screen no longer counts as
  me failing.

### Fixes

- Focusing a window on a locked screen now says the screen is locked, and
  assistant terminals are found by their own name after the shell renames them.
- Gmail reads through MyApi wait up to 30 seconds instead of 15.
- Reading GitHub with gh api, or reading a public SSH key, no longer asks for
  approval, and coding agents working in your home folder no longer ask.

## [0.12.1] - 2026-09-29

### Highlights

- You can install me from OmaStore. The first time you open me there, I finish setup on this machine and start listening.

## [0.12.0] - 2026-09-29

### Highlights

- If you use Flux on your phone, my notifications now reach your phone too:
  tasks that need you, finished or failed tasks, routines, and credit alerts.
- With Flux's fingerprint approval set up, you can approve my tasks, and the
  admin commands they run, with your fingerprint on the phone instead of a
  password.
- Gmail now sends and replies directly when you ask me to send, and never
  sends when you only asked me to write or draft.
- Your own tools now tell me what they are for, so I use them correctly the
  first time.
- Typing to me on the phone works again. Text-only mode could fail to start.

### Fixes

- The phone page's text-only mode crashed on load, so messages could not be
  sent.
- On a short phone screen, the ring and status no longer cover the text field
  while you type, and a new Omarchy button switches straight back to voice.
- A Gmail request that picked a draft method, or guessed a REST path, now uses
  the dedicated send and reply tools, or MyApi's documented execute call.
- The Assistant Settings panel works when installed from the Omarchy
  marketplace, and explains how to install the assistant when it is missing.
- The settings panel caps what it reads from the clipboard and from its
  helper, and fails safely if either runs too long.

### Under the hood

- Flux integration through its public interfaces only: `notify.send` for
  notifications (`flux_notifications`, on by default), fluxd's
  `approve.request` with the root-owned enrolled key for approvals, and
  `settings.set` for an allowlist of Flux features. See docs/FLUX.md.
- New phone-bridge endpoints for the Flux app: `/api/hello`, automatic
  pairing signed with the phone's Flux identity key, wake-word models,
  TV casting, `/api/ask`, and Flux settings. The TLS key now persists across
  restarts, so a pinned phone keeps trusting the server.
- With Flux approval on for sudo, tasks send no saved password, and the sudo
  timeout is 130 seconds so the phone can answer.
- The catalog picker includes each user-added tool's description.

## [0.11.3] - 2026-09-28

### Highlights

- Connected MyApi services can now create, send, update, and delete—not just
  read—after you confirm the exact change.
- MyApi answers arrive faster: ordinary results no longer wait for a second
  writing model and a separate fact-check before she can answer you.
- Repeated service and method lookups are reused during the conversation.

### Under the hood

- External MyApi writes support POST, PUT, PATCH, and DELETE within the
  enrolled device's scope. Jev verifies the confirmation and writes fail
  closed if that check is unavailable.
- Large structured results use one bounded Jev ranking pass; small results
  go directly to the live model.
- Service discovery metadata is cached for 15 minutes per enrolled identity.

## [0.11.2] - 2026-09-28

### Highlights

- When your email service is slow to answer, she tells you and you can ask
  again, instead of the search just failing.
- When her helper service is down, she no longer waits on it before reading
  you your email results.

### Under the hood

- MyApi read timeouts and dropped connections are reported as normal MyApi
  errors instead of crashing the action.
- Condensing MyApi results skips the Gateway while its circuit is open,
  counts its own Gateway failures toward it, and logs why it kept a raw
  result.

## [0.11.1] - 2026-09-28

### Highlights

- She finds the email you ask about even when you don't remember the exact
  name, spelling or subject, and answers with what's in it instead of
  reading out raw data.
- A very long email or search result no longer cuts off the conversation.
- She never types while your screen is locked, so nothing she types can end
  up in the lock screen's password field and lock your account.

### Under the hood

- New read-only `myapi_gmail_search` tool. When an exact Gmail search finds
  nothing, it keeps dates, labels, attachment and mailbox filters and
  loosens only the names and words, trying a few variants.
- MyApi results are condensed into a direct answer, used only when Jev
  confirms it answers the question and is backed by the result.
- Every tool reply to Gemini Live is capped at 40,000 characters, since an
  oversized reply closes the whole session.
- `type_text`, `press_key` and `submit_sudo_password` refuse while the screen
  is locked.

## [0.11.0] - 2026-09-28

### Highlights

- When Google's voice model has trouble on its side, she switches to an older
  model by herself, tells you so, and carries on the same conversation. If
  that doesn't help either, she says so and suggests trying another provider.
- When you ask for something that takes a while, she tells you right away
  that she heard you and what she's doing, and gives a short update if it
  runs long.
- Phone calls no longer go silent when she misses the end of what you said:
  she answers anyway.
- Her warnings, like running out of credits or switching models, are spoken
  in your language, whichever language you speak.
- When the Gateway is slow or down, she stops waiting on it and acts right
  away, instead of pausing ten seconds on every sentence.
- Assistant Settings keeps passwords, approval PINs and API keys out of view
  of other programs on your computer.

### Fixes

- Saying "the password is secure" is no longer taken for a password.
- A tool result that arrives after a dropped connection is logged instead of
  crashing the call.

### Under the hood

- Your languages now live in your own config.yaml, not in the code:
  `user_languages` tells her which languages you speak, and
  `extra_exit_phrases` / `extra_farewell_markers` hold your stop and goodbye
  words for the OpenAI and Gateway providers. Hebrew stop words are no longer
  built in; add them there if you use those providers.
- The fallback order is `gemini_fallback_models` (Gemini 3.1 Flash Live, then
  2.5 Flash Native Audio). A failing model is skipped for ten minutes.
- Warnings ship in English and are recorded once in your language on your
  machine, in ~/.local/state/omarchy-ai/alert_clips/.
- Deciding when to hand a failing job to a background task, and spotting a
  spoken password, now work in any language.
- The marketplace listing presents the full assistant, and its settings
  widget sends secrets over stdin.

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
