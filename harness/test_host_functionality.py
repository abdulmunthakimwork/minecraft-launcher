"""Host capability regressions using disposable roots and mocked OS boundaries."""
import http.client
import base64
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
with tempfile.TemporaryDirectory() as bootstrap:
    with mock.patch.dict(os.environ, {"LAUNCHER_DATA_DIR": bootstrap}):
        import server


class HostFunctionalityTests(unittest.TestCase):
    def test_local_modpack_picker_cancellation_has_no_job(self):
        with mock.patch.object(server,'_pick_path',return_value='') as picker, \
             mock.patch.object(server.modpacks,'busy',return_value=False), \
             mock.patch.object(server.modpacks,'start_import_file',create=True) as start:
            status,body=self.request('/__modpacks/import-file',{})
        self.assertEqual(status,200,body)
        self.assertTrue(body.get('cancelled'))
        self.assertFalse(body.get('started'))
        picker.assert_called_once()
        start.assert_not_called()

    def test_local_modpack_import_accepts_only_the_host_picker_path(self):
        chosen=str(self.root/'chosen.mrpack')
        with mock.patch.object(server,'_pick_path',return_value=chosen), \
             mock.patch.object(server.modpacks,'busy',return_value=False), \
             mock.patch.object(server.modpacks,'start_import_file',return_value=True,create=True) as start:
            status,body=self.request('/__modpacks/import-file',{'path':'C:/untrusted-request.mrpack'})
        self.assertEqual(status,200,body)
        self.assertTrue(body.get('started'))
        self.assertEqual(start.call_args.args[:3],(str(self.game),str(self.root/'state'),chosen))

    def test_local_modpack_picker_path_guard_returns_error_without_a_job(self):
        selected = self.root / "chosen.mrpack"
        selected.write_bytes(b"fixture selected archive")
        content = server.instance_content
        real_reparse = content._reparse

        def selected_is_reparse(path):
            if os.path.normcase(str(path)) == os.path.normcase(str(selected)):
                return True
            return real_reparse(path)

        previous_state = server.modpacks.state()
        with mock.patch.object(server, "_pick_path", return_value=str(selected)), \
             mock.patch.object(content, "_reparse", side_effect=selected_is_reparse):
            try:
                status, body = self.request("/__modpacks/import-file", {})
            except http.client.RemoteDisconnected:
                self.fail("A rejected picker path must return an HTTP error response")
        self.assertEqual(status, 400, body)
        self.assertFalse(body.get("ok"))
        self.assertEqual(body.get("code"), "unsafe_path")
        self.assertIn("Links and reparse points", body["error"])
        self.assertEqual(server.modpacks.state(), previous_state)
        self.assertEqual(selected.read_bytes(), b"fixture selected archive")
        self.assertFalse((self.game / "instances").exists())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game = self.root / "game"
        self.game.mkdir()
        (self.root / "ui").mkdir()
        (self.root / "ui/index.html").write_text("<!doctype html><html><head></head><body>Fixture</body></html>")
        patches = mock.patch.multiple(server, GAME_DIR=str(self.game), STATE=str(self.root / "state"),
            DATA_DIR=str(self.root / "data"), STORE_OVERLAY=str(self.root / "data/state.json"),
            UI=str(self.root / "ui"))
        patches.start()
        self.addCleanup(patches.stop)
        appdata = mock.patch.object(server, "build_appdata", return_value={})
        appdata.start()
        self.addCleanup(appdata.stop)

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
                data = {"raw": raw.decode("utf-8", errors="replace")[:100]}
            return response.status, data
        finally:
            connection.close()
            host.shutdown()
            host.server_close()
            worker.join(2)

    def test_disk_space_reports_actual_free_bytes_for_game_directory(self):
        expected = shutil.disk_usage(self.game)
        status, body = self.request("/__disk_space")
        self.assertEqual(status, 200, body)
        self.assertIs(type(body.get("value")), int)
        self.assertGreaterEqual(body["value"], 0)
        # The free count can change between reads; the disk's total cannot.
        self.assertEqual(body["total"], expected.total)
        self.assertLessEqual(body["value"], body["total"])

    def test_disk_space_for_new_install_location_uses_existing_ancestor(self):
        target = self.game / "new-instance" / "nested"
        usage = shutil._ntuple_diskusage(1000, 250, 750)
        with mock.patch.object(server.shutil, "disk_usage", return_value=usage) as probe:
            status, body = self.request("/__disk_space?" + urllib.parse.urlencode({"path": str(target)}))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["value"], 750)
        self.assertEqual(Path(probe.call_args.args[0]), self.game)
        self.assertFalse(target.exists(), "A read-only space check created the installation directory")

    def test_disk_space_known_volume_uses_its_mount(self):
        volumes = {"selectedVolumeName": "fixture", "volumes": [{"name": "fixture", "mountPoint": str(self.game)}]}
        with mock.patch.object(server, "_volumes_slice", return_value=volumes):
            status, body = self.request("/__disk_space?volume=fixture")
        self.assertEqual(status, 200, body)
        self.assertGreaterEqual(body["value"], 0)

    def test_unknown_volume_cannot_be_reported_as_available(self):
        with mock.patch.object(server.shutil, "disk_usage") as probe:
            status, body = self.request("/__disk_space?volume=missing")
        self.assertEqual(status, 400, body)
        self.assertIn("volume", body["error"].lower())
        probe.assert_not_called()

    def test_existing_file_is_not_an_install_directory(self):
        target = self.game / "file"
        target.write_text("fixture")
        status, body = self.request("/__disk_space?" + urllib.parse.urlencode({"path": str(target / 'child')}))
        self.assertEqual(status, 400, body)
        self.assertIn("directory", body["error"].lower())

    def test_disk_probe_failure_returns_honest_error(self):
        with mock.patch.object(server.shutil, "disk_usage", side_effect=OSError("fixture volume offline")):
            status, body = self.request("/__disk_space")
        self.assertEqual(status, 500, body)
        self.assertIn("fixture volume offline", body["error"])
        self.assertNotIn("value", body)

    def test_invalid_disk_counter_does_not_return_success(self):
        usage = shutil._ntuple_diskusage(1000, 200, float("nan"))
        with mock.patch.object(server.shutil, "disk_usage", return_value=usage):
            status, body = self.request("/__disk_space")
        self.assertEqual(status, 500, body)
        self.assertNotIn("value", body)

    def test_picker_cancel_remains_distinct_from_failure(self):
        result = subprocess.CompletedProcess([], 0, '{"path":""}\n', '')
        with mock.patch.object(server.subprocess, "run", return_value=result):
            status, body = self.request("/__dialog", {"options": {"directory": True}})
        self.assertEqual((status, body), (200, {"path": ""}))

    def test_picker_selection_returns_real_selected_path(self):
        selected = str(self.game / "selected.jar")
        result = subprocess.CompletedProcess([], 0, json.dumps({"path": selected}), '')
        with mock.patch.object(server.subprocess, "run", return_value=result):
            status, body = self.request("/__dialog", {"options": {}})
        self.assertEqual((status, body), (200, {"path": selected}))

    def test_picker_process_failures_do_not_look_like_cancel(self):
        for failure in (OSError("fixture picker unavailable"), subprocess.TimeoutExpired("fixture", 300)):
            with self.subTest(failure=type(failure).__name__):
                with mock.patch.object(server.subprocess, "run", side_effect=failure):
                    status, body = self.request("/__dialog", {"options": {}})
                self.assertEqual(status, 500, body)
                self.assertIn("picker", body["error"].lower())
                self.assertNotIn("path", body)

    def test_bundled_picker_failure_is_not_cancel(self):
        result = subprocess.CompletedProcess([], 0, '{"path":"","error":"Tk is unavailable"}', '')
        with mock.patch.object(server.subprocess, "run", return_value=result):
            status, body = self.request("/__dialog", {"options": {}})
        self.assertEqual(status, 500, body)
        self.assertIn("Tk is unavailable", body["error"])

    def test_picker_nonzero_exit_is_not_success(self):
        result = subprocess.CompletedProcess([], 1, '{"path":"unexpected.jar"}', 'fixture failed')
        with mock.patch.object(server.subprocess, "run", return_value=result):
            status, body = self.request("/__dialog", {"options": {}})
        self.assertEqual(status, 500, body)
        self.assertIn("fixture failed", body["error"])

    def test_malformed_picker_reply_is_not_cancel(self):
        for output in ('', '{broken', '{"path":23}', '{"different":"field"}', '[]'):
            with self.subTest(output=output):
                result = subprocess.CompletedProcess([], 0, output, '')
                with mock.patch.object(server.subprocess, "run", return_value=result):
                    status, body = self.request("/__dialog", {"options": {}})
                self.assertEqual(status, 500, body)
                self.assertIn("picker", body["error"].lower())

    def test_modpack_export_does_not_relabel_picker_failure_as_cancel(self):
        patches = [
            mock.patch.object(server, "_configuration", return_value={"name": "Fixture", "gameDir": str(self.game)}),
            mock.patch.object(server.launcher, "status", return_value={"running": False}),
            mock.patch.object(server.installer, "state", return_value={"active": False}),
            mock.patch.object(server.modpacks, "busy", return_value=False),
            mock.patch.object(server.subprocess, "run", side_effect=OSError("fixture picker unavailable")),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        status, body = self.request("/__modpacks/export", {"configurationId": "fixture"})
        self.assertEqual(status, 500, body)
        self.assertIn("picker", body["error"].lower())
        self.assertNotIn("cancelled", body["error"].lower())

    def client_files(self, root=None):
        directory = (root or self.game) / "versions/1.21.1"
        directory.mkdir(parents=True)
        for suffix in ("jar", "json"):
            (directory / ("1.21.1." + suffix)).write_text("fixture client")
        return directory

    def test_uninstall_rejects_linked_versions_without_deleting_external_client(self):
        external = self.root / "external"
        client = self.client_files(external)
        (self.game / "versions").symlink_to(external / "versions", target_is_directory=True)
        with mock.patch.object(server, "_load_versions", return_value={"latest": {"release": "1.21.1"}}):
            result = server._uninstall("java", False)
        self.assertIn("error", result)
        self.assertEqual((client / "1.21.1.jar").read_text(), "fixture client")
        self.assertEqual((client / "1.21.1.json").read_text(), "fixture client")

    def test_uninstall_preflights_saved_data_before_removing_client(self):
        client = self.client_files()
        external = self.root / "external-saves"
        external.mkdir()
        (external / "world").write_text("fixture world")
        (self.game / "saves").symlink_to(external, target_is_directory=True)
        with mock.patch.object(server, "_load_versions", return_value={"latest": {"release": "1.21.1"}}):
            result = server._uninstall("java", True)
        self.assertIn("error", result)
        self.assertTrue((client / "1.21.1.jar").is_file(), "Rejected uninstall already removed the client")
        self.assertEqual((client / "1.21.1.jar").read_text(), "fixture client")
        self.assertEqual((client / "1.21.1.json").read_text(), "fixture client")
        self.assertEqual((external / "world").read_text(), "fixture world")

    def test_uninstall_rejects_unsafe_release_tokens(self):
        with mock.patch.object(server, "_load_versions", return_value={"latest": {"release": "../outside"}}), \
             mock.patch.object(server.os, "remove") as remove:
            result = server._uninstall("java", False)
        self.assertIn("error", result)
        self.assertIn("invalid", result["error"].lower())
        remove.assert_not_called()

    def test_busy_operation_guard_consumes_body_before_rejecting_request(self):
        payload = json.dumps({"id": "fixture", "patch": {"name": "Blocked rename"}}).encode()
        handler = object.__new__(server.Handler)
        handler.path = "/__configurations/update"
        handler.headers = {"Content-Length": str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        handler._local_request = lambda: True
        consumed = []
        handler._send_json = lambda body, status=200: consumed.append((status, handler.rfile.tell()))
        with mock.patch.object(server.installation_service, "busy", return_value=True):
            handler.do_POST()
        self.assertEqual(consumed, [(409, len(payload))],
                         "An early rejection closed HTTP with an unread body, which can reset the client socket")

    def test_read_file_stays_bounded_when_file_grows_after_stat(self):
        stream = io.BytesIO(b"x" * (16 * 1024 * 1024 + 1))
        with mock.patch.object(server.os.path, "getsize", return_value=1), \
             mock.patch.object(server, "open", return_value=stream, create=True):
            result = server._read_file_body(str(self.game / "growing.png"))
        self.assertIn("error", result.keys())
        self.assertIn("16 MB", result["error"])
        self.assertNotIn("base64", result)

    def test_read_file_preserves_selected_file_contract(self):
        target = self.game / "selected.png"
        data = b"fixture bytes"
        target.write_bytes(data)
        result = server._read_file_body(str(target))
        self.assertEqual(result, {"name": target.name, "size": len(data),
                                  "base64": base64.b64encode(data).decode("ascii")})


if __name__ == "__main__":
    unittest.main()
