# Oma Receiver (Android)

Not started. Building even a minimal receiver launchable via ADB needs a JDK,
the Android SDK command-line tools, a target platform + build-tools package,
and Gradle — none of which are installed on this machine yet (confirmed in
Phase 0's environment audit, see `../docs/ADR-0001-architecture.md`). This is
a multi-GB one-time download; waiting on a go-ahead before starting it.

Planned entry point once started: `ai.oma.receiver/.ReceiverActivity`,
launched via `adb shell am start` with a short-lived session id/token (never
a long-lived secret on the command line — see the policy/secrets design in
ADR-0001 and the parent spec).
