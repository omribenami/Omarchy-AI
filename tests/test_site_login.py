import unittest
from unittest.mock import patch

from omarchy_ai.execution import site_login

TAB = {"id": "t", "ws": "ws://x", "url": "https://auth.example.com/log-in"}
FORM = {"url": "https://auth.example.com/log-in", "user": "user", "userValue": "", "pass": "pass", "code": None,
        "submit": "submit"}
DONE = {"url": "https://example.com/", "user": None, "pass": None, "code": None, "submit": None}


class SignInTests(unittest.TestCase):
    def run_sign_in(self, pages, args, saved=None, spoken=None, approve=(True, "approved")):
        typed, stored = [], []
        pages = list(pages)
        with patch.object(site_login, "_task_tab", return_value=TAB), \
                patch("omarchy_ai.execution.browser_control._eval",
                      side_effect=lambda tab, expr: pages.pop(0) if expr == site_login._FIND else None), \
                patch.object(site_login, "_type_into", side_effect=lambda tab, key, text: typed.append((key, text))), \
                patch.object(site_login, "_submit"), patch.object(site_login.time, "sleep"), \
                patch.object(site_login, "saved", return_value=saved), \
                patch.object(site_login, "save", side_effect=lambda *a: stored.append(a) or True), \
                patch("omarchy_ai.execution.passwords.spoken_password", return_value=spoken), \
                patch("omarchy_ai.execution.browser_inspect.inspect", return_value={"url": "https://example.com/"}):
            result = site_login.sign_in(args, approve=lambda site, user: approve)
        return result, typed, stored

    def test_without_a_login_it_asks_for_one_and_types_nothing(self):
        result, typed, _ = self.run_sign_in([FORM], {})
        self.assertFalse(result.ok)
        self.assertIn("No saved login for example.com", result.message)
        self.assertEqual(typed, [])

    def test_a_declined_fingerprint_types_nothing(self):
        secret = "pw-" + "x" * 8
        result, typed, _ = self.run_sign_in([FORM], {}, saved={"username": "me@example.com", "password": secret},
                                            approve=(False, "declined"))
        self.assertFalse(result.ok)
        self.assertEqual(typed, [])

    def test_a_given_login_is_typed_after_the_fingerprint_and_saved(self):
        secret = "pw-" + "y" * 8
        result, typed, stored = self.run_sign_in([FORM, DONE], {"username": "me@example.com",
                                                                "use_given_password": True}, spoken=secret)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(typed, [("user", "me@example.com"), ("pass", secret)])
        self.assertEqual(stored, [("example.com", "me@example.com", secret)])
        self.assertNotIn(secret, result.message)

    def test_a_form_that_moves_to_another_site_never_gets_the_password(self):
        secret = "pw-" + "z" * 8
        email_page = dict(FORM, **{"pass": None})
        elsewhere = dict(FORM, url="https://login.other.example/")
        _, typed, _ = self.run_sign_in([email_page, elsewhere], {},
                                       saved={"username": "me@example.com", "password": secret})
        self.assertNotIn(("pass", secret), typed)


if __name__ == "__main__":
    unittest.main()
