# Oma

An independent, native voice assistant for Omarchy Linux: local wake word,
realtime voice (`gpt-live-1`), an LLM planner that converts speech into typed,
policy-checked tool calls, and a casting subsystem that puts the desktop on a
paired Android TV or tablet without touching the TV's own remote or UI.

Not built on Hermes, OpenClaw, Open Interpreter, Home Assistant, or another
agent harness — Oma owns its conversation loop, planning, tools, policy,
device management, memory, and execution.

**Status: Phase 0 (environment audit + design).** See [STATUS.md](STATUS.md)
for what's done and what's blocked, and
[docs/ADR-0001-architecture.md](docs/ADR-0001-architecture.md) for the
decisions and the findings behind them. Nothing here is running yet.

## Repository layout

```
src/oma/
  core/         conversation loop, planner, tool registry, task state, memory
  voice/        wake word, VAD, capture, STT/realtime, TTS, echo/barge-in
  execution/    Hyprland, apps, system status, PipeWire, files, guarded shell
  vision/       screen capture, OCR, window inspection, GUI fallback
  devices/      discovery, registry, pairing, ADB, Cast, CEC, presence
  display/      virtual display lifecycle, capture, encoder, WebRTC, sessions
  policy/       permission levels, confirmation, audit log, secrets
  cli/          `oma status`, `oma ask`, `oma devices ...`, `oma display ...`
systemd/        oma.service (user unit)
android-receiver/   Kotlin receiver app (not started — needs Android SDK/Gradle)
docs/           architecture decision records
```

## Policy levels

Every tool is classified before it can run:

| Level | Examples | Gate |
|---|---|---|
| 1 — read-only | list windows, check status, read audio routing | automatic |
| 2 — reversible | switch workspace, move a window, adjust volume | automatic, logged |
| 3 — sensitive | install/remove packages, stop services, reboot | confirm required |
| 4 — dangerous | recursive delete, format, credential extraction | denied by default |

See ADR-0001 for why this exists — it's the mitigation for using an LLM
planner instead of a fixed command table (jarvisd's earlier, narrower
approach; see that repo's README for the trade-off this moves away from).

## Setup

Not ready to install yet — Phase 0 is still resolving toolchain blockers
(`adb`, `gst-plugins-bad`, and whether to install a full Android SDK). See
STATUS.md.

## License

MIT — see [LICENSE](LICENSE).
