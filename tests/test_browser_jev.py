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

        agent = browser_jev._agent_with_reused_tab(Agent, "https://www.heb.com", "add milk")

        self.assertIs(agent.browser, browser)
        self.assertEqual(agent.state["goal"], "add milk")
        browser.call.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
