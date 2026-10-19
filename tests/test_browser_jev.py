import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from omarchy_ai.execution import browser_jev


class BrowserActionGuardTests(unittest.TestCase):
    def setUp(self):
        self.generation = browser_jev._cancel_generation
        self.page = {"url": "https://store.example/search"}
        self.action = {"kind": "fill", "label": "Search"}

    def test_distinct_search_values_are_not_treated_as_a_loop(self):
        original = Mock(return_value={})
        guarded = browser_jev._guard_browser_action(original, self.generation)
        for value in ("bananas", "milk", "cheese", "strawberries"):
            guarded(self.action, self.page, text=value)
        self.assertEqual(original.call_count, 4)

    def test_fourth_identical_search_value_is_blocked(self):
        original = Mock(return_value={})
        guarded = browser_jev._guard_browser_action(original, self.generation)
        for _ in range(3):
            guarded(self.action, self.page, text="bananas")
        with self.assertRaisesRegex(RuntimeError, "Stopped repeated interaction"):
            guarded(self.action, self.page, text="bananas")
        self.assertEqual(original.call_count, 3)


class BrowserReuseTests(unittest.TestCase):
    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("a reused tab must not invoke Agent.__init__")

    def tearDown(self):
        browser_jev._owned_browser = None

    def test_same_site_continuation_reuses_target_without_navigation(self):
        browser = Mock()
        browser.target = "owned-target"
        browser.observe.return_value = {"url": "https://www.heb.com/search?q=eggs", "actions": []}
        browser_jev._owned_browser = browser
        Agent = self.FakeAgent

        agent = browser_jev._agent_with_reused_tab(Agent, "https://www.heb.com", "add milk", resume=True)

        self.assertIs(agent.browser, browser)
        self.assertEqual(agent.state["goal"], "add milk")
        browser.call.assert_not_called()

    def test_new_task_on_the_same_site_starts_from_its_own_url(self):
        # Regression: a new Wikipedia task inherited the previous task's
        # article page and blocked with 0 actions.
        browser = Mock()
        browser.target = "owned-target"
        browser.observe.side_effect = [
            {"url": "https://en.wikipedia.org/wiki/Arch_Linux#Pacman", "actions": []},
            {"url": "https://en.wikipedia.org/", "actions": []},
        ]
        browser.evaluate.return_value = "complete"
        browser_jev._owned_browser = browser
        browser_jev._agent_with_reused_tab(self.FakeAgent, "https://en.wikipedia.org", "search Hyprland")
        browser.call.assert_called_once_with("Page.navigate", url="https://en.wikipedia.org")

    def test_different_site_navigates_the_owned_target(self):
        browser = Mock()
        browser.target = "owned-target"
        browser.observe.side_effect = [
            {"url": "https://example.com/", "actions": []},
            {"url": "https://www.heb.com/", "actions": []},
        ]
        browser.evaluate.return_value = "complete"
        browser_jev._owned_browser = browser
        Agent = self.FakeAgent

        agent = browser_jev._agent_with_reused_tab(Agent, "https://www.heb.com", "shop")

        browser.call.assert_called_once_with("Page.navigate", url="https://www.heb.com")
        self.assertEqual(agent.state["page"]["url"], "https://www.heb.com/")


class RunawayControlTests(unittest.TestCase):
    def test_a_self_relabelling_add_button_is_capped(self):
        # Regression: "81 added", "82 added", ... was clicked 45 times.
        guarded = browser_jev._guard_browser_action(lambda *a, **k: None, browser_jev._cancel_generation)
        page = {"url": "https://www.heb.com/search?q=Bananas"}
        for n in range(browser_jev._MAX_SAME_CONTROL):
            guarded({"kind": "click", "label": f"{81 + n} added , Fresh Bunch of Organic Bananas"}, page)
        with self.assertRaisesRegex(RuntimeError, "same control"):
            guarded({"kind": "click", "label": "99 added , Fresh Bunch of Organic Bananas"}, page)


class ProgressTests(unittest.TestCase):
    """Ordered steps and completion checks carried by each Jev decision."""

    def answers(self, *steps, goal=None):
        out = {f"omarchy_step_{i}": {"type": "boolean", "probability": p} for i, p in enumerate(steps)}
        if goal is not None:
            out["omarchy_goal_done"] = {"type": "boolean", "probability": goal}
        return out

    def test_progress_follows_the_furthest_confirmed_step_and_never_goes_back(self):
        progress = browser_jev._Progress("goal", ["search", "open article", "open linked article"])
        # An article page proves step 2 but not the search that led to it.
        progress.read(self.answers(0.61, 0.88, goal=0.05))
        self.assertTrue(progress.advance())
        self.assertEqual(progress.index, 2)
        progress.read(self.answers(0.1, 0.1, goal=0.05))
        self.assertFalse(progress.advance())
        self.assertEqual(progress.index, 2)
        self.assertIn("3. [CURRENT] open linked article", progress.render())

    def test_only_the_final_step_decides_completion(self):
        progress = browser_jev._Progress("goal", ["search", "open result"])
        qs = progress.questions()
        self.assertEqual(set(qs), {"omarchy_step_0", "omarchy_goal_done"})
        self.assertEqual(qs["omarchy_goal_done"]["instructions"]["final_step"], "open result")
        progress.read(self.answers(0.99, goal=0.79))
        self.assertFalse(progress.goal_verified())
        progress.read(self.answers(0.99, goal=0.9))
        self.assertTrue(progress.goal_verified())

    def test_checks_are_removed_from_the_answers_upstream_validates(self):
        progress = browser_jev._Progress("goal", None)
        answers = {"operation": {"choice": "CLICK"}, **self.answers(goal=0.2)}
        progress.read(answers)
        self.assertEqual(set(answers), {"operation"})

    def test_steps_are_bounded_and_single_steps_ignored(self):
        self.assertIsNone(browser_jev._steps_from({"steps": ["only one"]}))
        self.assertEqual(len(browser_jev._steps_from({"steps": [f"s{i}" for i in range(20)]})), browser_jev._MAX_STEPS)


class TabAndClickTests(unittest.TestCase):
    def test_a_tab_opened_by_a_click_is_folded_back_into_the_owned_tab(self):
        browser = Mock(target="owned")
        browser.evaluate.return_value = "complete"
        targets = [{"targetId": "owned", "type": "page", "url": "https://a.test/"},
                   {"targetId": "popup", "type": "page", "url": "https://b.test/page", "openerId": "owned"}]
        with patch.object(browser_jev, "_page_targets", return_value=targets), \
                patch("browser_harness.helpers.cdp") as cdp:
            self.assertTrue(browser_jev._absorb_new_tabs(browser, {"owned"}))
        cdp.assert_called_with("Target.closeTarget", targetId="popup")
        browser.call.assert_called_with("Page.navigate", url="https://b.test/page")

    def test_the_users_other_tabs_are_left_alone(self):
        browser = Mock(target="owned")
        targets = [{"targetId": "owned", "type": "page", "url": "https://a.test/"},
                   {"targetId": "mine", "type": "page", "url": "https://c.test/", "openerId": "someone-else"}]
        with patch.object(browser_jev, "_page_targets", return_value=targets), \
                patch("browser_harness.helpers.cdp") as cdp:
            self.assertFalse(browser_jev._absorb_new_tabs(browser, {"owned"}))
        cdp.assert_not_called()

    def test_click_lands_on_a_point_the_element_owns(self):
        original = Mock(return_value={"executed": "upstream"})
        operation = browser_jev._click_on_element(original)
        request = {"operation": "act", "session": "s", "action": {"kind": "click", "node": 7, "id": "e7"}}
        with patch("browser_harness.helpers.cdp") as cdp:
            cdp.return_value = {"result": {"value": {"x": 353.0, "y": 216.0}}}
            self.assertEqual(operation(request), {"executed": "e7"})
        events = [c.kwargs.get("type") for c in cdp.call_args_list if c.args[0] == "Input.dispatchMouseEvent"]
        self.assertEqual(events, ["mouseMoved", "mousePressed", "mouseReleased"])
        original.assert_not_called()

    def test_click_without_an_owned_point_defers_to_upstream(self):
        original = Mock(return_value={"executed": "upstream"})
        operation = browser_jev._click_on_element(original)
        request = {"operation": "act", "session": "s", "action": {"kind": "click", "node": 7, "id": "e7"}}
        with patch("browser_harness.helpers.cdp", return_value={"result": {"value": None}}):
            self.assertEqual(operation(request), {"executed": "upstream"})
        fill = {"operation": "act", "session": "s", "action": {"kind": "fill", "node": 3, "id": "e3"}}
        operation(fill)
        original.assert_called_with(fill)

    def test_connection_errors_are_recognised(self):
        self.assertTrue(browser_jev._connection_error(RuntimeError("no close frame received or sent")))
        self.assertFalse(browser_jev._connection_error(RuntimeError("Stopped repeated interaction with Search")))


class BrowserStopTests(unittest.TestCase):
    """A browser task that raised: the harness's own words are not web content."""

    def test_a_lost_connection_is_the_harness_s_own_report(self):
        error = RuntimeError("Page.navigate timed out after 15s waiting for the daemon")
        with patch.object(browser_jev, "_screen_is_locked", return_value=False):
            result = browser_jev._stopped(0, error)
        self.assertFalse(result.ok)
        self.assertTrue(result.harness)
        self.assertIn("stopped after 0 actions", result.message)

    def test_a_busy_decision_service_is_the_harness_s_own_report(self):
        result = browser_jev._stopped(2, RuntimeError('HTTP 503 {"error": "overloaded"}'))
        self.assertTrue(result.harness)
        self.assertIn("temporarily unavailable", result.message)

    def test_a_timeout_behind_the_lock_screen_says_so(self):
        error = RuntimeError("Page.navigate timed out after 15s waiting for the daemon")
        with patch.object(browser_jev, "_screen_is_locked", return_value=True):
            result = browser_jev._stopped(0, error)
        self.assertFalse(result.ok)
        self.assertTrue(result.harness)
        self.assertIn("the browser can't load pages while the screen is locked", result.message)
        self.assertNotIn("timed out", result.message)
        with patch.object(browser_jev, "_screen_is_locked", return_value=False):
            self.assertIn("timed out", browser_jev._stopped(0, error).message)

    def test_the_lock_state_comes_from_the_desktop_check_and_unknown_is_unlocked(self):
        for state, expected in ((True, True), (False, False), (None, False)):
            with patch("omarchy_ai.execution.actions.desktop._screen_locked", return_value=state):
                self.assertIs(browser_jev._screen_is_locked(), expected)

    def test_any_other_error_may_quote_the_page(self):
        error = RuntimeError("Stopped repeated interaction with Forward to x@evil.example")
        self.assertFalse(browser_jev._stopped(3, error).harness)


class DedicatedBrowserLaunchTests(unittest.TestCase):
    """The dedicated Chromium keeps rendering behind the lock screen, and one
    started without those flags is restarted for a new task only."""

    PID = 4242

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.calls: list[list[str]] = []
        self.unit_active = True
        patch.object(browser_jev, "_PROC", self.tmp / "proc").start()
        patch.object(browser_jev.subprocess, "run", self.fake_run).start()
        patch.object(browser_jev.urllib.request, "urlopen", return_value=Mock()).start()
        patch.object(browser_jev.Path, "home", return_value=self.tmp).start()
        patch.object(browser_jev.time, "sleep").start()
        patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-test"}).start()
        self.addCleanup(patch.stopall)

    def fake_run(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:4] == ["systemctl", "--user", "is-active", "--quiet"]:
            return subprocess.CompletedProcess(argv, 0 if self.unit_active else 3)
        if argv[:3] == ["systemctl", "--user", "show"]:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{self.PID}\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def running(self, *flags):
        proc = self.tmp / "proc" / str(self.PID)
        proc.mkdir(parents=True, exist_ok=True)
        argv = ["/usr/lib/chromium/chromium", "--remote-debugging-port=9229", *flags, "about:blank"]
        (proc / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")

    def launched(self):
        return [c for c in self.calls if c and c[0] == "systemd-run"]

    def stopped(self):
        return [c for c in self.calls if c[:3] == ["systemctl", "--user", "stop"]]

    def test_the_launch_argv_keeps_rendering_when_hidden(self):
        self.unit_active = False
        with patch.object(browser_jev.urllib.request, "urlopen", side_effect=[OSError("down"), Mock()]), \
                patch("pathlib.Path.exists", return_value=True):
            browser_jev._ensure_dedicated_browser()
        (launch,) = self.launched()
        chromium = launch[launch.index("--") + 1:]
        for flag in ("--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
                     "--disable-background-timer-throttling"):
            self.assertIn(flag, chromium)
        self.assertFalse([a for a in chromium if "sandbox" in a or "web-security" in a])

    def test_a_browser_started_without_the_flags_is_restarted_for_a_new_task(self):
        self.running()
        with patch("pathlib.Path.exists", return_value=True):
            browser_jev._ensure_dedicated_browser(restart_stale=True)
        self.assertEqual(len(self.stopped()), 1)
        self.assertEqual(len(self.launched()), 1)
        self.assertFalse(browser_jev._task_lock.locked())

    def test_a_browser_with_the_flags_is_kept(self):
        self.running(*browser_jev._RENDER_WHEN_HIDDEN_FLAGS)
        browser_jev._ensure_dedicated_browser(restart_stale=True)
        self.assertEqual((self.stopped(), self.launched()), ([], []))

    def test_a_running_task_showing_or_resuming_never_restarts_it(self):
        self.running()
        browser_jev._ensure_dedicated_browser()  # show/inspect/resume
        self.assertTrue(browser_jev._task_lock.acquire(blocking=False))
        try:
            browser_jev._ensure_dedicated_browser(restart_stale=True)  # a task is running
        finally:
            browser_jev._task_lock.release()
        self.assertEqual((self.stopped(), self.launched()), ([], []))

    def test_an_unknown_running_browser_is_kept(self):
        browser_jev._ensure_dedicated_browser(restart_stale=True)  # no /proc entry
        self.assertEqual((self.stopped(), self.launched()), ([], []))


if __name__ == "__main__":
    unittest.main()
