import hashlib
import importlib.util
import io
from pathlib import Path
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_script(filename):
    path = ROOT / "scripts" / filename
    name = filename.removesuffix(".py").replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_bundle(root: Path, version="0.4.0", payload=b"bundle"):
    dist = root / "dist"
    dist.mkdir()
    archive = dist / f"omarchy-ai-{version}-linux-x86_64.tar.gz"
    archive.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    checksum = dist / (archive.name + ".sha256")
    checksum.write_text(f"{digest}  {archive.name}\n")
    return archive, checksum


class Result:
    def __init__(self, code, stdout="", stderr=""):
        self.returncode = code
        self.stdout = stdout
        self.stderr = stderr


class GitHubReleaseScriptTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_script("publish-github-release.py")

    def test_checksum_mismatch_is_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            archive, checksum = write_bundle(Path(tmp))
            checksum.write_text("0" * 64 + "  " + archive.name + "\n")
            with self.assertRaisesRegex(ValueError, "mismatch"):
                self.mod.verify_checksum(archive, checksum)

    def test_prepare_builds_release_urls_and_commands(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_bundle(root, "0.4.0")
            plan = self.mod.prepare(root, "0.4.0", "a" * 40)
        self.assertEqual(plan["tag"], "v0.4.0")
        self.assertIn("--clobber", self.mod.gh_upload_command(plan))
        create = self.mod.gh_create_command(plan)
        self.assertEqual(create[0:3], ["gh", "release", "create"])
        self.assertIn("a" * 40, create)
        text = self.mod.format_plan(plan)
        self.assertIn(
            "https://github.com/omribenami/Omarchy-AI/releases/download/v0.4.0/"
            "omarchy-ai-0.4.0-linux-x86_64.tar.gz",
            text,
        )
        self.assertIn("uploads.github.com", text)
        self.assertIn("download_count", text)
        self.assertNotIn("raw.githubusercontent.com", text)

    def test_gh_creates_when_release_is_missing_and_updates_when_present(self):
        plan = {"tag": "v0.4.0", "repo": "omribenami/Omarchy-AI", "archive": Path("a.tgz"),
                "checksum": Path("a.tgz.sha256"), "title": "Omarchy AI 0.4.0", "notes": "n",
                "commit": "a" * 40}
        calls = []

        def missing(args):
            calls.append(args)
            if args[1:3] == ["release", "view"]:
                return Result(1, stderr="release not found")
            return Result(0)

        self.assertEqual(self.mod.publish_with_gh(plan, missing), "created")
        self.assertEqual(calls[1][1:3], ["release", "create"])

        calls.clear()

        def present(args):
            calls.append(args)
            return Result(0)

        self.assertEqual(self.mod.publish_with_gh(plan, present), "updated")
        self.assertIn("--clobber", calls[1])

    def test_gh_auth_failure_is_not_treated_as_a_missing_release(self):
        plan = {"tag": "v0.4.0", "repo": "omribenami/Omarchy-AI"}

        def run(args):
            return Result(1, stderr="HTTP 401: Bad credentials")

        with self.assertRaises(SystemExit) as caught:
            self.mod.publish_with_gh(plan, run)
        self.assertIn("401", str(caught.exception))

    def test_api_create_uploads_both_assets_and_update_replaces_them(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            archive, checksum = write_bundle(Path(tmp), "0.4.0")
            plan = {
                "tag": "v0.4.0", "repo": "omribenami/Omarchy-AI", "commit": "c" * 40,
                "archive": archive, "checksum": checksum, "title": "Omarchy AI 0.4.0",
                "notes": "notes",
            }
            created = FakeApi(None)
            self.assertEqual(self.mod.publish_with_api(plan, created), "created")
            methods = [call[0] for call in created.calls]
            self.assertEqual(methods[0], "GET")
            self.assertEqual(methods[1], "POST")
            self.assertTrue(created.calls[1][1].endswith("/releases"))
            uploads = [url for method, url in created.calls if "uploads.github.com" in url]
            self.assertEqual(len(uploads), 2)
            self.assertTrue(any(archive.name in url for url in uploads))
            self.assertTrue(any(checksum.name in url for url in uploads))

            existing = FakeApi({
                "id": 4,
                "assets": [
                    {"name": archive.name, "id": 11},
                    {"name": checksum.name, "id": 12},
                ],
            })
            self.assertEqual(self.mod.publish_with_api(plan, existing), "updated")
            deletes = [url for method, url in existing.calls if method == "DELETE"]
            self.assertEqual(deletes, [
                "https://api.github.com/repos/omribenami/Omarchy-AI/releases/assets/11",
                "https://api.github.com/repos/omribenami/Omarchy-AI/releases/assets/12",
            ])
            self.assertFalse(any(method == "POST" and url.endswith("/releases") for method, url in existing.calls))

    def test_checkout_must_match_origin_main_and_may_dirty_only_the_archive(self):
        answers = iter([" M README.md\n"])
        with patch.object(self.mod.subprocess, "check_output", side_effect=lambda *args, **kwargs: next(answers)):
            with self.assertRaises(SystemExit) as caught:
                self.mod.ensure_publishable_checkout(set())
        self.assertIn("README.md", str(caught.exception))

        answers = iter(["", "a" * 40 + "\n", "b" * 40 + "\n"])
        with patch.object(self.mod.subprocess, "check_output", side_effect=lambda *args, **kwargs: next(answers)), \
                patch.object(self.mod.subprocess, "run") as fetch:
            with self.assertRaises(SystemExit) as caught:
                self.mod.ensure_publishable_checkout(set())
        fetch.assert_called_once()
        self.assertIn("not origin/main", str(caught.exception))

        path = "dist/omarchy-ai-0.4.0-linux-x86_64.tar.gz"
        answers = iter([f" M {path}\n", "c" * 40 + "\n", "c" * 40 + "\n"])
        with patch.object(self.mod.subprocess, "check_output", side_effect=lambda *args, **kwargs: next(answers)), \
                patch.object(self.mod.subprocess, "run"):
            self.assertEqual(self.mod.ensure_publishable_checkout({path}), "c" * 40)

    def test_dry_run_prints_the_plan_and_publish_selects_gh_or_the_api(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text('[project]\nversion = "0.4.0"\n')
            write_bundle(root, "0.4.0")
            with patch.object(self.mod, "ROOT", root), \
                    patch.object(self.mod, "ensure_publishable_checkout", return_value="d" * 40), \
                    patch.object(self.mod.shutil, "which", return_value="/usr/bin/gh"), \
                    patch.object(self.mod, "publish_with_gh") as gh, \
                    patch.object(self.mod, "publish_with_api") as api:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    self.assertEqual(self.mod.main([]), 0)
                gh.assert_not_called()
                api.assert_not_called()
                self.assertIn("releases/download/v0.4.0/", stdout.getvalue())
                self.assertIn("Dry run only", stdout.getvalue())

                gh.return_value = "created"
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(self.mod.main(["--publish"]), 0)
                gh.assert_called_once()
                api.assert_not_called()

            with patch.object(self.mod, "ROOT", root), \
                    patch.object(self.mod, "ensure_publishable_checkout", return_value="d" * 40), \
                    patch.object(self.mod.shutil, "which", return_value=None), \
                    patch.dict("os.environ", {"GH_TOKEN": "", "GITHUB_TOKEN": ""}):
                with redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        self.mod.main(["--publish"])
            self.assertIn("GH_TOKEN", str(caught.exception))

            with patch.object(self.mod, "ROOT", root), \
                    patch.object(self.mod, "ensure_publishable_checkout", return_value="d" * 40), \
                    patch.object(self.mod.shutil, "which", return_value=None), \
                    patch.dict("os.environ", {"GH_TOKEN": "secret", "GITHUB_TOKEN": ""}), \
                    patch.object(self.mod, "publish_with_api", return_value="created") as api:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(self.mod.main(["--publish"]), 0)
            api.assert_called_once()
            self.assertIsInstance(api.call_args.args[1], self.mod.GitHubApi)
            self.assertEqual(api.call_args.args[1].token, "secret")


class FakeApi:
    def __init__(self, existing):
        self.existing = existing
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if method == "GET":
            return self.existing
        if method == "POST" and url.endswith("/releases"):
            return {"id": 9, "assets": []}
        if method == "DELETE":
            return {}
        if method == "POST" and "uploads.github.com" in url:
            return {"id": 1}
        raise AssertionError((method, url))


class ReadmeInstallTests(unittest.TestCase):
    def test_fast_install_uses_a_release_asset_and_checks_sha256(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn(
            'base="https://github.com/omribenami/Omarchy-AI/releases/download/v${version}"',
            readme,
        )
        self.assertIn('sha256sum -c "$package.sha256"', readme)
        self.assertNotIn("raw.githubusercontent.com", readme)


class MyApiPublishTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_script("publish-via-myapi.py")

    def test_archive_paths_are_blocked_and_source_paths_are_not(self):
        changes = [
            ("M", "README.md"),
            ("A", "dist/omarchy-ai-0.3.11-linux-x86_64.tar.gz"),
            ("A", "dist/omarchy-ai-0.3.11-linux-x86_64.tar.gz.sha256"),
            ("M", "src/omarchy_ai/core/updates.py"),
        ]
        self.assertEqual(self.mod.release_archive_paths(changes), [
            "dist/omarchy-ai-0.3.11-linux-x86_64.tar.gz",
            "dist/omarchy-ai-0.3.11-linux-x86_64.tar.gz.sha256",
        ])

    def test_commit_message_uses_the_package_version(self):
        self.assertEqual(self.mod.commit_message("0.3.11"), "Publish Omarchy AI 0.3.11")
        with self.assertRaises(ValueError):
            self.mod.commit_message("0.2.0-beta")

    def test_main_refuses_to_push_release_archives(self):
        def fake_git(*args, text=True):
            if args == ("status", "--porcelain"):
                return ""
            if args == ("rev-parse", "origin/main"):
                return "a" * 40
            raise AssertionError(args)

        with patch.object(self.mod, "git", side_effect=fake_git), patch.object(
                self.mod, "changed_paths", return_value=[
                    ("M", "README.md"),
                    ("A", "dist/omarchy-ai-0.3.11-linux-x86_64.tar.gz")]):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as caught:
                    self.mod.main([])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("publish-github-release.py", stderr.getvalue())
        self.assertIn("omarchy-ai-0.3.11-linux-x86_64.tar.gz", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
