"""Worker backends (runtime/llm.py): Claude Code, then Codex, then the API;
and the browser failure diagnosis (execution/browser_inspect.py)."""
import json
import subprocess
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from omarchy_ai.runtime import llm
from omarchy_ai.runtime.llm import WorkerModel, WorkerModelError


def done(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def claude_says(result, is_error=False):
    return done(json.dumps({"type": "result", "is_error": is_error, "result": result}))


def codex_events(*items, error=None):
    lines = [json.dumps({"type": "thread.started"})]
    lines += [json.dumps({"type": "item.completed", "item": item}) for item in items]
    if error:
        lines.append(json.dumps({"type": "turn.failed", "error": {"message": error}}))
    return done("\n".join(lines))


class BackendTests(unittest.TestCase):
    def setUp(self):
        llm._cooldown.clear()
        patch("omarchy_ai.config.load_config",
              return_value=SimpleNamespace(task_worker_backends=["claude", "codex", "api"], task_agent_model="auto")).start()
        patch.object(llm, "_cli_ready", return_value=True).start()
        self.addCleanup(patch.stopall)
        self.api_calls = []

    def worker(self):
        wm = WorkerModel()
        wm._order = ["claude", "codex", "api"]

        def api(system, user, **kw):
            self.api_calls.append(user)
            return {"from": "api"}
        wm._api_complete = api
        wm._api_model = lambda: "s/small"
        return wm

    def test_claude_is_first_and_runs_with_no_tools(self):
        with patch.object(subprocess, "run", return_value=claude_says('{"action": "run", "commands": ["ss"]}')) as run:
            self.assertEqual(self.worker().complete("SYS", {"goal": "g"})["action"], "run")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["claude", "-p", "--tools", ""])
        self.assertIn("--safe-mode", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), {"goal": "g"})

    def test_escalation_moves_claude_to_opus(self):
        token = llm.TIER.set(1)
        try:
            with patch.object(subprocess, "run", return_value=claude_says("{}")) as run:
                wm = self.worker()
                wm.complete("SYS", "u")
        finally:
            llm.TIER.reset(token)
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(wm.last_used, "claude-code:opus")

    def test_claude_out_of_quota_falls_to_codex_and_sits_out(self):
        replies = [claude_says("Claude AI usage limit reached", is_error=True),
                   codex_events({"type": "agent_message", "text": '{"action": "finish"}'})]
        with patch.object(subprocess, "run", side_effect=replies):
            wm = self.worker()
            self.assertEqual(wm.complete("SYS", "u"), {"action": "finish"})
        self.assertEqual(wm.last_used, "codex:default")
        self.assertGreater(llm._cooldown["claude"], time.time() + 3000)
        self.assertEqual(wm.backends(), ["codex", "api"])

    def test_codex_acting_on_its_own_is_never_used(self):
        replies = [claude_says("not json at all"), claude_says("still not json"),
                   codex_events({"type": "command_execution", "command": "cat ~/.ssh/id_ed25519"},
                                {"type": "agent_message", "text": '{"action": "finish"}'})]
        with patch.object(subprocess, "run", side_effect=replies):
            self.assertEqual(self.worker().complete("SYS", "u"), {"from": "api"})
        self.assertEqual(len(self.api_calls), 1)

    def test_missing_clis_go_straight_to_the_api(self):
        with patch.object(llm, "_cli_ready", return_value=False), patch.object(subprocess, "run") as run:
            self.assertEqual(self.worker().complete("SYS", "u"), {"from": "api"})
        run.assert_not_called()

    def test_everything_failing_raises_with_every_reason(self):
        def broken(*a, **kw):
            raise WorkerModelError("gateway down")
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)):
            wm = self.worker()
            wm._api_complete = broken
            with self.assertRaises(WorkerModelError) as caught:
                wm.complete("SYS", "u")
        self.assertIn("claude:", str(caught.exception))
        self.assertIn("codex:", str(caught.exception))
        self.assertIn("gateway down", str(caught.exception))

    def test_a_pinned_model_or_injected_transport_is_api_only(self):
        self.assertEqual(WorkerModel("x/y", transport=lambda p, t: {}).backends(), ["api"])
        self.assertEqual(WorkerModel(transport=lambda p, t: {}).backends(), ["api"])


class BrowserDiagnosisTests(unittest.TestCase):
    # The tab from the real 2026-09-26 failure, as inspect() read it.
    SIGNED_OUT_GITHUB = {"url": "https://github.com/omacom/omarchy/issues?q=is%3Aissue+state%3Aopen",
                         "title": "Issues · omacom/omarchy · GitHub", "signed_in_user": "",
                         "sign_in_prompts": ["Sign in", "Sign up"], "password_field": False, "captcha": False,
                         "dialogs": [], "alerts": [], "controls": [{"label": "New issue", "disabled": False}]}

    def test_signed_out_site_is_named_as_the_cause(self):
        from omarchy_ai.execution.browser_inspect import diagnose
        [cause] = diagnose(self.SIGNED_OUT_GITHUB, "New issue")
        self.assertIn("not signed in to github.com", cause)
        self.assertIn("'New issue'", cause)
        self.assertIn("show_browser", cause)

    def test_signed_in_page_with_disabled_control_and_error(self):
        from omarchy_ai.execution.browser_inspect import diagnose
        state = dict(self.SIGNED_OUT_GITHUB, signed_in_user="omribenami", sign_in_prompts=[],
                     alerts=["Title can't be blank"], controls=[{"label": "Submit new issue", "disabled": True}])
        causes = diagnose(state, "Submit new issue")
        self.assertEqual(len(causes), 2)
        self.assertIn("Title can't be blank", causes[0])
        self.assertIn("disabled", causes[1])

    def test_failed_browser_task_result_carries_the_cause(self):
        from omarchy_ai.execution import actions
        from omarchy_ai.execution.actions import ActionResult
        stopped = ActionResult(False, "Browser task stopped after 10 actions: Stopped repeated interaction with New "
                                      "issue. The task tab has been left open; completion was not verified.")
        with patch("omarchy_ai.execution.browser_jev.run_browser_task", return_value=stopped), \
             patch("omarchy_ai.execution.browser_inspect.inspect", return_value=dict(self.SIGNED_OUT_GITHUB)):
            result = actions.browser_task({"goal": "open an issue"})
        self.assertFalse(result.ok)
        self.assertIn("not signed in to github.com", result.message)
        self.assertIn("'New issue'", result.message)

    def test_diagnosis_never_turns_into_a_new_failure(self):
        from omarchy_ai.execution.browser_inspect import explain_stop
        with patch("omarchy_ai.execution.browser_inspect.inspect", side_effect=OSError("browser gone")):
            self.assertEqual(explain_stop("x"), "")

    def test_tools_are_registered_and_inspection_is_read_only(self):
        from omarchy_ai.execution.actions import ACTIONS
        from omarchy_ai.execution.tools import TOOLS
        from omarchy_ai.voice.switchboard import READ_ONLY
        names = {t["name"] for t in TOOLS}
        self.assertTrue({"inspect_browser", "show_browser"} <= names & set(ACTIONS))
        self.assertIn("inspect_browser", READ_ONLY)
        self.assertNotIn("show_browser", READ_ONLY)


if __name__ == "__main__":
    unittest.main()
