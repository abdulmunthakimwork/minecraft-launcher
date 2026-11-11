"""Microsoft setup routes against a real loopback host and disposable data."""
import http.client
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock


with tempfile.TemporaryDirectory(prefix="msa-http-bootstrap-") as bootstrap:
    with mock.patch.dict(os.environ, {"LAUNCHER_DATA_DIR": bootstrap}):
        spec = importlib.util.spec_from_file_location(
            "msa_http_server_fixture", Path(__file__).with_name("server.py"))
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)


class MicrosoftConfigurationHttpRegressions(unittest.TestCase):
    FILE_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    ENV_ID = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="msa-config-http-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for name in ("data", "state", "game", "ui"):
            (self.root / name).mkdir()
        (self.root / "ui/index.html").write_text("<!doctype html><html><body></body></html>", encoding="utf-8")
        environment = mock.patch.dict(os.environ, {"LAUNCHER_MSA_CLIENT_ID": ""})
        environment.start()
        self.addCleanup(environment.stop)
        spec = importlib.util.spec_from_file_location("msa_http_configuration_fixture", Path(__file__).with_name("msa.py"))
        self.msa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.msa)
        self.msa.configure(str(self.root / "data"))
        override = mock.patch.multiple(server, msa=self.msa, UI=str(self.root / "ui"),
                                      STATE=str(self.root / "state"), GAME_DIR=str(self.root / "game"),
                                      DATA_DIR=str(self.root / "data"), STORE_OVERLAY=str(self.root / "data/state.json"),
                                      OFFLINE_STORE=str(self.root / "data/offline_accounts.json"))
        override.start()
        self.addCleanup(override.stop)
        network = mock.patch.object(self.msa.urllib.request, "urlopen",
                                   side_effect=AssertionError("setup must not contact authentication services"))
        network.start()
        self.addCleanup(network.stop)

        class QuietHandler(server.Handler):
            def log_message(self, *args):
                pass

        self.host = server.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.thread = threading.Thread(target=self.host.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_host)

    def close_host(self):
        self.host.shutdown()
        self.thread.join(3)
        self.host.server_close()

    def request(self, method, path="/__msa/config", body=None, raw=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.host.server_port, timeout=3)
        try:
            payload = raw if raw is not None else json.dumps(body).encode() if body is not None else None
            connection.request(method, path, body=payload, headers={"Content-Type": "application/json", **(headers or {})})
            response = connection.getresponse()
            raw_response = response.read()
            try:
                document = json.loads(raw_response)
            except ValueError:
                document = None
            return response.status, document
        finally:
            connection.close()

    def framed_request(self, framing):
        body = json.dumps({"clientId": self.FILE_ID}).encode("ascii")
        request = ("POST /__msa/config HTTP/1.0\r\nHost: 127.0.0.1:%d\r\n"
                   "Content-Type: application/json\r\n%s\r\n" %
                   (self.host.server_port, framing.format(length=len(body), longer=len(body) + 1))).encode("ascii") + body
        with socket.create_connection(("127.0.0.1", self.host.server_port), timeout=3) as connection:
            connection.sendall(request)
            connection.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        header, response = b"".join(chunks).split(b"\r\n\r\n", 1)
        return int(header.split(b" ")[1]), json.loads(response)

    def test_get_missing_configuration_returns_safe_setup_status(self):
        self.assertEqual(self.request("GET"), (200, {
            "configured": False, "clientId": None, "source": None, "environmentOverride": False}))
        self.assertEqual(list((self.root / "data").iterdir()), [])

    def test_post_persists_public_id_and_signin_uses_it_without_touching_sessions(self):
        token_path = self.root / "data/msa_tokens.json"
        token_path.write_bytes(b"synthetic session sentinel")
        expected = {"configured": True, "clientId": self.FILE_ID, "source": "file", "environmentOverride": False}
        self.assertEqual(self.request("POST", body={"clientId": self.FILE_ID.upper()}), (200, expected))
        self.assertEqual(self.request("GET"), (200, expected))
        self.assertEqual((self.root / "data/microsoft-client-id.txt").read_text().strip(), self.FILE_ID)
        status, document = self.request("GET", "/__msa/signin")
        self.assertEqual(status, 200)
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(document["url"]).query)
        self.assertEqual(params["client_id"], [self.FILE_ID])
        self.assertEqual(token_path.read_bytes(), b"synthetic session sentinel")

    def test_post_rejects_malformed_or_unexpected_configuration_fields(self):
        for body in ({}, {"clientId": "malformed-fixture"}, {"clientId": 1},
                     {"clientId": self.FILE_ID, "unexpected": "fixture"}, []):
            with self.subTest(body=body):
                status, document = self.request("POST", body=body)
                self.assertEqual(status, 400)
                self.assertIsInstance(document.get("error"), str)
                self.assertFalse((self.root / "data/microsoft-client-id.txt").exists())

    def test_post_invalid_json_or_oversized_body_returns_json_error_without_writing(self):
        for raw, expected_status in ((b"{", 400), (b" " * 1025, 413)):
            with self.subTest(size=len(raw)):
                status, document = self.request("POST", raw=raw)
                self.assertEqual(status, expected_status)
                self.assertIsInstance(document.get("error"), str)
        self.assertFalse((self.root / "data/microsoft-client-id.txt").exists())

    def test_post_rejects_ambiguous_framing_and_incomplete_body_without_saving(self):
        for framing in ("Content-Length: {length}\r\nContent-Length: {longer}\r\n",
                        "Transfer-Encoding: chunked\r\nContent-Length: {length}\r\n",
                        "Content-Length: {longer}\r\n"):
            with self.subTest(framing=framing):
                status, document = self.framed_request(framing)
                self.assertEqual(status, 400)
                self.assertEqual(document["code"], "INVALID_MSA_CONFIGURATION")
                self.assertFalse((self.root / "data/microsoft-client-id.txt").exists())

    def test_environment_override_is_explicit_after_saving_a_file(self):
        with mock.patch.dict(os.environ, {"LAUNCHER_MSA_CLIENT_ID": self.ENV_ID}):
            expected = {"configured": True, "clientId": self.ENV_ID,
                        "source": "environment", "environmentOverride": True}
            self.assertEqual(self.request("POST", body={"clientId": self.FILE_ID}), (200, expected))
            self.assertEqual(self.request("GET"), (200, expected))
        self.assertEqual((self.root / "data/microsoft-client-id.txt").read_text().strip(), self.FILE_ID)

    def test_active_browser_signin_returns_conflict_and_preserves_application_id(self):
        self.msa.save_client_id(self.FILE_ID)
        self.msa.sign_in(self.host.server_port)
        for phase in ("waiting", "completing"):
            with self.subTest(phase=phase):
                self.msa._STATE["phase"] = phase
                status, document = self.request("POST", body={"clientId": self.ENV_ID})
                self.assertEqual(status, 409)
                self.assertEqual(document["code"], "MSA_SIGNIN_IN_PROGRESS")
                self.assertEqual((self.root / "data/microsoft-client-id.txt").read_text().strip(), self.FILE_ID)

    def test_foreign_origin_cannot_read_or_change_configuration(self):
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                status, document = self.request(method, body={"clientId": self.FILE_ID} if method == "POST" else None,
                                                headers={"Origin": "https://example.invalid"})
                self.assertEqual(status, 403)
        self.assertFalse((self.root / "data/microsoft-client-id.txt").exists())


if __name__ == "__main__":
    unittest.main()
