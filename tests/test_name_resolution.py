"""Spoken names and argument names resolve to the one thing meant
(execution/naming.py, cameras.resolve, casting targets, catalog.conform,
scheduled-step tool names). No network, no real devices, no state dir."""
import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import no_live_desktop  # noqa: F401 -- run alone too: no real notification, push or desktop command
from omarchy_ai.core import agenda
from omarchy_ai.display import discovery, registry
from omarchy_ai.execution import cameras, catalog, naming
from omarchy_ai.execution.actions import casting

CAMERAS = ["front door", "back yard", "garage", "kids room"]


class NamingTests(unittest.TestCase):
    def match(self, said, names):
        return naming.match(said, {n: [n] for n in names})

    def test_partial_hebrew_and_misspelled_names_find_the_one_camera(self):
        for said, meant in (("Door", "front door"), ("הדלת", "front door"), ("הדלת, הרינג", "front door"),
                            ("the garage camera", "garage"), ("garag", "garage"), ("מוסך", "garage"),
                            ("בחצר", "back yard"), ("kids", "kids room"), ("חדר הילדים", "kids room")):
            self.assertEqual(self.match(said, CAMERAS), (meant, []), said)

    def test_a_tie_lists_the_options_and_nothing_fitting_lists_none(self):
        self.assertEqual(self.match("door", ["front door", "back door", "garage"]), (None, ["front door", "back door"]))
        self.assertEqual(self.match("roof", CAMERAS), (None, []))

    def test_room_words_reach_the_living_room(self):
        tvs = {"a": ["Office TV"], "b": ["Living Room TV", "adb-SERIAL123"]}
        for said in ("Living Room TV", "living room", "salon", "the salon TV", "סלון", "בסלון", "lounge",
                     "adb-SERIAL123"):
            self.assertEqual(naming.match(said, tvs), ("b", []), said)

    def test_only_a_kind_word_names_no_one(self):
        self.assertEqual(naming.match("the TV", {"a": ["Office TV"], "b": ["Den TV"]}), (None, ["a", "b"]))


class CameraResolveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        program = Path(temp.name) / "cams"
        program.write_text("#!/bin/sh\necho '" + json.dumps(CAMERAS + ["back door"]) + "'\n")
        program.chmod(program.stat().st_mode | stat.S_IXUSR)
        self.config = SimpleNamespace(camera_snapshot_command=str(program))

    def test_the_single_best_camera_is_used(self):
        self.assertEqual(cameras.resolve("Front", self.config), "front door")
        self.assertEqual(cameras.resolve("החניה, המוסך", self.config), "garage")

    def test_an_ambiguous_name_asks_with_the_options(self):
        with self.assertRaisesRegex(ValueError, "could be any of these cameras: front door, back door"):
            cameras.resolve("door", self.config)
        with self.assertRaisesRegex(ValueError, "the cameras are: front door, back yard"):
            cameras.resolve("roof", self.config)


def device(name, address, status="online", aliases=()):
    return {"name": name, "address": address, "status": status, "last_seen": 1.0, "aliases": list(aliases)}


class CastTargetTests(unittest.TestCase):
    def resolve(self, target, devices):
        with patch.object(registry, "get_or_refresh", return_value=devices), \
                patch.object(discovery, "fallback_tv", return_value=None):
            return casting._resolve_cast_target(target)

    def test_a_tv_is_found_by_any_of_its_names_and_room_words(self):
        devices = [device("Office TV", "192.0.2.10"), device("Living Room TV", "192.0.2.20", aliases=["adb-SERIAL1"])]
        for target in ("Living Room TV", "living room", "salon", "סלון", "adb-SERIAL1", "living rom tv"):
            self.assertEqual(self.resolve(target, devices), ("192.0.2.20:5555", None), target)

    def test_the_tv_with_no_name_is_the_only_live_one(self):
        devices = [device("Office TV", "192.0.2.10"), device("previously paired TV", "192.0.2.30", "unknown")]
        self.assertEqual(self.resolve("the TV", devices), ("192.0.2.10:5555", None))

    def test_unknown_and_ambiguous_targets_fail_with_the_names(self):
        devices = [device("Bedroom TV", "192.0.2.10"), device("Kids Bedroom TV", "192.0.2.11"),
                   device("Office TV", "192.0.2.12")]
        address, error = self.resolve("garden", devices)
        self.assertIsNone(address)
        self.assertIn("Available: Bedroom TV, Kids Bedroom TV, Office TV", error.message)
        self.assertEqual(self.resolve("Bedroom TV", devices), ("192.0.2.10:5555", None))   # the exact name wins
        address, error = self.resolve("bedroom", devices)
        self.assertEqual(address, "192.0.2.10:5555")                                     # all its words are the name
        address, error = self.resolve("192.0.2.99", devices)
        self.assertEqual(address, "192.0.2.99:5555")


class RegistryNameTests(unittest.TestCase):
    def setUp(self):
        for target, value in ((registry, "_devices"), (registry, "_last_refresh")):
            p = patch.object(target, value, {} if value == "_devices" else 0.0)
            p.start()
            self.addCleanup(p.stop)
        session = SimpleNamespace(target=lambda: None, read_state=lambda: {})
        p = patch.dict("sys.modules", {"omarchy_ai.display.session": session})
        p.start()
        self.addCleanup(p.stop)
        p = patch("omarchy_ai.display.registry.discovery.fallback_tv", return_value="192.0.2.30:5555")
        p.start()
        self.addCleanup(p.stop)

    def refresh(self, seen):
        with patch.object(registry.discovery, "discover_androidtv_devices", return_value=seen):
            return {d["address"]: d for d in registry.refresh()}

    def test_a_tv_keeps_its_friendly_name_when_a_pass_sees_only_its_serial(self):
        self.refresh([{"name": "Living Room TV", "address": "192.0.2.20"}])
        devices = self.refresh([{"name": "adb-SERIAL1", "address": "192.0.2.20"}])
        self.assertEqual(devices["192.0.2.20"]["name"], "Living Room TV")
        self.assertEqual(devices["192.0.2.20"]["aliases"], ["adb-SERIAL1"])
        self.assertEqual(devices["192.0.2.20"]["status"], "online")

    def test_the_fallback_tv_takes_its_real_name_once_seen(self):
        self.assertEqual(self.refresh([])["192.0.2.30"]["name"], "previously paired TV")
        devices = self.refresh([{"name": "Den TV", "address": "192.0.2.30"}])
        self.assertEqual((devices["192.0.2.30"]["name"], devices["192.0.2.30"]["aliases"]),
                         ("Den TV", ["previously paired TV"]))


class AdbNameTests(unittest.TestCase):
    def setUp(self):
        for name in ("_adb_model_name_cache", "_adb_name_failed_at"):
            p = patch.object(discovery, name, {})
            p.start()
            self.addCleanup(p.stop)

    def test_the_users_device_name_comes_first(self):
        def run(argv, **kwargs):
            out = "Living Room TV\n" if "device_name" in argv else "HY300Pro\n"
            return SimpleNamespace(returncode=0, stdout=out if argv[1] == "-s" else "connected")
        with patch.object(discovery.subprocess, "run", side_effect=run):
            self.assertEqual(discovery._resolve_adb_model_name("192.0.2.20", 5555, "adb-X"), "Living Room TV")

    def test_a_failed_lookup_is_not_kept_as_the_name(self):
        with patch.object(discovery.subprocess, "run", side_effect=discovery.subprocess.TimeoutExpired("adb", 3)):
            self.assertEqual(discovery._resolve_adb_model_name("192.0.2.20", 5555, "adb-X"), "adb-X")
        self.assertNotIn("192.0.2.20:5555", discovery._adb_model_name_cache)
        discovery._adb_name_failed_at["192.0.2.20:5555"] -= discovery._ADB_NAME_RETRY  # the retry is due
        ok = SimpleNamespace(returncode=0, stdout="Den TV\n")
        with patch.object(discovery.subprocess, "run", return_value=ok):
            self.assertEqual(discovery._resolve_adb_model_name("192.0.2.20", 5555, "adb-X"), "Den TV")


SMART_HOME = {"type": "object", "required": ["operation"], "properties": {
    "operation": {"type": "string", "enum": ["ask", "list", "get", "call"],
                  "description": "ask: plain-language request; list; get: one entity; call: a service."},
    "text": {"type": "string", "description": "For ask: the user's request, in their words."},
    "entity_id": {"type": "string", "description": "For get/call, or a substring filter for list."},
    "domain": {"type": "string", "description": "For call, e.g. light."},
    "service": {"type": "string", "description": "For call, e.g. turn_on."},
}}
SMART_TOOL = {"type": "function", "name": "smart_home", "description": "Control the house.", "parameters": SMART_HOME}


class ConformTests(unittest.TestCase):
    def test_a_synonym_moves_to_the_declared_key(self):
        args, moved = catalog.conform(SMART_HOME, {"operation": "Ask", "request": "dim the hall"})
        self.assertEqual(args, {"operation": "ask", "text": "dim the hall"})
        self.assertIn("request->text", moved)

    def test_the_operation_follows_from_the_other_arguments(self):
        args, _ = catalog.conform(SMART_HOME, {"domain": "cover", "service": "open_cover", "entity_id": "cover.x"})
        self.assertEqual(args["operation"], "call")
        args, _ = catalog.conform(SMART_HOME, {"entity_id": "cover.x"})      # get, call or list: not guessed
        self.assertNotIn("operation", args)

    def test_the_one_stray_string_fills_the_one_missing_required_string(self):
        schema = {"type": "object", "required": ["city"], "properties": {"city": {"type": "string"}}}
        self.assertEqual(catalog.conform(schema, {"town": "Haifa"})[0], {"city": "Haifa"})
        self.assertEqual(catalog.conform(schema, {"town": "a", "place": "b"})[0], {"town": "a", "place": "b"})

    def test_a_shortened_operation_is_settled_by_the_other_arguments(self):
        # Live 2026-10-26: operation "play" with media_type "music" failed.
        schema = {"type": "object", "required": ["operation"], "properties": {
            "operation": {"type": "string", "enum": ["play_music", "play_movie", "status"]},
            "query": {"type": "string"},
            "media_type": {"type": "string", "description": "Media type for play_music."}}}
        args, moved = catalog.conform(schema, {"operation": "play", "query": "x", "media_type": "music"})
        self.assertEqual(args["operation"], "play_music")
        self.assertIn("operation=play_music", moved)
        args, _ = catalog.conform(schema, {"operation": "play", "query": "x"})   # music or movie: not guessed
        self.assertEqual(args["operation"], "play")

    def resolve(self, args, fill=None, jev=None):
        with patch("omarchy_ai.execution.user_tools.schemas", return_value=[SMART_TOOL]):
            return catalog.resolve(args, "", fill=fill, jev=jev)

    def test_use_tool_maps_the_request_key_into_the_tool(self):
        pick = self.resolve({"name": "smart_home", "args": {"operation": "ask", "query": "is the door locked?"}})
        self.assertEqual((pick.run, pick.args), (True, {"operation": "ask", "text": "is the door locked?"}))
        pick = self.resolve({"name": "smart_home", "args": {"operation": "ask"}, "request": "lights off"})
        self.assertEqual(pick.args, {"operation": "ask", "text": "lights off"})
        pick = self.resolve({"name": "smart_home", "args": {"operation": "list"}, "request": "lights off"})
        self.assertEqual(pick.args, {"operation": "list"})

    def test_a_paraphrased_request_text_is_the_request_itself(self):
        # Live 2026-10-26: the filler wrote the text with other words, it was
        # dropped, and the tool ran `ask` with no text three times.
        fill = lambda tool, request, heard: {"operation": "ask", "text": "List all the cameras."}  # noqa: E731
        pick = self.resolve({"name": "smart_home", "request": "list cameras in the house"}, fill=fill)
        self.assertEqual(pick.args, {"operation": "ask", "text": "list cameras in the house"})
        fill = lambda tool, request, heard: {"operation": "ask", "text": "List cameras in the house."}  # noqa: E731
        pick = self.resolve({"name": "smart_home", "request": "list cameras in the house"}, fill=fill)
        self.assertEqual(pick.args["text"], "List cameras in the house.")

    def test_usage_names_the_key_with_an_example(self):
        text = catalog.usage(SMART_TOOL, ["text"], {"operation": "ask"})
        self.assertIn("`text`: For ask", text)
        self.assertIn('use_tool(name=\'smart_home\', args={"operation": "ask", "text": "<For ask', text)


class ScheduledStepTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(agenda, "_registry", return_value={"smart_home": SMART_TOOL, "set_volume": {
            "name": "set_volume", "parameters": {"type": "object", "properties": {"level": {"type": "integer"}}}}})
        p.start()
        self.addCleanup(p.stop)

    def test_near_miss_tool_names_and_missing_operation_are_fixed(self):
        call = {"domain": "script", "service": "turn_on", "entity_id": "script.x"}
        for name in ("Smart Home", "smart-home", "smart_home_call_service", "smarthome:call_service"):
            steps = agenda.steps_from({"tool": name, "args": call})
            self.assertEqual(steps, [{"tool": "smart_home", "args": {**call, "operation": "call"}}], name)

    def test_an_unknown_tool_still_fails_with_the_closest_names(self):
        with self.assertRaisesRegex(ValueError, r"no tool named 'smart_hom'.*closest: smart_home"):
            agenda.steps_from({"tool": "smart_hom", "args": {}})


if __name__ == "__main__":
    unittest.main()
