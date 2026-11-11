"""Account regressions with temporary stores, synthetic tokens and mocked HTTP."""
import ctypes
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from unittest import mock


class MicrosoftAccountRegressions(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "msa_regression_fixture", os.path.join(os.path.dirname(__file__), "msa.py"))
        self.msa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.msa)
        folder = tempfile.TemporaryDirectory(prefix="msa-regression-")
        self.addCleanup(folder.cleanup)
        self.msa.configure(folder.name)
        self.msa._CLIENT_ID = "fixture-public-client"
        self.network = mock.patch.object(
            self.msa.urllib.request, "urlopen", side_effect=self.http_response)
        self.network.start()
        self.addCleanup(self.network.stop)

    def payload(self, expiry=None, refresh="fixture-refresh"):
        return {
            "access_token": "fixture-minecraft-secret",
            "refresh_token": refresh,
            "expires_at": time.time() + 3600 if expiry is None else expiry,
            "client_id": "fixture-public-client",
            "xuid": "12345678",
            "profile": {"id": "0123456789abcdef0123456789abcdef", "name": "FixturePlayer"},
            "entitlements": ["game_minecraft"],
        }

    def write_plain(self, payload):
        with open(self.msa._STORE_PATH, "w", encoding="utf-8") as fh:
            json.dump({"v": 1, "plain": payload}, fh)

    def http_response(self, request, timeout=30):
        fixtures = {
            self.msa.AUTH + "/token": {
                "access_token": "fixture-ms-secret", "refresh_token": "fixture-rotated-refresh",
                "expires_in": 3600, "token_type": "Bearer", "scope": self.msa.SCOPES,
            },
            self.msa.XBL_USER: {"Token": "fixture-xbl-secret", "DisplayClaims": {"xui": [{"uhs": "123"}]}},
            self.msa.XSTS_AUTHORIZE: {"Token": "fixture-xsts-secret", "DisplayClaims": {"xui": [{"uhs": "123"}]}},
            self.msa.MCS + "/authentication/login_with_xbox": {
                "access_token": "fixture-minecraft-secret", "expires_in": 3600,
                "token_type": "Bearer", "DisplayClaims": {"xui": [{"xid": "12345678"}]},
            },
            self.msa.MCS + "/minecraft/profile": self.payload()["profile"],
            self.msa.MCS + "/entitlements/mcstore": {"items": [{"name": "game_minecraft", "signature": "fixture"}]},
        }
        if request.full_url not in fixtures:
            raise AssertionError("unexpected fixture URL: " + request.full_url)
        return io.BytesIO(json.dumps(fixtures[request.full_url]).encode("utf-8"))

    def start(self):
        url = self.msa.sign_in(8790)
        return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]

    def background(self, fn):
        result = {}

        def work():
            try:
                result["value"] = fn()
            except Exception as exc:
                result["error"] = exc

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        return thread, result

    def assert_finished(self, thread):
        thread.join(3)
        self.assertFalse(thread.is_alive(), "account operation remained blocked")

    def test_code_redemption_sends_form_parameters(self):
        captured = []

        def endpoint(request, timeout=30):
            captured.append(request)
            return self.http_response(request, timeout)

        pending = {"client_id": "fixture-public-client", "redirect": "http://localhost:8790/__msa/callback", "verifier": "v+/= space"}
        with mock.patch.object(self.msa.urllib.request, "urlopen", side_effect=endpoint):
            self.msa._redeem(pending, "code+/= space")
        request = captured[0]
        self.assertEqual(request.get_header("Content-type"), "application/x-www-form-urlencoded")
        self.assertEqual(urllib.parse.parse_qs(request.data.decode("utf-8")), {
            "client_id": ["fixture-public-client"], "grant_type": ["authorization_code"],
            "code": ["code+/= space"], "redirect_uri": ["http://localhost:8790/__msa/callback"],
            "code_verifier": ["v+/= space"], "scope": [self.msa.SCOPES],
        })

    def test_refresh_sends_form_parameters(self):
        captured = []

        def endpoint(request, timeout=30):
            captured.append(request)
            return self.http_response(request, timeout)

        with mock.patch.object(self.msa.urllib.request, "urlopen", side_effect=endpoint):
            self.msa._refresh("fixture-public-client", "refresh+/= space")
        request = captured[0]
        self.assertEqual(request.get_header("Content-type"), "application/x-www-form-urlencoded")
        self.assertEqual(urllib.parse.parse_qs(request.data.decode("utf-8")), {
            "client_id": ["fixture-public-client"], "grant_type": ["refresh_token"],
            "refresh_token": ["refresh+/= space"], "scope": [self.msa.SCOPES],
        })

    @unittest.skipUnless(sys.platform == "win32", "requires real Windows DPAPI")
    def test_windows_protection_round_trip(self):
        raw = b'{"fixture":"no real account"}'
        protected = self.msa._protect(raw)
        self.assertIsNotNone(protected, "DPAPI protection failed on this Windows runtime")
        self.assertNotEqual(protected, raw)
        self.assertEqual(self.msa._unprotect(protected), raw)

    @unittest.skipUnless(sys.platform == "win32", "requires real Windows DPAPI")
    def test_windows_unprotect_restores_an_independent_encrypted_blob(self):
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.c_void_p)]

        raw = b'{"fixture":"independently encrypted"}'
        buffer = ctypes.create_string_buffer(raw)
        source = Blob(len(raw), ctypes.cast(buffer, ctypes.c_void_p))
        encrypted = Blob()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        protect = crypt32.CryptProtectData
        protect.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob), ctypes.c_void_p,
                            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        protect.restype = wintypes.BOOL
        self.assertTrue(protect(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(encrypted)))
        free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        try:
            protected = ctypes.string_at(encrypted.pbData, encrypted.cbData)
        finally:
            free(encrypted.pbData)
        self.assertEqual(self.msa._unprotect(protected), raw)

    def test_failed_windows_protection_never_writes_plaintext(self):
        with mock.patch.object(self.msa.sys, "platform", "win32"), mock.patch.object(self.msa, "_protect", return_value=None):
            with self.assertRaises(ValueError):
                self.msa._save_store(self.payload())
        self.assertFalse(os.path.exists(self.msa._STORE_PATH))

    def test_failed_windows_protection_preserves_existing_store(self):
        self.write_plain(self.payload())
        with open(self.msa._STORE_PATH, "rb") as fh:
            old = fh.read()
        with mock.patch.object(self.msa.sys, "platform", "win32"), mock.patch.object(self.msa, "_protect", return_value=None):
            with self.assertRaises(ValueError):
                self.msa._save_store(self.payload(refresh="replacement"))
        with open(self.msa._STORE_PATH, "rb") as fh:
            self.assertEqual(fh.read(), old)

    def test_failed_atomic_store_write_preserves_session_and_cleans_temp_file(self):
        self.write_plain(self.payload())
        with open(self.msa._STORE_PATH, "rb") as fh:
            previous = fh.read()
        with mock.patch.object(self.msa.os, "replace", side_effect=OSError("fixture disk full")):
            with self.assertRaises(OSError):
                self.msa._save_store(self.payload(refresh="replacement"))
            snapshot = self.msa.status()
        with open(self.msa._STORE_PATH, "rb") as fh:
            self.assertEqual(fh.read(), previous)
        self.assertEqual(os.listdir(os.path.dirname(self.msa._STORE_PATH)), ["msa_tokens.json"])
        self.assertIsNotNone(snapshot["storageError"])
        self.assertIn("could not be saved", snapshot["storageError"])

    @unittest.skipUnless(sys.platform == "win32", "requires real Windows DPAPI")
    def test_legacy_plaintext_session_migrates_to_dpapi(self):
        self.write_plain(self.payload())
        self.assertEqual(self.msa.game_identity()["name"], "FixturePlayer")
        with open(self.msa._STORE_PATH, encoding="utf-8") as fh:
            document = json.load(fh)
        self.assertNotIn("plain", document)
        self.assertIn("dpapi", document)
        self.assertEqual(self.msa.storage_kind(), "dpapi")

    def test_legacy_plaintext_is_preserved_and_reported_when_migration_fails(self):
        self.write_plain(self.payload())
        with mock.patch.object(self.msa.sys, "platform", "win32"), mock.patch.object(self.msa, "_protect", return_value=None):
            self.assertEqual(self.msa.game_identity()["name"], "FixturePlayer")
            self.assertEqual(self.msa.status()["storage"], "plain")

    def test_renderer_account_record_contains_no_credentials(self):
        self.write_plain(self.payload())
        record = self.msa.account_record()
        serialized = json.dumps(record)
        self.assertEqual(record["javaProfileName"], "FixturePlayer")
        self.assertNotIn("fixture-minecraft-secret", serialized)
        self.assertNotIn("fixture-refresh", serialized)
        self.assertEqual(self.msa.game_identity()["access_token"], "fixture-minecraft-secret")

    def test_expired_session_without_refresh_cannot_supply_identity(self):
        self.write_plain(self.payload(expiry=time.time() - 1, refresh=""))
        self.assertIsNone(self.msa.game_identity())
        self.assertIsNone(self.msa.account_record())
        self.assertIsNone(self.msa.status()["account"])

    def test_refresh_failure_at_each_service_keeps_only_still_valid_sessions(self):
        urls = [self.msa.AUTH + "/token", self.msa.XBL_USER,
                self.msa.XSTS_AUTHORIZE, self.msa.MCS + "/authentication/login_with_xbox"]
        for expiry, expected in [(time.time() - 1, False), (time.time() + 60, True)]:
            for rejected_url in urls:
                with self.subTest(expired=not expected, service=rejected_url):
                    payload = self.payload(expiry=expiry)

                    def endpoint(request, timeout=30):
                        if request.full_url == rejected_url:
                            raise urllib.error.URLError("fixture network failure")
                        return self.http_response(request, timeout)

                    try:
                        with mock.patch.object(self.msa.urllib.request, "urlopen", side_effect=endpoint):
                            result = self.msa._refreshed(payload)
                    except Exception as exc:
                        self.fail("refresh propagated a service failure: %r" % exc)
                    self.assertEqual(result is not None, expected)
                    if expected:
                        self.assertEqual(result["access_token"], "fixture-minecraft-secret")

    def test_refresh_storage_failure_does_not_break_account_reading(self):
        payload = self.payload(expiry=time.time() - 1)
        with mock.patch.object(self.msa, "_save_store", side_effect=OSError("fixture disk full")):
            try:
                result = self.msa._refreshed(payload)
            except Exception as exc:
                self.fail("refresh propagated a storage failure: %r" % exc)
        self.assertIsNone(result)

    def test_malformed_stored_sessions_cannot_break_page_data(self):
        for field, value in [("profile", []), ("expires_at", "not a timestamp"),
                             ("access_token", []), ("entitlements", [{}])]:
            with self.subTest(field=field):
                payload = self.payload()
                payload[field] = value
                self.write_plain(payload)
                try:
                    record = self.msa.account_record()
                    identity = self.msa.game_identity()
                    snapshot = self.msa.status()
                except Exception as exc:
                    self.fail("malformed session propagated into page data: %r" % exc)
                self.assertIsNone(record)
                self.assertIsNone(identity)
                self.assertIsNone(snapshot["account"])

    def test_simultaneous_account_reads_refresh_the_session_once(self):
        self.write_plain(self.payload(expiry=time.time() - 1))
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls = []

        def endpoint(request, timeout=30):
            if request.full_url == self.msa.AUTH + "/token":
                calls.append(request)
                if len(calls) == 1:
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError("refresh fixture was not released")
            return self.http_response(request, timeout)

        with mock.patch.object(self.msa.urllib.request, "urlopen", side_effect=endpoint):
            first, first_result = self.background(self.msa.game_identity)
            self.assertTrue(entered.wait(3))
            second, second_result = self.background(self.msa.account_record)
            second.join(0.1)
            release.set()
            self.assert_finished(first)
            self.assert_finished(second)
        self.assertNotIn("error", first_result)
        self.assertNotIn("error", second_result)
        self.assertEqual(first_result["value"]["name"], "FixturePlayer")
        self.assertEqual(second_result["value"]["javaProfileName"], "FixturePlayer")
        self.assertEqual(len(calls), 1, "two readers redeemed the same refresh token")

    def test_expired_pending_request_allows_a_new_sign_in(self):
        old = self.start()
        with mock.patch.object(self.msa.time, "time", return_value=time.time() + 601):
            try:
                new = self.start()
            except ValueError as exc:
                self.fail("expired request prevented a retry: %s" % exc)
        self.assertNotEqual(new, old)
        self.assertEqual(self.msa.status()["phase"], "waiting")

    def test_polling_turns_expired_pending_request_into_retryable_error(self):
        self.start()
        with mock.patch.object(self.msa.time, "time", return_value=time.time() + 601):
            snapshot = self.msa.status()
        self.assertEqual(snapshot["phase"], "error")
        self.assertIn("expired", snapshot["error"])

    def test_logout_rejects_a_pending_callback_without_restoring_login(self):
        state = self.start()
        self.msa.logout()
        with self.assertRaises(ValueError):
            self.msa.callback("fixture-code", state)
        self.assertIsNone(self.msa.game_identity())
        self.assertEqual(self.msa.status()["phase"], "idle")

    def test_logout_reports_failed_session_deletion(self):
        self.write_plain(self.payload())
        with mock.patch.object(self.msa.os, "remove", side_effect=PermissionError("fixture file locked")):
            with self.assertRaises(ValueError):
                self.msa.logout()
        self.assertEqual(self.msa.status()["phase"], "error")
        self.assertIn("could not be removed", self.msa.status()["error"])

    def test_mismatched_callback_does_not_release_a_valid_pending_attempt(self):
        state = self.start()
        with self.assertRaises(ValueError):
            self.msa.callback("fixture-code", "stray-state")
        self.assertEqual(self.msa.status()["phase"], "waiting")
        with self.assertRaises(ValueError):
            self.start()
        self.msa.callback("fixture-code", state)
        self.assertEqual(self.msa.status()["phase"], "done")

    def test_provider_error_requires_the_pending_oauth_state(self):
        self.start()
        with self.assertRaises(ValueError):
            self.msa.fail("fixture provider rejection")
        self.assertEqual(self.msa.status()["phase"], "waiting")

    def test_matching_provider_error_cancels_attempt_and_allows_retry(self):
        state = self.start()
        try:
            self.msa.fail("fixture provider rejection", state)
        except Exception as exc:
            self.fail("matching provider error was not handled: %r" % exc)
        self.assertEqual(self.msa.status()["phase"], "error")
        self.assertEqual(self.msa.status()["error"], "fixture provider rejection")
        self.start()

    def test_stale_provider_error_cannot_cancel_a_new_attempt(self):
        state = self.start()
        self.msa.logout()
        new_state = self.start()
        try:
            with self.assertRaises(ValueError):
                self.msa.fail("fixture provider rejection", state)
        except TypeError as exc:
            self.fail("provider errors cannot validate their state: %s" % exc)
        self.assertEqual(self.msa.status()["phase"], "waiting")
        self.msa.callback("new-fixture-code", new_state)
        self.assertEqual(self.msa.status()["phase"], "done")

    def test_unexpected_service_response_cannot_expose_tokens_in_polling_error(self):
        state = self.start()

        def endpoint(request, timeout=30):
            if request.full_url == self.msa.XBL_USER:
                return io.BytesIO(b'{"access_token":"fixture-unexpected-secret"}')
            return self.http_response(request, timeout)

        with mock.patch.object(self.msa.urllib.request, "urlopen", side_effect=endpoint):
            with self.assertRaises(ValueError) as caught:
                self.msa.callback("fixture-code", state)
        self.assertNotIn("fixture-unexpected-secret", str(caught.exception))
        self.assertNotIn("fixture-unexpected-secret", json.dumps(self.msa.status()))

    def test_logout_invalidates_callback_already_redeeming_tokens(self):
        state = self.start()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def endpoint(request, timeout=30):
            if request.full_url == self.msa.AUTH + "/token":
                entered.set()
                if not release.wait(3):
                    raise AssertionError("callback fixture was not released")
            return self.http_response(request, timeout)

        with mock.patch.object(self.msa.urllib.request, "urlopen", side_effect=endpoint):
            thread, result = self.background(lambda: self.msa.callback("fixture-code", state))
            self.assertTrue(entered.wait(3))
            self.msa.logout()
            release.set()
            self.assert_finished(thread)
        self.assertIsNone(self.msa.game_identity())
        self.assertFalse(os.path.exists(self.msa._STORE_PATH))
        self.assertEqual(self.msa.status()["phase"], "idle")
        self.assertIsInstance(result.get("error"), ValueError)

    def test_cancelled_old_callback_cannot_clear_a_new_pending_request(self):
        state = self.start()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def endpoint(request, timeout=30):
            if request.full_url == self.msa.AUTH + "/token":
                entered.set()
                if not release.wait(3):
                    raise AssertionError("callback fixture was not released")
            return self.http_response(request, timeout)

        with mock.patch.object(self.msa.urllib.request, "urlopen", side_effect=endpoint):
            thread, _ = self.background(lambda: self.msa.callback("fixture-code", state))
            self.assertTrue(entered.wait(3))
            self.msa.logout()
            new_state = self.start()
            release.set()
            self.assert_finished(thread)
        self.assertEqual(self.msa.status()["phase"], "waiting")
        self.msa.callback("new-fixture-code", new_state)
        self.assertEqual(self.msa.status()["phase"], "done")

    def test_duplicate_callback_does_not_redeem_code_twice(self):
        state = self.start()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls = []

        def endpoint(request, timeout=30):
            if request.full_url == self.msa.AUTH + "/token":
                calls.append(request)
                if len(calls) == 1:
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError("callback fixture was not released")
            return self.http_response(request, timeout)

        with mock.patch.object(self.msa.urllib.request, "urlopen", side_effect=endpoint):
            thread, result = self.background(lambda: self.msa.callback("fixture-code", state))
            self.assertTrue(entered.wait(3))
            with self.assertRaises(ValueError):
                self.msa.callback("fixture-code", state)
            release.set()
            self.assert_finished(thread)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("error", result)
        self.assertEqual(self.msa.status()["phase"], "done")

    def test_logout_invalidates_refresh_already_in_flight(self):
        self.write_plain(self.payload(expiry=time.time() - 1))
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def endpoint(request, timeout=30):
            if request.full_url == self.msa.AUTH + "/token":
                entered.set()
                if not release.wait(3):
                    raise AssertionError("refresh fixture was not released")
            return self.http_response(request, timeout)

        with mock.patch.object(self.msa.urllib.request, "urlopen", side_effect=endpoint):
            thread, result = self.background(self.msa.game_identity)
            self.assertTrue(entered.wait(3))
            self.msa.logout()
            release.set()
            self.assert_finished(thread)
        self.assertNotIn("error", result)
        self.assertIsNone(result["value"])
        self.assertFalse(os.path.exists(self.msa._STORE_PATH))

    def test_changing_data_directory_invalidates_old_refresh(self):
        self.write_plain(self.payload(expiry=time.time() - 1))
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        new_folder = tempfile.TemporaryDirectory(prefix="msa-new-directory-")
        self.addCleanup(new_folder.cleanup)

        def endpoint(request, timeout=30):
            if request.full_url == self.msa.AUTH + "/token":
                entered.set()
                if not release.wait(3):
                    raise AssertionError("refresh fixture was not released")
            return self.http_response(request, timeout)

        with mock.patch.object(self.msa.urllib.request, "urlopen", side_effect=endpoint):
            thread, result = self.background(self.msa.game_identity)
            self.assertTrue(entered.wait(3))
            self.msa.configure(new_folder.name)
            release.set()
            self.assert_finished(thread)
        self.assertNotIn("error", result)
        self.assertIsNone(result["value"])
        self.assertFalse(os.path.exists(self.msa._STORE_PATH))

    def test_new_data_directory_resolves_its_own_public_client_id(self):
        with tempfile.TemporaryDirectory(prefix="msa-new-client-") as folder:
            with open(os.path.join(folder, "microsoft-client-id.txt"), "w", encoding="utf-8") as fh:
                fh.write("22222222-2222-4222-8222-222222222222")
            with mock.patch.dict(os.environ, {"LAUNCHER_MSA_CLIENT_ID": ""}):
                self.msa.configure(folder)
                self.assertEqual(self.msa.client_id(), "22222222-2222-4222-8222-222222222222")


class MicrosoftConfigurationRegressions(unittest.TestCase):
    FILE_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    ENV_ID = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="msa-config-regression-")
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.path = os.path.join(self.folder, "microsoft-client-id.txt")
        self.environment = mock.patch.dict(os.environ, {"LAUNCHER_MSA_CLIENT_ID": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.msa = self.fresh_module()
        network = mock.patch.object(self.msa.urllib.request, "urlopen",
                                   side_effect=AssertionError("configuration must not contact authentication services"))
        network.start()
        self.addCleanup(network.stop)

    def fresh_module(self):
        spec = importlib.util.spec_from_file_location(
            "msa_configuration_fixture", os.path.join(os.path.dirname(__file__), "msa.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.configure(self.folder)
        return module

    def test_unconfigured_setup_reports_no_id_without_loading_session(self):
        with mock.patch.object(self.msa, "_load_store", side_effect=AssertionError("setup must not read sessions")):
            self.assertEqual(self.msa.configuration(), {
                "configured": False, "clientId": None, "source": None, "environmentOverride": False})
        self.assertEqual(os.listdir(self.folder), [])

    def test_saved_public_id_survives_restart_and_builds_signin_url_without_touching_accounts(self):
        protected_files = {"msa_tokens.json": b"synthetic session sentinel",
                           "offline_accounts.json": b"synthetic account sentinel"}
        for name, raw in protected_files.items():
            with open(os.path.join(self.folder, name), "wb") as fh:
                fh.write(raw)
        result = self.msa.save_client_id("  " + self.FILE_ID.upper() + "\n")
        self.assertEqual(result, {"configured": True, "clientId": self.FILE_ID,
                                  "source": "file", "environmentOverride": False})
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), self.FILE_ID)
        restarted = self.fresh_module()
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(restarted.sign_in(8790)).query)
        self.assertEqual(params["client_id"], [self.FILE_ID])
        self.assertEqual(params["redirect_uri"], ["http://localhost:8790/__msa/callback"])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        for name, raw in protected_files.items():
            with open(os.path.join(self.folder, name), "rb") as fh:
                self.assertEqual(fh.read(), raw)

    def test_replacing_saved_id_refreshes_an_already_resolved_client(self):
        self.msa.save_client_id(self.FILE_ID)
        self.assertEqual(self.msa.client_id(), self.FILE_ID)
        self.msa.save_client_id(self.ENV_ID)
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.msa.sign_in(8790)).query)
        self.assertEqual(params["client_id"], [self.ENV_ID])

    def test_malformed_ids_are_rejected_without_creating_configuration(self):
        for value in (None, "", "not-an-application-id", "1" * 32, 17, {"clientId": self.FILE_ID},
                      self.FILE_ID + " extra", "00000000-0000-0000-0000-00000000000z"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.msa.save_client_id(value)
                self.assertFalse(os.path.exists(self.path))

    def test_environment_takes_precedence_while_saved_id_remains_available_after_restart(self):
        with mock.patch.dict(os.environ, {"LAUNCHER_MSA_CLIENT_ID": self.ENV_ID.upper()}):
            result = self.msa.save_client_id(self.FILE_ID)
            self.assertEqual(result, {"configured": True, "clientId": self.ENV_ID,
                                      "source": "environment", "environmentOverride": True})
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.msa.sign_in(8790)).query)
            self.assertEqual(params["client_id"], [self.ENV_ID])
        self.assertEqual(self.fresh_module().client_id(), self.FILE_ID)

    def test_invalid_environment_value_is_not_exposed_or_used_for_signin(self):
        with mock.patch.dict(os.environ, {"LAUNCHER_MSA_CLIENT_ID": "invalid-configuration-fixture"}):
            self.assertEqual(self.msa.configuration(), {
                "configured": False, "clientId": None, "source": "environment", "environmentOverride": True})
            with self.assertRaisesRegex(ValueError, "not configured"):
                self.msa.sign_in(8790)

    def test_blank_environment_does_not_override_saved_configuration(self):
        self.msa.save_client_id(self.FILE_ID)
        with mock.patch.dict(os.environ, {"LAUNCHER_MSA_CLIENT_ID": " \n"}):
            self.assertEqual(self.fresh_module().configuration(), {
                "configured": True, "clientId": self.FILE_ID, "source": "file", "environmentOverride": False})

    def test_waiting_or_completing_signin_cannot_change_its_application_id(self):
        for phase in ("waiting", "completing"):
            with self.subTest(phase=phase):
                self.msa = self.fresh_module()
                self.msa.save_client_id(self.FILE_ID)
                self.msa.sign_in(8790)
                self.msa._STATE["phase"] = phase
                with self.assertRaises(self.msa.ConfigurationBusy):
                    self.msa.save_client_id(self.ENV_ID)
                with open(self.path, encoding="utf-8") as fh:
                    self.assertEqual(fh.read().strip(), self.FILE_ID)
                self.assertEqual(self.msa._PENDING["client_id"], self.FILE_ID)

    def test_failed_atomic_save_preserves_previous_file_and_active_client(self):
        self.msa.save_client_id(self.FILE_ID)
        self.assertEqual(self.msa.client_id(), self.FILE_ID)
        with mock.patch.object(self.msa.os, "replace", side_effect=OSError("synthetic write failure")):
            with self.assertRaises(OSError):
                self.msa.save_client_id(self.ENV_ID)
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), self.FILE_ID)
        self.assertEqual(self.msa.client_id(), self.FILE_ID)
        self.assertEqual(os.listdir(self.folder), ["microsoft-client-id.txt"])

    def test_invalid_utf8_configuration_can_be_replaced_from_setup(self):
        with open(self.path, "wb") as fh:
            fh.write(b"\xff\xfeinvalid fixture")
        self.assertEqual(self.msa.configuration(), {
            "configured": False, "clientId": None, "source": "file", "environmentOverride": False})
        self.assertEqual(self.msa.save_client_id(self.FILE_ID)["clientId"], self.FILE_ID)

    def test_concurrent_uncached_read_cannot_restore_the_id_replaced_by_save(self):
        self.msa.save_client_id(self.FILE_ID)
        self.msa._CLIENT_ID = None
        captured, release, saved = threading.Event(), threading.Event(), threading.Event()
        read_configuration = self.msa._client_configuration
        results = {}

        def paused_configuration():
            result = read_configuration()
            if threading.current_thread().name == "msa-old-config-reader":
                captured.set()
                if not release.wait(3):
                    raise AssertionError("configuration reader fixture was not released")
            return result

        def read():
            results["read"] = self.msa.client_id()

        def save():
            results["save"] = self.msa.save_client_id(self.ENV_ID)
            saved.set()

        with mock.patch.object(self.msa, "_client_configuration", side_effect=paused_configuration):
            reader = threading.Thread(target=read, name="msa-old-config-reader", daemon=True)
            saver = threading.Thread(target=save, name="msa-new-config-saver", daemon=True)
            reader.start()
            try:
                self.assertTrue(captured.wait(3))
                saver.start()
                saved.wait(1)
            finally:
                release.set()
                reader.join(3)
                if saver.ident is not None:
                    saver.join(3)
        self.assertFalse(reader.is_alive())
        self.assertFalse(saver.is_alive())
        self.assertEqual(results["read"], self.FILE_ID)
        self.assertEqual(results["save"]["clientId"], self.ENV_ID)
        self.assertEqual(self.msa.client_id(), self.ENV_ID)


if __name__ == "__main__":
    unittest.main()
