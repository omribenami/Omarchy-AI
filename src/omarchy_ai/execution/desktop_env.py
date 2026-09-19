"""Recover the live graphical-session environment for user services.

The daemon can start before Hyprland exports its per-login variables to the
systemd user manager.  Desktop subprocesses must therefore discover them at
call time instead of relying on the daemon's startup environment.
"""

from __future__ import annotations

import json
import os
import subprocess


def _session_is_live(environment: dict[str, str], runtime: str) -> bool:
    signature = environment.get("HYPRLAND_INSTANCE_SIGNATURE")
    wayland = environment.get("WAYLAND_DISPLAY")
    if not signature or not wayland:
        return False
    return (
        os.path.exists(os.path.join(runtime, "hypr", signature, ".socket.sock"))
        and os.path.exists(os.path.join(runtime, wayland))
    )


def desktop_env() -> dict[str, str]:
    environment = os.environ.copy()
    runtime = environment.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    environment.setdefault("XDG_RUNTIME_DIR", runtime)

    # User services retain their startup environment. After Hyprland
    # restarts those variables remain set but refer to deleted sockets.
    if not _session_is_live(environment, runtime):
        try:
            probe_environment = environment.copy()
            probe_environment.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
            probe_environment.pop("WAYLAND_DISPLAY", None)
            probe = subprocess.run(
                ["hyprctl", "instances", "-j"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
                env=probe_environment,
            )
            instances = json.loads(probe.stdout or "[]")
            if instances:
                instance = max(instances, key=lambda item: item.get("time", 0))
                environment["HYPRLAND_INSTANCE_SIGNATURE"] = str(instance["instance"])
                if instance.get("wl_socket"):
                    environment["WAYLAND_DISPLAY"] = str(instance["wl_socket"])
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            pass

    # Non-Hyprland Wayland compositors still expose their socket here.
    if not environment.get("WAYLAND_DISPLAY"):
        try:
            sockets = sorted(
                name for name in os.listdir(runtime)
                if name.startswith("wayland-") and not name.endswith(".lock")
            )
            if sockets:
                environment["WAYLAND_DISPLAY"] = sockets[0]
        except OSError:
            pass
    environment.setdefault("DISPLAY", ":0")
    return environment
