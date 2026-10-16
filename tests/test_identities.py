import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omarchy_ai.core import identities
from omarchy_ai.phone import flux_pairing, server
from omarchy_ai.voice import speaker_id


class IdentitiesTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = self.root = Path(self.dir.name)
        (root / "flux").mkdir()
        self.devices = root / "flux" / "devices.json"
        self.sessions = root / "phone_bridge" / "paired_sessions.json"
        for patcher in (
            patch.object(identities, "PATH", root / "identities.json"),
            patch.object(speaker_id, "SPEAKERS_DIR", root / "speakers"),
            patch.object(speaker_id, "EMBEDDINGS_PATH", root / "speakers.json"),
            patch.object(speaker_id, "PEOPLE_PATH", root / "people.yaml"),
            patch.object(flux_pairing, "devices_path", lambda: self.devices),
            patch.object(server, "_CERT_DIR", root / "phone_bridge"),
            patch.object(server, "_SESSIONS_PATH", self.sessions),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)

    def test_store_operations(self):
        identities.add("person-a", "Person A")
        identities.add("Person-B")
        self.assertEqual([(p["key"], p["name"]) for p in identities.people()],
                         [("person-a", "Person A"), ("person-b", "Person-b")])
        identities.rename("person-b", "Person B")
        self.assertEqual(identities.people()[1]["name"], "Person B")
        for bad in ("", "has space", "../x"):
            with self.assertRaises(ValueError):
                identities.add(bad)
        with self.assertRaises(ValueError):
            identities.add("person-a")
        with self.assertRaises(ValueError):
            identities.rename("nobody", "X")
        identities.remove("person-b")
        self.assertEqual([p["key"] for p in identities.people()], ["person-a"])
        self.assertEqual(os.stat(identities.PATH).st_mode & 0o777, 0o600)

    def test_one_owner(self):
        identities.add("person-a")
        identities.add("person-b")
        self.assertIsNone(identities.owner())
        identities.set_owner("person-a")
        identities.set_owner("person-b")
        self.assertEqual(identities.owner(), "person-b")
        self.assertEqual([p["owner"] for p in identities.people()], [False, True])
        with self.assertRaises(ValueError):
            identities.set_owner("nobody")
        identities.remove("person-b")
        self.assertIsNone(identities.owner())
        identities.set_owner("person-a")
        identities.set_owner(None)
        self.assertIsNone(identities.owner())

    def test_a_phone_belongs_to_one_person(self):
        identities.add("person-a")
        identities.add("person-b")
        identities.attach_phone("person-a", "dev-1")
        identities.attach_phone("person-b", "dev-1")
        identities.attach_phone("person-b", "dev-1")
        self.assertEqual([p["phones"] for p in identities.people()], [[], ["dev-1"]])
        self.assertEqual(identities.person_for_device("dev-1"), "person-b")
        self.assertIsNone(identities.person_for_device("dev-2"))
        self.assertIsNone(identities.person_for_device(None))
        with self.assertRaises(ValueError):
            identities.attach_phone("nobody", "dev-2")
        identities.detach_phone("dev-1")
        self.assertIsNone(identities.person_for_device("dev-1"))

    def test_person_for_session(self):
        identities.add("person-a")
        identities.attach_phone("person-a", "dev-1")
        self.assertEqual(identities.person_for_session({"device": "dev-1"}), "person-a")
        self.assertEqual(identities.person_for_session({"device": None, "person": "person-a"}), "person-a")
        self.assertIsNone(identities.person_for_session({"device": None, "person": "gone"}))
        self.assertIsNone(identities.person_for_session(None))

    def test_legacy_session_file_and_device_lookup(self):
        self.sessions.parent.mkdir()
        self.sessions.write_text(json.dumps(["legacy-1", {"id": "new-1", "device": "dev-1", "name": "Phone"}, 7, {}]))
        sessions = server.load_sessions()
        self.assertEqual([s["id"] for s in sessions], ["legacy-1", "new-1"])
        self.assertEqual(sessions[0], {"id": "legacy-1", "device": None, "name": None, "person": None})
        self.assertEqual(server.paired_count(), 2)
        self.assertEqual(server.session_device("omarchy_session=new-1"), "dev-1")
        self.assertIsNone(server.session_device("omarchy_session=legacy-1"))
        self.assertIsNotNone(server.session_for_cookie("other=1; omarchy_session=legacy-1"))
        self.assertIsNone(server.session_for_cookie("omarchy_session=unknown"))
        self.assertIsNone(server.session_for_cookie(""))
        cookie = server._new_session_cookie(device="dev-2", name="Phone Two")
        session_id = cookie.split(";")[0].split("=", 1)[1]
        self.assertEqual(server.session_device(f"omarchy_session={session_id}"), "dev-2")
        self.assertEqual(len(json.loads(self.sessions.read_text())), 3)
        server.revoke_all_sessions()
        self.assertEqual(server.load_sessions(), [])

    def test_qr_pairing_carries_a_person(self):
        with patch.object(server, "_PAIR_TOKEN_PATH", self.root / "phone_bridge" / "token.json"), \
                patch.object(server, "_primary_lan_ip", return_value="192.0.2.20"), \
                patch.object(server, "_tailscale_address", return_value=(None, None)):
            server.mint_pairing_token(SimpleNamespace(phone_bridge_port=8766), person="person-a")
            self.assertEqual(json.loads((self.root / "phone_bridge" / "token.json").read_text())["person"], "person-a")

    def test_status(self):
        (self.root / "speakers" / "person-a").mkdir(parents=True)
        for n in range(3):
            (self.root / "speakers" / "person-a" / f"{n}.wav").touch()
        (self.root / "speakers" / "person-c").mkdir()
        (self.root / "speakers.json").write_text(json.dumps({"person-a": [0.1]}))
        (self.root / "people.yaml").write_text("people:\n  person-a:\n    name: Person A\n    relation: tester\n")
        self.devices.write_text(json.dumps([{"id": "dev-1", "name": "Phone One"}, {"id": "dev-2", "name": "Phone Two"}]))
        self.sessions.parent.mkdir()
        self.sessions.write_text(json.dumps(["legacy-1", {"id": "s2", "device": "dev-1"}, {"id": "s3", "person": "person-b"}]))
        identities.add("person-a")
        identities.add("person-b")
        identities.attach_phone("person-a", "dev-1")
        identities.attach_phone("person-a", "dev-9")
        identities.set_owner("person-a")

        status = identities.status(SimpleNamespace(speaker_id_enabled=True))
        a, b = status["people"]
        self.assertEqual((a["relation"], a["household_name"], a["owner"]), ("tester", "Person A", True))
        self.assertEqual((a["voice"]["state"], a["voice"]["clips"]), ("enrolled", 3))
        self.assertEqual(b["voice"]["state"], "none")
        self.assertEqual(a["phones"], [{"id": "dev-1", "name": "Phone One", "paired": True},
                                       {"id": "dev-9", "name": "dev-9", "paired": False}])
        self.assertEqual(b["qr_phones"], 1)
        self.assertEqual(status["unattached_phones"], [{"id": "dev-2", "name": "Phone Two"}])
        self.assertEqual(status["unlisted"], [{"key": "person-c", "name": "Person-c"}])
        self.assertEqual(status["anonymous_sessions"], 1)
        self.assertEqual(status["warnings"], [])

        status = identities.status(SimpleNamespace(speaker_id_enabled=False))
        self.assertEqual(status["people"][0]["voice"]["state"], "disabled")
        self.assertEqual(len(status["warnings"]), 1)

    def test_current_identity(self):
        self.assertEqual(identities.current(), identities.Identity())
        token = identities.set_current(identities.Identity("person-a", "phone"))
        self.assertEqual(identities.current().person, "person-a")
        identities.reset_current(token)
        self.assertIsNone(identities.current().how)


if __name__ == "__main__":
    unittest.main()
