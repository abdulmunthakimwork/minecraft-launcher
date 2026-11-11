"""Host integration regressions; all state and game files are disposable."""
import http.client
import hashlib
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import unittest
import uuid
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
with tempfile.TemporaryDirectory() as bootstrap:
    with mock.patch.dict(os.environ, {"LAUNCHER_DATA_DIR": bootstrap}):
        import server


class HostRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for folder in ("state", "game", "data", "ui/static/js"):
            (self.root / folder).mkdir(parents=True)
        self.state, self.game = self.root / "state", self.root / "game"
        overrides = {
            "STATE": str(self.state), "GAME_DIR": str(self.game),
            "DATA_DIR": str(self.root / "data"),
            "STORE_OVERLAY": str(self.root / "data/state.json"),
            "OFFLINE_STORE": str(self.root / "data/offline_accounts.json"),
            "UI": str(self.root / "ui"), "VERSIONS": str(self.root / "versions.json"),
        }
        patcher = mock.patch.multiple(server, **overrides)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.online = {
            "id": "msa-" + "a" * 32, "javaProfileName": "OnlinePlayer",
            "offline": False, "remoteId": "a" * 32,
        }
        for name, value in (("account_record", None), ("game_identity", None)):
            patcher = mock.patch.object(server.msa, name, return_value=value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        (self.root / "versions.json").write_text(json.dumps({
            "latest": {"release": "1.21.1"}, "versions": {}}))

    def offline(self, active="local"):
        record = server._offline_record("LocalPlayer", "local")
        server._save_offline({"accounts": [record], "active": active, "hidden": []})
        return record

    def test_installed_neoforge_exposes_parent_to_editor(self):
        ident = "neoforge-21.11.48"
        folder = self.state / "versions" / ident
        folder.mkdir(parents=True)
        (folder / (ident + ".json")).write_text(json.dumps({
            "id": ident, "inheritsFrom": "1.21.11", "type": "release",
        }))
        version = server._load_versions()["versions"][ident]
        self.assertEqual(version["minecraftVersion"], "1.21.11")
        self.assertIn("modded", version["issues"])
        self.assertNotIn(ident, server._load_versions(selector=True)["versions"])

    def test_bom_profiles_and_overlay_keep_rows_and_deletion_tombstones(self):
        profiles = self.state / "launcher_profiles.json"
        profiles.write_text(json.dumps({"profiles": {
            "kept": {"name": "Windows profile", "lastVersionId": "1.21.1"},
            "deleted": {"name": "Removed profile", "lastVersionId": "1.20.1"},
        }}), encoding="utf-8-sig")
        overlay = self.root / "data/state.json"
        overlay.write_text(json.dumps({"__configurations": {
            "added": {"name": "Local instance", "lastVersionId": "neoforge-21.1.2"},
        }, "__deleted_configurations": ["deleted"]}), encoding="utf-8-sig")
        before = (profiles.read_bytes(), overlay.read_bytes())
        self.assertEqual(set(server._effective_configurations()), {"kept", "added"})
        self.assertEqual((profiles.read_bytes(), overlay.read_bytes()), before)

    def test_alternate_and_game_root_sources_keep_all_profiles_without_rewriting(self):
        sources = [
            (self.state / "launcher_profiles.json", "primary"),
            (self.state / "launcher_profiles_microsoft_store.json", "store"),
            (self.game / "launcher_profiles.json", "game"),
            (self.game / ".minecraft/launcher_profiles_microsoft_store.json", "nested"),
        ]
        before = {}
        for path, ident in sources:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"profiles": {ident: {
                "name": ident, "lastVersionId": "1.21.1"}}}), encoding="utf-8-sig")
            before[path] = path.read_bytes()
        self.assertEqual(set(server._effective_configurations()), {"primary", "store", "game", "nested"})
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_distinct_source_id_collisions_are_visible_and_independently_editable(self):
        for filename, name in [("launcher_profiles.json", "Primary"),
                               ("launcher_profiles_microsoft_store.json", "Store")]:
            (self.state / filename).write_text(json.dumps({"profiles": {"collision": {
                "name": name, "lastVersionId": "1.21.1"}}}))
        rows = server._effective_configurations()
        self.assertEqual({p["name"] for p in rows.values()}, {"Primary", "Store"})
        store_id = next(key for key, value in rows.items() if value["name"] == "Store")
        server._save_configuration(store_id, dict(rows[store_id], name="Edited store"))
        reloaded = server._effective_configurations()
        self.assertEqual(set(reloaded), set(rows))
        self.assertEqual(reloaded["collision"]["name"], "Primary")
        self.assertEqual(reloaded[store_id]["name"], "Edited store")
        server._delete_configuration(store_id)
        self.assertEqual({p["name"] for p in server._effective_configurations().values()}, {"Primary"})

    def test_identical_mirrored_profile_has_one_row_and_deletion_stays_deleted(self):
        profile = {"name": "Mirrored", "lastVersionId": "1.21.1"}
        for folder in (self.state, self.game):
            (folder / "launcher_profiles.json").write_text(json.dumps({"profiles": {"same": profile}}))
        self.assertEqual(set(server._effective_configurations()), {"same"})
        server._delete_configuration("same")
        self.assertEqual(server._effective_configurations(), {})

    def test_source_switch_does_not_hide_an_unrelated_profile_with_same_id(self):
        other = self.root / "other-state"
        other.mkdir()
        for folder, name in [(self.state, "Source A"), (other, "Source B")]:
            (folder / "launcher_profiles.json").write_text(json.dumps({"profiles": {"same": {
                "name": name, "lastVersionId": "1.21.1"}}}))
        server._delete_configuration("same")
        with mock.patch.object(server, "STATE", str(other)):
            self.assertEqual(server._effective_configurations().get("same", {}).get("name"), "Source B")
        self.assertEqual(server._effective_configurations(), {})

    def test_source_switch_does_not_apply_an_unrelated_profile_override(self):
        other = self.root / "other-state"
        other.mkdir()
        for folder, name in [(self.state, "Source A"), (other, "Source B")]:
            (folder / "launcher_profiles.json").write_text(json.dumps({"profiles": {"same": {
                "name": name, "lastVersionId": "1.21.1"}}}))
        server._save_configuration("same", {"name": "Edited A", "lastVersionId": "1.21.1"})
        with mock.patch.object(server, "STATE", str(other)):
            self.assertEqual(server._effective_configurations()["same"]["name"], "Source B")
        self.assertEqual(server._effective_configurations()["same"]["name"], "Edited A")

    def test_same_id_deletions_in_two_sources_do_not_resurrect_either_source(self):
        other = self.root / "other-state"
        other.mkdir()
        for folder, name in [(self.state, "Source A"), (other, "Source B")]:
            (folder / "launcher_profiles.json").write_text(json.dumps({"profiles": {"same": {
                "name": name, "lastVersionId": "1.21.1"}}}))
        server._delete_configuration("same")
        with mock.patch.object(server, "STATE", str(other)):
            server._delete_configuration("same")
            self.assertEqual(server._effective_configurations(), {})
        self.assertEqual(server._effective_configurations(), {})

    def test_saving_same_id_in_other_source_keeps_original_source_deleted(self):
        other = self.root / "other-state"
        other.mkdir()
        for folder, name in [(self.state, "Source A"), (other, "Source B")]:
            (folder / "launcher_profiles.json").write_text(json.dumps({"profiles": {"same": {
                "name": name, "lastVersionId": "1.21.1"}}}))
        server._delete_configuration("same")
        with mock.patch.object(server, "STATE", str(other)):
            server._save_configuration("same", {"name": "Edited B", "lastVersionId": "1.21.1"})
            self.assertEqual(server._effective_configurations()["same"]["name"], "Edited B")
        self.assertEqual(server._effective_configurations(), {})

    def test_edits_to_same_id_in_two_roots_keep_both_source_overrides(self):
        other = self.root / "other-state"
        other.mkdir()
        for folder, name in [(self.state, "Source A"), (other, "Source B")]:
            (folder / "launcher_profiles.json").write_text(json.dumps({"profiles": {"same": {
                "name": name, "lastVersionId": "1.21.1"}}}))
        server._save_configuration("same", {"name": "Edited A", "lastVersionId": "1.21.1"})
        with mock.patch.object(server, "STATE", str(other)):
            server._save_configuration("same", {"name": "Edited B", "lastVersionId": "1.21.1"})
            self.assertEqual([p["name"] for p in server._effective_configurations().values()], ["Edited B"])
        self.assertEqual([p["name"] for p in server._effective_configurations().values()], ["Edited A"])

    def test_deleting_archived_source_override_preserves_other_roots_saved_edit(self):
        other = self.root / "other-state"
        other.mkdir()
        for folder, name in [(self.state, "Source A"), (other, "Source B")]:
            (folder / "launcher_profiles.json").write_text(json.dumps({"profiles": {"same": {
                "name": name, "lastVersionId": "1.21.1"}}}))
        server._save_configuration("same", {"name": "Edited A", "lastVersionId": "1.21.1"})
        with mock.patch.object(server, "STATE", str(other)):
            server._save_configuration("same", {"name": "Edited B", "lastVersionId": "1.21.1"})
        server._delete_configuration("same")
        self.assertEqual(server._effective_configurations(), {})
        with mock.patch.object(server, "STATE", str(other)):
            self.assertEqual([p["name"] for p in server._effective_configurations().values()], ["Edited B"])

    def test_mirrors_of_a_secondary_colliding_source_do_not_duplicate_rows(self):
        for path, name in [(self.state / "launcher_profiles.json", "Primary"),
                           (self.state / "launcher_profiles_microsoft_store.json", "Mirrored store"),
                           (self.game / "launcher_profiles.json", "Mirrored store")]:
            path.write_text(json.dumps({"profiles": {"same": {"name": name, "lastVersionId": "1.21.1"}}}))
        profiles = server._effective_configurations()
        self.assertEqual(sorted(p["name"] for p in profiles.values()), ["Mirrored store", "Primary"])

    def test_generated_collision_identity_cannot_overwrite_an_existing_raw_profile(self):
        alternate = os.path.normcase(os.path.abspath(self.state / "launcher_profiles_microsoft_store.json"))
        reserved = "source-" + hashlib.sha256((alternate + "\0collision").encode("utf-8")).hexdigest()
        (self.state / "launcher_profiles.json").write_text(json.dumps({"profiles": {
            "collision": {"name": "Primary", "lastVersionId": "1.21.1"},
            reserved: {"name": "Reserved raw ID", "lastVersionId": "1.21.1"}}}))
        (self.state / "launcher_profiles_microsoft_store.json").write_text(json.dumps({"profiles": {
            "collision": {"name": "Alternate", "lastVersionId": "1.21.1"}}}))
        self.assertEqual(sorted(p["name"] for p in server._effective_configurations().values()),
                         ["Alternate", "Primary", "Reserved raw ID"])

    def _assert_saved_profile_survives_unreadable_source(self, unavailable, full=True):
        paths = {
            "Primary": self.state / "launcher_profiles.json",
            "Alternate": self.state / "launcher_profiles_microsoft_store.json",
        }
        for name, path in paths.items():
            path.write_text(json.dumps({"profiles": {"same": {
                "name": name, "type": "custom", "lastVersionId": "1.21.1",
                "gameDir": str(self.game / (name + "-instance")), "javaArgs": "-Xmx2G"}}}))
        source_before = {path: path.read_bytes() for path in paths.values()}
        profiles = server._effective_configurations()
        selected_id, selected = next((key, value) for key, value in profiles.items()
                                     if value["name"] == unavailable)
        patch = {"name": unavailable + " saved", "javaArgs": "-Xmx4G"}
        server._save_configuration(selected_id, dict(selected, **patch) if full else patch)
        expected = server._effective_configurations()
        overlay = self.root / "data/state.json"
        overlay_before = overlay.read_bytes()
        try:
            paths[unavailable].write_text("{partially-written-json")
            actual = server._effective_configurations()
            selected_during = server._configuration(selected_id) or {}
            self.assertEqual(selected_during.get("gameDir"), selected["gameDir"],
                             "An existing installation ID rebound to another source directory")
            self.assertEqual(actual, expected)
            self.assertEqual(overlay.read_bytes(), overlay_before)
        finally:
            for path, value in source_before.items():
                path.write_bytes(value)
        self.assertEqual(server._effective_configurations(), expected)
        self.assertEqual(overlay.read_bytes(), overlay_before)

    def test_saved_primary_identity_survives_temporarily_unreadable_colliding_source(self):
        self._assert_saved_profile_survives_unreadable_source("Primary")

    def test_saved_alternate_identity_survives_temporarily_unreadable_colliding_source(self):
        self._assert_saved_profile_survives_unreadable_source("Alternate")

    def test_partial_saved_edit_keeps_source_root_when_profile_source_is_unreadable(self):
        self._assert_saved_profile_survives_unreadable_source("Primary", full=False)

    def test_saved_source_identity_survives_colliding_root_precedence_change(self):
        for folder, name in ((self.state, "Original"), (self.game, "Other")):
            (folder / "launcher_profiles.json").write_text(json.dumps({"profiles": {"same": {
                "name": name, "lastVersionId": "1.21.1", "gameDir": str(self.root / name)}}}))
        server._save_configuration("same", dict(server._configuration("same"), name="Original saved"))
        expected = server._effective_configurations()
        with mock.patch.multiple(server, STATE=str(self.game), GAME_DIR=str(self.state)):
            self.assertEqual(server._configuration("same")["gameDir"], str(self.root / "Original"))
            self.assertEqual(server._effective_configurations(), expected)

    def test_deleted_source_id_cannot_rebind_when_its_primary_file_is_unreadable(self):
        for filename, name in (("launcher_profiles.json", "Deleted primary"),
                               ("launcher_profiles_microsoft_store.json", "Surviving alternate")):
            (self.state / filename).write_text(json.dumps({"profiles": {"same": {
                "name": name, "lastVersionId": "1.21.1", "gameDir": str(self.root / name)}}}))
        server._delete_configuration("same")
        expected = server._effective_configurations()
        primary = self.state / "launcher_profiles.json"
        before = primary.read_bytes()
        overlay = self.root / "data/state.json"
        overlay_before = overlay.read_bytes()
        try:
            primary.write_text("{partially-written-json")
            self.assertIsNone(server._configuration("same"), "A deleted ID rebound to another installation")
            self.assertEqual(server._effective_configurations(), expected)
            self.assertEqual(overlay.read_bytes(), overlay_before)
        finally:
            primary.write_bytes(before)

    def test_unsaved_profile_id_fails_closed_during_source_failure_and_restores(self):
        for filename, name in (("launcher_profiles.json", "Primary"),
                               ("launcher_profiles_microsoft_store.json", "Alternate")):
            (self.state / filename).write_text(json.dumps({"profiles": {"same": {
                "name": name, "lastVersionId": "1.21.1", "gameDir": str(self.root / name)}}}))
        expected = server._effective_configurations()
        alternate_id = next(key for key, profile in expected.items() if profile["name"] == "Alternate")
        primary = self.state / "launcher_profiles.json"
        before = primary.read_bytes()
        try:
            primary.write_text("{partially-written-json")
            self.assertIsNone(server._configuration("same"), "A stale unsaved ID selected another directory")
            self.assertEqual(server._effective_configurations(), {alternate_id: expected[alternate_id]})
            with mock.patch.object(server.launcher, "status", return_value={"running": False}), \
                 mock.patch.object(server.modpacks, "busy", return_value=False), \
                 mock.patch.object(server.installer, "state", return_value={"active": False}):
                status, body = self.request("/__instances/same/general", {"patch": {"name": "Wrong target"}})
            self.assertEqual(status, 404, body)
            self.assertEqual(json.loads(body)["code"], "missing_instance")
            self.assertFalse((self.root / "data/state.json").exists())
        finally:
            primary.write_bytes(before)
        self.assertEqual(server._effective_configurations(), expected)
        self.assertFalse((self.root / "data/state.json").exists())

    def test_unsaved_identity_bindings_survive_root_precedence_change(self):
        for folder, name in ((self.state, "Original"), (self.game, "Other")):
            (folder / "launcher_profiles.json").write_text(json.dumps({"profiles": {"same": {
                "name": name, "lastVersionId": "1.21.1", "gameDir": str(self.root / name)}}}))
        expected = server._effective_configurations()
        with mock.patch.multiple(server, STATE=str(self.game), GAME_DIR=str(self.state)):
            self.assertEqual(server._configuration("same")["gameDir"], str(self.root / "Original"))
            self.assertEqual(server._effective_configurations(), expected)
        self.assertFalse((self.root / "data/state.json").exists())

    def test_stale_explicit_configuration_cannot_launch_or_install_default_root(self):
        for filename, name in (("launcher_profiles.json", "Primary"),
                               ("launcher_profiles_microsoft_store.json", "Alternate")):
            (self.state / filename).write_text(json.dumps({"profiles": {"same": {
                "name": name, "lastVersionId": "1.21.1", "gameDir": str(self.root / name)}}}))
        server._effective_configurations()
        version = self.state / "versions/1.21.1"
        version.mkdir(parents=True)
        (version / "1.21.1.json").write_text(json.dumps({"id": "1.21.1", "libraries": []}))
        self.offline()
        primary = self.state / "launcher_profiles.json"
        before = primary.read_bytes()
        try:
            primary.write_text("{partially-written-json")
            with mock.patch.object(server.launcher, "status", return_value={"running": False}), \
                 mock.patch.object(server.modpacks, "busy", return_value=False), \
                 mock.patch.object(server.installer, "state", return_value={"active": False}), \
                 mock.patch.object(server.launcher, "start", return_value={"running": False}) as launch, \
                 mock.patch.object(server.installer, "start", return_value={"active": False}) as install:
                for path in ("/__launch", "/__install"):
                    with self.subTest(path=path):
                        status, body = self.request(path, {"configurationId": "same", "versionId": "1.21.1"})
                        self.assertEqual(status, 404, body)
                launch.assert_not_called()
                install.assert_not_called()
            self.assertFalse((self.root / "data/state.json").exists())
        finally:
            primary.write_bytes(before)

    def test_unsaved_identity_bindings_survive_host_module_reload_without_writes(self):
        for filename, name in (("launcher_profiles.json", "Primary"),
                               ("launcher_profiles_microsoft_store.json", "Alternate")):
            (self.state / filename).write_text(json.dumps({"profiles": {"same": {
                "name": name, "lastVersionId": "1.21.1", "gameDir": str(self.root / name)}}}))
        script = '''
import importlib, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import server
primary = Path(server.STATE) / "launcher_profiles.json"
original = primary.read_bytes()
expected = server._effective_configurations()
alternate = next(key for key, value in expected.items() if value["name"] == "Alternate")
try:
    primary.write_text("{partially-written-json")
    importlib.reload(server)
    assert server._configuration("same") is None, "Hot reload rebound a stale installation ID"
    assert server._effective_configurations() == {alternate: expected[alternate]}
    assert not Path(server.STORE_OVERLAY).exists(), "Read or reload wrote an overlay"
finally:
    primary.write_bytes(original)
assert server._effective_configurations() == expected
print("identity reload passed")
'''
        env = dict(os.environ, LAUNCHER_ROOT=str(self.root), LAUNCHER_STATE_DIR=str(self.state),
                   LAUNCHER_GAME_DIR=str(self.game), LAUNCHER_DATA_DIR=str(self.root / "data"))
        result = subprocess.run([sys.executable, "-B", "-c", script, str(Path(__file__).parent)],
                                env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.root / "data/state.json").exists())

    def test_identity_registry_is_bounded_and_separates_fixture_root_sets(self):
        with mock.patch.multiple(server, _IDENTITY_CONTEXT_LIMIT=2, _IDENTITY_PROFILE_LIMIT=1, create=True):
            for index in range(3):
                selected = self.root / ("source-" + str(index))
                selected.mkdir()
                (selected / "launcher_profiles.json").write_text(json.dumps({"profiles": {
                    "one": {"name": "Root " + str(index), "lastVersionId": "1.21.1"},
                    "two": {"name": "Overflow " + str(index), "lastVersionId": "1.21.1"}}}))
                with mock.patch.object(server, "STATE", str(selected)):
                    initial = server._effective_configurations()
                    self.assertEqual(server._configuration("one")["name"], "Root " + str(index))
                    self.assertEqual(server._effective_configurations(), initial)
                    self.assertEqual(len(initial), 2)
            registry = getattr(server, "_CONFIGURATION_IDENTITIES", None)
            self.assertIsInstance(registry, dict)
            self.assertLessEqual(len(registry), 2)
            for context, identities in registry.items():
                if context[0] == os.path.normcase(os.path.abspath(self.root / "data")):
                    self.assertLessEqual(len(identities), 1)
        self.assertFalse((self.root / "data/state.json").exists())

    def test_profile_map_key_is_authoritative_over_stale_embedded_id(self):
        (self.state / "launcher_profiles.json").write_text(json.dumps({"profiles": {
            "authoritative": {"id": "stale", "name": "Imported", "lastVersionId": "1.21.1"}}}))
        server._save_configuration("new-authoritative", {
            "id": "stale", "name": "Saved", "lastVersionId": "1.21.1"})
        facade_ids = [{"id": key, **profile}["id"]
                      for key, profile in server._effective_configurations().items()]
        self.assertEqual(set(facade_ids), {"authoritative", "new-authoritative"})

    def test_uncatalogued_local_vanilla_release_is_not_hidden_by_modded_filter(self):
        folder = self.state / "versions/1.21.11"
        folder.mkdir(parents=True)
        (folder / "1.21.11.json").write_text(json.dumps({
            "id": "1.21.11", "type": "release", "libraries": [],
            "mainClass": "net.minecraft.client.main.Main"}))
        self.assertEqual(server._load_versions()["versions"]["1.21.11"]["issues"], [])
        self.assertIn("1.21.11", server._load_versions(selector=True)["versions"])

    def test_partial_local_library_metadata_does_not_break_profile_visibility(self):
        folder = self.state / "versions/1.21.11"
        folder.mkdir(parents=True)
        for libraries in (None, 7):
            with self.subTest(libraries=libraries):
                (folder / "1.21.11.json").write_text(json.dumps({
                    "id": "1.21.11", "type": "release", "libraries": libraries}))
                try:
                    versions = server._load_versions()["versions"]
                    context = server._installation_context({"lastVersionId": "1.21.11"})
                except TypeError as error:
                    self.fail("Partial library metadata crashed installation discovery: " + str(error))
                self.assertIn("1.21.11", versions)
                self.assertEqual(context["loader"], "vanilla")

    def test_copy_keeps_source_only_loader_metadata_and_inherited_metadata(self):
        instance = self.root / "source-instance"
        files = {
            "custom-local-loader": {"id": "custom-local-loader", "inheritsFrom": "private-base",
                "type": "release", "libraries": [{"name": "net.fabricmc:fabric-loader:0.15.11"}]},
            "private-base": {"id": "private-base", "inheritsFrom": "1.21.1", "libraries": []},
            "unused-loader": {"id": "unused-loader", "libraries": []},
        }
        for ident, doc in files.items():
            folder = instance / "versions" / ident
            folder.mkdir(parents=True)
            (folder / (ident + ".json")).write_text(json.dumps(doc), encoding="utf-8-sig")
            (folder / (ident + ".jar")).write_bytes(b"unrelated large game artifact")
        server._save_configuration("original", {"name": "Original", "type": "custom",
            "lastVersionId": "custom-local-loader", "gameDir": str(instance)})
        before = {path: path.read_bytes() for path in instance.rglob("*") if path.is_file()}
        with mock.patch.object(server.launcher, "status", return_value={"running": False}), \
             mock.patch.object(server.modpacks, "busy", return_value=False), \
             mock.patch.object(server.installer, "state", return_value={"active": False}), \
             mock.patch.object(server.urllib.request, "urlopen", side_effect=AssertionError("unexpected network")):
            service = server._installation_service()
            original_context = service.get("original")["context"]
            result = service.post("original", "copy", {})
            copied = server._configuration(result["instanceId"])
            self.assertEqual(service.get(result["instanceId"])["context"], original_context)
            for ident in ("custom-local-loader", "private-base"):
                path, error = server._version_metadata(ident, run_dir=copied["gameDir"])
                self.assertIsNone(error)
                self.assertEqual(Path(path).read_bytes(), before[instance / "versions" / ident / (ident + ".json")])
        copied_root = Path(copied["gameDir"])
        self.assertFalse((copied_root / "versions/unused-loader").exists())
        self.assertFalse(list(copied_root.rglob("*.jar")))
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_absent_catalog_uses_cached_manifest_and_keeps_local_profile_versions(self):
        (self.root / "versions.json").unlink()
        cached = self.state / "versions/version_manifest_v2.json"
        cached.parent.mkdir(parents=True)
        cached.write_text(json.dumps({"latest": {"release": "1.21.1", "snapshot": "24w14a"},
            "versions": [{"id": "1.21.1", "type": "release"},
                         {"id": "24w14a", "type": "snapshot"}]}))
        server._save_configuration("uncaptured", {"name": "Existing installation", "lastVersionId": "1.7.10"})
        versions = server._load_versions()
        self.assertEqual(versions["latest"], {"release": "1.21.1", "snapshot": "24w14a"})
        self.assertTrue({"1.21.1", "24w14a", "1.7.10"}.issubset(versions["versions"]))
        self.assertEqual(versions["versions"]["1.7.10"]["issues"], [])

    def test_malformed_catalog_does_not_drop_saved_installation_versions(self):
        (self.root / "versions.json").write_text("partial manifest")
        server._save_configuration("kept", {"name": "Modded", "lastVersionId": "fabric-loader-0.16-1.21.1"})
        self.assertIn("fabric-loader-0.16-1.21.1", server._load_versions()["versions"])

    def test_game_and_profile_directories_supply_installed_version_metadata(self):
        for ident, folder in [("neoforge-21.1.2", self.game),
                              ("neoforge-21.1.3", self.root / "separate-instance")]:
            version_dir = folder / "versions" / ident
            version_dir.mkdir(parents=True)
            (version_dir / (ident + ".json")).write_text(json.dumps({
                "id": ident, "type": "release", "inheritsFrom": "1.21.1",
            }), encoding="utf-8-sig")
            server._save_configuration(ident, {"name": ident, "lastVersionId": ident,
                                              "gameDir": str(folder)})
        versions = server._load_versions()["versions"]
        for ident in ("neoforge-21.1.2", "neoforge-21.1.3"):
            self.assertEqual(versions[ident]["minecraftVersion"], "1.21.1")
            self.assertEqual(versions[ident]["issues"], ["modded"])
            self.assertNotIn(ident, server._load_versions(selector=True)["versions"])

    def test_captured_manifest_keeps_latest_authority_and_state_metadata_wins(self):
        (self.root / "versions.json").write_text(json.dumps({
            "latest": {"release": "1.21.1", "snapshot": "new-snapshot"}, "versions": {}}))
        (self.state / "versions").mkdir()
        (self.state / "versions/version_manifest_v2.json").write_text(json.dumps({
            "latest": {"release": "1.20.1", "snapshot": "old-snapshot"}, "versions": []}))
        ident = "neoforge-21.1.2"
        for folder, parent in [(self.state, "1.21.1"), (self.game, "1.20.1")]:
            version_dir = folder / "versions" / ident
            version_dir.mkdir(parents=True)
            (version_dir / (ident + ".json")).write_text(json.dumps({"id": ident, "inheritsFrom": parent}))
        versions = server._load_versions()
        self.assertEqual(versions["latest"], {"release": "1.21.1", "snapshot": "new-snapshot"})
        self.assertEqual(versions["versions"][ident]["minecraftVersion"], "1.21.1")

    def test_shared_metadata_supplies_inherited_install_plan_without_download(self):
        for ident, folder, value in [
            ("base", self.state, {"downloads": {"client": {"url": "https://example.invalid/client", "size": 100}}}),
            ("neoforge-test", self.game, {"inheritsFrom": "base", "libraries": [{"downloads": {
                "artifact": {"path": "loader.jar", "url": "https://example.invalid/library", "size": 10},
            }}]}),
        ]:
            version_dir = folder / "versions" / ident
            version_dir.mkdir(parents=True)
            (version_dir / (ident + ".json")).write_text(json.dumps({"id": ident, **value}), encoding="utf-8-sig")
        with mock.patch.object(server.urllib.request, "urlopen", side_effect=AssertionError("unexpected metadata download")):
            path, error = server._version_metadata("neoforge-test")
            plan = server._install_plan("neoforge-test")
        self.assertIsNone(error)
        self.assertTrue(os.path.samefile(path, self.game / "versions/neoforge-test/neoforge-test.json"))
        self.assertEqual(plan, {"versionId": "neoforge-test", "downloadRequiredForAction": 110,
                                "storageUsedAfter": 110, "storageRequiredForAction": 110})

    def test_profile_instance_metadata_supplies_modpack_dependency(self):
        instance = self.root / "saved-instance"
        version_dir = instance / "versions/neoforge-21.1.2"
        version_dir.mkdir(parents=True)
        (version_dir / "neoforge-21.1.2.json").write_text(json.dumps({
            "id": "neoforge-21.1.2", "inheritsFrom": "1.21.1",
        }), encoding="utf-8-sig")
        self.assertEqual(server._modpack_dependencies({"lastVersionId": "neoforge-21.1.2", "gameDir": str(instance)}),
                         {"minecraft": "1.21.1", "neoforge": "21.1.2"})

    def test_loader_base_doc_accepts_bom_shared_metadata(self):
        version_dir = self.game / "versions/1.21.1"
        version_dir.mkdir(parents=True)
        doc = {"id": "1.21.1", "libraries": [], "mainClass": "FixtureVanilla"}
        (version_dir / "1.21.1.json").write_text(json.dumps(doc), encoding="utf-8-sig")
        with mock.patch.object(server.urllib.request, "urlopen", side_effect=AssertionError("unexpected metadata download")):
            actual, error = server._base_version_doc("1.21.1")
        self.assertIsNone(error)
        self.assertEqual(actual, doc)

    def test_install_keeps_selected_instance_metadata_context(self):
        instance = self.root / "saved-instance"
        version_dir = instance / "versions/neoforge-21.1.2"
        version_dir.mkdir(parents=True)
        metadata = version_dir / "neoforge-21.1.2.json"
        metadata.write_text(json.dumps({"id": "neoforge-21.1.2"}))
        server._save_configuration("instance", {"name": "Saved instance", "lastVersionId": "neoforge-21.1.2",
                                               "gameDir": str(instance)})
        with mock.patch.object(server.installer, "start", return_value={"phase": "calculating"}) as start, \
             mock.patch.object(server.urllib.request, "urlopen", side_effect=AssertionError("unexpected metadata download")):
            status, body = self.request("/__install", {"configurationId": "instance"})
        self.assertEqual(status, 200, body)
        self.assertEqual(start.call_args.args[:2], (str(self.game), str(self.state)))
        self.assertTrue(os.path.samefile(start.call_args.args[2], metadata))
        self.assertEqual(start.call_args.kwargs, {"run_dir": str(instance)})

    def test_version_metadata_cannot_read_outside_store(self):
        (self.state / "escape.json").write_text(json.dumps({"id": "escape"}))
        path, error = server._version_metadata("../escape")
        self.assertIsNone(path)
        self.assertTrue(error)

    def request(self, path, body=None, headers=None):
        host = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        worker = threading.Thread(target=host.serve_forever, daemon=True)
        worker.start()
        try:
            connection = http.client.HTTPConnection(*host.server_address, timeout=3)
            method = "POST" if body is not None else "GET"
            connection.request(method, path, json.dumps(body) if body is not None else None,
                               headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            host.shutdown()
            host.server_close()
            worker.join(2)

    def test_offline_selection_wins_over_online_session_for_launch(self):
        self.offline()
        self.account_record.return_value = self.online
        self.game_identity.return_value = {"name": "OnlinePlayer", "uuid": "a" * 32,
                                           "access_token": "synthetic-token", "xuid": "12"}
        with mock.patch.object(server.launcher, "start", return_value={"running": True}) as start:
            status, _ = self.request("/__launch", {"versionId": "1.21.1"})
        self.assertEqual(status, 200)
        player = start.call_args.kwargs["player"]
        self.assertEqual(player["name"], "LocalPlayer")
        self.assertEqual(player["user_type"], "legacy")
        self.assertEqual(player["access_token"], "0")
        self.assertEqual(player["uuid"], uuid.uuid3(uuid.NAMESPACE_DNS,
                         "OfflinePlayer:LocalPlayer").hex)
        self.game_identity.assert_not_called()

    def test_local_texture_identity_matches_selected_online_account(self):
        self.offline(active=self.online["id"])
        self.account_record.return_value = self.online
        self.assertEqual(server._active_identity(), ("OnlinePlayer", "a" * 32))

    def test_no_account_cannot_start_game_with_fallback_player(self):
        with mock.patch.object(server.launcher, "start", return_value={}) as start:
            status, body = self.request("/__launch", {"versionId": "1.21.1"})
        self.assertEqual(status, 409, body)
        start.assert_not_called()

    def test_stale_active_account_falls_back_to_existing_account(self):
        self.offline(active="msa-expired")
        accounts, active = server.build_accounts()
        self.assertIn(active, [a["id"] for a in accounts])

    def test_captured_tokens_are_not_returned_to_renderer(self):
        (self.state / "launcher_accounts_microsoft_store.json").write_text(json.dumps({
            "accounts": {"captured": {"localId": "captured", "username": "Captured",
                         "accessToken": "synthetic-secret", "azureToken": "azure-secret",
                         "xbox": {"token": "xbox-secret"}}},
            "activeAccountLocalId": "captured"}))
        serialized = json.dumps(server.accounts_payload())
        for value in ("synthetic-secret", "azure-secret", "xbox-secret"):
            self.assertNotIn(value, serialized)

    def test_parallel_store_writes_preserve_both_values(self):
        original_load = server._load_overlay
        first_read, second_attempt = threading.Event(), threading.Event()
        def controlled_load():
            result = original_load()
            if threading.current_thread().name == "first":
                first_read.set()
                self.assertTrue(second_attempt.wait(2))
                # Give the baseline second writer time to finish reading/writing.
                second_done.wait(0.15)
            return result
        second_done = threading.Event()
        errors = []
        def write_second():
            first_read.wait(2)
            second_attempt.set()
            try:
                server.write_store("second", "B")
            except Exception as exc:
                errors.append(exc)
            finally:
                second_done.set()
        with mock.patch.object(server, "_load_overlay", side_effect=controlled_load):
            first = threading.Thread(target=lambda: server.write_store("first", "A"), name="first")
            second = threading.Thread(target=write_second, name="second")
            first.start(); second.start()
            first.join(3); second.join(3)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertFalse(errors)
        self.assertEqual(original_load(), {"first": "A", "second": "B"})

    def test_manual_loader_export_includes_loader_and_base_version(self):
        examples = {
            "fabric-loader-0.16.9-1.21.1": {"minecraft": "1.21.1", "fabric-loader": "0.16.9"},
            "quilt-loader-0.26.4-1.21.1": {"minecraft": "1.21.1", "quilt-loader": "0.26.4"},
            "forge-1.21.1-52.0.16": {"minecraft": "1.21.1", "forge": "52.0.16"},
            "neoforge-21.1.72": {"minecraft": "1.21.1", "neoforge": "21.1.72"},
        }
        for ident, expected in examples.items():
            folder = self.state / "versions" / ident
            folder.mkdir(parents=True)
            (folder / (ident + ".json")).write_text(json.dumps({"id": ident, "inheritsFrom": "1.21.1"}))
            with self.subTest(ident=ident):
                self.assertEqual(server._modpack_dependencies({"lastVersionId": ident}), expected)

    def test_saved_data_is_removed_only_when_requested(self):
        for folder in ("saves", "screenshots", "logs"):
            target = self.game / folder
            target.mkdir()
            (target / "example").write_text("fixture")
        vdir = self.game / "versions/1.21.1"
        vdir.mkdir(parents=True)
        (vdir / "1.21.1.jar").write_bytes(b"fixture")
        self.assertTrue(server._uninstall("java", False)["ok"])
        self.assertTrue((self.game / "saves/example").is_file())
        self.assertTrue(server._uninstall("java", True)["ok"])
        for folder in ("saves", "screenshots", "logs"):
            self.assertFalse((self.game / folder).exists())

    def test_raw_static_path_cannot_escape_ui_root(self):
        (self.root / "outside.js").write_text("synthetic outside fixture")
        status, body = self.request("/static/js/../../../outside.js")
        self.assertEqual(status, 404)
        self.assertNotIn(b"synthetic outside fixture", body)

    def test_foreign_host_is_refused_on_get(self):
        status, _ = self.request("/__accounts", headers={"Host": "untrusted.invalid"})
        self.assertEqual(status, 403)

    def test_localhost_alias_origin_can_write(self):
        # Check directly against a real listener's port, avoiding DNS variability.
        host = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        worker = threading.Thread(target=host.serve_forever, daemon=True)
        worker.start()
        try:
            connection = http.client.HTTPConnection(*host.server_address, timeout=3)
            connection.request("POST", "/__state/test", json.dumps({"value": "yes"}),
                               {"Origin": "http://localhost:%d" % host.server_address[1]})
            response = connection.getresponse()
            self.assertEqual(response.status, 200, response.read())
        finally:
            host.shutdown(); host.server_close(); worker.join(2)

    def test_skin_delete_library_and_selection_persist_together(self):
        server.write_store("__custom_skins", json.dumps({"old": {"id": "old"}}))
        server.write_store("__active_skin_id", "old")
        status, body = self.request("/__skin_state", {"customSkins": {}, "activeSkinId": "steve"})
        self.assertEqual(status, 200, body)
        overlay = server._load_overlay()
        self.assertEqual(json.loads(overlay["__custom_skins"]), {})
        self.assertEqual(overlay["__active_skin_id"], "steve")

    def test_persistence_failure_returns_http_failure(self):
        with mock.patch.object(server, "_write_overlay", side_effect=OSError("synthetic disk failure")):
            status, body = self.request("/__launcher_settings", {"locale": "fr-FR"})
        self.assertEqual(status, 500, body)
        self.assertIn(b"synthetic disk failure", body)

    def test_configuration_settings_patch_preserves_native_filters_sort_and_output_flags(self):
        source = self.state / "launcher_settings.json"
        source.write_text(json.dumps({"locale": "en-US", "enableReleases": True,
            "enableSnapshots": False, "uiGameOutputEnabled": True}))
        source_before = source.read_bytes()
        server.write_store("__configuration_settings", {"enableSnapshots": True, "profileSorting": "ByLastPlayed"})
        status, raw = self.request("/__configuration_settings", {"showGameLog": True})
        self.assertEqual(status, 200, raw)
        result = json.loads(raw)
        self.assertIsInstance(result.get("settings"), dict)
        settings = result["settings"]
        self.assertTrue(settings["showGameLog"])
        self.assertTrue(settings["enableSnapshots"])
        self.assertTrue(settings["enableReleases"])
        self.assertTrue(settings["uiGameOutputEnabled"])
        self.assertEqual(settings["profileSorting"], "ByLastPlayed")
        self.assertEqual(server.build_appdata()["configurationData"]["settings"], settings)
        self.assertEqual(source.read_bytes(), source_before)

    def test_second_configuration_settings_patch_keeps_previous_toggle_after_reload(self):
        source = self.state / "launcher_settings.json"
        source.write_text(json.dumps({"locale": "en-US", "showGameLog": False}))
        for patch in ({"showGameLog": True}, {"enableHistorical": True}):
            status, raw = self.request("/__configuration_settings", patch)
            self.assertEqual(status, 200, raw)
        settings = server.build_appdata()["configurationData"]["settings"]
        self.assertTrue(settings["showGameLog"])
        self.assertTrue(settings["enableHistorical"])
        self.assertEqual(settings["locale"], "en-US")

    def test_configuration_settings_failure_returns_error_and_keeps_saved_settings(self):
        server.write_store("__configuration_settings", {"showGameLog": False, "profileSorting": "ByLastPlayed"})
        before = Path(server.STORE_OVERLAY).read_bytes()
        with mock.patch.object(server, "_write_overlay", side_effect=OSError("synthetic settings disk failure")):
            status, raw = self.request("/__configuration_settings", {"showGameLog": True})
        self.assertEqual(status, 500, raw)
        self.assertIn(b"synthetic settings disk failure", raw)
        self.assertEqual(Path(server.STORE_OVERLAY).read_bytes(), before)

    def test_non_object_post_is_rejected_without_disconnect(self):
        status, body = self.request("/__launcher_settings", ["invalid"])
        self.assertEqual(status, 400, body)

    def test_appdata_does_not_treat_client_jar_as_complete_installation(self):
        vdir = self.game / "versions/1.21.1"
        vdir.mkdir(parents=True)
        (vdir / "1.21.1.jar").write_bytes(b"fixture")
        with mock.patch.object(server.installer, "is_installed", return_value=False), \
             mock.patch.object(server, "_install_plan", return_value={"versionId": "1.21.1"}):
            entry = server.build_appdata()["productInfo"]["products"]["java"]["versionData"][0]
        self.assertFalse(entry["isInstalled"])
        self.assertIsNotNone(entry["pendingUpdate"])

    def test_prerelease_loader_export_retains_complete_loader_version(self):
        ident = "quilt-loader-0.26.4-beta.1-1.21.1"
        folder = self.state / "versions" / ident
        folder.mkdir(parents=True)
        (folder / (ident + ".json")).write_text(json.dumps({"id": ident, "inheritsFrom": "1.21.1"}))
        self.assertEqual(server._modpack_dependencies({"lastVersionId": ident}),
                         {"minecraft": "1.21.1", "quilt-loader": "0.26.4-beta.1"})

    def test_official_forge_installer_profile_exports_loader_dependency(self):
        ident = "1.21.1-forge-52.0.16"
        folder = self.state / "versions" / ident
        folder.mkdir(parents=True)
        (folder / (ident + ".json")).write_text(json.dumps({
            "id": ident, "inheritsFrom": "1.21.1",
            "libraries": [{"name": "net.minecraftforge:forge:1.21.1-52.0.16:universal"}]}))
        self.assertEqual(server._modpack_dependencies({"lastVersionId": ident}),
                         {"minecraft": "1.21.1", "forge": "52.0.16"})

    def test_first_run_seed_does_not_copy_builder_offline_accounts(self):
        assets = self.root / "resources/harness"
        assets.mkdir(parents=True)
        (assets / "offline_accounts.json").write_text(json.dumps({"accounts": [{"username": "Builder"}]}))
        Path(server.OFFLINE_STORE).unlink(missing_ok=True)
        with mock.patch.object(server, "BASE", str(self.root / "resources")):
            server._seed_data_dir()
        self.assertFalse(Path(server.OFFLINE_STORE).exists())

    def test_release_does_not_package_builder_offline_accounts(self):
        package = json.loads((Path(__file__).parent.parent / "app/package.json").read_text())
        filters = next(item["filter"] for item in package["build"]["extraResources"]
                       if item.get("to") == "root/harness")
        self.assertNotIn("offline_accounts.json", filters)

    def test_native_row_identity_patch_matches_the_extracted_component_once(self):
        source = Path(__file__).parent.parent / "minecraft-launcher-re/src/ui/static/js/1375.e8efa6e2.chunk.js"
        text = source.read_text(encoding="utf-8")
        seam = next((old, new) for old, new in server._ORIGIN_REWRITES
                    if 'data-configuration-id' in new)
        self.assertEqual(text.count(seam[0]), 1,
                         "Re-extraction changed the installation row; update its serve-time identity seam.")


if __name__ == "__main__":
    unittest.main()
