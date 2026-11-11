"""Installation readiness resolves saved metadata without moving instance assets."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import installer


class InstallationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="launcher-installation-stores-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state, self.game, self.run = [self.root / name for name in ("state", "shared", "instance")]
        for folder in (self.state, self.game, self.run):
            folder.mkdir()
        previous_state, previous_hints = copy.deepcopy(installer._STATE), dict(installer.DIR_HINTS)
        self.addCleanup(installer._STATE.update, previous_state)
        self.addCleanup(installer.DIR_HINTS.update, previous_hints)
        installer._STATE.update(active=False, phase="idle", todo=[], result=None, _detail={}, error=None)
        installer.DIR_HINTS.update(game_dir=str(self.game), state=str(self.state), version_id=None, run_dir=None)
        patcher = mock.patch.object(installer, "_system_java", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(installer.urllib.request, "urlopen", side_effect=AssertionError("fixture attempted network"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path, data=b"fixture"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def metadata(self, root, ident, bom=False, **extra):
        path = root / "versions" / ident / (ident + ".json")
        data = json.dumps({"id": ident, "libraries": [], **extra}).encode()
        return self.write(path, (b"\xef\xbb\xbf" if bom else b"") + data)

    def artifacts(self, root, ident):
        self.write(root / "versions" / ident / (ident + ".jar"))
        self.write(root / "runtime" / "jre-legacy" / "bin" / "java.exe")
        self.write(root / "runtime" / "jre-legacy" / "manifest.json", json.dumps({
            "files": {"bin/java.exe": {"type": "file", "size": 7}},
        }).encode())

    def test_shared_store_bom_metadata_is_ready(self):
        self.metadata(self.game, "shared-only", bom=True)
        self.artifacts(self.game, "shared-only")
        self.assertTrue(installer.is_installed(str(self.game), str(self.state), "shared-only"))

    def test_instance_child_inherits_bom_state_parent(self):
        self.metadata(self.state, "base", bom=True)
        self.metadata(self.run, "child", bom=True, inheritsFrom="base")
        self.artifacts(self.game, "base")
        self.assertTrue(installer.is_installed(str(self.game), str(self.state), "child", run_dir=str(self.run)))

    def test_state_metadata_precedes_conflicting_shared_store(self):
        self.metadata(self.state, "conflict", libraries=[{"downloads": {"artifact": {
            "path": "fixture/missing.jar", "url": "https://example.invalid/missing.jar", "size": 7,
        }}}])
        self.metadata(self.game, "conflict")
        self.artifacts(self.game, "conflict")
        self.assertFalse(installer.is_installed(str(self.game), str(self.state), "conflict"))

    def test_instance_only_artifacts_do_not_mark_shared_install_ready(self):
        self.metadata(self.run, "isolated")
        self.artifacts(self.run, "isolated")
        self.assertFalse(installer.is_installed(str(self.game), str(self.state), "isolated", run_dir=str(self.run)))

    def test_bom_latest_manifest_resolves_shared_metadata(self):
        self.write(self.state / "versions" / "version_manifest_v2.json", b"\xef\xbb\xbf" + json.dumps({
            "latest": {"release": "shared-only"},
        }).encode())
        self.metadata(self.game, "shared-only")
        self.artifacts(self.game, "shared-only")
        self.assertTrue(installer.is_installed(str(self.game), str(self.state), "latest-release"))

    def test_status_finds_shared_metadata_when_no_install_is_selected(self):
        self.metadata(self.game, "shared-only")
        self.artifacts(self.game, "shared-only")
        self.assertTrue(installer.state()["isInstalled"])

    def test_inherited_path_cannot_escape_version_store(self):
        child = self.metadata(self.state, "child", inheritsFrom="../escape")
        self.write(self.state / "escape.json", json.dumps({"id": "base", "libraries": []}).encode())
        with self.assertRaises(RuntimeError):
            installer._resolved_version(json.loads(child.read_text()), str(child))

    def test_instance_installer_uses_shared_artifacts_and_original_state_sources(self):
        self.metadata(self.state, "base", bom=True, downloads={"client": {
            "url": "https://example.invalid/client.jar", "size": 7,
        }}, libraries=[{"downloads": {"artifact": {"path": "fixture/generated.jar", "size": 7}}}])
        child = self.metadata(self.run, "child", bom=True, inheritsFrom="base")
        self.artifacts(self.game, "base")
        self.write(self.state / "libraries" / "fixture" / "generated.jar")

        real_thread = installer.threading.Thread

        def immediate_thread(target, args=(), **options):
            if options.get("name") == "mc-installer":
                return types.SimpleNamespace(start=lambda: target(*args))
            return real_thread(target=target, args=args, **options)

        with mock.patch.object(installer.threading, "Thread", side_effect=immediate_thread):
            result = installer.start(str(self.game), str(self.state), str(child), run_dir=str(self.run))
        self.assertEqual(result["phase"], "done", result)
        self.assertTrue(result["isInstalled"])
        self.assertEqual((self.game / "libraries" / "fixture" / "generated.jar").read_bytes(), b"fixture")
        self.assertFalse((self.run / "libraries").exists())


if __name__ == "__main__":
    unittest.main()
