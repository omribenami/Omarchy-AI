"""browser_control: steering the assistant browser by hand (tabs, scroll,
click, type), with Jev choosing only among real tabs and elements."""
import unittest
from unittest.mock import patch

from omarchy_ai.execution import browser_control as bc

GITHUB = {"n": 1, "id": "GH", "title": "Issues · omacom/omarchy · GitHub",
          "url": "https://github.com/omacom/omarchy/issues", "ws": "ws://gh"}
BLANK = {"n": 2, "id": "BL", "title": "about:blank", "url": "about:blank", "ws": "ws://bl"}


class FakeJev:
    def __init__(self, pick=None, p=0.95):
        self.pick, self.p, self.asked = pick, p, []

    def ask(self, state, questions, **kw):
        self.asked.append((state, questions))
        options = questions["target"]["criteria"]
        return {"target": {"choice": self.pick(options) if callable(self.pick) else self.pick, "p": self.p}}


class BrowserControlTests(unittest.TestCase):
    def setUp(self):
        self.pages = [dict(GITHUB), dict(BLANK)]
        self.http, self.cdp, self.fronted = [], [], []
        self.window_title = "about:blank"  # the real 2026-09-26 situation
        self.elements = [{"id": "e0", "text": "Sign in", "disabled": False},
                         {"id": "e1", "text": "New issue", "disabled": False},
                         {"id": "e2", "text": "Pull requests 2.6k", "disabled": False},
                         {"id": "e3", "text": "Submit new issue", "disabled": True}]
        self.fields = [{"id": "e0", "text": "Search Issues", "disabled": False},
                       {"id": "e1", "text": "Title", "disabled": False}]
        self.urls = iter([])

        def fake_eval(tab, expression):
            if expression.startswith(bc._COLLECT):
                return self.fields if '"field"' in expression[len(bc._COLLECT):] else self.elements
            if expression.startswith(bc._CENTER):
                return {"x": 10, "y": 20}
            if expression.startswith("[location.href"):
                return next(self.urls, "https://github.com/omacom/omarchy/pulls|Pulls|5")
            if expression.startswith("({title"):
                return {"title": tab["title"], "url": tab["url"], "ready": "complete"}
            if "scrollTo" in expression:
                self.cdp.append(("scroll", expression))
                return 50
            if "createTreeWalker" in expression:
                return None
            return None

        for name, value in (("tabs", lambda: [dict(p) for p in self.pages]), ("_eval", fake_eval),
                            ("_http", lambda path, method="GET": self.http.append((method, path)) or {"id": "NEW"}),
                            ("_run", lambda result: result), ("_session", lambda ws, calls: self.cdp.extend(calls) or []),
                            ("_front", lambda tab: self.fronted.append(tab["id"])),
                            ("_window_title", lambda: self.window_title)):
            patch.object(bc, name, value).start()
        patch("omarchy_ai.execution.browser_jev._ensure_dedicated_browser").start()
        patch("omarchy_ai.execution.browser_jev._TARGET_FILE").start().read_text.return_value = "GH"
        patch.object(bc.time, "sleep").start()
        self.addCleanup(patch.stopall)

    def run_action(self, jev=None, **args):
        return bc.browser_control(args, jev=jev)

    def test_switching_by_description_uses_jev_over_the_real_tabs_and_closes_blank(self):
        jev = FakeJev(pick=lambda options: next(k for k, v in options.items() if "GitHub" in v))
        result = self.run_action(jev, action="switch_tab", tab="the github one")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.fronted, ["GH"])
        self.assertIn(("GET", "/json/close/BL"), self.http)
        self.assertIn("closed 1 blank tab", result.message)
        self.assertEqual(set(jev.asked[0][1]["target"]["criteria"]), {"t1", "t2"})

    def test_switching_by_number_or_exact_title_needs_no_jev(self):
        self.assertTrue(self.run_action(FakeJev(pick="nope"), action="switch_tab", tab="1").ok)
        self.assertTrue(self.run_action(None, action="switch_tab", tab="about:blank").ok)
        self.assertEqual(self.fronted, ["GH", "BL"])

    def test_unsure_jev_lists_the_tabs_instead_of_guessing(self):
        result = self.run_action(FakeJev(pick="t1", p=0.2), action="switch_tab", tab="my bank")
        self.assertFalse(result.ok)
        self.assertIn("1. Issues", result.message)
        self.assertEqual(self.fronted, [])

    def test_actions_run_on_the_tab_the_window_shows(self):
        self.run_action(action="scroll", direction="down")
        self.assertEqual(self.fronted, ["BL"], "the window title named about:blank as the front tab")
        self.window_title = "Issues · omacom/omarchy · GitHub"
        self.run_action(action="scroll", direction="down")
        self.assertEqual(self.fronted[-1], "GH")
        self.window_title = None  # window not found: fall back to the task tab
        self.run_action(action="scroll", direction="top")
        self.assertEqual(self.fronted[-1], "GH")

    def test_click_is_a_real_mouse_click_on_the_matched_element(self):
        self.window_title = GITHUB["title"]
        self.urls = iter(["https://github.com/omacom/omarchy/issues|Issues|5",
                          "https://github.com/omacom/omarchy/pulls|Pulls|9"])
        result = self.run_action(action="click", target="pull requests")
        self.assertTrue(result.ok, result.message)
        self.assertIn("clicked 'Pull requests 2.6k' (match)", result.message)
        self.assertIn("the page changed", result.message)
        kinds = [params["type"] for method, params in self.cdp if method == "Input.dispatchMouseEvent"]
        self.assertEqual(kinds, ["mouseMoved", "mousePressed", "mouseReleased"])

    def test_click_by_description_goes_through_jev(self):
        self.window_title = GITHUB["title"]
        jev = FakeJev(pick="e1")
        result = self.run_action(jev, action="click", target="the button to make a new issue")
        self.assertIn("'New issue' (Jev p=0.95)", result.message)
        self.assertEqual(set(jev.asked[0][1]["target"]["criteria"]), {"e0", "e1", "e2", "e3"})

    def test_click_that_changes_nothing_says_so(self):
        self.window_title = GITHUB["title"]
        same = "https://github.com/omacom/omarchy/issues|Issues|5"
        self.urls = iter([same] * 20)
        result = self.run_action(action="click", target="New issue")
        self.assertIn("NO visible change", result.message)

    def test_disabled_and_missing_targets_are_reported_not_clicked(self):
        self.window_title = GITHUB["title"]
        disabled = self.run_action(action="click", target="Submit new issue")
        self.assertFalse(disabled.ok)
        self.assertIn("disabled", disabled.message)
        missing = self.run_action(FakeJev(pick="e0", p=0.1), action="click", target="flux capacitor")
        self.assertFalse(missing.ok)
        self.assertIn("'New issue'", missing.message, "lists what is there")
        self.assertFalse([c for c in self.cdp if c[0] == "Input.dispatchMouseEvent"])

    def test_type_into_a_field_and_submit(self):
        self.window_title = GITHUB["title"]
        result = self.run_action(action="type", target="search", text="iwlwifi", submit=True)
        self.assertTrue(result.ok, result.message)
        self.assertIn("'Search Issues'", result.message)
        methods = [m for m, _ in self.cdp]
        self.assertIn("Input.insertText", methods)
        self.assertEqual(methods[-2:], ["Input.dispatchKeyEvent", "Input.dispatchKeyEvent"])

    def test_open_only_web_pages(self):
        self.assertFalse(self.run_action(action="open", url="file:///etc/passwd").ok)
        self.assertTrue(self.run_action(action="open", url="github.com").ok)
        self.assertIn(("PUT", "/json/new?https://github.com"), self.http)

    def test_blank_tab_is_kept_when_it_is_the_only_page(self):
        self.pages = [dict(BLANK)]
        self.assertEqual(bc.tidy_blank_tabs(), 0)
        self.assertEqual(self.http, [])

    def test_bad_arguments(self):
        self.assertFalse(self.run_action(action="teleport").ok)
        self.assertFalse(self.run_action(action="scroll", direction="sideways").ok)
        self.assertFalse(self.run_action(action="scroll", to="not on the page").ok)
        self.assertFalse(self.run_action(action="click").ok)

    def test_registered_as_an_acting_tool(self):
        from omarchy_ai.execution.actions import ACTIONS
        from omarchy_ai.execution.tools import TOOLS
        from omarchy_ai.voice.switchboard import READ_ONLY
        tool = next(t for t in TOOLS if t["name"] == "browser_control")
        self.assertIn("switch_tab", tool["parameters"]["properties"]["action"]["enum"])
        self.assertIn("browser_control", ACTIONS)
        self.assertNotIn("browser_control", READ_ONLY)


if __name__ == "__main__":
    unittest.main()
