"""HTTP media regressions with disposable roots and no account or game writes."""
import base64
import http.client
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
with tempfile.TemporaryDirectory() as bootstrap:
    with mock.patch.dict(os.environ, {"LAUNCHER_DATA_DIR": bootstrap}):
        import server


class MediaRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ui = self.root / "ui"
        self.assets = self.root / "assets"
        self.ui.mkdir()
        self.assets.mkdir()
        patcher = mock.patch.multiple(server, UI=str(self.ui), ASSETS=str(self.assets))
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

    def request(self, path):
        connection = http.client.HTTPConnection(*self.host.server_address, timeout=3)
        self.addCleanup(connection.close)
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()

    def restore(self, relative, body):
        target = self.assets / "media" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        return target

    def test_restored_png_is_served_instead_of_transparent_placeholder(self):
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jr1kAAAAASUVORK5CYII="
        )
        self.restore("creatortools/EditorStillRelease.927b50f8.png", png)
        status, headers, body = self.request("/static/media/creatortools/EditorStillRelease.927b50f8.png")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(body, png)
        self.assertNotIn("X-Harness-Media", headers)

    def test_restored_svg_keeps_original_art(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h20v20z"/></svg>'
        self.restore("logos/java.402de8b2.svg", svg)
        status, headers, body = self.request("/static/media/logos/java.402de8b2.svg")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/svg+xml")
        self.assertEqual(body, svg)

    def test_restored_video_returns_bytes_instead_of_empty_204(self):
        video = b"\x1a\x45\xdf\xa3disposable-webm-fixture"
        self.restore("creatortools/introVidRelease.16fe5c80.webm", video)
        status, headers, body = self.request("/static/media/creatortools/introVidRelease.16fe5c80.webm")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "video/webm")
        self.assertEqual(body, video)

    def test_extracted_media_takes_precedence(self):
        relative = "logos/example.svg"
        original = b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M1 1h4"/></svg>'
        path = self.ui / "static/media" / relative
        path.parent.mkdir(parents=True)
        path.write_bytes(original)
        self.restore(relative, b"different-restored-fixture")
        status, _, body = self.request("/static/media/" + relative)
        self.assertEqual(status, 200)
        self.assertEqual(body, original)

    def test_recovery_requires_exact_folder_and_filename(self):
        self.restore("icons/same.png", b"wrong-folder-fixture")
        status, headers, body = self.request("/static/media/creatortools/same.png")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Harness-Media"], "unrecovered")
        self.assertNotEqual(body, b"wrong-folder-fixture")

    def test_media_request_cannot_traverse_out_of_recovery_root(self):
        (self.root / "outside.svg").write_bytes(b"private-outside-fixture")
        status, _, body = self.request("/static/media/%2e%2e/%2e%2e/outside.svg")
        self.assertEqual(status, 404)
        self.assertNotIn(b"private-outside-fixture", body)

    def test_media_symlink_cannot_escape_recovery_root(self):
        outside = self.root / "outside.svg"
        outside.write_bytes(b"private-symlink-fixture")
        target = self.assets / "media/logos/escape.svg"
        target.parent.mkdir(parents=True)
        try:
            target.symlink_to(outside)
        except OSError as exc:
            self.skipTest("Creating a symlink is unavailable: " + str(exc))
        status, _, body = self.request("/static/media/logos/escape.svg")
        self.assertEqual(status, 404)
        self.assertNotIn(b"private-symlink-fixture", body)


if __name__ == "__main__":
    unittest.main()
