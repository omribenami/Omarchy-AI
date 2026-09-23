import unittest
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


if __name__ == "__main__":
    unittest.main()
