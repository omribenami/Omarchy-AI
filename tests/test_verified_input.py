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
        self.window = {}
        def execute(name, args):
            if name == 'list_windows':
                return ActionResult(True, json.dumps([{'address': self.address, 'app': 'foot', 'focused': True,
                                                       **self.window}]))
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

    def test_typed_text_says_it_is_not_submitted_and_where_to_read(self):
        # 2026-09-24: 'ls' typed ten times without Return; the log read came
        # from another terminal with the same title.
        self.guard.run(self.execute, 'focus_window', {'target': self.address})
        typed = self.guard.run(self.execute, 'type_text', {'text': 'ls'})
        self.assertIn('press_key Return', typed.message)
        self.assertIn("read_tile_log window='0x123'", typed.message)
        pressed = self.guard.run(self.execute, 'press_key', {'key': 'Return'})
        self.assertNotIn('press_key Return', pressed.message)

    def test_a_second_return_with_nothing_typed_is_not_sent(self):
        self.guard.run(self.execute, 'focus_window', {'target': self.address})
        self.guard.run(self.execute, 'type_text', {'text': 'ssh host'})
        self.guard.run(self.execute, 'press_key', {'key': 'Return'})
        second = self.guard.run(self.execute, 'press_key', {'key': 'Return'})
        self.assertIn('Not pressed', second.message)
        self.assertEqual([c.args[0] for c in self.execute.call_args_list].count('press_key'), 1)
        self.guard.run(self.execute, 'type_text', {'text': 'ls'})
        self.assertNotIn('Not pressed', self.guard.run(self.execute, 'press_key', {'key': 'Return'}).message)

    def test_focus_change_blocks_return_until_refocused(self):
        self.guard.run(self.execute, 'focus_window', {'target': self.address})
        self.address = '0x456'
        self.assertFalse(self.guard.run(self.execute, 'press_key', {'key': 'Return'}).ok)
        self.guard.run(self.execute, 'focus_window', {'target': self.address})
        self.assertTrue(self.guard.run(self.execute, 'press_key', {'key': 'Return'}).ok)

    def test_new_session_cannot_reuse_previous_focus(self):
        self.guard.run(self.execute, 'focus_window', {'target': self.address})
        self.assertFalse(InputGuard().run(self.execute, 'type_text', {'text': 'hello'}).ok)

    def test_prompt_for_another_agent_is_relayed_verbatim(self):
        # Regression: the user dictated a Claude Code prompt into a terminal
        # and it must reach the terminal untouched (URLs, markdown, the lot).
        prompt = ("please use the claude_design MCP (https://api.anthropic.com/v1/design/mcp) "
                  "to import this project:\n- `Phone Bridge.dc.html`\nImplement: `Phone Bridge.dc.html`")
        self.guard.run(self.execute, 'focus_window', {'target': self.address})
        result = self.guard.run(self.execute, 'type_text', {'text': prompt})
        self.assertTrue(result.ok)
        self.execute.assert_called_with('type_text', {'text': prompt})

    def test_focus_dispatch_success_is_not_focus_verification(self):
        with patch('omarchy_ai.execution.actions._hyprctl_dispatch', return_value=ActionResult(True)), patch('omarchy_ai.execution.actions._run', return_value=ActionResult(True, '{"address":"0x456"}')), patch('omarchy_ai.execution.actions.time.sleep'):
            self.assertFalse(focus_window({'target': '0x123'}).ok)

    def test_focus_verified_against_requested_address(self):
        with patch('omarchy_ai.execution.actions._hyprctl_dispatch', return_value=ActionResult(True)), patch('omarchy_ai.execution.actions._run', return_value=ActionResult(True, '{"address":"0x123"}')):
            self.assertTrue(focus_window({'target': '0x123'}).ok)

    def test_exit_needs_the_users_answer_to_a_question(self):
        # 2026-09-24 22:44: mid-sentence, `exit` + Return went into the
        # user's Claude Code window and ended that session.
        self.window = {'title': 'Omarchy thinking step freeze', 'running': 'claude',
                       'machine': 'local (Jarvis-HQ): this computer'}
        self.guard.run(self.execute, 'focus_window', {'target': self.address})
        blocked = self.guard.run(self.execute, 'type_text', {'text': 'exit'})
        self.assertFalse(blocked.ok)
        self.assertIn('END the claude session', blocked.message)
        self.guard.heard_user()           # the rest of the user's sentence
        self.assertFalse(self.guard.run(self.execute, 'type_text', {'text': 'exit'}).ok)
        self.guard.assistant_replied()    # she asks
        self.guard.heard_user()           # the user answers
        self.assertTrue(self.guard.run(self.execute, 'type_text', {'text': 'exit'}).ok)
        self.assertEqual([c.args for c in self.execute.call_args_list if c.args[0] == 'type_text'],
                         [('type_text', {'text': 'exit'})])
        # Used once: the next exit asks again.
        self.assertFalse(self.guard.run(self.execute, 'type_text', {'text': 'exit'}).ok)

    def test_session_ending_input_forms(self):
        from omarchy_ai.execution.verified_input import ends_session
        for text in ('exit', ' EXIT ', 'logout', '/exit', '/quit', 'exit 0', 'exit()', 'quit()'):
            self.assertTrue(ends_session('type_text', {'text': text}), text)
        for text in ('exit the loop when done', 'echo exit', 'git commit -m exit', 'exits'):
            self.assertFalse(ends_session('type_text', {'text': text}), text)
        self.assertTrue(ends_session('press_key', {'key': 'd', 'modifiers': ['ctrl']}))
        self.assertFalse(ends_session('press_key', {'key': 'c', 'modifiers': ['ctrl']}))

    def test_input_results_say_which_machine_runs_it(self):
        self.window = {'machine': 'REMOTE ben-ami@x230 (ssh): commands typed here run on that machine'}
        focused = self.guard.run(self.execute, 'focus_window', {'target': self.address})
        self.assertIn('NOT on this computer', focused.message)
        self.assertIn('ben-ami@x230', self.guard.run(self.execute, 'type_text', {'text': 'docker ps'}).message)
        self.window = {'machine': 'local (Jarvis-HQ): this computer'}
        self.guard.run(self.execute, 'focus_window', {'target': self.address})
        self.assertIn('not a remote server', self.guard.run(self.execute, 'type_text', {'text': 'docker ps'}).message)


class SshDestinationTests(unittest.TestCase):
    def test_destination_from_command_line(self):
        from omarchy_ai.execution.actions import _ssh_destination
        self.assertEqual(_ssh_destination(['ssh', 'ben-ami@x230']), 'ben-ami@x230')
        self.assertEqual(_ssh_destination(['ssh', '-p', '2222', '-l', 'me', 'box']), 'me@box')
        self.assertEqual(_ssh_destination(['ssh', '-vp22', 'host', 'uptime']), 'host')
        self.assertEqual(_ssh_destination(['ssh', '-o', 'A=1', '-t', 'h']), 'h')
        self.assertEqual(_ssh_destination(['ssh', 'ssh://u@h:22']), 'u@h')
        self.assertIsNone(_ssh_destination(['ssh']))
