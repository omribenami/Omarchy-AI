import json
import unittest
from unittest.mock import Mock, patch

from omarchy_ai.execution.actions import ActionResult, focus_window
from omarchy_ai.execution.verified_input import InputGuard


class InputGuardTests(unittest.TestCase):
    def setUp(self):
        self.guard = InputGuard()
        self.address = '0x123'
        self.focus_ok = True
        def execute(name, args):
            if name == 'list_windows':
                return ActionResult(True, json.dumps([{'address': self.address, 'app': 'foot', 'focused': True}]))
            return ActionResult(self.focus_ok if name == 'focus_window' else True)
        self.execute = Mock(side_effect=execute)

    def test_failed_focus_blocks_typing_even_after_listing_windows(self):
        self.focus_ok = False
        self.guard.run(self.execute, 'focus_window', {'target': 'missing'})
        self.guard.run(self.execute, 'list_windows', {})
        result = self.guard.run(self.execute, 'type_text', {'text': 'hello'})
        self.assertFalse(result.ok)
        self.assertNotIn('type_text', [c.args[0] for c in self.execute.call_args_list])

    def test_successful_focus_allows_input_but_does_not_claim_completion(self):
        self.guard.run(self.execute, 'focus_window', {'target': self.address})
        result = self.guard.run(self.execute, 'type_text', {'text': 'hello'})
        self.assertTrue(result.ok)
        self.assertIn('NOT verified', result.message)
        self.execute.assert_called_with('type_text', {'text': 'hello'})

    def test_focus_change_blocks_return_until_refocused(self):
        self.guard.run(self.execute, 'focus_window', {'target': self.address})
        self.address = '0x456'
        self.assertFalse(self.guard.run(self.execute, 'press_key', {'key': 'Return'}).ok)
        self.guard.run(self.execute, 'focus_window', {'target': self.address})
        self.assertTrue(self.guard.run(self.execute, 'press_key', {'key': 'Return'}).ok)

    def test_new_session_cannot_reuse_previous_focus(self):
        self.guard.run(self.execute, 'focus_window', {'target': self.address})
        self.assertFalse(InputGuard().run(self.execute, 'type_text', {'text': 'hello'}).ok)

    def test_conversational_request_is_not_typed_into_an_open_editor(self):
        self.guard.run(self.execute, 'focus_window', {'target': self.address})
        with patch.object(InputGuard, '_editor_running', return_value=True):
            result = self.guard.run(self.execute, 'type_text', {
                'text': 'please make sure we are using the Jev model and browser'
            })
        self.assertFalse(result.ok)
        self.assertIn('conversational request', result.message)
        self.assertEqual([call.args[0] for call in self.execute.call_args_list].count('type_text'), 0)

    def test_conversational_phrasing_is_allowed_at_a_plain_shell_prompt(self):
        self.guard.run(self.execute, 'focus_window', {'target': self.address})
        with patch.object(InputGuard, '_editor_running', return_value=False):
            result = self.guard.run(self.execute, 'type_text', {
                'text': 'please make sure we are using the Jev model and browser'
            })
        self.assertTrue(result.ok)
        self.execute.assert_called_with('type_text', {
            'text': 'please make sure we are using the Jev model and browser'
        })

    def test_focus_dispatch_success_is_not_focus_verification(self):
        with patch('omarchy_ai.execution.actions._hyprctl_dispatch', return_value=ActionResult(True)), patch('omarchy_ai.execution.actions._run', return_value=ActionResult(True, '{"address":"0x456"}')), patch('omarchy_ai.execution.actions.time.sleep'):
            self.assertFalse(focus_window({'target': '0x123'}).ok)

    def test_focus_verified_against_requested_address(self):
        with patch('omarchy_ai.execution.actions._hyprctl_dispatch', return_value=ActionResult(True)), patch('omarchy_ai.execution.actions._run', return_value=ActionResult(True, '{"address":"0x123"}')):
            self.assertTrue(focus_window({'target': '0x123'}).ok)
