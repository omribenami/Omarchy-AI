import unittest
from types import SimpleNamespace
from unittest.mock import patch

from omarchy_ai.display import assistant_huds


class AssistantHudsTest(unittest.TestCase):
    def test_task_items_only_include_current_work(self):
        tasks = [
            SimpleNamespace(id="one", goal="Active", status="running", phase="work", question=None,
                            pending_approval=None),
            SimpleNamespace(id="two", goal="Needs me", status="waiting_user", phase="wait", question="Which?",
                            pending_approval=None),
            SimpleNamespace(id="three", goal="Finished", status="certified", phase="done", question=None,
                            pending_approval=None),
        ]
        with patch("omarchy_ai.runtime.task.TaskStore.list", return_value=tasks):
            items = assistant_huds.task_items()
        self.assertEqual([item["id"] for item in items], ["one", "two"])
        self.assertEqual(items[1]["detail"], "Which?")
        self.assertIn("Still working", items[0]["detail"])

    def test_routine_items_are_active_agenda_jobs(self):
        jobs = [{"id": "t-1", "title": "Morning", "kind": "assistant",
                 "schedule": "cron 0 9 * * 1-5", "next_run": "Mon 09:00"}]
        with patch("omarchy_ai.core.agenda.list_jobs", return_value=jobs) as listed:
            self.assertEqual(assistant_huds.routine_items()[0]["detail"], "cron 0 9 * * 1-5")
        listed.assert_called_once_with(False)

    @patch.object(assistant_huds, "_ipc")
    @patch.object(assistant_huds, "task_items", return_value=[{"id": "one"}])
    def test_show_tasks_sends_wrapped_array(self, items, ipc):
        assistant_huds.show_tasks()
        ipc.assert_called_once_with("showTasks", {"items": [{"id": "one"}]})

    @patch.object(assistant_huds, "_ipc_async")
    @patch.object(assistant_huds, "approval_items", return_value=[])
    @patch.object(assistant_huds, "task_items", return_value=[])
    def test_refresh_tasks_updates_without_opening(self, items, approvals, ipc):
        assistant_huds.refresh_tasks()
        self.assertEqual([c.args for c in ipc.call_args_list],
                         [("updateTasks", {"items": []}), ("updateApprovals", {"items": []})])

    @patch.object(assistant_huds, "show_routines")
    @patch.object(assistant_huds, "show_tasks")
    def test_automatic_flags_are_independent(self, show_tasks, show_routines):
        assistant_huds.open_automatic(SimpleNamespace(tasks_hud_on_call=True, routines_hud_on_call=False))
        show_tasks.assert_called_once_with()
        show_routines.assert_not_called()

    @patch.object(assistant_huds, "_ipc_async")
    @patch.object(assistant_huds, "routine_items", return_value=[])
    def test_refresh_routines_updates_without_opening(self, items, ipc):
        assistant_huds.refresh_routines()
        ipc.assert_called_once_with("updateRoutines", {"items": []})

    def test_tool_schemas_and_actions_are_registered(self):
        from omarchy_ai.execution.actions import ACTIONS
        from omarchy_ai.execution.tools import TOOLS
        names = {tool["name"] for tool in TOOLS}
        expected = {"show_tasks_hud", "hide_tasks_hud", "show_routines_hud", "hide_routines_hud"}
        self.assertTrue(expected <= names)
        self.assertTrue(expected <= ACTIONS.keys())


if __name__ == "__main__":
    unittest.main()
