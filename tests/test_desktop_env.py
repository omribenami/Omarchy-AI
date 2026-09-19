import json
import os
import unittest
from unittest.mock import patch

from omarchy_ai.execution.desktop_env import desktop_env


class DesktopEnvTests(unittest.TestCase):
    def test_recovers_hyprland_and_wayland_variables_together(self):
        instance = [{"instance": "live-instance", "wl_socket": "wayland-7"}]
        completed = type("Completed", (), {"stdout": json.dumps(instance)})()
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1000"}, clear=True), \
             patch("subprocess.run", return_value=completed):
            environment = desktop_env()
        self.assertEqual(environment["HYPRLAND_INSTANCE_SIGNATURE"], "live-instance")
        self.assertEqual(environment["WAYLAND_DISPLAY"], "wayland-7")
        self.assertEqual(environment["DISPLAY"], ":0")

    def test_keeps_existing_graphical_session(self):
        existing = {
            "HYPRLAND_INSTANCE_SIGNATURE": "existing",
            "WAYLAND_DISPLAY": "wayland-2",
        }
        with patch.dict(os.environ, existing, clear=True), \
             patch("os.path.exists", return_value=True), patch("subprocess.run") as run:
            environment = desktop_env()
        run.assert_not_called()
        self.assertEqual(environment["HYPRLAND_INSTANCE_SIGNATURE"], "existing")
        self.assertEqual(environment["WAYLAND_DISPLAY"], "wayland-2")

    def test_replaces_stale_graphical_session(self):
        stale = {
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "HYPRLAND_INSTANCE_SIGNATURE": "old-instance",
            "WAYLAND_DISPLAY": "wayland-0",
        }
        instances = [
            {"instance": "older-live", "wl_socket": "wayland-1", "time": 10},
            {"instance": "newest-live", "wl_socket": "wayland-2", "time": 20},
        ]
        completed = type("Completed", (), {"stdout": json.dumps(instances)})()
        with patch.dict(os.environ, stale, clear=True), \
             patch("os.path.exists", return_value=False), \
             patch("subprocess.run", return_value=completed) as run:
            environment = desktop_env()
        self.assertEqual(environment["HYPRLAND_INSTANCE_SIGNATURE"], "newest-live")
        self.assertEqual(environment["WAYLAND_DISPLAY"], "wayland-2")
        self.assertNotIn("HYPRLAND_INSTANCE_SIGNATURE", run.call_args.kwargs["env"])
        self.assertNotIn("WAYLAND_DISPLAY", run.call_args.kwargs["env"])


if __name__ == "__main__":
    unittest.main()
