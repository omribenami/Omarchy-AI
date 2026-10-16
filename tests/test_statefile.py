"""core/statefile.py: state files the daemon and the CLIs change at once.

Before it, pending_actions.json had a thread lock only, learned_preferences
was written in place, and every loader turned a file it could not parse into
an empty one, which the next save made permanent."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from omarchy_ai.core import memory, pending_actions, statefile

SRC = str(Path(__file__).resolve().parents[1] / "src")

# Each worker adds its own keys and bumps one shared counter, so a lost
# update shows up as a missing key or a short count.
WORKER = textwrap.dedent("""
    import sys
    sys.path.insert(0, {src!r})
    from pathlib import Path
    from omarchy_ai.core import statefile
    path, name, count = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
    for i in range(count):
        with statefile.locked_json(path, {{}}, expect=dict) as doc:
            doc.data[f"{{name}}-{{i}}"] = i
            doc.data["count"] = doc.data.get("count", 0) + 1
""")


def _env(root: Path) -> dict:
    """Workers keep any state they might touch inside the test's directory."""
    return {**os.environ, "XDG_STATE_HOME": str(root), "XDG_CONFIG_HOME": str(root)}


def _run_workers(script: str, args_per_worker: list[list[str]], root: Path) -> None:
    procs = [
        subprocess.Popen([sys.executable, "-c", script, *args], env=_env(root))
        for args in args_per_worker
    ]
    for proc in procs:
        proc.wait(timeout=180)
        if proc.returncode != 0:
            raise AssertionError(f"worker exited {proc.returncode}")


class StatefileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_two_processes_interleaving_1000_writes_lose_no_update(self):
        path = self.root / "shared.json"
        _run_workers(
            WORKER.format(src=SRC),
            [[str(path), "a", "500"], [str(path), "b", "500"]],
            self.root,
        )
        data = json.loads(path.read_text())
        self.assertEqual(data["count"], 1000)
        self.assertEqual(sum(1 for k in data if k.startswith(("a-", "b-"))), 1000)
        self.assertEqual(
            [p.name for p in self.root.iterdir() if p.name.endswith(".tmp")], []
        )

    def test_a_corrupt_file_is_quarantined_and_reported_not_reset(self):
        path = self.root / "agenda.json"
        path.write_text('[{"id": "t-1", "title": "half a wri')
        with self.assertLogs("omarchy_ai.core.statefile", "WARNING"):
            self.assertEqual(statefile.read_json(path, [], expect=list), [])
        kept = statefile.corrupt_files(self.root)
        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0].name.startswith("agenda.json.corrupt-"))
        self.assertEqual(kept[0].read_text(), '[{"id": "t-1", "title": "half a wri')
        self.assertFalse(path.exists())

    def test_a_file_of_the_wrong_type_is_quarantined_before_a_write(self):
        path = self.root / "pending.json"
        path.write_text("[1, 2]")
        with self.assertLogs("omarchy_ai.core.statefile", "WARNING"):
            with statefile.locked_json(path, {}, expect=dict) as doc:
                self.assertEqual(doc.data, {})
                doc.data["x"] = 1
        self.assertEqual(json.loads(path.read_text()), {"x": 1})
        self.assertEqual(statefile.corrupt_files(self.root)[0].read_text(), "[1, 2]")

    def test_an_unchanged_document_is_not_rewritten(self):
        path = self.root / "doc.json"
        path.write_text('{"a":1}')  # legacy formatting stays as it is
        with statefile.locked_json(path, {}, expect=dict) as doc:
            self.assertEqual(doc.data, {"a": 1})
        self.assertEqual(path.read_text(), '{"a":1}')

    def test_a_block_that_raises_writes_nothing(self):
        path = self.root / "doc.json"
        path.write_text('{"a": 1}')
        with self.assertRaises(RuntimeError):
            with statefile.locked_json(path, {}) as doc:
                doc.data["a"] = 2
                raise RuntimeError
        self.assertEqual(json.loads(path.read_text()), {"a": 1})

    def test_the_lock_is_reentrant_in_one_thread(self):
        path = self.root / "doc.json"
        with statefile.locked(path):
            with statefile.locked_json(path, {}) as doc:
                doc.data["a"] = 1
        self.assertEqual(json.loads(path.read_text()), {"a": 1})

    def test_legacy_files_are_version_0_and_a_newer_one_is_refused(self):
        path = self.root / "health.json"
        path.write_text('{"x": 1}')
        with statefile.locked_json(path, {}, version=1) as doc:
            self.assertEqual(statefile.schema_version(doc.data), 0)
        with statefile.locked_json(path, {}, version=1) as doc:
            doc.data["x"] = 2
        self.assertEqual(json.loads(path.read_text()), {"x": 2, "schema_version": 1})
        path.write_text('{"x": 3, "schema_version": 2}')
        with self.assertRaises(statefile.StateFileError):
            with statefile.locked_json(path, {}, version=1):
                pass
        self.assertEqual(json.loads(path.read_text())["x"], 3)

    def test_an_existing_file_keeps_its_mode(self):
        path = self.root / "doc.json"
        path.write_text("{}")
        os.chmod(path, 0o600)
        with statefile.locked_json(path, {}) as doc:
            doc.data["a"] = 1
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_jsonl_lines_that_do_not_parse_are_kept_when_rewritten(self):
        path = self.root / "inbox.jsonl"
        path.write_text('{"id": "a"}\n{"id": "b", "tor\n')
        bad = []
        with statefile.locked(path):
            self.assertEqual(statefile.read_jsonl(path, bad), [{"id": "a"}])
            with self.assertLogs("omarchy_ai.core.statefile", "WARNING"):
                statefile.rewrite_jsonl(path, '{"id": "a"}\n', bad)
        self.assertEqual(path.read_text(), '{"id": "a"}\n')
        self.assertEqual(
            statefile.corrupt_files(self.root)[0].read_text(), '{"id": "b", "tor\n'
        )


class PendingActionsStateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "pending_actions.json"
        patcher = patch.object(pending_actions, "PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_existing_file_loads_and_keeps_its_shape(self):
        legacy = {
            "abc123": {
                "id": "abc123",
                "created": time.time(),
                "status": "waiting",
                "outcome": "",
                "service": "gmail",
            }
        }
        self.path.write_text(json.dumps(legacy, indent=1))
        self.assertEqual(pending_actions.claim("abc123")["status"], "running")
        on_disk = json.loads(self.path.read_text())
        self.assertEqual(set(on_disk), {"abc123"})
        self.assertEqual(on_disk["abc123"]["status"], "running")

    def test_only_one_of_two_processes_claims_an_action(self):
        aid = pending_actions.create({"service": "gmail"})
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {SRC!r})
            from pathlib import Path
            from omarchy_ai.core import pending_actions
            pending_actions.PATH = Path(sys.argv[1])
            print("won" if pending_actions.claim(sys.argv[2]) else "lost")
        """)
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.path), aid],
                stdout=subprocess.PIPE,
                text=True,
                env=_env(self.path.parent),
            )
            for _ in range(4)
        ]
        said = sorted(p.communicate(timeout=60)[0].strip() for p in procs)
        self.assertEqual(said, ["lost", "lost", "lost", "won"])

    def test_a_corrupt_file_is_kept_not_overwritten_by_a_new_action(self):
        self.path.write_text('{"abc": {"id": "abc", "stat')
        with self.assertLogs("omarchy_ai.core.statefile", "WARNING"):
            aid = pending_actions.create({"service": "gmail"})
        self.assertEqual(set(json.loads(self.path.read_text())), {aid})
        self.assertEqual(len(statefile.corrupt_files(self.path.parent)), 1)


class LearnedPreferencesStateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "learned_preferences.yaml"
        patcher = patch.object(memory, "PREFERENCES_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_existing_yaml_list_loads_and_is_written_in_the_same_shape(self):
        self.path.write_text(
            yaml.safe_dump(["call me Sam"], allow_unicode=True, sort_keys=False)
        )
        memory.add_preference("always confirm before muting")
        self.assertEqual(
            yaml.safe_load(self.path.read_text()),
            ["call me Sam", "always confirm before muting"],
        )
        self.assertEqual(
            memory.load_preferences(), ["call me Sam", "always confirm before muting"]
        )

    def test_two_processes_adding_at_once_keep_every_preference(self):
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {SRC!r})
            from pathlib import Path
            from omarchy_ai.core import memory
            memory.PREFERENCES_PATH = Path(sys.argv[1])
            for i in range(20):
                memory.add_preference(f"{{sys.argv[2]}} {{i}}")
        """)
        _run_workers(
            script, [[str(self.path), "a"], [str(self.path), "b"]], self.path.parent
        )
        self.assertEqual(len(memory.load_preferences()), 40)

    def test_a_corrupt_file_is_quarantined_not_replaced_by_one_entry(self):
        self.path.write_text("- one\n- [unclosed\n")
        with self.assertLogs("omarchy_ai.core.statefile", "WARNING"):
            self.assertEqual(memory.load_preferences(), [])
        self.assertEqual(
            statefile.corrupt_files(self.path.parent)[0].read_text(),
            "- one\n- [unclosed\n",
        )


if __name__ == "__main__":
    unittest.main()
