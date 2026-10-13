import base64
import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omarchy_ai.core import jev
from omarchy_ai.execution import cameras, vision

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class JudgeImageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.config = SimpleNamespace(omarchy_image_decision_model="some/decision-model")

    def test_sends_the_image_as_a_file_part_and_parses_typed_answers(self):
        image = self.base / "frame"
        image.write_bytes(PNG)
        questions = {"met": jev.boolean("Is a parcel visible?"),
                     "kind": jev.choice("What is it?", {"box": "a box", "none": "nothing"})}
        response = {"answers": {"met": {"type": "boolean", "probability": 0.93},
                                "kind": {"type": "choice", "choice": "box", "probabilities": {"box": 0.9, "none": 0.1}}},
                    "providerMetadata": {"gateway": {"routing": {}}, "openai": {"confidence": {"kind": 0.8}}}}
        with patch("omarchy_ai.voice.omarchy.GatewayClient") as client:
            client.return_value.decide_response.return_value = response
            answers = vision.judge_image(image, questions, self.config, "A camera frame.")
        parts, asked = client.return_value.decide_response.call_args.args
        self.assertEqual(client.return_value.decide_response.call_args.kwargs["model"], "some/decision-model")
        self.assertEqual(parts[0], {"type": "text", "text": "A camera frame."})
        self.assertEqual(parts[1], {"type": "file", "mediaType": "image/png",
                                    "data": {"type": "data", "data": base64.b64encode(PNG).decode()}})
        self.assertEqual(asked, questions)
        self.assertEqual(answers["met"], {"p": 0.93})
        self.assertEqual((answers["kind"]["choice"], answers["kind"]["confidence"]), ("box", 0.8))

    def test_a_refusal_or_a_file_that_is_not_an_image_is_an_error_not_a_no(self):
        image = self.base / "frame"
        image.write_bytes(PNG)
        with patch("omarchy_ai.voice.omarchy.GatewayClient") as client:
            client.return_value.decide_response.return_value = {"answers": {"met": {"type": "refusal"}}}
            with self.assertRaises(jev.JevError):
                vision.judge_image(image, {"met": jev.boolean("?")}, self.config)
            image.write_bytes(b"not an image")
            with self.assertRaises(jev.JevError):
                vision.judge_image(image, {"met": jev.boolean("?")}, self.config)
            client.return_value.decide_response.assert_called_once()


class CameraProgramTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        patch.object(cameras, "FRAME_DIR", self.base / "frames").start()
        self.addCleanup(patch.stopall)

    def program(self, body):
        path = self.base / "cams"
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return SimpleNamespace(camera_snapshot_command=str(path))

    def test_names_come_from_the_users_program_and_a_wrong_name_lists_them(self):
        config = self.program("echo '" + json.dumps(["Porch", "garden"]) + "'\n")
        self.assertEqual(cameras.resolve("porch ", config), "Porch")
        self.assertEqual(cameras.resolve("the Garden camera", config), "garden")
        with self.assertRaisesRegex(ValueError, "the cameras are: Porch, garden"):
            cameras.resolve("roof", config)
        with self.assertRaisesRegex(LookupError, "no cameras are set up"):
            cameras.names(SimpleNamespace(camera_snapshot_command=None))

    def test_snapshot_returns_the_frame_and_never_an_old_one_after_a_failure(self):
        good = self.program('[ "$1" = snapshot ] && printf \'\\211PNG\\r\\n\\032\\n0000\' > "$3"\n')
        frame = cameras.snapshot("a camera; rm -rf x", good, "t-1")
        self.assertTrue(vision._valid_image(frame))
        with self.assertRaisesRegex(LookupError, "offline"):
            cameras.snapshot("porch", self.program("echo offline >&2; exit 1\n"), "t-1")
        self.assertFalse(frame.exists())
        with self.assertRaisesRegex(LookupError, "returned no image"):
            cameras.snapshot("porch", self.program('echo hello > "$3"\n'), "t-1")

    def test_only_a_program_that_answers_wait_can_be_waited_on(self):
        self.assertTrue(cameras.can_wait("porch", self.program('[ "$1 $3" = "wait 0" ] && exit 3\n')))
        self.assertFalse(cameras.can_wait("porch", self.program("exit 2\n")))
        self.assertFalse(cameras.can_wait("porch", self.program("echo usage >&2; exit 1\n")))
        self.assertFalse(cameras.can_wait("porch", SimpleNamespace(camera_snapshot_command=None)))


if __name__ == "__main__":
    unittest.main()
