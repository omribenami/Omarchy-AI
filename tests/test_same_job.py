"""One job, one task: a repeat request or a failure streak joins the task
already doing it instead of starting another (2026-09-27 10:40-10:49: four
escalations of one README video job ran at once, plus one waiting for
approval), and she can correct a task that is going the wrong way."""
import json
from unittest.mock import patch

from omarchy_ai.core.jev import JevError
from omarchy_ai.runtime import service
from omarchy_ai.runtime.task import Task
from omarchy_ai.voice import escalation

from test_task_runtime import RuntimeHarness, ScriptedJev


class JobJev:
    def __init__(self, pick=None, p=0.95, fail=False):
        self.pick, self.p, self.fail, self.asked = pick, p, fail, []

    def ask(self, state, questions, **kw):
        self.asked.append((state, questions))
        if self.fail:
            raise JevError("down")
        options = list(questions["job"]["criteria"])
        pick = self.pick or "new"
        return {"job": {"choice": pick, "p": self.p, "probabilities": {o: (self.p if o == pick else 0) for o in options}}}


ESCALATED = ("Escalated from the voice assistant (2 attempts failed). Finish what the user asked, properly.\n\n"
             "What the user said, oldest first. This is speech recognition: ...:\n"
             "- replace the demo video in the omarchy-ai README with the new recording\n\n"
             "What the voice assistant tried, which failed:\n- browser_task(...) -> stopped")


class SameJobTests(RuntimeHarness):
    def setUp(self):
        super().setUp()
        self.rt = self.runtime([], ScriptedJev())
        self.task = Task(id="t-video", goal=ESCALATED, workspace=str(self.ws), status="running", source="voice")
        self.task.next_dispatch = {"executor": "SYSTEM_AGENT", "context": {}}
        self.store.save(self.task)
        patch.object(service, "get_runtime", return_value=self.rt).start()
        patch("omarchy_ai.config.load_config").start()
        self.addCleanup(patch.stopall)

    def start(self, goal, jev):
        with patch("omarchy_ai.core.jev.Jev", return_value=jev), \
                patch.object(self.rt, "start", side_effect=AssertionError("a duplicate task was started")) as new:
            return json.loads(service.start_task({"goal": goal}).message), new

    def test_the_same_job_joins_the_running_task(self):
        jev = JobJev(pick="t-video")
        result, _ = self.start("the video is still the old one, replace it with the new recording", jev)
        self.assertEqual((result["task_id"], result["existing"]), ("t-video", True))
        self.assertIn("NOT started again", result["note"])
        saved = self.store.load("t-video")
        self.assertIn("still the old one", saved.answers[-1])
        self.assertIn("still the old one", saved.next_dispatch["context"]["user_update"])
        # Jev compares what the user wants, not the escalation boilerplate.
        [(state, questions)] = jev.asked
        self.assertIn("replace the demo video", questions["job"]["criteria"]["t-video"])
        self.assertNotIn("Escalated from", questions["job"]["criteria"]["t-video"])

    def test_a_different_job_starts_its_own_task(self):
        with patch("omarchy_ai.core.jev.Jev", return_value=JobJev(pick="new")), \
                patch.object(self.rt, "start", return_value=Task(id="t-new", goal="g", workspace="/")) as new:
            result = json.loads(service.start_task({"goal": "why is bluetooth dropping"}).message)
        new.assert_called_once()
        self.assertEqual(result["task_id"], "t-new")

    def test_unsure_or_unavailable_jev_starts_a_new_task(self):
        for jev in (JobJev(pick="t-video", p=0.4), JobJev(fail=True)):
            with patch("omarchy_ai.core.jev.Jev", return_value=jev), \
                    patch.object(self.rt, "start", return_value=Task(id="t-new", goal="g", workspace="/")) as new:
                service.start_task({"goal": "replace the video"})
            new.assert_called_once()

    def test_finished_and_cli_tasks_are_not_joined(self):
        for status, source in (("certified", "voice"), ("running", "cli")):
            self.task.status, self.task.source = status, source
            self.store.save(self.task)
            self.assertIsNone(service._same_job(self.rt, "replace the video", jev=JobJev(pick="t-video")))

    def test_guidance_corrects_a_running_task(self):
        result = service.task_respond({"task_id": "t-video", "guidance": "use github_upload_attachment, not git"})
        self.assertTrue(result.ok, result.message)
        self.assertIn("github_upload_attachment", self.store.load("t-video").answers[-1])
        self.assertFalse(service.task_respond({"guidance": "x"}).ok, "needs a task id")

    def test_a_finished_task_cannot_be_steered(self):
        self.task.status = "cancelled"
        self.store.save(self.task)
        self.assertFalse(self.rt.steer("t-video", "x")["ok"])

    def test_escalation_says_it_joined_the_task(self):
        self.assertIn("already working on this", escalation.notice("t-video", existing=True))
        self.assertIn("handed it to a background task", escalation.notice("t-video"))


class GithubUploadInputTests(RuntimeHarness):
    def test_repo_is_read_from_a_slug_url_or_checkout(self):
        from omarchy_ai.execution import github_upload as g
        self.assertEqual(g.repo_slug("omribenami/Omarchy-AI"), "omribenami/Omarchy-AI")
        self.assertEqual(g.repo_slug("https://github.com/omribenami/Omarchy-AI.git"), "omribenami/Omarchy-AI")
        self.assertEqual(g.repo_slug("git@github.com:omribenami/Omarchy-AI.git"), "omribenami/Omarchy-AI")
        self.assertIsNone(g.repo_slug(""))

    def test_a_missing_file_is_refused_before_the_browser(self):
        from omarchy_ai.execution import github_upload as g
        with patch("omarchy_ai.execution.browser_jev._ensure_dedicated_browser") as browser:
            result = g.upload(str(self.ws / "nope.mp4"), "o/r")
        self.assertFalse(result.ok)
        browser.assert_not_called()

    def test_it_is_a_catalog_tool(self):
        from omarchy_ai.execution import catalog
        self.assertIn("github_upload_attachment", catalog.catalog())
