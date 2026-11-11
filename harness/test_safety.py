"""Focused regression tests for the launcher host.

    python -m unittest discover -s harness -p "test_*.py"

No network is required: every test either exercises a pure function or builds
its own throwaway directories. Three areas are covered, in priority order:

  1. untrusted modpack paths  -- a malicious .mrpack must never write outside
     its own instance folder, and a loader installer may only ever be built
     from validated version tokens against the loader's official Maven;
  2. installation persistence -- created/edited/deleted installations must
     survive a reload of the host state, and a modpack import must write a
     profile the merged list keeps;
  3. release safety           -- the captured Microsoft account store (with
     another machine's tokens) must never be packaged or seeded.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import modpacks  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SafeRelativeTests(unittest.TestCase):
    """`_safe_relative` is the gate between a pack index/zip and the disk."""

    def test_accepts_plain_relative_paths(self):
        for name in ("mods/sodium.jar", "config/x.toml", "./a/b.txt", "a//b.txt"):
            self.assertTrue(modpacks._safe_relative(name), name)

    def test_rejects_escaping_and_absolute_paths(self):
        bad = (
            None, "", "..", "../evil", "a/../../evil", "..\\evil",
            "/etc/passwd", "/", "//server/share/x", "C:/evil", "C:\\evil",
            "c:evil", "a:b", "\\\\?\\C:\\evil", "mods/../../out.jar",
        )
        for name in bad:
            self.assertIsNone(modpacks._safe_relative(name), repr(name))

    def test_inside_stays_under_root(self):
        root = os.path.join(os.sep, "tmp", "instance")
        target = modpacks._inside(root, "mods/x.jar")
        self.assertEqual(os.path.normpath(target),
                         os.path.normpath(os.path.join(root, "mods", "x.jar")))


class InstallerDeduplicationTests(unittest.TestCase):
    def test_download_plan_deduplicates_same_destination(self):
        import installer

        downloads = []
        duplicate = {"url": "https://example.invalid/runtime", "dest": "runtime/bin/java.exe", "size": 10}
        with mock.patch.object(installer, "_download_one", side_effect=lambda item: downloads.append(item)):
            installer._install_all("unused", [dict(duplicate), dict(duplicate)], {}, None, {})
        self.assertEqual(len(downloads), 1)
        self.assertEqual(downloads[0]["dest"], duplicate["dest"])

    def test_distinct_runtime_paths_remain_distinct(self):
        import installer

        downloads = []
        version = {
            "id": "test",
            "downloads": {},
            "assetIndex": None,
            "libraries": [],
        }
        original_arch = installer._machine_arch
        installer._machine_arch = lambda: "x86_64"
        self.addCleanup(setattr, installer, "_machine_arch", original_arch)
        jre = {
            "manifest": {
                "windows-x64": {
                    "runtime-a": [{"manifest": {"url": "https://example.invalid/runtime.json"}}],
                }
            }
        }
        runtime = {"files": {
            "bin/java.exe": {"type": "file", "downloads": {"raw": {"url": "https://example.invalid/java", "size": 10}}},
            "bin/javaw.exe": {"type": "file", "downloads": {"raw": {"url": "https://example.invalid/java", "size": 10}}},
        }}
        game_dir = tempfile.mkdtemp(prefix="hx-java-plan-")
        self.addCleanup(shutil.rmtree, game_dir, True)

        def write_runtime(_url, destination, _size, _sha1):
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with open(destination, "w", encoding="utf-8") as fh:
                json.dump(runtime, fh)

        with mock.patch.object(installer, "_fetch", side_effect=write_runtime), \
             mock.patch.object(installer, "_download_one", side_effect=lambda item: downloads.append(item)):
            plan = installer._java_plan(game_dir, jre, "runtime-a")
            installer._install_all(game_dir, plan, jre, "runtime-a", version)
        self.assertEqual(len(plan), 2)
        self.assertEqual(len(downloads), 2)
        self.assertNotEqual(downloads[0]["dest"], downloads[1]["dest"])


class LoaderInstallInheritanceTests(unittest.TestCase):
    def test_forge_loader_install_plan_includes_vanilla_boot_files(self):
        import installer

        base = tempfile.mkdtemp(prefix="hx-loader-parent-")
        self.addCleanup(shutil.rmtree, base, True)
        state = os.path.join(base, "state")
        game = os.path.join(base, "game")
        loader_id = "forge-test"
        version = {
            "id": loader_id,
            "inheritsFrom": "1.21.11",
            "mainClass": "net.minecraftforge.bootstrap.ForgeBootstrap",
            "libraries": [{"name": "forge:loader", "downloads": {
                "artifact": {"path": "forge/loader.jar", "url": "https://example.invalid/loader.jar", "size": 10},
            }}],
        }
        parent = {
            "id": "1.21.11",
            "downloads": {"client": {"url": "https://example.invalid/client.jar", "size": 100}},
            "assetIndex": {"id": "test-assets", "url": "https://example.invalid/assets.json", "size": 20},
            "javaVersion": {"component": "java-runtime-delta", "majorVersion": 21},
            "libraries": [{"name": "minecraft:library", "downloads": {
                "artifact": {"path": "minecraft/library.jar", "url": "https://example.invalid/library.jar", "size": 30},
            }}],
        }
        loader_path = os.path.join(state, "versions", loader_id, loader_id + ".json")
        parent_path = os.path.join(state, "versions", "1.21.11", "1.21.11.json")
        os.makedirs(os.path.dirname(loader_path))
        os.makedirs(os.path.dirname(parent_path))
        with open(loader_path, "w", encoding="utf-8") as fh:
            json.dump(version, fh)
        with open(parent_path, "w", encoding="utf-8") as fh:
            json.dump(parent, fh)

        resolved = installer._resolved_version(version, loader_path)
        plan = installer._framework(game, resolved)
        self.assertEqual([entry["phase"] for entry in plan[:2]], ["client", "libraries"])
        self.assertEqual(plan[0]["dest"], os.path.join(game, "versions", "1.21.11", "1.21.11.jar"))
        self.assertIn(os.path.join(game, "libraries", "forge", "loader.jar"),
                      [entry["dest"] for entry in plan])
        self.assertIn(os.path.join(game, "assets", "indexes", "test-assets.json"),
                      [entry["dest"] for entry in plan])
        self.assertEqual(resolved["jar"], "1.21.11")
        self.assertEqual(resolved["javaVersion"]["component"], "java-runtime-delta")

    def test_empty_url_installer_library_is_copied_from_local_maven_tree(self):
        import installer

        with tempfile.TemporaryDirectory(prefix="hx-forge-local-artifact-") as root:
            state = os.path.join(root, "state")
            game = os.path.join(root, "game")
            version_id = "forge-test"
            artifact_rel = "net/minecraftforge/forge/forge-test/client.jar"
            source = os.path.join(state, "libraries", *artifact_rel.split("/"))
            os.makedirs(os.path.dirname(source))
            with open(source, "wb") as fh:
                fh.write(b"generated client artifact")
            digest = installer._sha1_of(source)
            version = {"id": version_id, "libraries": [{"name": "net.minecraftforge:forge:forge-test:client",
                "downloads": {"artifact": {"path": artifact_rel, "url": "", "sha1": digest, "size": os.path.getsize(source)}}}]}

            plan = installer._framework(game, version, state)
            self.assertEqual(len(plan), 1)
            self.assertEqual(plan[0]["source"], source)
            self.assertNotIn("url", plan[0])
            installer._install_all(game, plan, {}, None, version)

            target = os.path.join(game, "libraries", *artifact_rel.split("/"))
            with open(target, "rb") as fh:
                self.assertEqual(fh.read(), b"generated client artifact")


class OverrideExtractionTests(unittest.TestCase):
    """A zip-slip member must not write outside the instance folder."""

    def test_zip_slip_is_contained(self):
        base = tempfile.mkdtemp(prefix="hx-test-")
        self.addCleanup(shutil.rmtree, base, True)
        instance = os.path.join(base, "instance")
        outside = os.path.join(base, "outside.txt")
        pack = os.path.join(base, "pack.zip")
        with zipfile.ZipFile(pack, "w") as archive:
            archive.writestr("overrides/../../outside.txt", "escaped")
            archive.writestr("overrides/mods/ok.txt", "kept")
            archive.writestr("client-overrides/../escape2.txt", "escaped")
            archive.writestr("overrides/", "")
        with zipfile.ZipFile(pack) as archive:
            modpacks._apply_overrides(archive, instance)
        self.assertFalse(os.path.exists(outside),
                         "a traversal member wrote outside the instance")
        self.assertFalse(os.path.exists(os.path.join(base, "escape2.txt")))
        with open(os.path.join(instance, "mods", "ok.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "kept")


class ForgeNeoForgeUrlTests(unittest.TestCase):
    """Installer URLs are built only from validated tokens and official hosts."""

    def test_forge_url(self):
        url = modpacks.installer_url("forge", "1.20.1", "47.4.0")
        self.assertEqual(
            url,
            "https://maven.minecraftforge.net/net/minecraftforge/forge/"
            "1.20.1-47.4.0/forge-1.20.1-47.4.0-installer.jar",
        )

    def test_neoforge_url(self):
        url = modpacks.installer_url("neoforge", "1.21.1", "21.1.72")
        self.assertEqual(
            url,
            "https://maven.neoforged.net/releases/net/neoforged/neoforge/"
            "21.1.72/neoforge-21.1.72-installer.jar",
        )

    def test_neoforge_legacy_1201_url(self):
        url = modpacks.installer_url("neoforge", "1.20.1", "47.1.106")
        self.assertIn("net/neoforged/forge/1.20.1-47.1.106/", url)

    def test_rejects_hostile_tokens(self):
        for mc, ver in (("1.20.1", "../evil"), ("1.20.1", "a b"),
                        ("../etc", "1.0"), ("1.20.1", ""), ("", "1.0")):
            with self.assertRaises(ValueError, msg="%s/%s" % (mc, ver)):
                modpacks.installer_url("forge", mc, ver)

    def test_neoforge_version_filter(self):
        versions = ["21.0.1", "21.1.1", "21.1.72", "20.4.237", "22.0.1"]
        self.assertEqual(
            modpacks.neoforge_versions_for("1.21.1", versions), ["21.1.72", "21.1.1"]
        )
        # Pre-1.20.5 lines are not offered: their schemes do not map safely.
        self.assertEqual(modpacks.neoforge_versions_for("1.20.1", versions), [])
        self.assertEqual(modpacks.neoforge_versions_for("24w14a", versions), [])


class LoaderFlattenTests(unittest.TestCase):
    def test_inherited_client_jar_uses_vanilla_jar_id(self):
        import launcher

        with tempfile.TemporaryDirectory(prefix="hx-loader-classpath-") as game:
            jar_dir = os.path.join(game, "versions", "1.21.11")
            os.makedirs(jar_dir)
            jar = os.path.join(jar_dir, "1.21.11.jar")
            with open(jar, "wb") as fh:
                fh.write(b"client")
            version = {"id": "forge-test", "jar": "1.21.11"}
            self.assertEqual(launcher._classpath(version, game), [jar])

    def test_child_overrides_parent(self):
        child = {"id": "fabric-loader-1", "mainClass": "net.fabricmc.loader",
                 "libraries": [{"name": "c:1"}]}
        parent = {"id": "1.21", "mainClass": "net.minecraft.client.main.Main",
                  "libraries": [{"name": "p:1"}], "arguments": {"game": ["--p"]}}
        merged = modpacks.flatten_loader_profile(child, parent)
        self.assertEqual(merged["id"], "fabric-loader-1")
        self.assertEqual(merged["mainClass"], "net.fabricmc.loader")
        self.assertNotIn("inheritsFrom", merged)
        self.assertEqual([lib["name"] for lib in merged["libraries"]],
                         ["c:1", "p:1"])


class InstallationPersistenceTests(unittest.TestCase):
    """The installations list must survive a host reload (fresh module read)."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="hx-test-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.state = os.path.join(self.base, "state")
        self.data = os.path.join(self.base, "data")
        self.game = os.path.join(self.base, "game")
        for path in (self.state, self.data, self.game):
            os.makedirs(path)
        os.makedirs(os.path.join(self.state, "versions"))
        with open(os.path.join(self.state, "launcher_profiles.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"profiles": {"release": {"name": "Latest release",
                                                "lastVersionId": "latest-release"}}}, fh)
        # Point the server module at the throwaway trees before using it.
        import server
        self.server = server
        server.STATE = self.state
        server.GAME_DIR = self.game
        server.DATA_DIR = self.data
        server.STORE_OVERLAY = os.path.join(self.data, "state.json")
        server.OFFLINE_STORE = os.path.join(self.data, "offline_accounts.json")
        self.versions = os.path.join(self.base, "versions.json")
        with open(self.versions, "w", encoding="utf-8") as fh:
            json.dump({
                "latest": {"release": "1.20.6", "snapshot": "24w14a"},
                "versions": {
                    "1.20.6": {"id": "1.20.6", "type": "release", "issues": []},
                    "24w14a": {"id": "24w14a", "type": "snapshot", "issues": []},
                },
            }, fh)
        previous_versions = server.VERSIONS
        self.addCleanup(setattr, server, "VERSIONS", previous_versions)
        server.VERSIONS = self.versions

    def test_unresolved_installation_versions_remain_resolvable(self):
        self.server._save_configuration("unknown-vanilla", {
            "name": "Old vanilla", "lastVersionId": "1.7.10", "type": "custom",
        })
        self.server._save_configuration("missing-loader", {
            "name": "Missing loader files",
            "lastVersionId": "fabric-loader-0.16-1.21",
            "type": "custom",
        })

        versions = self.server._load_versions()["versions"]
        self.assertEqual(versions["1.7.10"]["issues"], [])
        self.assertEqual(versions["fabric-loader-0.16-1.21"]["issues"], ["modded"])

    def test_profile_only_versions_do_not_enter_version_selector(self):
        self.server._save_configuration("unknown-vanilla", {
            "name": "Old vanilla", "lastVersionId": "1.7.10", "type": "custom",
        })
        versions = self.server._load_versions(selector=True)["versions"]
        self.assertNotIn("1.7.10", versions)
        self.assertIn("1.20.6", versions)

    def test_create_update_delete_survives_reload(self):
        profiles = self.server._effective_configurations()
        self.assertIn("release", profiles)

        created = {"name": "Modded", "lastVersionId": "fabric-loader-0.16-1.21"}
        self.server._save_configuration("new1", created)
        # A fresh read (the state a page reload performs) still has it.
        self.assertIn("new1", self.server._effective_configurations())

        self.server._save_configuration("new1", dict(created, name="Renamed"))
        self.assertEqual(self.server._effective_configurations()["new1"]["name"],
                         "Renamed")

        overlay = self.server._load_overlay()
        overlay["__configurations"].pop("new1", None)
        overlay["__deleted_configurations"] = ["new1"]
        self.server._write_overlay(overlay)
        self.assertNotIn("new1", self.server._effective_configurations())

    def test_modpack_import_profile_is_merged_and_durable(self):
        instance = os.path.join(self.game, "instances", "Pack A")
        os.makedirs(instance)
        result = self.server._modpack_imported({
            "name": "Pack A", "instanceDir": instance,
            "lastVersionId": "neoforge-21.1.72", "minecraft": "1.21.1",
            "loader": "neoforge", "loaderVersion": "21.1.72",
            "projectId": "p", "versionId": "v", "files": 1, "bytes": 1,
        })
        profiles = self.server._effective_configurations()
        ident = result["id"]
        self.assertIn(ident, profiles)
        self.assertEqual(profiles[ident]["lastVersionId"], "neoforge-21.1.72")
        self.assertEqual(profiles[ident]["gameDir"], instance)
        # The dependencies an export writes back must recover the MC version
        # from the profile metadata the import recorded.
        deps = self.server._modpack_dependencies(profiles[ident])
        self.assertEqual(deps.get("minecraft"), "1.21.1")
        self.assertEqual(deps.get("neoforge"), "21.1.72")

    def test_forge_profile_dependency_derivation(self):
        deps = self.server._modpack_dependencies(
            {"lastVersionId": "forge-1.20.1-47.4.0", "modpack": {}})
        self.assertEqual(deps.get("minecraft"), "1.20.1")


class ReleaseSafetyTests(unittest.TestCase):
    """The captured account store must never ship or be seeded."""

    def test_package_json_excludes_captured_account_store(self):
        with open(os.path.join(REPO, "app", "package.json"), encoding="utf-8") as fh:
            package = json.load(fh)
        serialized = json.dumps(package["build"]["extraResources"])
        self.assertNotIn("launcher_accounts_microsoft_store.json", serialized)

    def test_state_seed_excludes_captured_account_store(self):
        with open(os.path.join(REPO, "app", "main.js"), encoding="utf-8") as fh:
            text = fh.read()
        seed = text.split("const STATE_SEED = [", 1)[1].split("];", 1)[0]
        self.assertNotIn("launcher_accounts_microsoft_store.json", seed)

    def test_state_seed_includes_runtime_manifest(self):
        with open(os.path.join(REPO, "app", "main.js"), encoding="utf-8") as fh:
            text = fh.read()
        seed = text.split("const STATE_SEED = [", 1)[1].split("];", 1)[0]
        self.assertIn('"versions/jre_manifest.json"', seed)

    def test_packaged_resources_include_runtime_manifest(self):
        with open(os.path.join(REPO, "app", "package.json"), encoding="utf-8") as fh:
            package = json.load(fh)
        resources = package["build"]["extraResources"]
        state_resource = next(r for r in resources if r.get("to") == "root/default-state")
        self.assertIn("versions/**/*.json", state_resource["filter"])


class MicrosoftSignInTests(unittest.TestCase):
    """The sign-in flow's own machinery, with no network."""

    def setUp(self):
        import msa
        self.msa = msa
        self.base = tempfile.mkdtemp(prefix="hx-test-")
        self.addCleanup(shutil.rmtree, self.base, True)
        msa.configure(self.base)
        msa._CLIENT_ID = None
        msa.logout()

    def tearDown(self):
        self.msa._CLIENT_ID = None
        os.environ.pop("LAUNCHER_MSA_CLIENT_ID", None)

    def test_unconfigured_signin_fails_honestly(self):
        self.assertFalse(self.msa.configured())
        with self.assertRaises(ValueError) as caught:
            self.msa.sign_in(8790)
        self.assertIn("not configured", str(caught.exception))

    def test_authorize_url_shape(self):
        os.environ["LAUNCHER_MSA_CLIENT_ID"] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        url = self.msa.sign_in(8790)
        self.assertIn("login.microsoftonline.com/consumers/oauth2/v2.0/authorize", url)
        self.assertIn("http%3A%2F%2Flocalhost%3A8790%2F__msa%2Fcallback", url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("xboxlive.signin", url)
        status = self.msa.status()
        self.assertEqual(status["phase"], "waiting")
        self.assertTrue(status["configured"])

    def test_callback_rejects_wrong_state(self):
        os.environ["LAUNCHER_MSA_CLIENT_ID"] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        self.msa.sign_in(8790)
        with self.assertRaises(ValueError) as caught:
            self.msa.callback("code", "not-the-state")
        self.assertIn("did not match", str(caught.exception))

    def test_second_signin_while_pending_is_refused(self):
        os.environ["LAUNCHER_MSA_CLIENT_ID"] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        self.msa.sign_in(8790)
        with self.assertRaises(ValueError):
            self.msa.sign_in(8790)

    def test_pkce_challenge_matches_verifier(self):
        import base64
        import hashlib
        verifier, challenge = self.msa._pkce()
        recomputed = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode()
        self.assertEqual(challenge, recomputed)
        self.assertGreaterEqual(len(verifier), 43)


if __name__ == "__main__":
    unittest.main()
