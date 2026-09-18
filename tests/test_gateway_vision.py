import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from omarchy_ai.execution.actions import screenshot, describe_screen
from omarchy_ai.execution.vision import inspect_gateway_image


class GatewayVisionTests(unittest.TestCase):
    def test_image_bytes_sent_without_tools(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'screen.png'
            path.write_bytes(b'png-test')
            cfg = SimpleNamespace(omarchy_vision_model='google/gemini-2.5-flash-lite')
            with patch('omarchy_ai.voice.omarchy.GatewayClient') as client:
                client.return_value._request.return_value = json.dumps({'choices': [{'message': {'content': 'A terminal is visible.'}}]}).encode()
                self.assertEqual(inspect_gateway_image(path, 'What is visible?', cfg), 'A terminal is visible.')
                body = json.loads(client.return_value._request.call_args.args[1])
                self.assertNotIn('tools', body)
                self.assertTrue(body['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/png;base64,'))

    def test_capture_failure_never_claims_success(self):
        with patch('omarchy_ai.execution.vision._capture', return_value=None):
            self.assertFalse(screenshot({}).ok)

    def test_saved_image_is_inspected_and_failure_preserved(self):
        cfg = SimpleNamespace(provider='omarchy')
        with patch('omarchy_ai.config.load_config', return_value=cfg), patch('omarchy_ai.execution.vision._capture', return_value=Path('/tmp/screen.png')), patch('omarchy_ai.execution.vision.inspect_gateway_image', return_value='error: unavailable') as inspect:
            result = screenshot({})
            self.assertFalse(result.ok)
            self.assertIn('saved at', result.message)
            inspect.assert_called_once()
            self.assertFalse(describe_screen({}).ok)
