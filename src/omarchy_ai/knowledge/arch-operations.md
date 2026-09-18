# Arch operations reference

Reviewed 2026-09-18. Installed versions and live state override these notes.
These references inform decisions; they do not grant execution permission.

## Audio: PipeWire and WirePlumber

Use `wpctl status` to discover audio nodes. `wpctl get-volume
@DEFAULT_AUDIO_SINK@` reports output volume and mute state. The corresponding
source identifier is `@DEFAULT_AUDIO_SOURCE@`. Default identifiers resolve at
runtime; inspect again if routing changes. Volume is a scalar where 1 means
100%, and can exceed 1. Check observed state after changes. Do not restart the
audio stack during a conversation.
Source: https://man.archlinux.org/man/wpctl.1.en

## Services and troubleshooting

Use `systemctl --user` for the user's services; system services have a separate
manager. Inspect service state and journal evidence before diagnosing a failure.
Starting a process does not establish application readiness. A service restart
can terminate ongoing work; it is not a generic first troubleshooting step.
Source: https://man.archlinux.org/man/systemctl.1.en

## Package and desktop references

Omarchy is Arch based. Use the installed Omarchy command inventory and help.
Package installation, removal, privilege changes and config resets remain outside
the Jev desktop executor. Hyprland syntax is version dependent; the local
executor already probes Lua dispatch with a classic fallback.
Further references (not ingested; ArchWiki blocked automated access at research time):
https://wiki.archlinux.org/title/Hyprland
https://wiki.archlinux.org/title/Pacman
https://wiki.archlinux.org/title/PipeWire

## Lessons from the local action audit

The 2026-09-17 audit at ~/omarchy-action-audit-2026-09-17.md found that the Agent
launcher opened Claude rather than the assistant settings panel. Discover bar
plugin IDs; never substitute a similarly named launcher. Focus must be verified
before input. Command success does not establish visual rendering, app acceptance,
work progress or completion. Old terminal text is not fresh evidence.
