import asyncio
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omarchy_ai.core import quota


# Real error text from this machine's logs (see core/quota.py).
OPENAI = ('{"error": {"message": "You have no credits remaining. Add credits to continue using the API at '
          'https://platform.openai.com/settings/organization/billing/.", "type": "insufficient_quota", '
          '"code": "credit_balance_exhausted"}}')
GEMINI = "1011 None. Resource has been exhausted (e.g. check quota)."


class RecognitionTests(unittest.TestCase):
    def test_real_provider_errors_are_quota(self):
        self.assertTrue(quota.is_quota_error(OPENAI, 429))
        self.assertTrue(quota.is_quota_error(GEMINI))
        self.assertTrue(quota.is_quota_error("{}", 402))
        self.assertTrue(quota.is_quota_error('{"error":{"type":"insufficient_funds"}}', 403))

    def test_outages_and_rate_limits_are_not(self):
        # A real Gateway 503 from 2026-09-24, retried elsewhere; must not cry wolf.
        self.assertFalse(quota.is_quota_error('{"error":{"message":"Service temporarily unavailable. Please try '
                                              'again shortly.","type":"service_unavailable_error"}}', 503))
        self.assertFalse(quota.is_quota_error('{"error":{"message":"Rate limit reached, slow down"}}', 429))
        self.assertFalse(quota.is_quota_error("Unauthorized", 401))


class AlertTests(unittest.TestCase):
    def setUp(self):
        quota._last.clear()
        self.addCleanup(quota._last.clear)
        self.runs = []
        p = patch.object(quota, "_run", side_effect=lambda argv, timeout=5: self.runs.append(argv))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(setattr, quota, "speaker", None)

    def alert_now(self, provider, **kw):
        """report() without the thread, so the steps can be checked."""
        with patch.object(quota.threading, "Thread", lambda target, args, **_: SimpleNamespace(
                start=lambda: target(*args))):
            return quota.report(provider, "detail", **kw)

    def test_screen_notification_and_clip(self):
        with patch.object(quota, "language", return_value="he"):
            self.assertTrue(self.alert_now("openai"))
        tools = [argv[0] for argv in self.runs]
        self.assertEqual(tools, ["omarchy-shell", "notify-send", "pw-play"])
        self.assertEqual(json.loads(self.runs[0][-1]), {"provider": "OpenAI", "message": "credits used up"})
        self.assertIn("-u", self.runs[1])
        self.assertTrue(self.runs[2][1].endswith("quota-openai-he.ogg"))

    def test_every_shipped_clip_exists(self):
        for provider, lang in quota.MESSAGES:
            self.assertTrue((quota.CLIP_DIR / f"quota-{provider}-{lang}.ogg").exists(), (provider, lang))

    def test_an_open_conversation_on_another_provider_says_it_instead_of_the_clip(self):
        said = []
        quota.speaker = lambda provider, text: said.append((provider, text)) or provider != "gemini"
        with patch.object(quota, "language", return_value="en"):
            self.alert_now("vercel")
        self.assertEqual(said[0][0], "vercel")
        self.assertIn("Vercel AI Gateway credits used up", said[0][1])
        self.assertNotIn("pw-play", [argv[0] for argv in self.runs])
        self.runs.clear()
        with patch.object(quota, "language", return_value="en"):
            self.alert_now("gemini")       # the conversation's own provider is out: clip
        self.assertIn("pw-play", [argv[0] for argv in self.runs])

    def test_background_alerts_are_rate_limited_but_a_failed_wake_always_answers(self):
        self.assertTrue(self.alert_now("vercel"))
        self.assertFalse(self.alert_now("vercel"))
        quota._last["openai"] = quota.time.monotonic() - quota.USER_DEBOUNCE - 1
        self.assertTrue(self.alert_now("openai", user_initiated=True))
        self.assertFalse(self.alert_now("openai"))      # background repeat right after: quiet

    def test_unknown_provider_is_ignored(self):
        self.assertFalse(quota.report("anthropic"))


class LanguageTests(unittest.TestCase):
    def history(self, *user_texts):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name)
        (path / "conversation_history.jsonl").write_text(
            json.dumps({"turns": [{"role": "user", "text": t} for t in user_texts]}) + "\n")
        return patch("omarchy_ai.config.STATE_DIR", path)

    def test_hebrew_speaker_gets_the_hebrew_clip(self):
        with self.history("תעבירי ל-Workspace 5", "תודה"):
            self.assertEqual(quota.language(), "he")
        with self.history("switch to workspace 4"):
            self.assertEqual(quota.language(), "en")

    def test_no_history_is_english(self):
        with patch("omarchy_ai.config.STATE_DIR", Path("/nonexistent")):
            self.assertEqual(quota.language(), "en")


class HookTests(unittest.TestCase):
    def test_gateway_credit_error_alerts_vercel_and_still_raises(self):
        from omarchy_ai.voice.omarchy import GatewayClient, GatewayError
        client = GatewayClient.__new__(GatewayClient)
        client.key = "k"
        error = urllib.error.HTTPError("u", 402, "Payment Required", {}, io.BytesIO(b'{"error":"insufficient_funds"}'))
        with patch("urllib.request.urlopen", side_effect=error), patch.object(quota, "report") as report:
            with self.assertRaises(GatewayError):
                client._request("/chat/completions", b"{}", "application/json")
        report.assert_called_once()
        self.assertEqual(report.call_args.args[0], "vercel")

    def test_gateway_outage_does_not_alert(self):
        from omarchy_ai.voice.omarchy import GatewayClient, GatewayError
        client = GatewayClient.__new__(GatewayClient)
        client.key = "k"
        error = urllib.error.HTTPError("u", 503, "x", {}, io.BytesIO(b'{"error":{"message":"Service temporarily unavailable"}}'))
        with patch("urllib.request.urlopen", side_effect=error), patch.object(quota, "report") as report:
            with self.assertRaises(GatewayError):
                client._request("/chat/completions", b"{}", "application/json")
        report.assert_not_called()

    def test_gemini_notice_is_queued_on_the_conversation_loop(self):
        from omarchy_ai.config import Config
        from omarchy_ai.voice.gemini_live import GeminiLiveSession
        with patch("omarchy_ai.voice.gemini_live.EchoCancellation"):
            s = GeminiLiveSession(Config())
        self.assertFalse(s.notice("x"))       # no loop yet

        async def run():
            s._loop = asyncio.get_running_loop()
            import threading
            threading.Thread(target=s.notice, args=("Vercel AI Gateway credits used up.",)).start()
            return await asyncio.wait_for(s._announcements.get(), 2)
        self.assertEqual(asyncio.run(run()), {"notice": "Vercel AI Gateway credits used up."})


if __name__ == "__main__":
    unittest.main()
