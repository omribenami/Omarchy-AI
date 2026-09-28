"""MyApi vault for programs, never the model (2026-09-28: she could not find
the Home Assistant token; a task borrowed the HA owner's session instead)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omarchy_ai.myapi import vault
from omarchy_ai.myapi.client import MyApiError

TOKENS = {"data": [
    {"id": "vt_a", "label": "home assistant", "service": "ha", "discoveredApiUrl": "https://ha.example",
     "tokenPreview": "abc…", "workspaceId": "ws_other"},
    {"id": "vt_b", "label": "ElevenLabs", "service": "elevenlabs", "tokenPreview": "sk_…", "workspaceId": "ws_mine"}]}
WORKSPACES = {"workspaces": [{"id": "ws_mine", "name": "MyApi WS"}, {"id": "ws_other", "name": "My Workspace"}]}


class FakeClient:
    def request(self, method, path, **kw):
        if path == "/vault/tokens":
            return TOKENS
        if path == "/workspaces":
            return WORKSPACES
        if path == "/vault/tokens/vt_b/reveal":
            return {"data": {"token": "value-for-b-123"}}
        raise MyApiError("Token not found")


class VaultTests(unittest.TestCase):
    def setUp(self):
        vault._cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        patcher = patch.object(vault, "_revealed_path", return_value=Path(self.tmp.name) / "revealed.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_labels_never_carry_values(self):
        items = vault.labels(FakeClient())
        self.assertEqual([i["name"] for i in items], ["home assistant", "ElevenLabs"])
        self.assertEqual(items[0]["workspace"], "My Workspace")
        self.assertFalse(any(k in i for i in items for k in ("token", "tokenPreview")))

    def test_get_returns_the_value_and_records_only_the_id(self):
        self.assertEqual(vault.get("elevenlabs", FakeClient()), "value-for-b-123")
        self.assertNotIn("value-for-b-123", vault._revealed_path().read_text())
        self.assertIn("vt_b", vault._revealed_path().read_text())

    def test_other_workspace_explains_what_to_do(self):
        with self.assertRaises(MyApiError) as ctx:
            vault.get("Home Assistant", FakeClient())
        self.assertIn("'My Workspace'", str(ctx.exception))
        self.assertIn("move it", str(ctx.exception))

    def test_unknown_name_lists_what_exists(self):
        with self.assertRaises(MyApiError) as ctx:
            vault.get("github", FakeClient())
        self.assertIn("ElevenLabs", str(ctx.exception))


from test_task_runtime import RuntimeHarness, ScriptedJev, ScriptExecutor  # noqa: E402
from omarchy_ai.runtime.executors.base import DONE, Report  # noqa: E402


class FailureMessageTests(RuntimeHarness):
    """2026-09-27 20:43: the user heard "failed. I could not complete this.
    Implemented and verified the connection" -- the worker's own claim."""

    def test_says_why_and_labels_the_worker_claim_unverified(self):
        worker = ScriptExecutor("SYSTEM_AGENT", lambda a, ctx: Report(DONE, claim="Implemented and verified it."))
        runtime = self.runtime([worker], ScriptedJev(directives=["FAIL"]))
        task = self.store.load(runtime.start("connect it", str(self.ws), background=False).id)
        self.assertEqual(task.status, "failed")
        self.assertTrue(task.result.startswith("I could not complete this: "))
        self.assertIn("the thing is done", task.result)            # what it had to achieve
        self.assertIn("NOT verified: Implemented and verified it.", task.result)


if __name__ == "__main__":
    unittest.main()
