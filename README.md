# Omarchy AI

An independent, native voice assistant for Omarchy Linux: local wake word,
realtime voice (`gpt-live-1`), an LLM planner that converts speech into typed,
policy-checked tool calls, and a casting subsystem that puts the desktop on a
paired Android TV or tablet without touching the TV's own remote or UI.

Not built on Hermes, OpenClaw, Open Interpreter, Home Assistant, or another
agent harness — Omarchy AI owns its conversation loop, planning, tools,
policy, device management, memory, and execution.

**Status: Phase 1 — voice works.** Wake word ("hey jarvis" — a pretrained
placeholder, see below) → a real, two-way conversation with `gpt-live-1` →
say "bye"/"stop"/"that's all" to hang up and go back to listening, all
running as a systemd user service. Full debugging trail, the
reverse-engineered `gpt-live-1` session schema, and what's still open are in
[STATUS.md](STATUS.md); the architecture decisions and why in
[docs/ADR-0001-architecture.md](docs/ADR-0001-architecture.md). The
policy/tool-calling/OS-control layer described below is designed but not
built yet — right now this is a voice conversation, not yet a "do things on
the desktop" assistant.

## Install

```bash
git clone <this repo> ~/Git/omarchy-ai
cd ~/Git/omarchy-ai
./scripts/setup.sh
systemctl --user enable --now omarchy-ai
```

Needs a few system packages first — see
[docs/DEPENDENCIES.md](docs/DEPENDENCIES.md) for the exact list and why each
one's there. Needs an OpenAI API key with `gpt-live-1` access at
`~/.config/omavoice/key` (shared with the omavoice project) or wherever
`api_key_path` in `~/.config/omarchy-ai/config.yaml` points.

Say **"hey jarvis"**, then talk. Say **"bye"**, **"stop"**, or **"that's
all"** (any language) to end the conversation and go back to listening — no
connection stays open outside an active conversation, since `gpt-live-1` is
billed per second.

## Repository layout

```
src/omarchy_ai/
  core/         the daemon loop (wake -> conversation -> wake); planner,
                tool registry, task state, memory (not built yet)
  voice/        wake word (openWakeWord), the gpt-live-1 WebRTC client
                (aiortc), tone-cue feedback
  execution/    Hyprland, apps, system status, PipeWire, files, guarded shell
                (not built yet)
  vision/       screen capture, OCR, window inspection, GUI fallback
                (not built yet)
  devices/      discovery, registry, pairing, ADB, Cast, CEC, presence
                (not built yet)
  display/      virtual display lifecycle, capture, encoder, WebRTC, sessions
                (not built yet — Phase 2, see STATUS.md)
  policy/       permission levels, confirmation, audit log, secrets
                (not built yet)
  cli/          not built yet
systemd/        omarchy-ai.service (user unit)
android-receiver/   Kotlin receiver app skeleton (Phase 2, casting)
docs/           architecture decision records, dependency list
scripts/        spike_live_*.py (the WebRTC/API reverse-engineering scripts
                this was built from — kept for reference), setup.sh,
                uninstall.sh
```

## Policy levels (designed, not implemented yet)

Every tool will be classified before it can run:

| Level | Examples | Gate |
|---|---|---|
| 1 — read-only | list windows, check status, read audio routing | automatic |
| 2 — reversible | switch workspace, move a window, adjust volume | automatic, logged |
| 3 — sensitive | install/remove packages, stop services, reboot | confirm required |
| 4 — dangerous | recursive delete, format, credential extraction | denied by default |

See ADR-0001 for why this exists — it's the mitigation for using an LLM
planner instead of a fixed command table (jarvisd's earlier, narrower
approach; see that repo's README for the trade-off this moves away from).

## License

MIT — see [LICENSE](LICENSE).
