"""Export path boundaries with disposable files and Windows directory junctions."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).parent))
import instance_content
import modpacks


class ModpackExportPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hx-pack-export-paths-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game = self.root / "game"
        self.outside = self.root / "outside"
        self.output = self.root / "exports"
        for folder in (self.game, self.outside, self.output):
            folder.mkdir()
        (self.game / "mods").mkdir()
        (self.game / "mods/safe.jar").write_bytes(b"safe selected mod")
        (self.outside / "private-token.txt").write_bytes(b"outside installation sentinel")
        previous_state = modpacks.state()
        self.addCleanup(modpacks._set, **previous_state)

    def junction(self, path, destination):
        if os.name != "nt":
            self.skipTest("Windows junction behavior requires Windows")
        result = subprocess.run(
            [os.environ.get("COMSPEC", "cmd.exe"), "/c", "mklink", "/J",
             str(path), str(destination)], capture_output=True, text=True,
        )
        if result.returncode:
            self.skipTest("This Windows host cannot create a disposable junction: " + result.stderr)
        # Remove only the junction itself before TemporaryDirectory cleanup.
        self.addCleanup(os.rmdir, path)

    def export_members(self):
        with mock.patch.object(modpacks, "_resolve_hashes", return_value={}):
            result = modpacks.export(str(self.game), {"name": "Path fixture"},
                                     str(self.output), {"minecraft": "1.21.1"})
        with zipfile.ZipFile(result["path"]) as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def assert_safe_export(self, members):
        self.assertEqual(members["overrides/mods/safe.jar"], b"safe selected mod")
        self.assertFalse(any("private-token" in name for name in members), members)
        self.assertEqual((self.outside / "private-token.txt").read_bytes(),
                         b"outside installation sentinel")

    def test_export_skips_a_pack_folder_junction(self):
        self.junction(self.game / "config", self.outside)
        self.assert_safe_export(self.export_members())

    def test_export_skips_nested_junction_and_preserves_safe_nested_files(self):
        (self.game / "config/safe").mkdir(parents=True)
        (self.game / "config/safe/settings.toml").write_bytes(b"enabled = true")
        self.junction(self.game / "config/private", self.outside)
        members = self.export_members()
        self.assert_safe_export(members)
        self.assertEqual(members["overrides/config/safe/settings.toml"], b"enabled = true")

    def test_export_rejects_a_selected_installation_junction(self):
        selected = self.root / "linked-game"
        self.junction(selected, self.game)
        with mock.patch.object(modpacks, "_resolve_hashes", return_value={}):
            with self.assertRaises(instance_content.ContentError) as caught:
                modpacks.export(str(selected), {"name": "Linked fixture"},
                                str(self.output), {"minecraft": "1.21.1"})
        self.assertEqual(caught.exception.code, "unsafe_path")
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual((self.game / "mods/safe.jar").read_bytes(), b"safe selected mod")

    def test_export_skips_a_file_marked_as_a_reparse_point(self):
        (self.game / "config").mkdir()
        reparse_file = self.game / "config/reparse-token.txt"
        reparse_file.write_bytes(b"reparse file sentinel")
        reparse_path = os.path.normcase(str(reparse_file.resolve()))
        real_reparse = instance_content._reparse

        def marked_reparse(path):
            if os.path.normcase(str(path)) == reparse_path:
                return True
            return real_reparse(path)

        with mock.patch.object(instance_content, "_reparse", side_effect=marked_reparse):
            members = self.export_members()
        self.assert_safe_export(members)
        self.assertNotIn("overrides/config/reparse-token.txt", members)
        self.assertEqual(reparse_file.read_bytes(), b"reparse file sentinel")


if __name__ == "__main__":
    unittest.main()
