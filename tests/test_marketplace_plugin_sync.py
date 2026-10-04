import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "scripts" / "marketplace-plugin"
SEED = PLUGIN / "seed"
WORKFLOW = ROOT / ".github" / "workflows" / "sync-marketplace-plugin.yml"

GIT_ENV = dict(
    os.environ,
    GIT_AUTHOR_NAME="marketplace-sync-test",
    GIT_AUTHOR_EMAIL="marketplace-sync-test@example.com",
    GIT_COMMITTER_NAME="marketplace-sync-test",
    GIT_COMMITTER_EMAIL="marketplace-sync-test@example.com",
)


def run(args, **kwargs):
    env = dict(GIT_ENV)
    env.update(kwargs.pop("env", {}))
    return subprocess.run(args, check=True, env=env, capture_output=True, text=True, **kwargs)


class MarketplacePluginSyncTests(unittest.TestCase):
    def assemble(self, dest: Path) -> None:
        run(["python3", str(PLUGIN / "assemble.py"), str(dest)])

    def test_seed_matches_assembler_and_keeps_plugin_identity(self):
        source = json.loads((ROOT / "quickshell/plugins/omarchy-ai.settings/manifest.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "tree"
            self.assemble(dest)
            built = {path.relative_to(dest).as_posix(): path.read_bytes() for path in dest.rglob("*") if path.is_file()}
        seeded = {path.relative_to(SEED).as_posix(): path.read_bytes() for path in SEED.rglob("*") if path.is_file()}
        self.assertEqual(seeded, built)

        manifest = json.loads((SEED / "manifest.json").read_text())
        self.assertEqual(manifest["id"], "omarchy-ai.settings")
        self.assertEqual(manifest["name"], "Omarchy-AI")
        self.assertEqual(manifest["name"], source["name"])
        self.assertNotIn("Settings", manifest["name"])
        self.assertEqual(manifest["version"], "0.4.0")
        self.assertEqual(manifest["version"], source["version"])
        self.assertEqual(manifest["barWidget"]["displayName"], "Omarchy-AI")
        self.assertNotIn("Settings", manifest["barWidget"]["displayName"])
        self.assertEqual(manifest["kinds"], ["bar-widget"])
        self.assertEqual(manifest["entryPoints"], {"barWidget": "Panel.qml"})
        self.assertEqual(manifest["author"], "Omri Ben Ami")
        for text in (manifest["description"], manifest["barWidget"]["description"]):
            lowered = text.lower()
            self.assertLessEqual(len(text), 500)
            self.assertTrue(lowered.startswith("self-hosted agentic voice assistant"))
            self.assertIn("wake word", lowered)
            self.assertIn("settings", lowered)
            self.assertIn("full assistant", lowered)
            self.assertIn("does not run the daemon", lowered)
        preview = SEED / "preview.png"
        self.assertTrue(preview.is_file())
        self.assertTrue(preview.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertLess(preview.stat().st_size, 50 * 1024 * 1024)
        self.assertIn("preview.png", built)
        panel = (SEED / "Panel.qml").read_text()
        self.assertEqual(panel, (ROOT / "quickshell/plugins/omarchy-ai.settings/Panel.qml").read_text())
        self.assertNotIn("@OMARCHY_AI_SETTINGS@", panel)
        self.assertNotRegex(panel, r"@[A-Z0-9_]+@")
        self.assertIn("resolve-settings.sh", panel)
        self.assertIn("assistant_installed", panel)
        self.assertIn("https://github.com/omribenami/Omarchy-AI#installation", panel)
        self.assertNotIn("selectByMouse", panel)
        self.assertIn("install.sh", panel)
        self.assertIn("does not install the voice assistant", panel)
        resolver = (SEED / "resolve-settings.sh").read_text()
        self.assertEqual(
            resolver,
            (ROOT / "quickshell/plugins/omarchy-ai.settings/resolve-settings.sh").read_text(),
        )
        self.assertNotIn("@OMARCHY_AI_SETTINGS@", resolver)
        self.assertIn("configure-sudo", panel)
        readme = (SEED / "README.md").read_text()
        self.assertTrue(readme.startswith("# Omarchy-AI\n"))
        self.assertLess(
            readme.index("https://github.com/omribenami/Omarchy-AI#installation"),
            readme.index("omarchy plugin add https://github.com/omribenami/omarchy-ai-plugin.git --enable"),
        )
        self.assertIn("## Settings panel only", readme)
        self.assertIn("omarchy plugin remove omarchy-ai.settings", readme)
        self.assertIn("omarchy bar move omarchy-ai.settings --section right", readme)
        self.assertIn("does not run the daemon", readme)
        self.assertNotIn("@OMARCHY_AI_SETTINGS@", readme)
        self.assertNotIn("## Manual setup", readme)
        self.assertIn("## How the panel finds the assistant", readme)
        self.assertIn("resolve-settings.sh", readme)
        self.assertIn("does not install Omarchy-AI", readme)
        self.assertNotIn("Omarchy AI Settings", readme)
        self.assertNotIn("Omarchy-AI. Settings", readme)
        self.assertNotIn("Omarchy-AI Settings", readme)
        self.assertIn("## Features", readme)
        self.assertIn("Task Runtime", readme)
        self.assertLess(
            readme.index("https://github.com/user-attachments/assets/ea736181-9cf3-423a-b7d5-91a895fe6589"),
            readme.index("## Install the full assistant"),
        )
        self.assertIn("https://github.com/user-attachments/assets/7abed3fa-ed55-4835-b77a-4d0a1ab85f1f", readme)
        self.assertIn("https://github.com/user-attachments/assets/6d20a7b9-3806-4248-be12-83bdddf63f66", readme)
        self.assertIn("omarchy plugin add` does not install the daemon", readme)
        license_text = (SEED / "LICENSE").read_text()
        self.assertIn("Copyright (c) 2026 Omri Ben-Ami", license_text)
        self.assertTrue(license_text.startswith("MIT License\n"))

    def test_workflow_triggers_only_for_the_settings_plugin_and_names_the_secret(self):
        text = WORKFLOW.read_text()
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("MARKETPLACE_PLUGIN_SYNC_TOKEN", text)
        self.assertIn("quickshell/plugins/omarchy-ai.settings/**", text)
        self.assertIn("scripts/marketplace-plugin/**", text)
        self.assertIn(".github/workflows/sync-marketplace-plugin.yml", text)
        self.assertIn("branches:", text)
        self.assertIn("- main", text)
        self.assertNotIn("src/omarchy_ai/**", text)

    def test_sync_fails_clearly_without_a_token(self):
        env = dict(GIT_ENV)
        env.pop("MARKETPLACE_PLUGIN_SYNC_TOKEN", None)
        env.pop("LISTING_REMOTE", None)
        result = subprocess.run(
            ["bash", str(PLUGIN / "sync.sh")],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MARKETPLACE_PLUGIN_SYNC_TOKEN is not set", result.stdout + result.stderr)
        self.assertIn("No listing commit was created", result.stdout + result.stderr)

    def test_sync_pushes_once_and_skips_an_unchanged_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            remote = base / "listing.git"
            run(["git", "init", "--bare", "-b", "main", str(remote)])
            env = {"LISTING_REMOTE": str(remote), "SOURCE_SHA": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
            first = run(["bash", str(PLUGIN / "sync.sh")], cwd=ROOT, env=env)
            self.assertIn("Pushed initial", first.stdout)
            count = run(["git", "--git-dir", str(remote), "rev-list", "--count", "main"]).stdout.strip()
            self.assertEqual(count, "1")
            message = run(["git", "--git-dir", str(remote), "log", "-1", "--format=%B", "main"]).stdout
            self.assertIn("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", message)

            second = run(
                ["bash", str(PLUGIN / "sync.sh")],
                cwd=ROOT,
                env={"LISTING_REMOTE": str(remote), "SOURCE_SHA": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
            )
            self.assertIn("skipping commit", second.stdout)
            count = run(["git", "--git-dir", str(remote), "rev-list", "--count", "main"]).stdout.strip()
            self.assertEqual(count, "1")

            checkout = base / "checkout"
            run(["git", "clone", str(remote), str(checkout)])
            readme = checkout / "README.md"
            readme.write_text(readme.read_text() + "\nlocal edit\n")
            run(["git", "add", "README.md"], cwd=checkout)
            run(["git", "commit", "-m", "local edit"], cwd=checkout)
            run(["git", "push", "origin", "main"], cwd=checkout)

            third = run(
                ["bash", str(PLUGIN / "sync.sh")],
                cwd=ROOT,
                env={"LISTING_REMOTE": str(remote), "SOURCE_SHA": "cccccccccccccccccccccccccccccccccccccccc"},
            )
            self.assertIn("Pushed omarchy-ai.settings sync", third.stdout)
            count = run(["git", "--git-dir", str(remote), "rev-list", "--count", "main"]).stdout.strip()
            self.assertEqual(count, "3")
            tip = run(["git", "--git-dir", str(remote), "log", "-1", "--format=%B", "main"]).stdout
            self.assertIn("cccccccccccccccccccccccccccccccccccccccc", tip)
            shown = run(["git", "--git-dir", str(remote), "show", "main:manifest.json"]).stdout
            self.assertEqual(json.loads(shown)["id"], "omarchy-ai.settings")
            panel = run(["git", "--git-dir", str(remote), "show", "main:Panel.qml"]).stdout
            self.assertNotIn("@OMARCHY_AI_SETTINGS@", panel)
            self.assertIn("resolve-settings.sh", panel)
            resolver = run(["git", "--git-dir", str(remote), "show", "main:resolve-settings.sh"]).stdout
            self.assertIn("assistant_installed", resolver)
            self.assertNotIn("local edit", run(["git", "--git-dir", str(remote), "show", "main:README.md"]).stdout)


if __name__ == "__main__":
    unittest.main()
