"""Product capability regressions with disposable accounts and game directories."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
with tempfile.TemporaryDirectory() as bootstrap:
    with mock.patch.dict(os.environ, {"LAUNCHER_DATA_DIR": bootstrap}):
        import server


class ProductRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.data = self.root / "data"
        self.data.mkdir()
        patcher = mock.patch.multiple(server, STATE=str(self.state), GAME_DIR=str(self.root / "game"),
            DATA_DIR=str(self.data), STORE_OVERLAY=str(self.data / "state.json"),
            OFFLINE_STORE=str(self.data / "offline_accounts.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(server.msa, "account_record", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def snapshot(self, installed=False):
        with mock.patch.object(server.installer, "is_installed", return_value=installed), \
             mock.patch.object(server, "_install_plan", return_value=None):
            return server.build_appdata()

    def test_offline_account_has_local_capability_without_store_ownership(self):
        account = server._offline_record("FixturePlayer", "fixture")
        self.assertEqual(account["entitlements"], [])
        self.assertEqual(account["entitlementsSource"], {})
        self.assertEqual(account["licenseProductIds"], [])

    def test_saved_offline_compatibility_entitlements_are_not_republished(self):
        saved = {"id": "fixture", "type": "Offline", "offline": True, "username": "FixturePlayer",
            "javaProfileName": "FixturePlayer", "entitlements": ["game_minecraft", "game_minecraft_bedrock"],
            "entitlementsSource": {"game_minecraft": "RETAIL"}, "licenseProductIds": ["fixture-old-license"]}
        store = self.data / "offline_accounts.json"
        store.write_text(json.dumps({"accounts": [saved], "active": "fixture", "hidden": []}))
        before = store.read_bytes()
        accounts, active = server.build_accounts()
        self.assertEqual(active, "fixture")
        self.assertEqual(accounts[0]["entitlements"], [])
        self.assertEqual(accounts[0]["entitlementsSource"], {})
        self.assertEqual(accounts[0]["licenseProductIds"], [])
        self.assertEqual(store.read_bytes(), before)

    def test_captured_account_missing_entitlements_does_not_gain_ownership(self):
        (self.state / "launcher_accounts_microsoft_store.json").write_text(json.dumps({
            "accounts": {"fixture": {"localId": "fixture", "username": "FixturePlayer"}},
            "activeAccountLocalId": "fixture"}))
        accounts, _ = server.build_accounts()
        self.assertEqual(accounts[0]["entitlements"], [])
        self.assertEqual(accounts[0]["entitlementsSource"], {})

    def test_only_ready_java_installation_is_playable_offline(self):
        for installed in (False, True):
            with self.subTest(installed=installed):
                products = self.snapshot(installed)["productInfo"]["products"]
                self.assertEqual(products["java"]["versionData"][0]["isPlayableOffline"], installed)
                self.assertFalse(products["bedrock"]["versionData"][0]["isPlayableOffline"])
                self.assertFalse(products["bedrock"]["versionData"][0]["isInstalled"])

    def test_bedrock_keeps_windows_browsing_and_native_launcher_warning(self):
        data = self.snapshot()
        self.assertTrue(data["productInfo"]["products"]["bedrock"]["hasPlatformSupport"])
        self.assertEqual(data["productInfo"]["products"]["bedrock"]["productType"], "Uwp")
        self.assertEqual(data["systemInfo"]["platform"], "windows")
        self.assertTrue(data["gamecore"]["isGamecoreClientAvailable"])
        self.assertIsNone(data["gamecore"]["gamecoreClientInstallState"])

    def test_unimplemented_services_are_not_reported_as_connected(self):
        data = self.snapshot()
        self.assertTrue(data["connectionStatus"]["isOnline"])
        self.assertTrue(data["connectionStatus"]["substatus"]["launcherContent"])
        self.assertFalse(data["connectionStatus"]["substatus"]["minecraftServices"])
        self.assertFalse(data["connectionStatus"]["substatus"]["xalServices"])
        self.assertFalse(data["realmsData"]["java"]["supported"])
        self.assertFalse(data["realmsData"]["bedrock"]["supported"])

    def test_home_bedrock_launch_promo_keeps_article_without_unavailable_launch(self):
        promo = {"entries": [{"promoTitle": "Fixture", "heroImage": {"url": "/v2/images/fixture.png"},
            "associatedButtons": [{"type": "ReadMore", "readMoreLink": "https://example.invalid/article",
                "readMoreLaunchGame": {"launchGame": "bedrock", "launchGameText": "Launch Game"}}]}]}
        path = self.root / "promos.json"
        path.write_text(json.dumps(promo))
        filtered = json.loads(server.Handler._filter_promos(str(path)))
        button = filtered["entries"][0]["associatedButtons"][0]
        self.assertNotIn("readMoreLaunchGame", button)
        self.assertEqual(button["readMoreLink"], "https://example.invalid/article")
        self.assertEqual(button["readMoreGenericLink"], {"readMoreText": "Read More"})
        self.assertEqual(filtered["entries"][0]["heroImage"], promo["entries"][0]["heroImage"])
        self.assertEqual(json.loads(path.read_text()), promo)


if __name__ == "__main__":
    unittest.main()
