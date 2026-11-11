"""Behavioral modpack regressions; all archives and runtime files are disposable."""
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import modpacks


class ModpackFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hx-pack-regression-")
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        self.game = os.path.join(self.root, "game")
        self.state = os.path.join(self.root, "state")
        self.output = os.path.join(self.root, "exports")
        for folder in (self.game, self.state, self.output):
            os.makedirs(folder)
        self.previous_state = modpacks.state()
        self.addCleanup(modpacks._set, **self.previous_state)

    def write(self, relative, data):
        path = os.path.join(self.game, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def exported(self, name="Fixture pack", hashes=None):
        try:
            with mock.patch.object(modpacks, "_resolve_hashes", return_value=hashes or {}):
                result = modpacks.export(self.game, {"name": name}, self.output,
                                         {"minecraft": "1.21.1"})
        except (OSError, ValueError) as exc:
            self.fail("valid installation could not be exported: %s" % exc)
        return result

    def import_archive(self, archive_bytes, archive_hashes=None):
        version = {"id": "fixture-v", "version_number": "1", "version_type": "release",
                   "files": [{"primary": True, "filename": "fixture.mrpack",
                              "url": modpacks.CDN + "fixture.mrpack",
                              "hashes": archive_hashes or {}}]}

        def get(path, *args, **kwargs):
            return {"title": "Imported fixture"} if path.startswith("/project/") else {}

        def response(request, **kwargs):
            if request.full_url.endswith("fixture.mrpack"):
                return io.BytesIO(archive_bytes)
            return io.BytesIO(b"indexed file")

        with mock.patch.object(modpacks, "_get", side_effect=get), \
             mock.patch.object(modpacks, "_pick_version", return_value=version), \
             mock.patch.object(modpacks.urllib.request, "urlopen", side_effect=response):
            modpacks._import_job(self.game, self.state, "fixture", None, None, None)
        return modpacks.state()

    def pack_bytes(self, files=(), overrides=()):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("modrinth.index.json", json.dumps({
                "formatVersion": 1, "game": "minecraft", "files": list(files),
                "dependencies": {"minecraft": "1.21.1"},
            }))
            for name, contents in overrides:
                archive.writestr(name, contents)
        return data.getvalue()


class ModpackExportRegressions(ModpackFixture):
    def test_secondary_file_url_matches_exported_hash_and_reimports(self):
        data = b"secondary artifact"
        self.write("mods/secondary.jar", data)
        digest = hashlib.sha1(data).hexdigest()
        summary = {digest: {"files": [
            {"primary": True, "filename": "primary.jar", "size": 16,
             "url": modpacks.CDN + "primary.jar",
             "hashes": {"sha1": hashlib.sha1(b"primary artifact").hexdigest()}},
            {"primary": False, "filename": "secondary.jar", "size": len(data),
             "url": modpacks.CDN + "secondary.jar", "hashes": {"sha1": digest}},
        ]}}
        result = self.exported(hashes=summary)
        with zipfile.ZipFile(result["path"]) as archive:
            entry = json.loads(archive.read("modrinth.index.json"))["files"][0]
        self.assertEqual(entry["downloads"], [modpacks.CDN + "secondary.jar"])
        destination = os.path.join(self.root, "reimport")
        with mock.patch.object(modpacks.urllib.request, "urlopen", return_value=io.BytesIO(data)):
            modpacks._fetch_pack_file(destination, entry)
        with open(os.path.join(destination, "mods", "secondary.jar"), "rb") as fh:
            self.assertEqual(fh.read(), data)

    def test_unmatched_version_file_is_bundled_instead_of_referenced(self):
        data = b"local jar"
        self.write("mods/local.jar", data)
        digest = hashlib.sha1(data).hexdigest()
        result = self.exported(hashes={digest: {"files": [
            {"primary": True, "url": modpacks.CDN + "wrong.jar",
             "hashes": {"sha1": "0" * 40}},
        ]}})
        with zipfile.ZipFile(result["path"]) as archive:
            self.assertEqual(json.loads(archive.read("modrinth.index.json"))["files"], [])
            self.assertEqual(archive.read("overrides/mods/local.jar"), data)

    def test_unsafe_installation_names_stay_in_selected_export_folder(self):
        self.write("mods/local.jar", b"jar")
        for name in ("Pack: test", "Pack / test", "../escaped", "CON", "Pack\\test"):
            with self.subTest(name=name):
                result = self.exported(name)
                self.assertEqual(os.path.dirname(os.path.abspath(result["path"])), self.output)
                self.assertTrue(os.path.isfile(result["path"]))
                with zipfile.ZipFile(result["path"]) as archive:
                    self.assertEqual(json.loads(archive.read("modrinth.index.json"))["name"], name)

    def test_configuration_is_preserved_without_saved_data(self):
        self.write("mods/local.jar", b"jar")
        self.write("config/nested/settings.toml", b"enabled = false")
        self.write("saves/world/level.dat", b"private world")
        self.write("logs/latest.log", b"private log")
        self.write("options.txt", b"private options")
        result = self.exported()
        with zipfile.ZipFile(result["path"]) as archive:
            self.assertIn("overrides/config/nested/settings.toml", archive.namelist())
            self.assertEqual(archive.read("overrides/config/nested/settings.toml"), b"enabled = false")
            self.assertFalse(any(n.startswith(("overrides/saves/", "overrides/logs/"))
                                 or n == "overrides/options.txt" for n in archive.namelist()))

    def test_unpacked_resource_and_shader_packs_are_preserved(self):
        self.write("resourcepacks/Directory pack/pack.mcmeta", b'{"pack": {}}')
        self.write("resourcepacks/Directory pack/assets/example/textures/test.png", b"texture")
        self.write("shaderpacks/Directory shader/shaders/basic.vsh", b"shader")
        result = self.exported()
        with zipfile.ZipFile(result["path"]) as archive:
            self.assertEqual(archive.read("overrides/resourcepacks/Directory pack/pack.mcmeta"),
                             b'{"pack": {}}')
            self.assertEqual(archive.read("overrides/resourcepacks/Directory pack/assets/example/textures/test.png"), b"texture")
            self.assertEqual(archive.read("overrides/shaderpacks/Directory shader/shaders/basic.vsh"), b"shader")

    def test_failed_archive_write_removes_partial_export(self):
        self.write("mods/local.jar", b"jar")
        with mock.patch.object(modpacks, "_resolve_hashes", return_value={}), \
             mock.patch.object(zipfile.ZipFile, "write", side_effect=OSError("fixture read failure")):
            with self.assertRaises(OSError):
                modpacks.export(self.game, {"name": "Partial"}, self.output)
        self.assertEqual(os.listdir(self.output), [])


class ModpackImportRegressions(ModpackFixture):
    def test_completion_counts_final_files_including_bundled_overrides(self):
        files = [
            {"path": "config/test.txt", "downloads": [modpacks.CDN + "indexed.txt"],
             "hashes": {"sha1": hashlib.sha1(b"indexed file").hexdigest()}},
            {"path": "mods/server.jar", "env": {"client": "disabled"},
             "hashes": {"sha1": "0" * 40}},
        ]
        status = self.import_archive(self.pack_bytes(files, [
            ("overrides/config/test.txt", b"common override"),
            ("client-overrides/config/test.txt", b"client override"),
            ("overrides/resourcepacks/Home/pack.mcmeta", b"local pack metadata"),
        ]))
        self.assertIsNone(status['error'], status)
        self.assertEqual(status['result']['files'], 2)
        self.assertFalse(os.path.exists(os.path.join(status['result']['instanceDir'], 'mods', 'server.jar')))

    def test_local_export_roundtrip_works_without_catalog_or_archive_download(self):
        original = b'local custom mod'
        self.write('mods/custom.jar', original)
        try:
            with mock.patch.object(modpacks, '_resolve_hashes', side_effect=modpacks.urllib.error.URLError('offline')):
                result = modpacks.export(self.game, {'name':'Home pack'}, self.output, {'minecraft':'1.21.1'})
        except Exception as error:
            self.fail('locally available files must remain exportable during catalog outage: %s' % error)
        self.assertTrue(result.get('warnings'))
        self.assertEqual(result['bundled'], 1)
        with mock.patch.object(modpacks, '_get', side_effect=AssertionError('no catalog lookup')), \
             mock.patch.object(modpacks.urllib.request, 'urlopen', side_effect=AssertionError('no archive download')):
            modpacks._import_job(self.game, self.state, None, None, None, None, archive_path=result['path'])
        status = modpacks.state()
        self.assertIsNone(status['error'], status)
        self.assertEqual(status['result']['name'], 'Home pack')
        self.assertEqual(status['result']['source'], 'local')
        self.assertEqual(status['result']['files'], 1)
        with open(os.path.join(status['result']['instanceDir'],'mods','custom.jar'),'rb') as file:
            self.assertEqual(file.read(), original)
        self.assertTrue(os.path.isfile(result['path']))

    def test_local_pack_retains_source_and_rejects_invalid_index_before_publication(self):
        self.assertIn('archive_path', modpacks._import_job.__code__.co_varnames, 'local archives must enter the validated import pipeline')
        target = os.path.join(self.output,'invalid.mrpack')
        with zipfile.ZipFile(target,'w') as archive:
            archive.writestr('modrinth.index.json', json.dumps({'formatVersion':1,'game':'minecraft','dependencies':{'minecraft':'1.21.1'},'files':[{'path':'../escape.jar','hashes':{'sha1':'0'*40},'downloads':[modpacks.CDN+'fixture.jar']}]}))
        completed=[]
        modpacks._import_job(self.game,self.state,None,None,None,completed.append,archive_path=target)
        status=modpacks.state()
        self.assertIsNotNone(status['error'])
        self.assertEqual(completed,[])
        self.assertTrue(os.path.isfile(target))
        self.assertEqual(os.listdir(os.path.join(self.game,'instances')),[])

    def test_local_pack_index_size_limit_precedes_json_read(self):
        self.assertIn('archive_path', modpacks._import_job.__code__.co_varnames, 'local archives must enter the validated import pipeline')
        target=os.path.join(self.output,'too-large.mrpack')
        with open(target,'wb') as file:
            file.write(self.pack_bytes(overrides=[('overrides/config/local.txt',b'keep')]))
        with mock.patch.object(modpacks,'MAX_PACK_INDEX_BYTES',8,create=True):
            modpacks._import_job(self.game,self.state,None,None,None,None,archive_path=target)
        self.assertIsNotNone(modpacks.state()['error'])
        self.assertEqual(os.listdir(os.path.join(self.game,'instances')),[])

    def test_local_import_rejects_non_pack_before_a_worker_or_job_starts(self):
        self.assertTrue(callable(getattr(modpacks,'start_import_file',None)), 'local modpack import must have a validated entry point')
        target=os.path.join(self.output,'settings.json')
        with open(target,'w') as file:
            file.write('{}')
        with mock.patch.object(modpacks.threading,'Thread') as thread:
            with self.assertRaises(ValueError):
                modpacks.start_import_file(self.game,self.state,target)
        thread.assert_not_called()

    def test_overrides_replace_indexed_files_and_client_override_wins(self):
        files = [{"path": "config/test.txt", "downloads": [modpacks.CDN + "indexed.txt"],
                  "hashes": {"sha1": hashlib.sha1(b"indexed file").hexdigest()}}]
        pack = self.pack_bytes(files, [
            ("client-overrides/config/test.txt", b"client override"),
            ("overrides/config/test.txt", b"common override"),
        ])
        status = self.import_archive(pack)
        self.assertEqual(status['result']['files'], 1)
        self.assertIsNone(status["error"])
        with open(os.path.join(status["result"]["instanceDir"], "config", "test.txt"), "rb") as fh:
            self.assertEqual(fh.read(), b"client override")

    def test_corrupt_pack_archive_is_rejected_and_staging_is_cleaned(self):
        pack = self.pack_bytes(overrides=[("overrides/config/settings.txt", b"configuration")])
        status = self.import_archive(pack, archive_hashes={"sha1": "0" * 40})
        self.assertIsNotNone(status["error"])
        self.assertIsNone(status["result"])
        self.assertEqual(os.listdir(os.path.join(self.game, "instances")), [])

    def test_loader_document_cannot_escape_version_store(self):
        with self.assertRaises(ValueError):
            modpacks._store_version_doc(self.state, {"id": "../escaped", "mainClass": "fixture.Main"})
        self.assertFalse(os.path.exists(os.path.join(self.state, "escaped.json")))


class ForgeBootstrapRegressions(ModpackFixture):
    def test_real_runtime_bootstrap_prepares_java_before_loader_process(self):
        import installer
        java_data, support_data = b"fixture java", b"fixture support"
        runtime_data = json.dumps({"files": {
            "bin/java.exe": {"type": "file", "downloads": {"raw": {
                "url": "https://example.invalid/java", "size": len(java_data),
                "sha1": hashlib.sha1(java_data).hexdigest()}}},
            "lib/jvm.dll": {"type": "file", "downloads": {"raw": {
                "url": "https://example.invalid/support", "size": len(support_data),
                "sha1": hashlib.sha1(support_data).hexdigest()}}},
        }}).encode()
        metadata = os.path.join(self.state, "versions", "jre_manifest.json")
        os.makedirs(os.path.dirname(metadata))
        with open(metadata, "w", encoding="utf-8") as fh:
            json.dump({"manifest": {"windows-x64": {"runtime-fixture": [{"manifest": {
                "url": "https://example.invalid/runtime.json", "size": len(runtime_data),
                "sha1": hashlib.sha1(runtime_data).hexdigest()}}]}}}, fh)
        responses = {"https://example.invalid/java": java_data,
                     "https://example.invalid/support": support_data,
                     "https://example.invalid/runtime.json": runtime_data}
        ident = "1.21.1-forge-52.0.1"

        def process(args, **kwargs):
            with open(args[0], "rb") as fh:
                self.assertEqual(fh.read(), java_data)
            with open(os.path.join(self.game, "runtime", "runtime-fixture", "lib", "jvm.dll"), "rb") as fh:
                self.assertEqual(fh.read(), support_data)
            staged = os.path.join(args[-1], "versions", ident)
            os.makedirs(staged)
            with open(os.path.join(staged, ident + ".json"), "w", encoding="utf-8") as fh:
                json.dump({"id": ident, "inheritsFrom": "1.21.1"}, fh)
            return mock.Mock(returncode=0, stdout=b"installed")

        with mock.patch.object(installer.urllib.request, "urlopen", side_effect=lambda request, **kw: io.BytesIO(responses[request.full_url])), \
             mock.patch.object(modpacks, "_write_verified_download", side_effect=lambda url, dest: dest), \
             mock.patch.object(modpacks.subprocess, "run", side_effect=process):
            result = modpacks.materialize_loader(self.state, "1.21.1", "forge", lambda mc: ({
                "id": "1.21.1", "javaVersion": {"component": "runtime-fixture", "majorVersion": 21}}, None),
                version="52.0.1", game_dir=self.game)
        self.assertEqual(result, ident)
        self.assertFalse(any(name.startswith(".loader-") for name in os.listdir(self.state)))

    def test_fresh_forge_and_neoforge_bootstrap_declared_runtime(self):
        import installer
        parent = {"id": "1.21.1", "javaVersion": {"component": "java-runtime-delta", "majorVersion": 21}}
        for loader, version, ident in (("forge", "52.0.1", "1.21.1-forge-52.0.1"),
                                       ("neoforge", "21.1.72", "neoforge-21.1.72")):
            with self.subTest(loader=loader):
                def runtime(game, state, document, java_component=None):
                    self.assertEqual(document, parent)
                    path = os.path.join(game, "runtime", "java-runtime-delta", "bin", "java.exe")
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "wb") as fh:
                        fh.write(b"fixture runtime")
                    return path

                def download(url, dest):
                    with open(dest, "wb") as fh:
                        fh.write(b"fixture installer")
                    return dest

                def run(args, **kwargs):
                    self.assertEqual(args[0], os.path.join(self.game, "runtime", "java-runtime-delta", "bin", "java.exe"))
                    staged = os.path.join(args[-1], "versions", ident)
                    os.makedirs(staged)
                    with open(os.path.join(staged, ident + ".json"), "w", encoding="utf-8") as fh:
                        json.dump({"id": ident, "inheritsFrom": "1.21.1"}, fh)
                    return mock.Mock(returncode=0, stdout=b"installed")

                with mock.patch.object(installer, "ensure_runtime", side_effect=runtime, create=True), \
                     mock.patch.object(modpacks, "_find_java", return_value=None), \
                     mock.patch.object(modpacks, "_write_verified_download", side_effect=download), \
                     mock.patch.object(modpacks.subprocess, "run", side_effect=run):
                    try:
                        result = modpacks.materialize_loader(self.state, "1.21.1", loader,
                                                             lambda mc: (dict(parent), None),
                                                             version=version, game_dir=self.game)
                    except ValueError as exc:
                        self.fail("fresh loader installation needs to bootstrap Java: %s" % exc)
                self.assertEqual(result, ident)
                self.assertFalse(any(name.startswith(".loader-") for name in os.listdir(self.state)))

    def test_loader_staging_does_not_reuse_or_remove_existing_folder(self):
        sentinel = os.path.join(self.state, ".loader-123")
        os.makedirs(sentinel)
        marker = os.path.join(sentinel, "keep.txt")
        with open(marker, "wb") as fh:
            fh.write(b"existing stage")
        with mock.patch.object(modpacks.time, "time", return_value=123), \
             mock.patch.object(modpacks, "_write_verified_download", side_effect=ValueError("fixture abort")):
            with self.assertRaises(ValueError):
                modpacks.install_forge_like(self.game, self.state, "forge", "1.21.1", "52.0.1")
        self.assertTrue(os.path.isfile(marker), "another staging folder was removed")
        with open(marker, "rb") as fh:
            self.assertEqual(fh.read(), b"existing stage")


if __name__ == "__main__":
    unittest.main()
