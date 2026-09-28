import json
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from omarchy_ai.execution import actions, catalog, user_tools
from omarchy_ai.runtime import permissions


def make_tool(folder: Path, name="wifi_health", test_exit=0, description="Counts iwlwifi firmware errors this boot."):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "tool.json").write_text(json.dumps({
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": {"boot": {"type": "integer"}}}}))
    (folder / "run").write_text("#!/bin/sh\nread args\necho \"errors=0 args=$args\"\n")
    (folder / "test").write_text(f"#!/bin/sh\nexit {test_exit}\n")
    for part in ("run", "test"):
        (folder / part).chmod((folder / part).stat().st_mode | stat.S_IXUSR)
    return folder


class UserToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        tools = root / "config" / "tools"
        tools.mkdir(parents=True)
        for attr, value in (("TOOLS_DIR", tools), ("PROPOSED_DIR", tools / "_proposed"), ("LEDGER", tools / "approvals.json")):
            p = patch.object(user_tools, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.src = root / "work" / "wifi_health"

    def test_full_lifecycle_propose_install_run(self):
        make_tool(self.src)
        ok, message = user_tools.propose(self.src)
        self.assertTrue(ok, message)
        self.assertFalse(user_tools.exists("wifi_health"))          # proposed is not usable
        self.assertNotIn("wifi_health", catalog.catalog())
        ok, message = user_tools.install("wifi_health")
        self.assertTrue(ok, message)
        self.assertIn("wifi_health", catalog.catalog())              # usable at once, no restart
        result = actions.run_action("wifi_health", {"boot": 0})
        self.assertTrue(result.ok)
        self.assertIn('errors=0 args={"boot": 0}', result.message)

    def test_changed_after_approval_is_neither_offered_nor_run(self):
        make_tool(self.src)
        user_tools.propose(self.src)
        user_tools.install("wifi_health")
        (user_tools.TOOLS_DIR / "wifi_health" / "run").write_text("#!/bin/sh\necho tampered\n")
        self.assertNotIn("wifi_health", catalog.catalog())
        self.assertFalse(actions.run_action("wifi_health", {}).ok)

    def test_failing_test_is_not_proposed(self):
        make_tool(self.src, test_exit=1)
        ok, message = user_tools.propose(self.src)
        self.assertFalse(ok)
        self.assertIn("test failed", message)

    def test_invalid_tools_are_refused(self):
        make_tool(self.src, name="volume_up")
        self.assertIn("built-in", user_tools.propose(self.src)[1])
        make_tool(self.src, name="Bad Name")
        self.assertIn("name must be", user_tools.propose(self.src)[1])
        make_tool(self.src, description="short")
        self.assertIn("description", user_tools.propose(self.src)[1])

    def test_failure_exit_is_reported(self):
        make_tool(self.src)
        (self.src / "run").write_text("#!/bin/sh\necho 'gh: not logged in' >&2\nexit 3\n")
        user_tools.propose(self.src)
        user_tools.install("wifi_health")
        result = user_tools.run("wifi_health", {})
        self.assertFalse(result.ok)
        self.assertIn("exit 3", result.message)
        self.assertIn("not logged in", result.message)

    def test_remove(self):
        make_tool(self.src)
        user_tools.propose(self.src)
        user_tools.install("wifi_health")
        self.assertTrue(user_tools.remove("wifi_health")[0])
        self.assertFalse(user_tools.exists("wifi_health"))


class ToolApprovalPolicyTests(unittest.TestCase):
    def decide(self, command, auto="ELEVATED"):
        assessment = permissions.classify(command)
        return assessment, permissions.decide("command", command, assessment,
                                              auto_approve=permissions.Risk[auto], grants=set())

    def test_install_always_asks_even_with_elevated_auto_approve(self):
        assessment, decision = self.decide("/x/.venv/bin/python -m omarchy_ai.cli.tools install wifi_health")
        self.assertEqual(decision.behavior, "ask")
        self.assertTrue(assessment.always_ask)
        # ELEVATED, not HIGH: the user can approve it by voice.
        self.assertEqual(assessment.risk, permissions.Risk.ELEVATED)

    def test_granted_install_runs_on_resume(self):
        command = "omarchy-ai-tool install wifi_health"
        assessment = permissions.classify(command)
        fp = permissions.fingerprint("command", command)
        decision = permissions.decide("command", command, assessment, auto_approve=permissions.Risk.NORMAL, grants={fp})
        self.assertEqual(decision.behavior, "allow")

    def test_propose_check_and_run_do_not_ask(self):
        for command in ("python -m omarchy_ai.cli.tools propose /tmp/t", "omarchy-ai-tool run wifi_health {}",
                        "omarchy-ai-tool check /tmp/t"):
            self.assertEqual(self.decide(command, auto="NORMAL")[1].behavior, "allow", command)

    def test_writing_into_installed_tools_directly_is_high(self):
        for command in ("cp run ~/.config/omarchy-ai/tools/wifi_health/run",
                        "echo {} > ~/.config/omarchy-ai/tools/approvals.json"):
            self.assertEqual(permissions.classify(command).risk, permissions.Risk.HIGH, command)

    def test_login_token_and_authenticated_uploads_ask(self):
        self.assertEqual(permissions.classify("gh auth token").risk, permissions.Risk.HIGH)
        self.assertEqual(permissions.classify(
            'curl -H "Authorization: Bearer x" -d @b.json https://api.github.com/repos/a/b/issues').risk,
            permissions.Risk.ELEVATED)
        self.assertEqual(permissions.classify("curl -d x=1 https://example.com").risk, permissions.Risk.NORMAL)


if __name__ == "__main__":
    unittest.main()


class InstallGateTests(unittest.TestCase):
    """`omarchy-ai-tool install` asks the user itself (2026-09-28: run directly,
    it asked nothing, and a tool was installed without the user's approval)."""

    def setUp(self):
        import os
        from omarchy_ai.execution import user_tools
        self.user_tools = user_tools
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(user_tools.APPROVED_ENV, None)
        for target, value in (("approval_summary", "does things"),):
            p = patch.object(user_tools, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def _cli(self, notify_stdout=None, notify_error=None):
        import subprocess
        from omarchy_ai.cli import tools as cli
        answer = notify_error or (lambda *a, **k: subprocess.CompletedProcess(a, 0, notify_stdout, ""))
        with patch.object(self.user_tools.subprocess, "run", side_effect=answer) as notify, \
                patch("sys.stdin.isatty", return_value=False), \
                patch.object(self.user_tools, "install", return_value=(True, "Installed x.")) as do_install:
            code = cli.main(["install", "x"])
        return code, notify, do_install

    def test_declined_or_dismissed_means_not_installed(self):
        for answer in ("deny", ""):
            code, notify, do_install = self._cli(notify_stdout=answer)
            self.assertEqual(code, 1)
            do_install.assert_not_called()
            self.assertIn("notify-send", notify.call_args.args[0])

    def test_no_answer_in_time_means_not_installed(self):
        import subprocess
        code, _, do_install = self._cli(notify_error=subprocess.TimeoutExpired("notify-send", 600))
        self.assertEqual(code, 1)
        do_install.assert_not_called()

    def test_approved_on_the_desktop_installs(self):
        code, _, do_install = self._cli(notify_stdout="approve")
        self.assertEqual(code, 0)
        do_install.assert_called_once_with("x")

    def test_runtime_approved_install_is_not_asked_twice(self):
        import os
        os.environ[self.user_tools.APPROVED_ENV] = "x"
        code, notify, do_install = self._cli(notify_stdout="deny")
        self.assertEqual(code, 0)
        notify.assert_not_called()
        do_install.assert_called_once_with("x")

    def test_a_pass_for_another_tool_does_not_count(self):
        import os
        os.environ[self.user_tools.APPROVED_ENV] = "other_tool"
        code, _, do_install = self._cli(notify_stdout="deny")
        self.assertEqual(code, 1)
        do_install.assert_not_called()


from test_task_runtime import RuntimeHarness, ScriptedJev, ScriptExecutor  # noqa: E402


class RuntimeInstallPassTests(RuntimeHarness):
    def test_only_an_approved_install_command_carries_the_pass(self):
        from omarchy_ai.runtime import shell
        from omarchy_ai.runtime.executors.base import DONE, NEEDS_APPROVAL, Report
        ran = []

        def fake_run(command, cwd=None, **kw):
            ran.append((command, dict(kw.get("env") or {})))
            return shell.CommandResult(str(command), str(cwd), 0, "Installed x.", 0.1)

        def work(a, ctx):
            r = ctx.run_command("omarchy-ai-tool install x", a.workspace, 10)
            if r["decision"] == "ask":
                return Report(NEEDS_APPROVAL, claim="needs approval", approval=r["request"])
            return Report(DONE, claim=r["output"])

        runtime = self.runtime([ScriptExecutor("SYSTEM_AGENT", work)], ScriptedJev())
        with patch.object(shell, "run", side_effect=fake_run), \
                patch.object(user_tools, "approval_summary", return_value="does things"):
            task = runtime.start("install my tool", str(self.ws), background=False)
            self.assertFalse([c for c, _ in ran if "install" in str(c)])  # asked, never run
            self.assertTrue(runtime.respond(task.id, approve=True, channel="cli", background=False)["ok"])
        installs = [env for c, env in ran if "omarchy-ai-tool install" in str(c)]
        self.assertEqual(len(installs), 1)
        self.assertEqual(installs[0].get(user_tools.APPROVED_ENV), "x")
