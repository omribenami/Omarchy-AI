"""A coding agent the user names does the work (real case 2026-09-27 15:45:
"open codex --yolo and ask him as follows: ..." ran on qwen, then grok)."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from omarchy_ai.runtime import service
from omarchy_ai.runtime.executors.base import DONE, Assignment, Report
from omarchy_ai.runtime.executors.coding_agents import ClaudeCode, Codex
from omarchy_ai.voice.switchboard import Context, Switchboard

from test_task_runtime import RuntimeHarness, ScriptedJev, ScriptExecutor

SAID = "Open a terminal and cd to Git/Omarchy-AI, in it open codex --yolo and ask him as follows: fix the tests"


class PinnedRuntimeTests(RuntimeHarness):
    def executors(self):
        done = lambda a, ctx: Report(DONE, claim="done")  # noqa: E731
        return [ScriptExecutor("SYSTEM_AGENT", done), ScriptExecutor("CODEX", done, kind="external")]

    def test_named_agent_does_every_step_even_when_jev_prefers_another(self):
        system, codex = self.executors()
        jev = ScriptedJev(route="SYSTEM_AGENT", directives=["CONTINUE", "CHANGE_EXECUTOR"], executor="SYSTEM_AGENT")
        runtime = self.runtime([system, codex], jev)
        task = runtime.start("fix the tests", str(self.ws), background=False, agent="codex", unsandboxed=True)
        task = self.store.load(task.id)
        self.assertEqual({s["executor"] for s in task.steps}, {"CODEX"})
        self.assertEqual(system.calls, [])
        self.assertEqual(codex.calls[0].role, "work")
        self.assertTrue(codex.calls[0].context.get("unsandboxed"))
        self.assertTrue(all("CHANGE_EXECUTOR" not in allowed for allowed in jev.seen_allowed))

    def test_moving_a_waiting_task_to_codex(self):
        from omarchy_ai.runtime.executors.base import NEEDS_USER
        asked = []

        def system(a, ctx):
            asked.append(1)
            return Report(NEEDS_USER, claim="install the key yourself", question="Run ssh-copy-id yourself?")
        sys_exec = ScriptExecutor("SYSTEM_AGENT", system)
        codex = ScriptExecutor("CODEX", lambda a, ctx: Report(DONE, claim="key installed"), kind="external")
        runtime = self.runtime([sys_exec, codex], ScriptedJev(route="SYSTEM_AGENT"))
        task = runtime.start("set up HA", str(self.ws), background=False)
        self.assertEqual(self.store.load(task.id).status, "waiting_user")
        result = runtime.reassign(task.id, "codex", unsandboxed=True, background=False)
        self.assertTrue(result["ok"], result)
        task = self.store.load(task.id)
        self.assertEqual((task.agent, task.unsandboxed, task.question), ("CODEX", True, None))
        self.assertEqual(task.steps[-1]["executor"], "CODEX")
        self.assertIn("ssh-copy-id", codex.calls[0].context["previous_worker_was_waiting_on"])
        self.assertTrue(codex.calls[0].context["unsandboxed"])


class Agent(ScriptExecutor):
    def __init__(self, name, fn, ok=True):
        super().__init__(name, fn, kind="external" if name in ("CODEX", "CLAUDE_CODE") else "internal")
        self.ok = ok

    def available(self):
        return (self.ok, "" if self.ok else "usage limit reached")


class CodingFirstTests(RuntimeHarness):
    """The user's rule: Codex or Claude Code by default; the paid API worker
    only when neither can run, and only after the user approves."""

    def setUp(self):
        super().setUp()
        self.config = {}  # the real policy (task_api_worker: fallback)
        self.done = lambda a, ctx: Report(DONE, claim="done")  # noqa: E731

    def test_api_worker_is_never_chosen_while_a_coding_agent_can_run(self):
        system, claude = Agent("SYSTEM_AGENT", self.done), Agent("CLAUDE_CODE", self.done)
        runtime = self.runtime([system, claude], ScriptedJev(route="SYSTEM_AGENT", executor="SYSTEM_AGENT"))
        task = self.store.load(runtime.start("fix it", str(self.ws), background=False).id)
        self.assertEqual(system.calls, [])
        self.assertEqual({s["executor"] for s in task.steps}, {"CLAUDE_CODE"})

    def test_both_out_of_quota_asks_before_using_the_api(self):
        system = Agent("SYSTEM_AGENT", self.done)
        runtime = self.runtime([system, Agent("CODEX", self.done, ok=False), Agent("CLAUDE_CODE", self.done, ok=False)],
                               ScriptedJev(route="SYSTEM_AGENT"))
        task = self.store.load(runtime.start("fix it", str(self.ws), background=False).id)
        self.assertEqual((task.status, task.pending_approval["kind"]), ("waiting_approval", "api"))
        self.assertIn("usage limit reached", " ".join(task.pending_approval["reasons"]))
        self.assertEqual(system.calls, [])
        self.assertTrue(runtime.respond(task.id, approve=True, channel="voice", background=False)["ok"])
        self.assertEqual(len(system.calls), 1)

    def test_named_agent_out_of_quota_hands_over_to_the_other(self):
        codex, claude = Agent("CODEX", self.done, ok=False), Agent("CLAUDE_CODE", self.done)
        runtime = self.runtime([Agent("SYSTEM_AGENT", self.done), codex, claude], ScriptedJev())
        task = self.store.load(runtime.start("fix it", str(self.ws), background=False, agent="codex").id)
        self.assertEqual(task.agent, "CLAUDE_CODE")
        self.assertEqual(len(claude.calls), 1)
        self.assertTrue(any("CODEX cannot run" in n for n in task.notes))

    def test_usage_limit_marks_the_agent_exhausted(self):
        from omarchy_ai.runtime.executors import coding_agents
        self.addCleanup(coding_agents._EXHAUSTED.clear)
        until = coding_agents.exhausted_until("CODEX", "ERROR: You've hit your usage limit. Try again in 2 hours 5 minutes.")
        self.assertAlmostEqual(until - __import__("time").time(), 2 * 3600 + 300, delta=5)
        with patch.object(Codex, "detect", return_value={"authenticated": True, "reason": ""}):
            ok, reason = Codex().available()
        self.assertFalse(ok)
        self.assertIn("usage limit", reason)
        self.assertGreater(coding_agents.exhausted_until("CLAUDE_CODE", "Claude AI usage limit reached|9999999999"), 9e9)


class UnsandboxedAgentTests(unittest.TestCase):
    def test_yolo_flags(self):
        a = Assignment("t", "work", "g", "i", "/tmp")
        codex = Codex()
        argv = codex.unsandbox(codex.command(a, True, Path("/tmp"))[0])
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertNotIn("-s", argv)
        claude = ClaudeCode()
        argv = claude.unsandbox(claude.command(a, True, Path("/tmp"))[0])
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("--allowedTools", argv)

    def test_yolo_needs_a_high_approval(self):
        asked = []

        class Ctx:
            def check_external(self, kind, subject, risk, reasons):
                asked.append((subject, risk.name, reasons))
                return {"decision": "ask", "request": {"subject": subject}}

        with patch("omarchy_ai.runtime.executors.coding_agents.coding_agent_risk", return_value=(
                __import__("omarchy_ai.runtime.permissions", fromlist=["Risk"]).Risk.NORMAL, [])):
            report = Codex().run(Assignment("t", "work", "g", "i", "/tmp", context={"unsandboxed": True},
                                            write_access=True), Ctx())
        self.assertEqual(report.status, "needs_approval")
        self.assertEqual(asked[0][1], "HIGH")
        self.assertIn(":unsandboxed:", asked[0][0])


class VoiceAgentTests(unittest.TestCase):
    def test_detects_the_named_agent(self):
        self.assertEqual(service.named_agent(SAID), ("codex", True))
        self.assertEqual(service.named_agent("have Claude Code review it"), ("claude_code", False))
        self.assertEqual(service.named_agent("find a jev based smart home solution"), ("", False))

    def test_switchboard_rejects_a_task_that_drops_the_named_agent(self):
        jev = type("J", (), {"ask": lambda *a, **k: self.fail("code decides this, not Jev")})()
        ctx = Context(request=SAID, earlier=[], hint=None, calls=[])
        verdict = Switchboard(jev).review("start_task", {"goal": "fix the tests"}, ctx)
        self.assertEqual(verdict.action, "reject")
        self.assertIn("agent='codex'", verdict.message)
        self.assertIn("unsandboxed=true", verdict.message)

    def test_escalation_goal_quoting_the_user_gets_the_agent(self):
        started = {}

        class Runtime:
            def start(self, goal, workspace, **kw):
                started.update(kw)
                raise ValueError("stop here")

        with patch.object(service, "get_runtime", return_value=Runtime()), \
                patch.object(service, "_same_job", return_value=None):
            service.start_task({"goal": f"Escalated from the voice assistant.\n\nWhat the user said:\n- {SAID}"})
        self.assertEqual((started["agent"], started["unsandboxed"]), ("CODEX", True))


if __name__ == "__main__":
    unittest.main()


class CodingAgentVerificationTests(CodingFirstTests.__mro__[1]):
    """With the API test agent reserved for fallback, a coding agent designs
    the checks the harness runs (2026-09-27 20:29: a correct Claude Code
    answer ended "failed": no independent evidence was possible)."""

    def setUp(self):
        super().setUp()
        self.config = {}

    def test_tests_are_designed_by_a_coding_agent_and_run_by_the_harness(self):
        from omarchy_ai.runtime.executors.base import TEST

        def claude(a, ctx):
            if a.role == TEST:
                return Report(DONE, claim="checks", test_commands=["test -d ."])
            return Report(DONE, claim="done")
        agent = Agent("CLAUDE_CODE", claude)
        jev = ScriptedJev(directives=["REQUEST_MORE_TESTS", "RUN_TESTS", "CERTIFY"])
        runtime = self.runtime([Agent("SYSTEM_AGENT", lambda a, c: self.fail("API worker used")),
                                Agent("TEST_AGENT", lambda a, c: self.fail("API test agent used")), agent], jev)
        task = self.store.load(runtime.start("check it", str(self.ws), background=False).id)
        self.assertIn(TEST, [c.role for c in agent.calls])
        self.assertTrue(any(e["source"] == "harness" and e["ok"] for e in task.evidence))
        self.assertEqual(task.status, "certified")

    def test_parses_test_commands(self):
        text = "I checked.\nTEST_COMMAND: `find src -name '*.py' | wc -l | grep -qx 94`\nTEST_COMMAND: test -f README.md"
        from omarchy_ai.runtime.executors import coding_agents
        self.assertEqual(coding_agents._TEST_COMMAND.findall(text),
                         ["find src -name '*.py' | wc -l | grep -qx 94", "test -f README.md"])
