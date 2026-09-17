import unittest
from unittest.mock import AsyncMock, patch
from omarchy_ai.voice.echo_cancel import EchoCancellation


class EchoTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_routes_and_cleanup(self):
        with patch('omarchy_ai.voice.echo_cancel.pactl', new_callable=AsyncMock) as command:
            command.side_effect = ['physical_mic', 'physical_speaker', '123', '']
            echo = EchoCancellation()
            await echo.start()
            args = command.call_args.args
            self.assertIn('source_master=physical_mic', args)
            self.assertIn('sink_master=physical_speaker', args)
            self.assertIn('source_name=' + echo.source, args)
            self.assertIn('sink_name=' + echo.sink, args)
            self.assertIn('aec_args="noise_suppression=1 high_pass_filter=1 analog_gain_control=0 digital_gain_control=0"', args)
            self.assertFalse(any(c.args[0].startswith('set-default') for c in command.call_args_list))
            await echo.close()
            command.assert_awaited_with('unload-module', '123')
            await echo.close()
            self.assertEqual(command.await_count, 4)

    async def test_monitor_rejected(self):
        with patch('omarchy_ai.voice.echo_cancel.pactl', new_callable=AsyncMock, side_effect=['speakers.monitor', 'speakers']):
            with self.assertRaisesRegex(RuntimeError, 'microphone'):
                await EchoCancellation().start()

    async def test_explicit_microphone_is_preserved(self):
        with patch('omarchy_ai.voice.echo_cancel.pactl', new_callable=AsyncMock, side_effect=['speakers', '124', '']) as command:
            echo = EchoCancellation()
            await echo.start('usb_mic')
            self.assertIn('source_master=usb_mic', command.call_args.args)
            await echo.close()
