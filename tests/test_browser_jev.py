import unittest
from unittest.mock import Mock

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


if __name__ == "__main__":
    unittest.main()
