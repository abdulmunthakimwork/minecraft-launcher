"""Installation workspace HTTP regressions with disposable files and host callbacks."""
import base64
import http.client
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).parent))
with tempfile.TemporaryDirectory() as bootstrap:
    with mock.patch.dict(os.environ, {"LAUNCHER_DATA_DIR": bootstrap}):
        import server


class InstallationHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state, self.game, self.data = (self.root / name for name in ("state", "game", "data"))
        for path in (self.state, self.game, self.data, self.root / "ui"):
            path.mkdir()
        self.one, self.two = self.game / "instances/one", self.game / "instances/two"
        self.one.mkdir(parents=True)
        self.two.mkdir(parents=True)
        self.profiles = {
            "one": {"name": "Same name", "type": "custom", "lastVersionId": "1.20.1", "gameDir": str(self.one), "unknown": {"keep": True}},
            "two": {"name": "Same name", "type": "custom", "lastVersionId": "1.20.1", "gameDir": str(self.two)},
        }
        (self.state / "launcher_profiles.json").write_text(json.dumps({"profiles": self.profiles}))
        (self.root / "versions.json").write_text(json.dumps({"latest": {"release": "1.20.1"}, "versions": {
            "1.20.1": {"id": "1.20.1", "type": "release"}, "1.21.1": {"id": "1.21.1", "type": "release"}}}))
        (self.root / "ui/index.html").write_text("<!doctype html><html><head></head><body>Fixture</body></html>")
        patches = mock.patch.multiple(server, STATE=str(self.state), GAME_DIR=str(self.game), DATA_DIR=str(self.data),
            STORE_OVERLAY=str(self.data / "state.json"), OFFLINE_STORE=str(self.data / "offline_accounts.json"),
            UI=str(self.root / "ui"), VERSIONS=str(self.root / "versions.json"), _LAUNCH_INSTANCE=None, create=True)
        patches.start()
        self.addCleanup(patches.stop)
        self.running = False
        self.installing = False
        self.modpacking = False
        self.launch_snapshot = {"running": False, "logPath": None}
        for target, name, callback in (
            (server.launcher, "status", lambda: dict(self.launch_snapshot, running=self.running)),
            (server.installer, "state", lambda: {"active": self.installing}),
            (server.modpacks, "busy", lambda: self.modpacking),
            (server, "build_appdata", lambda: {}),
        ):
            patcher = mock.patch.object(target, name, side_effect=callback)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.host = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.worker = threading.Thread(target=self.host.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.close_host)

    def close_host(self):
        self.host.shutdown()
        self.host.server_close()
        self.worker.join(2)

    def request(self, path, body=None, headers=None, raw=None):
        connection = http.client.HTTPConnection(*self.host.server_address, timeout=3)
        try:
            payload = json.dumps(body) if body is not None else raw
            connection.request("POST" if payload is not None else "GET", path, payload, headers or {})
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def json(self, path, body=None, headers=None):
        status, raw, _ = self.request(path, body, headers)
        return status, json.loads(raw)

    def test_exact_id_selects_identically_named_installations(self):
        for ident, root in (("one", self.one), ("two", self.two)):
            with self.subTest(ident=ident):
                status, data = self.json("/__instances/" + ident)
                self.assertEqual(status, 200, data)
                self.assertEqual(data["profile"]["id"], ident)
                self.assertTrue(os.path.samefile(data["ownership"]["root"], root))
                self.assertTrue(data["ownership"]["private"])
        status, data = self.json("/__instances/absent")
        self.assertEqual(status, 404, data)
        self.assertEqual(data["code"], "missing_instance")

    def test_logs_diagnosis_is_redacted_and_scoped_to_selected_installation(self):
        logs = self.one / 'logs'
        logs.mkdir()
        (logs / 'latest.log').write_text('java.lang.OutOfMemoryError: Java heap space\naccess_token=secret-fixture')
        status, snapshot = self.json('/__instances/one/tools?kind=logs')
        self.assertEqual(status, 200, snapshot)
        self.assertIn('memory', [finding['id'] for finding in snapshot['diagnosis']['findings']])
        self.assertNotIn('secret-fixture', json.dumps(snapshot))
        self.assertFalse(snapshot['running'])
        self.running = True
        server._LAUNCH_INSTANCE = 'two'
        self.launch_snapshot.update(startedAt=9999999999, exitCode=None)
        status, snapshot = self.json('/__instances/one/tools?kind=logs')
        self.assertEqual(status, 200, snapshot)
        self.assertFalse(snapshot['running'])
        self.assertNotIn('stop', snapshot['capabilities'])

    def test_logs_diagnosis_uses_host_output_for_matching_session(self):
        host_log = self.data / 'host.log'
        host_log.write_text('java.lang.OutOfMemoryError: Java heap space')
        server._LAUNCH_INSTANCE = 'one'
        self.launch_snapshot.update(logPath=str(host_log), exitCode=1, startedAt=0)
        with mock.patch.object(server, 'GAME_LOG', str(host_log)):
            code, snapshot = self.json('/__instances/one/tools?kind=logs')
        self.assertEqual(code, 200, snapshot)
        self.assertEqual(snapshot['source'], 'Launcher output')
        self.assertEqual(snapshot['diagnosis']['state'], 'failed')

    def test_logs_diagnosis_respects_native_crash_assistance_setting(self):
        logs = self.one / 'logs'
        logs.mkdir()
        (logs / 'latest.log').write_text('java.lang.OutOfMemoryError: Java heap space')
        code, saved = self.json('/__configuration_settings', {'crashAssistance': False})
        self.assertEqual(code, 200, saved)
        code, snapshot = self.json('/__instances/one/tools?kind=logs')
        self.assertEqual(code, 200, snapshot)
        self.assertFalse(snapshot['diagnosisEnabled'])
        self.assertEqual(snapshot['diagnosis']['findings'], [])
        self.assertIn('OutOfMemoryError', snapshot['text'])

    def test_account_validation_failure_preserves_previous_log_owner(self):
        server._LAUNCH_INSTANCE = 'one'
        host_log = self.data / 'host.log'
        host_log.write_text('java.lang.OutOfMemoryError: Java heap space')
        self.launch_snapshot.update(exitCode=1, startedAt=0)
        with mock.patch.object(server, '_selected_game_identity', side_effect=RuntimeError('Account requires setup')), \
                mock.patch.object(server.launcher, 'start') as start, mock.patch.object(server, 'GAME_LOG', str(host_log)):
            code, _ = self.json('/__launch', {'configurationId': 'two'})
            self.assertEqual(code, 409)
            start.assert_not_called()
            self.assertEqual(server._LAUNCH_INSTANCE, 'one')
            code, snapshot = self.json('/__instances/two/tools?kind=logs')
            self.assertEqual(code, 200, snapshot)
            self.assertEqual(snapshot['diagnosis']['state'], 'no-logs')

    def test_served_native_java_progress_uses_host_control_capabilities(self):
        source = Path(__file__).resolve().parents[1] / 'minecraft-launcher-re/src/ui/static/js/2626.253ef851.chunk.js'
        raw = source.read_bytes()
        target = self.root / 'ui/static/js/2626.253ef851.chunk.js'
        target.parent.mkdir(parents=True)
        target.write_bytes(raw)
        server._js_cache.clear()
        code, body, _ = self.request('/static/js/2626.253ef851.chunk.js')
        self.assertEqual(code, 200)
        script = body.decode('utf-8')
        for old, new in server._DOWNLOAD_REWRITES:
            self.assertIn(old, raw.decode('utf-8'))
            self.assertNotIn(old, script)
            self.assertIn(new, script)

    def test_bulk_preview_route_keeps_files_until_reviewed_command_is_applied(self):
        directory = self.one / 'mods'
        directory.mkdir()
        originals = {}
        for name in ('first', 'second'):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w') as archive:
                archive.writestr('fabric.mod.json', json.dumps({'id': name, 'version': '1.0.0'}))
            originals[name + '.jar'] = stream.getvalue()
            (directory / (name + '.jar')).write_bytes(stream.getvalue())
        status, inventory = self.json('/__instances/one/content')
        self.assertEqual(status, 200, inventory)
        status, preview = self.json('/__instances/one/preview-change', {'action': 'toggle', 'kind': 'mods',
            'ids': [item['id'] for item in inventory['items']], 'enabled': False, 'revision': inventory['revision']})
        self.assertEqual(status, 200, preview)
        self.assertEqual(len(preview['selected']), 2)
        self.assertEqual(len(preview['changed']), 2)
        self.assertEqual(preview['additional'], [])
        self.assertEqual(preview['conflicts'], [])
        self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, originals)
        status, applied = self.json('/__instances/one/toggle', preview['applyBody'])
        self.assertEqual(status, 200, applied)
        self.assertEqual(len(applied['summary']['disabled']), 2)
        self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()},
                         {name + '.disabled': raw for name, raw in originals.items()})

    def test_bulk_preview_route_rejects_repeated_ids_without_effects(self):
        directory = self.one / 'resourcepacks'
        directory.mkdir()
        path = directory / 'home.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('pack.mcmeta', json.dumps({'pack': {'pack_format': 15, 'description': 'Home'}}))
        original = path.read_bytes()
        status, inventory = self.json('/__instances/one/content')
        self.assertEqual(status, 200)
        item_id = inventory['items'][0]['id']
        status, result = self.json('/__instances/one/preview-change', {'action': 'remove', 'kind': 'resourcepacks',
            'ids': [item_id, item_id], 'revision': inventory['revision']})
        self.assertEqual(status, 400, result)
        self.assertEqual(result['code'], 'invalid_selection')
        self.assertEqual(path.read_bytes(), original)

    def test_index_injects_workspace_after_shared_ui_and_serves_actual_module(self):
        status, html, _ = self.request("/games/java/configurations")
        self.assertEqual(status, 200)
        self.assertLess(html.index(b'src="/__ui.js"'), html.index(b'src="/__installation_ui.js"'))
        status, source, headers = self.request("/__installation_ui.js")
        self.assertEqual(status, 200)
        self.assertIn("application/javascript", headers["Content-Type"])
        self.assertIn(b"window.__installationWorkspace", source)

    def test_general_acknowledges_persisted_profile_without_touching_twin(self):
        status, result = self.json("/__instances/one/general", {"patch": {"name": "Renamed", "javaArgs": "-Xmx3G -Dkeep=true"}})
        self.assertEqual(status, 200, result)
        persisted = server._effective_configurations()
        self.assertEqual(result["profile"]["name"], persisted["one"]["name"])
        self.assertEqual(persisted["one"]["unknown"], {"keep": True})
        self.assertEqual(persisted["two"]["name"], "Same name")

    def test_general_success_returns_authoritative_profile_map_for_native_list(self):
        status, result = self.json("/__instances/one/general", {"patch": {"name": "Immediately renamed"}})
        self.assertEqual(status, 200, result)
        self.assertIsInstance(result.get("profiles"), dict)
        self.assertEqual(set(result["profiles"]), {"one", "two"})
        self.assertEqual(result["profiles"]["one"]["name"], "Immediately renamed")
        self.assertEqual(result["profiles"]["two"]["name"], "Same name")
        self.assertEqual(result["profiles"]["one"]["unknown"], {"keep": True})
        self.assertEqual(result["profiles"], server._effective_configurations())
        self.assertEqual(result.get("gameVersions", {}).get("latest", {}).get("release"), "1.20.1")
        self.assertNotIn("users", result)
        self.assertNotIn("__accounts", result)

    def test_copy_success_returns_new_profile_and_source_local_version_for_native_list(self):
        version_id = "private-fabric-build"
        directory = self.one / "versions" / version_id
        directory.mkdir(parents=True)
        (directory / (version_id + ".json")).write_text(json.dumps({
            "id": version_id, "type": "release", "inheritsFrom": "1.20.1",
            "libraries": [{"name": "net.fabricmc:fabric-loader:0.16.5"}]}))
        server._save_configuration("one", dict(self.profiles["one"], lastVersionId=version_id))
        status, result = self.json("/__instances/one/copy", {"name": "Immediately visible copy"})
        self.assertEqual(status, 200, result)
        self.assertIsInstance(result.get("profiles"), dict)
        copied_id = result["instanceId"]
        self.assertEqual(set(result["profiles"]), {"one", "two", copied_id})
        self.assertEqual(result["profiles"][copied_id]["name"], "Immediately visible copy")
        self.assertEqual(result["profiles"][copied_id]["lastVersionId"], version_id)
        self.assertEqual(result["profiles"]["one"]["name"], "Same name")
        version = result.get("gameVersions", {}).get("versions", {}).get(version_id, {})
        self.assertEqual(version.get("issues"), ["modded"])
        self.assertEqual(version.get("minecraftVersion"), "1.20.1")
        self.assertEqual(result["profiles"], server._effective_configurations())

    def test_general_prepared_loader_success_refreshes_full_native_version_slice(self):
        version_id = "fabric-loader-0.16.5-1.20.1"
        def prepared_loader(state, minecraft, loader, base_doc, **kwargs):
            directory = self.state / "versions" / version_id
            directory.mkdir(parents=True)
            (directory / (version_id + ".json")).write_text(json.dumps({
                "id": version_id, "type": "release", "inheritsFrom": "1.20.1",
                "libraries": [{"name": "net.fabricmc:fabric-loader:0.16.5"}]}))
            return version_id
        with mock.patch.object(server.modpacks, "materialize_loader", side_effect=prepared_loader):
            status, result = self.json("/__instances/one/general", {"patch": {
                "minecraft": "1.20.1", "loader": "fabric", "loaderVersion": "0.16.5"}})
        self.assertEqual(status, 200, result)
        self.assertEqual(result.get("profiles", {}).get("one", {}).get("lastVersionId"), version_id)
        version = result.get("gameVersions", {}).get("versions", {}).get(version_id, {})
        self.assertEqual(version.get("issues"), ["modded"])
        self.assertEqual(version.get("minecraftVersion"), "1.20.1")

    def test_general_persistence_failure_does_not_acknowledge_success(self):
        with mock.patch.object(server, "_write_overlay", side_effect=OSError("fixture disk error")):
            status, result = self.json("/__instances/one/general", {"patch": {"name": "Unsaved"}})
        self.assertEqual(status, 500, result)
        self.assertFalse(result["ok"])
        self.assertNotIn("profiles", result)
        self.assertNotIn("gameVersions", result)
        self.assertEqual(server._effective_configurations()["one"]["name"], "Same name")

    def test_foreign_host_and_origin_cannot_read_or_change_installation(self):
        baseline = (self.state / "launcher_profiles.json").read_bytes()
        for headers in ({"Host": "evil.invalid"}, {"Origin": "https://evil.invalid"}):
            with self.subTest(headers=headers):
                status, data = self.json("/__instances/one", headers=headers)
                self.assertEqual(status, 403, data)
                status, data = self.json("/__instances/one/general", {"patch": {"name": "Untrusted"}}, headers)
                self.assertEqual(status, 403, data)
        self.assertEqual((self.state / "launcher_profiles.json").read_bytes(), baseline)
        self.assertFalse((self.data / "state.json").exists())

    def test_malformed_and_oversized_requests_fail_without_file_changes(self):
        for raw, headers, expected in (("[1]", {}, 400), ("{broken", {}, 400), ("{}", {"Content-Length": str(48 * 1024 * 1024 + 1)}, 413)):
            with self.subTest(raw=raw, headers=headers):
                status, _, _ = self.request("/__instances/one/general", headers=headers, raw=raw)
                self.assertEqual(status, expected)
        self.assertFalse((self.data / "state.json").exists())
        # An unhashable plan ID is a validation error, not a disconnected HTTP handler.
        status, data = self.json("/__instances/one/apply", {"planId": {"invalid": True}})
        self.assertEqual(status, 400, data)

    def test_content_operation_blocks_existing_launcher_mutations(self):
        service = server._installation_service()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def operation(job):
            entered.set()
            if not release.wait(4):
                raise RuntimeError("Fixture timeout")
            finished.set()
            return {"items": []}
        with server.installation_service.coordinator():
            job_id = service._start_job("one", "Fixture operation", operation)
        self.assertTrue(entered.wait(2))
        try:
            with mock.patch.object(server.launcher, "start") as launch, mock.patch.object(server.installer, "start") as install:
                for path, body in (("/__launch", {"configurationId": "one"}), ("/__install", {"configurationId": "one"}),
                                   ("/__configurations/update", {"id": "one", "patch": {"name": "Race"}}),
                                   ("/__loaders/materialize", {"minecraft": "1.20.1", "loader": "fabric-loader"})):
                    with self.subTest(path=path):
                        status, data = self.json(path, body)
                        self.assertEqual(status, 409, data)
                launch.assert_not_called()
                install.assert_not_called()
                self.assertEqual(server._effective_configurations()["one"]["name"], "Same name")
        finally:
            release.set()
            self.assertTrue(finished.wait(2))
            # Wait for the real coordinator to release its background ownership.
            with server.installation_service.coordinator():
                job = server.installation_service._JOBS[job_id]
            for _ in range(100):
                if service.get("one", "job", {"jobId": job_id})["job"]["status"] in ("succeeded", "failed"):
                    break
                threading.Event().wait(.01)

    def test_running_game_cannot_rebind_log_and_stop_ownership_on_second_launch(self):
        self.running = True
        server._LAUNCH_INSTANCE = "one"
        with mock.patch.object(server.launcher, "start", return_value={"running": True}) as launch:
            status, data = self.json("/__launch", {"configurationId": "two"})
        self.assertIn(status, (200, 409), data)
        launch.assert_not_called()
        self.assertEqual(server._LAUNCH_INSTANCE, "one", "Second Play rebound the existing game's installation ID")
        with mock.patch.object(server.launcher, "stop", return_value={"running": False}) as stop:
            status, result = self.json("/__instances/two/tools", {"kind": "logs", "action": "stop"})
        self.assertEqual(status, 409, result)
        stop.assert_not_called()

    def test_legacy_profile_state_writes_obey_operation_guard(self):
        self.running = True
        for key in ('__configurations', '__deleted_configurations'):
            status, result = self.json('/__state/' + key, {'value': {} if key == '__configurations' else ['one']})
            self.assertEqual(status, 409, result)
        self.assertIn('one', server._effective_configurations())

    def test_active_game_installer_blocks_launch_of_shared_artifacts(self):
        self.installing = True
        with mock.patch.object(server.launcher, "start", return_value={"running": True}) as launch:
            status, data = self.json("/__launch", {"configurationId": "one"})
        self.assertEqual(status, 409, data)
        launch.assert_not_called()

    def test_modpack_job_blocks_loader_materialization(self):
        self.modpacking = True
        with mock.patch.object(server.modpacks, "materialize_loader", return_value="fabric-loader-fixture") as materialize:
            status, data = self.json("/__loaders/materialize", {"minecraft": "1.20.1", "loader": "fabric-loader"})
        self.assertEqual(status, 409, data)
        materialize.assert_not_called()

    def test_screenshot_routes_are_scoped_to_exact_installation_and_filename(self):
        image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+XGMsAAAAASUVORK5CYII=")
        folder = self.one / "screenshots"
        folder.mkdir()
        (folder / "my image.png").write_bytes(image)
        status, inventory = self.json("/__instances/one/tools?kind=screenshots")
        self.assertEqual(status, 200, inventory)
        self.assertEqual(len(inventory["items"]), 1)
        url = inventory["items"][0]["url"]
        self.assertTrue(url.startswith("/__instances/one/image?"))
        status, returned, headers = self.request(url)
        self.assertEqual((status, returned), (200, image))
        self.assertEqual(headers["Content-Type"], "image/png")
        status, data = self.json(url.replace("/one/", "/two/"))
        self.assertEqual(status, 404, data)
        status, _, _ = self.request("/__instances/one/image?name=..%2Foutside.png")
        self.assertEqual(status, 404)

    def test_direct_general_version_id_cannot_bypass_modded_copy_rule(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("fabric.mod.json", json.dumps({"id": "fixture", "version": "1.0.0"}))
        (self.one / "mods").mkdir()
        (self.one / "mods/fixture.jar").write_bytes(data.getvalue())
        status, result = self.json("/__instances/one/general", {"patch": {"lastVersionId": "1.21.1"}})
        self.assertEqual(status, 409, result)
        self.assertEqual(server._effective_configurations()["one"]["lastVersionId"], "1.20.1")

    def test_context_preserves_prerelease_loader_build_from_its_native_id(self):
        ident = "quilt-loader-0.26.4-beta.1-1.20.1"
        directory = self.state / "versions" / ident
        directory.mkdir(parents=True)
        (directory / (ident + ".json")).write_text(json.dumps({"id": ident, "inheritsFrom": "1.20.1"}))
        server._save_configuration("one", dict(self.profiles["one"], lastVersionId=ident))
        status, result = self.json("/__instances/one")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["context"]["minecraft"], "1.20.1")
        self.assertEqual(result["context"]["loaderVersion"], "0.26.4-beta.1")

    def test_flattened_loader_metadata_uses_minecraft_version_in_live_catalog_filter(self):
        manifest = json.loads((self.root / "versions.json").read_text())
        manifest["versions"].update({"26.2": {"id": "26.2", "type": "release"},
                                    "1.21-pre1": {"id": "1.21-pre1", "type": "snapshot"}})
        (self.root / "versions.json").write_text(json.dumps(manifest))
        for loader, build, minecraft, library, extra in (
            ("fabric", "0.20.1", "26.2", "net.fabricmc:fabric-loader:0.20.1", {}),
            ("quilt", "0.26.4-beta.1", "1.21-pre1", "org.quiltmc:quilt-loader:0.26.4-beta.1", {}),
            ("fabric", "0.20.1", "26.2", "net.fabricmc:fabric-loader:0.20.1", {"jar": "fabric-loader-0.20.1-26.2"}),
        ):
            with self.subTest(loader=loader, extra=extra):
                ident = loader + "-loader-" + build + "-" + minecraft
                directory = self.state / "versions" / ident
                directory.mkdir(parents=True, exist_ok=True)
                document = dict(id=ident, libraries=[{"name": library}], javaVersion={"majorVersion": 25}, **extra)
                (directory / (ident + ".json")).write_text(json.dumps(document))
                server._save_configuration("one", dict(self.profiles["one"], lastVersionId=ident))
                status, result = self.json("/__instances/one")
                self.assertEqual(status, 200, result)
                self.assertEqual(result["context"]["minecraft"], minecraft)
                self.assertEqual(result["context"]["loaderVersion"], build)
                with mock.patch.object(server.content_provider.ModrinthProvider, "search", return_value={"hits": [], "total_hits": 0}) as search:
                    status, catalog = self.json("/__instances/one/catalog?kind=mods")
                self.assertEqual(status, 200, catalog)
                search.assert_called_once_with("", "mods", minecraft, [loader], offset=0)

    def test_loader_context_preserves_authoritative_inheritance_and_library_build(self):
        ident = "fabric-loader-0.16.5-1.20.1"
        directory = self.state / "versions" / ident
        directory.mkdir(parents=True)
        (directory / (ident + ".json")).write_text(json.dumps({"id": ident, "inheritsFrom": "1.21.1",
            "libraries": [{"name": "net.fabricmc:fabric-loader:0.17.0"}]}))
        server._save_configuration("one", dict(self.profiles["one"], lastVersionId=ident))
        status, result = self.json("/__instances/one")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["context"]["minecraft"], "1.21.1")
        self.assertEqual(result["context"]["loaderVersion"], "0.17.0")

    def test_context_inherits_known_java_requirement_from_base_version(self):
        for ident, document in (
            ("1.20.1", {"id": "1.20.1", "javaVersion": {"majorVersion": 17}}),
            ("fabric-loader-0.16.5-1.20.1", {"id": "fabric-loader-0.16.5-1.20.1", "inheritsFrom": "1.20.1", "libraries": [{"name": "net.fabricmc:fabric-loader:0.16.5"}]}),
        ):
            directory = self.state / "versions" / ident
            directory.mkdir(parents=True)
            (directory / (ident + ".json")).write_text(json.dumps(document))
        server._save_configuration("one", dict(self.profiles["one"], lastVersionId="fabric-loader-0.16.5-1.20.1"))
        status, result = self.json("/__instances/one")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["context"].get("javaMajor"), 17)

    def test_listed_webp_screenshot_has_a_working_preview_route(self):
        folder = self.one / "screenshots"
        folder.mkdir()
        # The inventory uses the same bounded RIFF/WebP signature as the host.
        raw = b"RIFF" + (12).to_bytes(4, "little") + b"WEBPVP8 " + b"\0" * 4
        (folder / "fixture.webp").write_bytes(raw)
        status, result = self.json("/__instances/one/tools?kind=screenshots")
        self.assertEqual(status, 200, result)
        self.assertEqual(len(result["items"]), 1)
        status, returned, headers = self.request(result["items"][0]["url"])
        self.assertEqual((status, returned), (200, raw))
        self.assertEqual(headers["Content-Type"], "image/webp")

    def test_version_metadata_change_invalidates_an_approved_content_plan(self):
        ident = "fabric-loader-0.16.5-1.20.1"
        directory = self.state / "versions" / ident
        directory.mkdir(parents=True)
        metadata = directory / (ident + ".json")
        document = {"id": ident, "inheritsFrom": "1.20.1", "libraries": [{"name": "net.fabricmc:fabric-loader:0.16.5"}]}
        metadata.write_text(json.dumps(document))
        server._save_configuration("one", dict(self.profiles["one"], lastVersionId=ident))
        proposal = {"files": [], "reuse": [], "enable": [], "remove": [], "conflicts": [], "warnings": []}
        with mock.patch.object(server.installation_service, "resolve_plan", return_value=proposal):
            status, plan = self.json("/__instances/one/plan", {"kind": "mods", "selections": [{"versionId": "fixture-v"}]})
        self.assertEqual(status, 200, plan)
        document["inheritsFrom"] = "1.21.1"
        metadata.write_text(json.dumps(document))
        status, result = self.json("/__instances/one/apply", {"planId": plan["planId"]})
        if result.get("jobId"):
            self.wait_job(result["jobId"])
        self.assertEqual(status, 409, result)
        self.assertEqual(result["code"], "changed_installation")

    def wait_job(self, job_id):
        service = server._installation_service()
        for _ in range(300):
            job = service.get("one", "job", {"jobId": job_id})["job"]
            if job["status"] in ("succeeded", "failed"):
                return job
            threading.Event().wait(.01)
        self.fail("Fixture operation timed out")

    def test_independent_copy_preserves_managed_content_identity_and_channels(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as file:
            file.writestr("fabric.mod.json", json.dumps({"id": "managed", "version": "1.0.0"}))
        staged = self.root / "managed.jar"
        staged.write_bytes(archive.getvalue())
        store = server.instance_content.ContentStore(str(self.one), str(self.data), "one")
        store.transact([{"action": "install", "kind": "mods", "filename": "managed.jar", "source": str(staged), "metadata": {
            "provider": "modrinth", "projectId": "managed-p", "versionId": "managed-v", "releaseChannels": ["beta"], "userRequested": True}}])
        status, copied = self.json("/__instances/one/copy", {"name": "Copied pack"})
        self.assertEqual(status, 200, copied)
        status, inventory = self.json("/__instances/" + copied["instanceId"] + "/content")
        self.assertEqual(status, 200, inventory)
        self.assertEqual(len(inventory["items"]), 1)
        item = inventory["items"][0]
        self.assertEqual(item.get("projectId"), "managed-p")
        self.assertEqual(item.get("releaseChannels"), ["beta"])


if __name__ == "__main__":
    unittest.main()
