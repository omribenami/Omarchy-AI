import unittest
from unittest.mock import patch

from omarchy_ai.myapi.client import MyApiClient, MyApiError


class MyApiClientErrorTests(unittest.TestCase):
    def _client(self):
        with patch("omarchy_ai.myapi.client._load_identity", return_value=None):
            return MyApiClient()

    def test_read_timeout_becomes_myapi_error(self):
        # Real session 2026-09-28 17:29: TimeoutError escaped during the
        # response read and the Gmail action reported "crashed".
        with patch("urllib.request.urlopen", side_effect=TimeoutError("The read operation timed out")):
            with self.assertRaisesRegex(MyApiError, "did not answer"):
                self._client()._raw_request("GET", "/services", signed=False)

    def test_connection_reset_becomes_myapi_error(self):
        with patch("urllib.request.urlopen", side_effect=ConnectionResetError(104, "Connection reset by peer")):
            with self.assertRaisesRegex(MyApiError, "connection to myapiai.com failed"):
                self._client()._raw_request("GET", "/services", signed=False)

    def test_injection_scanner_block_is_an_error_not_an_empty_answer(self):
        blocked = {"data": {"service": "gmail", "status": "executed", "response": {"ok": True, "data": {
            "ok": False, "code": "PROMPT_INJECTION_BLOCKED", "service": "gmail"}}}}
        client = self._client()
        with patch.object(client, "_raw_request", return_value=blocked):
            with self.assertRaisesRegex(MyApiError, "prompt-injection scanner withheld this gmail answer") as caught:
                client.request("POST", "/services/gmail/execute", body={})
        self.assertEqual(caught.exception.status, 422)          # permanent: core/retry.py does not retry it
        with patch.object(client, "_raw_request", return_value={"data": {"messages": []}}):
            self.assertEqual(client.request("GET", "/services"), {"data": {"messages": []}})


if __name__ == "__main__":
    unittest.main()
