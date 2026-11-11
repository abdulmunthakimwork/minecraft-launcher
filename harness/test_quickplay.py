"""Native Quick Play contract and persistence with disposable installation sources."""
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
with tempfile.TemporaryDirectory() as bootstrap:
    with mock.patch.dict(os.environ, {"LAUNCHER_DATA_DIR": bootstrap}):
        import server


class QuickPlayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state, self.game, self.data = (self.root / name for name in ("state", "game", "data"))
        for folder in (self.state, self.game, self.data, self.root / "ui"):
            folder.mkdir()
        (self.root / "ui/index.html").write_text("<html><head></head><body>Fixture</body></html>")
        self.source = self.state / "launcher_profiles.json"
        self.profiles = {
            "one": {"name": "Fixture one", "lastVersionId": "1.21.1", "gameDir": str(self.game / "one"),
                    "lastUsed": "2026-11-10T10:00:00Z", "javaArgs": "-Xmx2G"},
            "two": {"name": "Fixture two", "lastVersionId": "1.21.1", "gameDir": str(self.game / "two"),
                    "lastPlayed": "2026-11-10T11:00:00Z"},
            "new": {"name": "Never played", "lastVersionId": "1.21.1", "gameDir": str(self.game / "new")},
        }
        self.source.write_text(json.dumps({"profiles": self.profiles}))
        (self.root / "versions.json").write_text(json.dumps({"latest": {"release": "1.21.1"}, "versions": {
            "1.21.1": {"id": "1.21.1", "type": "release", "issues": []}}}))
        patches = mock.patch.multiple(server, STATE=str(self.state), GAME_DIR=str(self.game), DATA_DIR=str(self.data),
            STORE_OVERLAY=str(self.data / "state.json"), OFFLINE_STORE=str(self.data / "offline_accounts.json"),
            VERSIONS=str(self.root / "versions.json"), UI=str(self.root / "ui"))
        patches.start()
        self.addCleanup(patches.stop)
        for target, name, result in (
            (server.launcher, "status", {"running": False}),
            (server.installer, "state", {"active": False}),
            (server.modpacks, "busy", False),
            (server.msa, "account_record", None),
            (server.msa, "game_identity", None),
            (server, "_install_plan", None),
            (server, "_selected_game_identity", {"name": "Fixture"}),
        ):
            patcher = mock.patch.object(target, name, return_value=result)
            patcher.start()
            self.addCleanup(patcher.stop)

    def request(self, path, body=None):
        host = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        worker = threading.Thread(target=lambda: host.serve_forever(poll_interval=0.01), daemon=True)
        worker.start()
        connection = http.client.HTTPConnection(*host.server_address, timeout=3)
        try:
            connection.request("POST" if body is not None else "GET", path,
                               json.dumps(body) if body is not None else None)
            response = connection.getresponse()
            raw = response.read()
            try:
                data = json.loads(raw)
            except ValueError:
                data = {"raw": raw.decode("utf-8", errors="replace")[:120]}
            return response.status, data
        finally:
            connection.close()
            host.shutdown()
            host.server_close()
            worker.join(2)

    def records(self):
        status, data = self.request("/__quickplay")
        self.assertEqual(status, 200, data)
        self.assertIsInstance(data.get("playRecords"), list, data)
        return data["playRecords"]

    def record(self, configuration_id):
        return next(item for item in self.records() if item["javaInstance"]["configId"] == configuration_id)

    def test_recent_installations_have_native_java_schema_and_no_invented_worlds(self):
        source_before = self.source.read_bytes()
        records = self.records()
        self.assertEqual([item["javaInstance"]["configId"] for item in records], ["two", "one"])
        for item in records:
            self.assertEqual(item["source"], "Java")
            self.assertIs(type(item["epochLastPlayedTimeMs"]), int)
            self.assertGreater(item["epochLastPlayedTimeMs"], 0)
            self.assertNotIn("game", item["javaInstance"])
        status, data = self.request("/__quickplay")
        self.assertEqual(data["playRecordSourceMap"]["Java"], records)
        self.assertEqual(data["playRecordSourceMap"]["Bedrock"], [])
        self.assertEqual(server.build_appdata()["quickPlayData"], data)
        self.assertEqual(self.source.read_bytes(), source_before)
        self.assertFalse((self.data / "state.json").exists())

    def test_customization_persists_and_structural_fields_are_owned_by_server(self):
        record = self.record("one")
        edited = dict(record, name="Home", pinnedIndex=0, letterGlyphVisible=True, customizedIcon="Grass")
        status, data = self.request("/__quickplay/update", {"record": edited})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        result = self.record("one")
        for field in ("name", "pinnedIndex", "letterGlyphVisible", "customizedIcon"):
            self.assertEqual(result[field], edited[field])
        before = (self.data / "state.json").read_bytes()
        status, data = self.request("/__quickplay/update", {"record": dict(result, javaInstance={"configId": "two"})})
        self.assertEqual(status, 400, data)
        self.assertEqual((self.data / "state.json").read_bytes(), before)

    def test_deleting_history_preserves_installation_and_stays_deleted(self):
        record = self.record("one")
        source_before = self.source.read_bytes()
        status, data = self.request("/__quickplay/delete", {"id": record["id"]})
        self.assertEqual(status, 200, data)
        self.assertEqual([item["javaInstance"]["configId"] for item in self.records()], ["two"])
        self.assertIn("one", server._effective_configurations())
        self.assertEqual(self.source.read_bytes(), source_before)
        self.assertEqual(data["quickPlayData"]["playRecords"], self.records())

    def test_removed_configuration_disappears_without_deleting_private_history(self):
        record = self.record("one")
        self.request("/__quickplay/update", {"record": dict(record, name="Saved home")})
        server._delete_configuration("one")
        history = server.read_store("__quickplay_state")
        self.assertNotIn("one", [item["javaInstance"]["configId"] for item in self.records()])
        self.assertEqual(server.read_store("__quickplay_state"), history)

    def test_same_raw_id_in_another_root_cannot_inherit_quickplay_customization(self):
        record = self.record("one")
        self.request("/__quickplay/update", {"record": dict(record, name="Root A home", pinnedIndex=0)})
        other_state, other_game = self.root / "other-state", self.root / "other-game"
        other_state.mkdir(); other_game.mkdir()
        (other_state / "launcher_profiles.json").write_text(json.dumps({"profiles": {
            "one": dict(self.profiles["one"], name="Root B", gameDir=str(other_game / "one"))}}))
        with mock.patch.multiple(server, STATE=str(other_state), GAME_DIR=str(other_game)):
            other = self.record("one")
            self.assertNotEqual(other["id"], record["id"])
            self.assertNotEqual(other.get("name"), "Root A home")
            self.assertNotIn("pinnedIndex", other)
        self.assertEqual(self.record("one")["name"], "Root A home")

    def test_unavailable_source_history_is_preserved_and_does_not_rebind(self):
        record = self.record("one")
        self.request("/__quickplay/update", {"record": dict(record, name="Original home")})
        source_before = self.source.read_bytes()
        overlay_before = (self.data / "state.json").read_bytes()
        self.source.write_text("{incomplete")
        alternate = self.state / "launcher_profiles_microsoft_store.json"
        alternate.write_text(json.dumps({"profiles": {"one": dict(self.profiles["one"], name="Different", gameDir=str(self.game / "different"))}}))
        records = self.records()
        self.assertNotIn(record["id"], [item["id"] for item in records])
        self.assertEqual((self.data / "state.json").read_bytes(), overlay_before)
        self.source.write_bytes(source_before)
        self.assertEqual(self.record("one")["name"], "Original home")

    def test_launch_records_selected_installation_and_preserves_racing_edit(self):
        source_before = self.source.read_bytes()
        def spawn(*args, **kwargs):
            server._save_configuration("one", {"name": "Racing rename", "javaArgs": "-Xmx4G"})
            return {"running": True, "pid": 42}
        with mock.patch.object(server.launcher, "start", side_effect=spawn) as launch:
            status, data = self.request("/__launch", {"configurationId": "one"})
        self.assertEqual(status, 200, data)
        self.assertEqual(launch.call_args.kwargs["run_dir"], self.profiles["one"]["gameDir"])
        self.assertEqual(server._configuration("one")["name"], "Racing rename")
        self.assertEqual(server._configuration("one")["javaArgs"], "-Xmx4G")
        self.assertGreater(self.record("one")["epochLastPlayedTimeMs"], self.record("two")["epochLastPlayedTimeMs"])
        self.assertEqual(data["quickPlayData"]["playRecords"], self.records())
        self.assertEqual(self.source.read_bytes(), source_before)

    def test_failed_launch_does_not_create_quickplay_history(self):
        with mock.patch.object(server.launcher, "start", side_effect=RuntimeError("fixture launch failed")):
            status, data = self.request("/__launch", {"configurationId": "new"})
        self.assertEqual(status, 409, data)
        self.assertNotIn("new", [item["javaInstance"]["configId"] for item in self.records()])
        self.assertFalse((self.data / "state.json").exists())

    def test_history_write_failure_warns_after_actual_spawn(self):
        with mock.patch.object(server.launcher, "start", return_value={"running": True, "pid": 42}), \
             mock.patch.object(server, "_write_overlay", side_effect=OSError("fixture history disk failure")):
            status, data = self.request("/__launch", {"configurationId": "new"})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["running"])
        self.assertIn("history", data.get("warning", "").lower())

    def test_clear_all_and_legacy_clear_stay_empty_until_next_actual_launch(self):
        server.write_store("__quick_play_cleared", True)
        self.assertEqual(self.records(), [])
        with mock.patch.object(server.launcher, "start", return_value={"running": True, "pid": 42}):
            status, data = self.request("/__launch", {"configurationId": "one"})
        self.assertEqual(status, 200, data)
        self.assertEqual([item["javaInstance"]["configId"] for item in self.records()], ["one"])
        status, data = self.request("/__quickplay/delete", {"all": True})
        self.assertEqual(status, 200, data)
        self.assertEqual(self.records(), [])

    def test_pin_reorder_and_unpin_leave_unique_dense_positions(self):
        one, two = self.record("one"), self.record("two")
        self.assertEqual(self.request("/__quickplay/update", {"record": dict(one, pinnedIndex=0)})[0], 200)
        self.assertEqual(self.request("/__quickplay/update", {"record": dict(two, pinnedIndex=0)})[0], 200)
        pinned = self.records()
        self.assertEqual([item["javaInstance"]["configId"] for item in pinned], ["two", "one"])
        self.assertEqual([item["pinnedIndex"] for item in pinned], [0, 1])
        status, data = self.request("/__quickplay/update", {"record": dict(self.record("one"), pinnedIndex=0)})
        self.assertEqual(status, 200, data)
        self.assertEqual([item["javaInstance"]["configId"] for item in self.records()], ["one", "two"])
        one = self.record("one")
        one.pop("pinnedIndex")
        self.assertEqual(self.request("/__quickplay/update", {"record": one})[0], 200)
        self.assertEqual(self.record("two")["pinnedIndex"], 0)
        self.assertNotIn("pinnedIndex", self.record("one"))

    def test_invalid_customizations_fail_without_persisting_changes(self):
        record = self.record("one")
        for patch in ({"name": "x" * 257}, {"name": "bad\nname"}, {"pinnedIndex": True},
                      {"pinnedIndex": -1}, {"pinnedIndex": 5}, {"letterGlyphVisible": "true"},
                      {"customizedIcon": "../../file"}, {"customizedIcon": "data:text/html,invalid"},
                      {"customizedIcon": "data:image/png;base64,broken"}):
            with self.subTest(patch=patch):
                status, data = self.request("/__quickplay/update", {"record": dict(record, **patch)})
                self.assertEqual(status, 400, data)
                self.assertFalse((self.data / "state.json").exists())

    def test_malformed_saved_customization_returns_honest_error_and_preserves_bytes(self):
        record = self.record("one")
        self.request("/__quickplay/update", {"record": dict(record, pinnedIndex=0)})
        state = server.read_store("__quickplay_state")
        state["entries"][record["id"]]["customization"]["pinnedIndex"] = "bad position"
        server.write_store("__quickplay_state", state)
        before = (self.data / "state.json").read_bytes()
        status, data = self.request("/__quickplay")
        self.assertEqual(status, 500, data)
        self.assertIn("history", data["error"].lower())
        self.assertEqual((self.data / "state.json").read_bytes(), before)
        appdata = server.build_appdata()
        self.assertEqual(appdata["quickPlayData"]["playRecords"], [])
        self.assertIn("error", appdata["quickPlayData"])

    def test_launch_retains_unavailable_source_binding_without_failing_spawn(self):
        original = self.record("one")
        source_before = self.source.read_bytes()
        def spawn(*args, **kwargs):
            self.source.write_text("{incomplete")
            return {"running": True, "pid": 42}
        with mock.patch.object(server.launcher, "start", side_effect=spawn):
            status, data = self.request("/__launch", {"configurationId": "one"})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["running"])
        self.assertIn("source", data.get("warning", ""))
        self.assertEqual(self.records(), [])
        history = server.read_store("__quickplay_state")
        self.assertIn(original["id"], history["entries"])
        self.source.write_bytes(source_before)
        self.assertEqual(self.record("one")["id"], original["id"])

    def test_quickplay_edits_are_seen_by_existing_host_watchers(self):
        before = server._watch_stamp(server.__file__)
        original_stat = server.os.stat
        def changed(path, *args, **kwargs):
            info = original_stat(path, *args, **kwargs)
            if os.path.basename(path) == "quickplay.py":
                return types.SimpleNamespace(st_mtime=info.st_mtime + 1, st_size=info.st_size)
            return info
        with mock.patch.object(server.os, "stat", side_effect=changed):
            after = server._watch_stamp(server.__file__)
        self.assertNotEqual(before, after)

    def test_hot_reload_refreshes_quickplay_module_with_os_operations_mocked(self):
        modules = []
        host = types.SimpleNamespace(RequestHandlerClass=None)
        with mock.patch.object(server.importlib, "reload", side_effect=lambda module: modules.append(module)), \
             mock.patch.object(server, "exec", return_value=None, create=True), \
             mock.patch.object(server.msa, "reset_after_reload", return_value=None):
            self.assertTrue(server._reload_idle_host(host))
        self.assertIn("quickplay", [module.__name__ for module in modules])

    def mirrored_profile(self):
        mirror = self.game / "launcher_profiles.json"
        mirror.write_text(json.dumps({"profiles": {"one": dict(self.profiles["one"])}}))
        before = self.record("one")
        status, data = self.request("/__quickplay/update", {"record": dict(before, name="Primary home", pinnedIndex=0)})
        self.assertEqual(status, 200, data)
        return mirror, before

    def split_mirror(self, mirror):
        mirror.write_text(json.dumps({"profiles": {"one": dict(self.profiles["one"],
            name="Separate mirror", gameDir=str(self.game / "split"))}}))
        return next(key for key, profile in server._effective_configurations().items()
                    if profile.get("gameDir") == str(self.game / "split"))

    def test_split_mirror_has_unique_quickplay_identity_and_keeps_primary_customization(self):
        mirror, original = self.mirrored_profile()
        split_id = self.split_mirror(mirror)
        records = self.records()
        self.assertEqual(len({item["id"] for item in records}), len(records))
        primary, split = self.record("one"), self.record(split_id)
        self.assertEqual(primary["id"], original["id"])
        self.assertEqual(primary["name"], "Primary home")
        self.assertEqual(primary["pinnedIndex"], 0)
        self.assertNotEqual(split["id"], original["id"])
        self.assertNotIn("name", split)
        self.assertNotIn("pinnedIndex", split)
        status, data = self.request("/__quickplay/update", {"record": dict(split, name="Separate home", pinnedIndex=1)})
        self.assertEqual(status, 200, data)
        self.assertEqual(self.record("one")["name"], "Primary home")
        self.assertEqual(self.record(split_id)["name"], "Separate home")
        mirror.write_text(json.dumps({"profiles": {"one": dict(self.profiles["one"])}}))
        merged = self.records()
        self.assertEqual(len(merged), 2)
        self.assertEqual(self.record("one")["id"], original["id"])
        self.assertEqual(self.record("one")["name"], "Primary home")
        split_id = self.split_mirror(mirror)
        self.assertEqual(self.record(split_id)["id"], split["id"])
        self.assertEqual(self.record(split_id)["name"], "Separate home")

    def test_diverged_mirror_cannot_claim_history_while_primary_source_is_unavailable(self):
        mirror, original = self.mirrored_profile()
        self.split_mirror(mirror)
        source_before = self.source.read_bytes()
        overlay_before = (self.data / "state.json").read_bytes()
        self.source.write_text("{incomplete")
        records = self.records()
        self.assertNotIn(original["id"], [item["id"] for item in records])
        self.assertNotIn("Primary home", [item.get("name") for item in records])
        self.assertEqual((self.data / "state.json").read_bytes(), overlay_before)
        self.source.write_bytes(source_before)
        self.assertEqual(self.record("one")["id"], original["id"])
        self.assertEqual(self.record("one")["name"], "Primary home")

    def test_split_mirror_launch_updates_its_own_history_and_profile_only(self):
        mirror, original = self.mirrored_profile()
        split_id = self.split_mirror(mirror)
        original_last_used = server._configuration("one")["lastUsed"]
        with mock.patch.object(server.launcher, "start", return_value={"running": True, "pid": 42}):
            status, data = self.request("/__launch", {"configurationId": split_id})
        self.assertEqual(status, 200, data)
        self.assertEqual(server._configuration("one")["lastUsed"], original_last_used)
        self.assertEqual(self.record("one")["id"], original["id"])
        self.assertEqual(self.record("one")["name"], "Primary home")
        self.assertNotEqual(self.record(split_id)["id"], original["id"])
        self.assertNotIn("name", self.record(split_id))

    def test_launch_does_not_update_diverged_mirror_when_primary_disappears_after_spawn(self):
        mirror, original = self.mirrored_profile()
        source_before = self.source.read_bytes()
        self.split_mirror(mirror)
        def spawn(*args, **kwargs):
            self.source.write_text("{incomplete")
            return {"running": True, "pid": 42}
        with mock.patch.object(server.launcher, "start", side_effect=spawn):
            status, data = self.request("/__launch", {"configurationId": "one"})
        self.assertEqual(status, 200, data)
        self.assertIn("source", data.get("warning", ""))
        self.assertNotIn(original["id"], [item["id"] for item in self.records()])
        self.source.write_bytes(source_before)
        self.assertEqual(self.record("one")["id"], original["id"])
        self.assertEqual(self.record("one")["name"], "Primary home")

    def test_hide_untracked_flag_requires_boolean_and_preserves_malformed_state(self):
        for value in ("false", "true", 0, 1, None, [], {}):
            with self.subTest(value=value):
                server.write_store("__quickplay_state", {"entries": {}, "hideUntracked": value})
                before = (self.data / "state.json").read_bytes()
                status, data = self.request("/__quickplay")
                self.assertEqual(status, 500, data)
                self.assertIn("history", data["error"].lower())
                self.assertEqual((self.data / "state.json").read_bytes(), before)
        server.write_store("__quickplay_state", {"entries": {}, "hideUntracked": False})
        self.assertEqual(len(self.records()), 2)
        server.write_store("__quickplay_state", {"entries": {}, "hideUntracked": True})
        self.assertEqual(self.records(), [])
