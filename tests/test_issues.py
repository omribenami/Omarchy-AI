import json
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from omarchy_ai.core import issues
from omarchy_ai.execution import actions


class _Response:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self, _n=None):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class IssuesTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.token_path = Path(self.temp.name) / "github-issue-token"
        self.patch = patch.object(issues, "TOKEN_PATH", self.token_path)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_no_token_configured_does_not_attempt_a_request(self):
        self.assertFalse(issues.has_token())
        with patch.object(issues.urllib.request, "urlopen") as urlopen:
            ok, message = issues.file_issue("title", "body")
        urlopen.assert_not_called()
        self.assertFalse(ok)
        self.assertIn("no GitHub", message)

    def test_successful_filing_returns_the_issue_url(self):
        self.token_path.write_text("ghp_faketoken\n")
        self.assertTrue(issues.has_token())
        with patch.object(issues.urllib.request, "urlopen",
                           return_value=_Response({"html_url": "https://github.com/omribenami/Omarchy-AI/issues/1"})) as urlopen:
            ok, message = issues.file_issue("A bug", "Steps to reproduce...")
        self.assertTrue(ok)
        self.assertEqual(message, "https://github.com/omribenami/Omarchy-AI/issues/1")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer ghp_faketoken")
        body = json.loads(request.data)
        self.assertEqual(body["title"], "A bug")

    def test_rejected_token_reports_a_clear_reason_not_a_traceback(self):
        self.token_path.write_text("ghp_bad\n")
        error = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
        with patch.object(issues.urllib.request, "urlopen", side_effect=error):
            with patch.object(error, "read", return_value=b'{"message": "Bad credentials"}'):
                ok, message = issues.file_issue("title", "body")
        self.assertFalse(ok)
        self.assertIn("rejected", message)

    def test_network_failure_is_reported_not_raised(self):
        self.token_path.write_text("ghp_ok\n")
        with patch.object(issues.urllib.request, "urlopen", side_effect=OSError("no route")):
            ok, message = issues.file_issue("title", "body")
        self.assertFalse(ok)
        self.assertIn("could not reach GitHub", message)


class ReportIssueActionTests(unittest.TestCase):
    def test_requires_title_and_description(self):
        result = actions.report_issue({"title": "", "description": ""})
        self.assertFalse(result.ok)

    def test_delegates_to_issues_module_and_surfaces_the_url(self):
        with patch("omarchy_ai.core.issues.file_issue", return_value=(True, "https://github.com/x/y/issues/9")):
            result = actions.report_issue({"title": "Bug", "description": "It broke"})
        self.assertTrue(result.ok)
        self.assertIn("https://github.com/x/y/issues/9", result.message)

    def test_failure_is_reported_as_not_filed(self):
        with patch("omarchy_ai.core.issues.file_issue", return_value=(False, "no GitHub issue token configured on this machine")):
            result = actions.report_issue({"title": "Bug", "description": "It broke"})
        self.assertFalse(result.ok)
        self.assertIn("NOT filed", result.message)


if __name__ == "__main__":
    unittest.main()
