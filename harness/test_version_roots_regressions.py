"""Launch installations whose metadata lives outside the captured state store."""
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import launcher


class VersionStoreLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="launcher-version-stores-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.game = self.root / "install"
        self.run = self.root / "instance"
        for path in (self.state, self.game, self.run):
            path.mkdir()
        previous = copy.deepcopy(launcher.STATE)
        previous_lines = list(launcher.LINES)
        self.addCleanup(launcher.STATE.update, previous)
        self.addCleanup(lambda: launcher.LINES.__setitem__(slice(None), previous_lines))
        self.addCleanup(setattr, launcher, "_LINE_OFFSET", launcher._LINE_OFFSET)
        self.addCleanup(setattr, launcher, "_PROCESS", launcher._PROCESS)
        self.addCleanup(setattr, launcher, "_LOG_PATH", launcher._LOG_PATH)
        self.addCleanup(setattr, launcher, "_SECRET_VALUES", launcher._SECRET_VALUES)
        launcher.STATE.update(running=False)

    def metadata(self, root, ident, main="FixtureMain", **extra):
        folder = root / "versions" / ident
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (ident + ".json")).write_text(json.dumps({
            "id": ident, "mainClass": main, "libraries": [], **extra,
        }), encoding="utf-8")

    def launch(self, ident, jar_id=None):
        jar_id = jar_id or ident
        jar = self.game / "versions" / jar_id / (jar_id + ".jar")
        jar.parent.mkdir(parents=True, exist_ok=True)
        jar.write_bytes(b"fixture")
        process = types.SimpleNamespace(pid=42, stdout=io.BytesIO(), wait=lambda: 0)
        captured = []

        def spawn(args, **options):
            captured.append((args, options))
            return process

        with mock.patch.object(launcher, "_find_java", return_value="fixture-java"), \
                mock.patch.object(launcher.subprocess, "Popen", side_effect=spawn), \
                mock.patch.object(launcher, "threading", types.SimpleNamespace(Thread=lambda **kw: types.SimpleNamespace(start=lambda: None))):
            try:
                launcher.start(str(self.state), str(self.game), ident, run_dir=str(self.run),
                               player={"name": "Fixture", "uuid": "a" * 32, "access_token": "0"},
                               log_path=str(self.root / "game.log"))
            except RuntimeError as exc:
                self.fail("Saved installation could not launch: " + str(exc))
        self.assertEqual(len(captured), 1)
        return captured[0]

    def test_game_store_only_version_can_launch(self):
        self.metadata(self.game, "game-only", "GameStoreMain")
        args, options = self.launch("game-only")
        self.assertIn("GameStoreMain", args)
        self.assertEqual(options["cwd"], str(self.run))

    def test_explicit_instance_store_only_version_can_launch(self):
        self.metadata(self.run, "instance-only", "InstanceStoreMain")
        args, _ = self.launch("instance-only")
        self.assertIn("InstanceStoreMain", args)

    def test_instance_child_can_inherit_state_parent(self):
        self.metadata(self.state, "base", "VanillaMain")
        self.metadata(self.run, "child", "LoaderMain", inheritsFrom="base")
        args, options = self.launch("child", jar_id="base")
        self.assertIn("LoaderMain", args)
        self.assertIn(str(self.game / "versions" / "base" / "base.jar"), options["env"]["CLASSPATH"])

    def test_state_store_wins_conflicting_version_ids(self):
        self.metadata(self.state, "shared", "StateMain")
        self.metadata(self.game, "shared", "GameMain")
        self.metadata(self.run, "shared", "InstanceMain")
        args, _ = self.launch("shared")
        self.assertIn("StateMain", args)
        self.assertNotIn("GameMain", args)
        self.assertNotIn("InstanceMain", args)

    def test_game_store_utf8_bom_metadata_can_launch(self):
        self.metadata(self.game, "bom-game", "BomMain")
        path = self.game / "versions" / "bom-game" / "bom-game.json"
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
        args, _ = self.launch("bom-game")
        self.assertIn("BomMain", args)

    def test_latest_alias_reads_utf8_bom_manifest(self):
        self.metadata(self.state, "release", "ReleaseMain")
        manifest = self.state / "versions" / "version_manifest_v2.json"
        manifest.write_text(json.dumps({"latest": {"release": "release"}}), encoding="utf-8-sig")
        args, _ = self.launch("latest-release", jar_id="release")
        self.assertIn("ReleaseMain", args)

    def test_version_ids_cannot_read_outside_the_store(self):
        (self.state / "escape.json").write_text(json.dumps({"id": "outside", "libraries": []}))
        outside = self.root / "outside"
        outside.with_suffix(".json").write_text(json.dumps({"id": "outside", "libraries": []}))
        for ident in ("../escape", str(outside), "C:\\escape"):
            with self.subTest(version=ident), self.assertRaisesRegex(RuntimeError, "invalid version"):
                launcher._resolve_version(str(self.state), ident)

    def test_inherited_version_ids_cannot_read_outside_the_store(self):
        (self.state / "escape.json").write_text(json.dumps({"id": "outside", "libraries": []}))
        self.metadata(self.state, "child", inheritsFrom="../escape")
        with self.assertRaisesRegex(RuntimeError, "invalid version"):
            launcher._resolve_version(str(self.state), "child")

    def test_version_store_cannot_follow_a_symlink_outside_its_root(self):
        target = self.root / "outside-folder"
        target.mkdir()
        (target / "linked.json").write_text(json.dumps({"id": "linked", "libraries": []}))
        versions = self.state / "versions"
        versions.mkdir()
        try:
            os.symlink(target, versions / "linked", target_is_directory=True)
        except OSError as exc:
            self.skipTest("Directory symlinks are unavailable: " + str(exc))
        with self.assertRaises(RuntimeError):
            launcher._resolve_version(str(self.state), "linked")


if __name__ == "__main__":
    unittest.main()
