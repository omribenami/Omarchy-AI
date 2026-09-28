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


if __name__ == "__main__":
    unittest.main()
