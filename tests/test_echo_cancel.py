import unittest
from unittest.mock import AsyncMock, patch
from omarchy_ai.voice.echo_cancel import EchoCancellation


class EchoTests(unittest.IsolatedAsyncioTestCase):
    def volume_backend(self, original=(39606, 33000), fail_load=False):
        state = {'volume': original}
        async def command(*args):
            if args[0] == 'get-default-source': return 'physical_mic'
            if args[0] == 'get-default-sink': return 'physical_speaker'
            if args[0] == 'get-source-volume':
                return 'Volume: ' + ', '.join(f'channel{i}: {v} / 60% / -13.12 dB' for i, v in enumerate(state['volume']))
            if args[0] == 'set-source-volume':
                state['volume'] = tuple(int(v) for v in args[2:])
                return ''
            if args[0] == 'load-module':
                if fail_load: raise RuntimeError('module unavailable')
                return '123'
            if args[0] == 'unload-module': return ''
            raise AssertionError(args)
        return state, AsyncMock(side_effect=command)

    async def test_session_cap_restores_original_channels_and_preserves_balance(self):
        original = (39606, 33000)
        state, command = self.volume_backend(original)
        with patch('omarchy_ai.voice.echo_cancel.pactl', command):
            echo = EchoCancellation()
            await echo.start(volume_percent=35)
            self.assertEqual(max(state['volume']), round(65536 * .35))
            self.assertAlmostEqual(state['volume'][1] / state['volume'][0], original[1] / original[0], places=4)
            await echo.close()
            self.assertEqual(state['volume'], original)
            count = command.await_count
            await echo.close()
            self.assertEqual(command.await_count, count)

    async def test_session_cap_does_not_overwrite_user_volume_change(self):
        state, command = self.volume_backend()
        with patch('omarchy_ai.voice.echo_cancel.pactl', command):
            echo = EchoCancellation()
            await echo.start(volume_percent=35)
            state['volume'] = (25000, 25000)
            await echo.close()
            self.assertEqual(state['volume'], (25000, 25000))

    async def test_module_failure_restores_microphone(self):
        state, command = self.volume_backend(fail_load=True)
        with patch('omarchy_ai.voice.echo_cancel.pactl', command):
            with self.assertRaisesRegex(RuntimeError, 'module unavailable'):
                await EchoCancellation().start(volume_percent=35)
            self.assertEqual(state['volume'], (39606, 33000))

    async def test_quiet_microphone_is_never_raised(self):
        state, command = self.volume_backend((10000, 10000))
        with patch('omarchy_ai.voice.echo_cancel.pactl', command):
            echo = EchoCancellation()
            await echo.start(volume_percent=35)
            await echo.close()
        self.assertFalse(any(c.args[0] == 'set-source-volume' for c in command.call_args_list))

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
