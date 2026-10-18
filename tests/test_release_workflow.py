"""Structure of .github/workflows/release.yml: what triggers it, and that only a
pushed vX.Y.Z tag can publish. actionlint runs too when it is installed."""

import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/release.yml"


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.doc = yaml.safe_load(WORKFLOW.read_text())
        # PyYAML reads the bare key `on` as the boolean True.
        self.on = self.doc.get("on", self.doc.get(True))
        self.jobs = self.doc["jobs"]

    def _run(self, job):
        return "\n".join(step.get("run", "") for step in self.jobs[job]["steps"])

    def _uses(self, job):
        return [step.get("uses", "") for step in self.jobs[job]["steps"]]

    def test_triggers(self):
        self.assertEqual(self.on["push"], {"tags": ["v*.*.*"]})
        self.assertIn("workflow_dispatch", self.on)
        self.assertIn(".github/workflows/release.yml", self.on["pull_request"]["paths"])
        self.assertEqual(self.doc["permissions"], {"contents": "read"})

    def test_build_job_cannot_publish(self):
        build = self.jobs["build"]
        self.assertEqual(build["permissions"], {"contents": "read"})
        self.assertNotIn("if", build)
        run = self._run("build")
        self.assertIn("bash scripts/build-install-package.sh --skip-android", run)
        self.assertIn("sha256sum -c", run)
        self.assertIn("minisign -S", run)
        self.assertIn("python -m omarchy_ai.core.signing verify", run)
        self.assertIn('"$GITHUB_REF_NAME" != "v$version"', run)
        self.assertNotIn("publish-github-release", run)
        self.assertNotIn("gh release", run)

    def test_only_a_pushed_tag_publishes(self):
        publish = self.jobs["publish"]
        self.assertEqual(publish["needs"], "build")
        self.assertEqual(
            publish["if"],
            "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')",
        )
        self.assertEqual(
            publish["permissions"],
            {"contents": "write", "id-token": "write", "attestations": "write"},
        )
        self.assertIn("actions/attest-build-provenance@v2", self._uses("publish"))
        self.assertIn(
            "scripts/publish-github-release.py --ci --publish", self._run("publish")
        )

    def test_dry_run_signs_with_a_throwaway_key_and_the_reference_verifier(self):
        run = self._run("build")
        self.assertIn("minisign -G -W", run)
        self.assertIn("minisign -V", run)
        self.assertIn("secrets.MINISIGN_SECRET_KEY", WORKFLOW.read_text())

    @unittest.skipUnless(shutil.which("actionlint"), "actionlint is not installed")
    def test_actionlint(self):
        result = subprocess.run(
            ["actionlint", str(WORKFLOW)], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
