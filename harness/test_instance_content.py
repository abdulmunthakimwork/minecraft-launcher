"""Content storage contracts use disposable installations and real archives."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import struct
import sys
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).parent))
import instance_content as content


class ContentStoreTests(unittest.TestCase):
    def test_local_mod_original_logo_is_inspected_as_data(self):
        original = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1QAAAABJRU5ErkJggg==')
        self.archive('game/mods/logo.jar', {'fabric.mod.json': {'id': 'logo', 'version': '1', 'icon': {'16': 'assets/icon.png', '64': 'assets/icon.png'}}, 'assets/icon.png': original})
        item = self.store.inventory()['items'][0]
        self.assertEqual(item.get('iconData'), 'data:image/png;base64,' + base64.b64encode(original).decode('ascii'))
        self.assertEqual(item['modIds'], ['logo'])

    def test_bundled_dependency_logo_does_not_replace_root_mod_artwork(self):
        original = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1QAAAABJRU5ErkJggg==')
        child = self.archive('stage/child.jar', {'fabric.mod.json': {'id': 'child', 'version': '1', 'icon': 'icon.png'}, 'icon.png': original})
        self.archive('game/mods/root.jar', {'fabric.mod.json': {'id': 'root', 'version': '1', 'jars': [{'file': 'child.jar'}]}, 'child.jar': child.read_bytes()})
        item = self.store.inventory()['items'][0]
        self.assertIn('child', item['embeddedModIds'])
        self.assertNotIn('iconData', item)

    def test_quilt_forge_neoforge_legacy_and_resource_pack_logos_use_original_bytes(self):
        original = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1QAAAABJRU5ErkJggg==')
        self.archive('game/mods/quilt.jar', {'quilt.mod.json': {'quilt_loader': {'id': 'quilt', 'version': '1', 'metadata': {'icon': 'icon.png'}}}, 'icon.png': original})
        for name, descriptor in (('forge', 'META-INF/mods.toml'), ('neoforge', 'META-INF/neoforge.mods.toml')):
            self.archive('game/mods/' + name + '.jar', {descriptor: '[[mods]]\nmodId="' + name + '"\nversion="1"\nlogoFile="icon.png"', 'icon.png': original})
        self.archive('game/mods/legacy.jar', {'mcmod.info': [{'modid': 'legacy', 'version': '1', 'logoFile': 'icon.png'}], 'icon.png': original})
        self.archive('game/resourcepacks/pack.zip', {'pack.mcmeta': {'pack': {'pack_format': 15, 'description': 'Original pack'}}, 'pack.png': original})
        folder = self.root / 'resourcepacks' / 'unpacked'
        folder.mkdir()
        (folder / 'pack.mcmeta').write_text(json.dumps({'pack': {'pack_format': 15, 'description': 'Unpacked'}}))
        (folder / 'pack.png').write_bytes(original)
        items = self.store.inventory()['items']
        self.assertEqual(len(items), 6)
        expected = 'data:image/png;base64,' + base64.b64encode(original).decode('ascii')
        self.assertTrue(all(item.get('iconData') == expected for item in items), [(item['path'], item.get('iconData')) for item in items])

    def test_oversized_local_logo_is_optional_and_keeps_mod_scan_usable(self):
        self.archive('game/mods/oversized.jar', {'fabric.mod.json': {'id': 'oversized', 'version': '1', 'icon': 'icon.png'}, 'icon.png': b'x' * (256 * 1024 + 1)})
        inventory = self.store.inventory()
        self.assertFalse(inventory['problems'])
        self.assertEqual(inventory['items'][0]['modIds'], ['oversized'])
        self.assertNotIn('iconData', inventory['items'][0])

    def test_untrusted_saved_logo_url_is_discarded_without_losing_provider_identity(self):
        self.install(self.fabric(), metadata={'provider': 'modrinth', 'projectId': 'p', 'versionId': 'v', 'iconUrl': 'https://evil.test/icon.png'})
        item = self.store.inventory()['items'][0]
        self.assertEqual(item['projectId'], 'p')
        self.assertNotIn('iconUrl', item)

    def test_unsupported_or_missing_logo_does_not_invalidate_mod_metadata(self):
        for index, icon in enumerate(('missing.png', '../elsewhere.png', 'assets/unsafe.svg')):
            self.archive('game/mods/logo%d.jar' % index, {'fabric.mod.json': {'id': 'logo%d' % index, 'version': '1', 'icon': icon},
                'assets/unsafe.svg': '<svg><script>bad()</script></svg>'})
        inventory = self.store.inventory()
        self.assertFalse(inventory['problems'])
        self.assertTrue(all('iconData' not in item for item in inventory['items']))

    def test_provider_logo_survives_reopen_and_content_change_invalidates_provenance(self):
        metadata = {'provider': 'modrinth', 'projectId': 'p', 'versionId': 'v', 'iconUrl': 'https://cdn.modrinth.com/data/p/icon.png'}
        self.install(self.fabric(), metadata=metadata)
        reopened = content.ContentStore(self.root, self.data, 'fixture').inventory()['items'][0]
        self.assertEqual(reopened.get('iconUrl'), metadata['iconUrl'])
        target = self.root / reopened['path']
        target.write_bytes(target.read_bytes() + b'changed')
        self.assertNotIn('iconUrl', self.store.inventory()['items'][0])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="launcher-content-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "game"
        self.root.mkdir()
        self.data = self.base / "state"
        self.data.mkdir()
        self.store = content.ContentStore(self.root, self.data, "fixture")

    def archive(self, relative, entries):
        path = self.base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, value in entries.items():
                archive.writestr(name, json.dumps(value) if isinstance(value, (dict, list)) else value)
        return path

    def fabric(self, relative="stage/sample.jar", ident="sample", version="1.0", **extra):
        return self.archive(relative, {"fabric.mod.json": {
            "schemaVersion": 1, "id": ident, "name": "Sample mod", "version": version, **extra}})

    def install(self, source=None, filename="sample.jar", metadata=None, **extra):
        return self.store.transact([{"action": "install", "kind": "mods", "filename": filename,
            "source": str(source or self.fabric()), "metadata": metadata or {}, **extra}])

    def assert_code(self, code, callback):
        with self.assertRaises(content.ContentError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code, str(caught.exception))

    def test_missing_root_inventory_is_read_only_and_matches_empty_created_root(self):
        missing = self.base / "instances" / "new"
        store = content.ContentStore(missing, self.data, "new", allow_missing=True)
        baseline = store.inventory()
        self.assertEqual(baseline["items"], [])
        self.assertEqual(baseline["problems"], [])
        self.assertFalse(missing.parent.exists())
        missing.mkdir(parents=True)
        self.assertEqual(store.inventory()["revision"], baseline["revision"])

    def test_first_content_transaction_creates_missing_root_after_validation(self):
        missing = self.base / "instances" / "new"
        store = content.ContentStore(missing, self.data, "new", allow_missing=True)
        baseline = store.inventory()
        source = self.fabric()
        result = store.transact([{"action": "install", "kind": "mods", "filename": "sample.jar", "source": str(source)}], baseline["revision"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual((missing / "mods/sample.jar").read_bytes(), source.read_bytes())
        self.assertEqual(content.ContentStore(missing, self.data, "new").inventory()["revision"], result["revision"])

    def test_missing_root_invalid_or_stale_transaction_does_not_create_directory(self):
        missing = self.base / "instances" / "new"
        store = content.ContentStore(missing, self.data, "new", allow_missing=True)
        self.assert_code("stale_revision", lambda: store.transact([], "stale"))
        self.assert_code("invalid_change", lambda: store.transact([{"action": "invalid"}]))
        self.assertFalse(missing.parent.exists())

    def test_allow_missing_does_not_accept_existing_file_or_weaken_strict_default(self):
        missing = self.base / "missing"
        self.assert_code("missing_root", lambda: content.ContentStore(missing, self.data, "new"))
        missing.write_text("existing file")
        self.assert_code("missing_root", lambda: content.ContentStore(missing, self.data, "new", allow_missing=True))
        self.assertEqual(missing.read_text(), "existing file")

    def test_fabric_inventory_reads_identity_aliases_requirements_and_hashes(self):
        source = self.fabric("game/mods/sample.jar", provides=["alias"],
            depends={"minecraft": "~1.20.1", "helper": [">=1", "<3"]}, breaks={"bad": "*"})
        result = self.store.inventory()
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual((item["name"], item["version"], item["kind"]), ("Sample mod", "1.0", "mods"))
        self.assertEqual(item["modIds"], ["sample", "alias"])
        self.assertIn({"id": "helper", "requirement": [">=1", "<3"]}, item["depends"])
        self.assertEqual(item["conflicts"], [{"id": "bad", "requirement": "*"}])
        self.assertEqual(item["hashes"]["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(result["revision"], self.store.inventory()["revision"])

    def test_quilt_forge_neoforge_legacy_and_unpacked_pack_metadata(self):
        self.archive("game/mods/quilt.jar", {"quilt.mod.json": {"quilt_loader": {
            "id": "quilt", "version": "2", "metadata": {"name": "Quilt sample"},
            "provides": [{"id": "quilt_alias", "version": "2"}],
            "depends": [{"id": "helper", "versions": ">=2"}], "breaks": [{"id": "bad"}]}}})
        for name, descriptor in (("forge", "mods.toml"), ("neo", "neoforge.mods.toml")):
            self.archive(f"game/mods/{name}.jar", {f"META-INF/{descriptor}":
                '[[mods]]\nmodId = "one"\nversion = "3"\ndisplayName = "First"\n'
                '[[mods]]\nmodId = "two"\nversion = "3"\n'
                '[[dependencies.one]]\nmodId = "helper"\nmandatory = true\nversionRange = "[2,)"\n'
                '[[dependencies.two]]\nmodId = "bad"\ntype = "incompatible"\nversionRange = "*"\n'})
        self.archive("game/mods/legacy.jar.disabled", {"mcmod.info": [{"modid": "old", "name": "Old", "version": "0.8"}]})
        pack = self.root / "resourcepacks" / "unpacked"
        pack.mkdir(parents=True)
        (pack / "pack.mcmeta").write_text(json.dumps({"pack": {"pack_format": 15, "description": "Plain pack"}}))
        items = {item["filename"]: item for item in self.store.inventory()["items"]}
        self.assertEqual(items["quilt.jar"]["modIds"], ["quilt", "quilt_alias"])
        self.assertEqual(items["forge.jar"]["modIds"], ["one", "two"])
        self.assertEqual(items["neo.jar"]["conflicts"][0]["id"], "bad")
        self.assertFalse(items["legacy.jar.disabled"]["enabled"])
        self.assertEqual(items["unpacked"]["name"], "Plain pack")
        self.assertEqual(items["unpacked"]["packFormat"], 15)

    def test_declared_embedded_fabric_module_is_bounded_and_provided(self):
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, "w") as archive:
            archive.writestr("fabric.mod.json", json.dumps({"id": "embedded", "version": "4", "depends": {"helper": "*"}}))
        self.archive("game/mods/container.jar", {"fabric.mod.json": {
            "id": "container", "version": "1", "jars": [{"file": "nested/child.jar"}]},
            "nested/child.jar": nested.getvalue()})
        item = self.store.inventory()["items"][0]
        self.assertEqual(item["modIds"], ["container", "embedded"])
        self.assertEqual(item["providedVersions"]["embedded"], "4")
        self.assertEqual(item["depends"], [{"id": "helper", "requirement": "*"}])

    def test_nested_candidates_keep_own_versions_relations_and_parent_identity(self):
        children = []
        for version in ('1.0', '2.0'):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w') as archive:
                archive.writestr('fabric.mod.json', json.dumps({'id': 'shared_api', 'version': version,
                    'provides': ['api_alias'], 'depends': {'helper': '=' + version}}))
            children.append(stream.getvalue())
        source = self.archive('stage/bundle.jar', {'fabric.mod.json': {'id': 'bundle', 'version': '1.0',
            'depends': {'shared_api': '=1.0'}, 'jars': [{'file': 'nested/one.jar'}, {'file': 'nested/two.jar'}]},
            'nested/one.jar': children[0], 'nested/two.jar': children[1]})
        item = content.inspect_file(source)
        candidates = item.get('modCandidates', [])
        self.assertEqual(len(candidates), 3)
        root = next(candidate for candidate in candidates if candidate['root'])
        self.assertEqual(root['modIds'], ['bundle'])
        self.assertEqual(root['depends'], [{'id': 'shared_api', 'requirement': '=1.0', 'sourceFormat': 'fabric'}])
        embedded = [candidate for candidate in candidates if not candidate['root']]
        self.assertEqual([candidate['version'] for candidate in embedded], ['1.0', '2.0'])
        self.assertEqual([candidate['depends'][0]['requirement'] for candidate in embedded], ['=1.0', '=2.0'])
        self.assertTrue(all(candidate['parents'] == [root['key']] for candidate in embedded))
        self.assertTrue(all(candidate['providedVersions']['api_alias'] == candidate['version'] for candidate in embedded))

    def test_shared_bundled_modules_are_distinct_from_duplicate_root_mods(self):
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, "w") as archive:
            archive.writestr("fabric.mod.json", json.dumps({"id": "shared_api", "version": "1.0"}))
        for ident in ("iris", "sodium"):
            source = self.archive("stage/" + ident + ".jar", {"fabric.mod.json": {
                "id": ident, "version": "1.0", "jars": [{"file": "nested/api.jar"}]},
                "nested/api.jar": nested.getvalue()})
            self.store.transact([{"action": "install", "kind": "mods", "filename": ident + ".jar", "source": str(source)}])
        inventory = self.store.inventory()
        self.assertEqual(len(inventory["items"]), 2)
        self.assertFalse(any(problem["code"] == "duplicate_mod_id" for problem in inventory["problems"]))
        for item in inventory["items"]:
            self.assertEqual(item["rootModIds"], [item["filename"][:-4]])
            self.assertEqual(item["embeddedModIds"], ["shared_api"])
            self.assertIn("shared_api", item["modIds"])
        self.assert_code("duplicate_mod_id", lambda: self.install(self.fabric("stage/copy.jar", ident="iris"), filename="copy.jar"))

    def test_fabric_metadata_accepts_description_newlines_like_loader_reader(self):
        source = self.archive("stage/newline.jar", {"fabric.mod.json":
            '{"schemaVersion":1,"id":"grass","version":"1.8.7+fabric.26.2",'
            '"description":"First line\nSecond line","depends":{"minecraft":"~26.2-"}}'})
        item = content.inspect_file(source)
        self.assertEqual(item["modIds"], ["grass"])
        self.assertEqual(item["depends"], [{"id": "minecraft", "requirement": "~26.2-"}])
        self.assertFalse(item.get("unknown"))
        self.assertFalse(item.get("metadataProblems"))

    def test_mod_asset_pack_description_does_not_replace_mod_name(self):
        source = self.archive("stage/entities.jar", {"fabric.mod.json": {
            "id": "entities", "name": "Entity Model Features", "version": "3.3.12"},
            "pack.mcmeta": {"pack": {"pack_format": 15, "description": "Example Mod"}}})
        item = content.inspect_file(source)
        self.assertEqual(item["name"], "Entity Model Features")

    def test_invalid_other_loader_descriptor_does_not_discard_valid_fabric_metadata(self):
        source = self.archive("stage/universal.jar", {"fabric.mod.json": {
            "id": "mixintrace", "version": "1.0", "depends": {"minecraft": ">=26.1-"}},
            "META-INF/mods.toml": '[[dependencies.${mod_id}]]\nmodId="forge"\n'})
        item = content.inspect_file(source)
        self.assertEqual(item["rootMetadataFormats"], ["fabric"])
        self.assertEqual(item["modIds"], ["mixintrace"])
        self.assertEqual(item["depends"], [{"id": "minecraft", "requirement": ">=26.1-", "sourceFormat": "fabric"}])
        self.assertEqual(item["metadataProblems"][0]["sourceFormat"], "forge")
        self.assertFalse(item.get("unknown"))

    def test_forge_loader_requirement_is_preserved_as_environment_metadata(self):
        source = self.archive("stage/forge.jar", {"META-INF/mods.toml":
            'modLoader="javafml"\nloaderVersion="[47,)"\n[[mods]]\nmodId="sample"\nversion="1"\n'})
        item = content.inspect_file(source)
        self.assertIn({"id": "javafml", "requirement": "[47,)"}, item["depends"])

    def test_forge_standard_jar_version_token_uses_bounded_main_manifest(self):
        entries = {"META-INF/mods.toml": '[[mods]]\nmodId="sample"\nversion="${file.jarVersion}"\n',
            "META-INF/MANIFEST.MF": 'Manifest-Version: 1.0\r\nImplementation-Version: 1.2.\r\n 3\r\n\r\nName: nested.class\r\nImplementation-Version: 99\r\n'}
        item = content.inspect_file(self.archive("stage/version.jar", entries))
        self.assertEqual(item["version"], "1.2.3")
        self.assertEqual(item["providedVersions"]["sample"], "1.2.3")
        entries.pop("META-INF/MANIFEST.MF")
        unresolved = content.inspect_file(self.archive("stage/missing-version.jar", entries))
        self.assertIsNone(unresolved["version"])
        self.assertNotIn("sample", unresolved.get("providedVersions", {}))
        self.assertIn("unresolved_version", {problem["code"] for problem in unresolved["metadataProblems"]})

    def test_multi_format_archive_only_applies_selected_loader_requirements(self):
        from content_resolver import validate_inventory
        source = self.archive("stage/multiple.jar", {
            "fabric.mod.json": {"id": "sample", "version": "1", "environment": "server", "depends": {"fabricloader": ">=0.16"}},
            "META-INF/mods.toml": 'modLoader="javafml"\nloaderVersion="[47,)"\n[[mods]]\nmodId="sample"\nversion="1"\n'})
        item = content.inspect_file(source)
        provider = type("Provider", (), {"name": "modrinth"})()
        result = validate_inventory(provider, {"minecraft": "1.20.1", "loader": "forge", "loaderVersion": "47.1.0"}, [item])
        self.assertFalse(result["conflicts"])
        fabric = validate_inventory(provider, {"minecraft": "1.20.1", "loader": "fabric", "loaderVersion": "0.16.5"}, [item])
        self.assertIn("client_unsupported", {problem["code"] for problem in fabric["conflicts"]})

    def test_neoforge_discouraged_dependency_is_advisory(self):
        from content_resolver import validate_inventory
        source = self.archive("stage/discouraged.jar", {"META-INF/neoforge.mods.toml":
            '[[mods]]\nmodId="sample"\nversion="1"\n[[dependencies.sample]]\nmodId="other"\ntype="discouraged"\nversionRange="*"\n'})
        item = content.inspect_file(source)
        other = content.inspect_file(self.archive("stage/other.jar", {"META-INF/neoforge.mods.toml":
            '[[mods]]\nmodId="other"\nversion="1"\n'}))
        provider = type("Provider", (), {"name": "modrinth"})()
        result = validate_inventory(provider, {"minecraft": "1.20.1", "loader": "neoforge"}, [item, other])
        self.assertFalse(result["conflicts"])
        self.assertIn("discouraged_content", {warning["code"] for warning in result["warnings"]})

    def test_unknown_files_and_malformed_archives_are_preserved(self):
        folder = self.root / "mods"
        folder.mkdir()
        (folder / "notes.txt").write_text("keep me")
        (folder / "broken.jar").write_bytes(b"not a jar")
        self.archive("game/resourcepacks/malformed.zip", {"pack.mcmeta": "{broken"})
        inventory = self.store.inventory()
        self.assertEqual(len(inventory["items"]), 3)
        self.assertTrue(inventory["problems"])
        self.install()
        self.assertEqual((folder / "notes.txt").read_text(), "keep me")
        self.assertEqual((folder / "broken.jar").read_bytes(), b"not a jar")

    def test_same_content_retry_reuses_existing_item_without_revision_change(self):
        source = self.fabric()
        first = self.install(source, metadata={"provider": "modrinth", "projectId": "p", "versionId": "v"})
        second = self.install(source, filename="different.jar", metadata={"provider": "modrinth", "projectId": "p", "versionId": "v"})
        self.assertEqual(second["revision"], first["revision"])
        self.assertEqual(len(second["items"]), 1)
        self.assertFalse((self.root / "mods" / "different.jar").exists())

    def test_reused_dependency_can_gain_user_ownership_and_new_reverse_dependents(self):
        source = self.fabric()
        first = self.install(source, metadata={"provider": "modrinth", "projectId": "p", "userRequested": False, "requiredBy": ["first"]})
        result = self.install(source, metadata={"provider": "modrinth", "projectId": "p", "userRequested": True, "requiredBy": ["first", "second"]})
        self.assertTrue(result["items"][0]["userRequested"])
        self.assertEqual(result["items"][0]["requiredBy"], ["first", "second"])
        self.assertNotEqual(result["revision"], first["revision"])
        self.assertEqual(len(result["items"]), 1)

    def test_provider_compatibility_metadata_survives_reopen_and_local_edits_invalidate_it(self):
        metadata = {"provider": "modrinth", "projectId": "p", "versionId": "v",
            "releaseChannels": ["release", "beta"], "gameVersions": ["1.20.1"], "loaders": ["fabric"],
            "providerDependencies": [{"project_id": "helper", "version_id": "helper-v", "dependency_type": "required"}],
            "embeddedProjects": ["bundled"], "embeddedVersions": ["bundled-v"],
            "modIds": ["forged"], "depends": [], "optionalDepends": [], "metadataFormats": ["forged"], "rootMetadataFormats": ["forged"],
            "unknown": True, "metadataProblems": [{"code": "forged"}]}
        result = self.install(self.fabric(depends={"helper": "*"}, recommends={"optional": "*"}), metadata=metadata)
        reopened = content.ContentStore(self.root, self.data, "fixture").inventory()["items"][0]
        for field in ("releaseChannels", "gameVersions", "loaders", "providerDependencies", "embeddedProjects", "embeddedVersions"):
            self.assertEqual(reopened.get(field), metadata[field], field)
        self.assertEqual(reopened["modIds"], ["sample"])
        self.assertEqual(reopened["depends"], [{"id": "helper", "requirement": "*"}])
        self.assertEqual(reopened["optionalDepends"], [{"id": "optional", "requirement": "*"}])
        self.assertEqual(reopened["metadataFormats"], ["fabric"])
        self.assertEqual(reopened["rootMetadataFormats"], ["fabric"])
        self.assertFalse(reopened.get("unknown"))
        self.assertFalse(reopened["metadataProblems"])
        target = self.root / reopened["path"]
        target.write_bytes(target.read_bytes() + b"local edit")
        changed = self.store.inventory()["items"][0]
        self.assertTrue(changed["localModified"])
        for field in metadata:
            if field not in ("modIds", "depends", "optionalDepends", "metadataFormats", "rootMetadataFormats", "unknown", "metadataProblems"):
                self.assertNotIn(field, changed, field)

    def test_exact_byte_reuse_can_update_release_channel_preferences(self):
        source = self.fabric()
        self.install(source, metadata={"provider": "modrinth", "projectId": "p", "releaseChannels": ["release"], "userRequested": True})
        result = self.install(source, metadata={"releaseChannels": ["release", "beta"], "userRequested": False})
        self.assertEqual(result["items"][0].get("releaseChannels"), ["release", "beta"])
        self.assertTrue(result["items"][0]["userRequested"])

    def test_enabled_mod_id_collision_is_rejected_before_touching_files(self):
        first = self.install()
        source = self.fabric("stage/other.jar", version="2")
        self.assert_code("duplicate_mod_id", lambda: self.install(source, "other.jar"))
        self.assertEqual(self.store.inventory()["revision"], first["revision"])
        self.assertFalse((self.root / "mods" / "other.jar").exists())

    def test_project_collision_is_provider_scoped_and_requires_replacement(self):
        first = self.install(metadata={"provider": "modrinth", "projectId": "p"})
        source = self.fabric("stage/other.jar", ident="other")
        self.assert_code("duplicate_project", lambda: self.install(source, "other.jar", {"provider": "modrinth", "projectId": "p"}))
        result = self.install(source, "other.jar", {"provider": "modrinth", "projectId": "p"}, replaces=first["items"][0]["id"])
        self.assertEqual([item["filename"] for item in result["items"]], ["other.jar"])

    def test_toggle_keeps_identity_and_duplicate_disabled_ids_cannot_be_enabled(self):
        first = self.install()
        ident = first["items"][0]["id"]
        disabled = self.store.transact([{"action": "toggle", "id": ident, "enabled": False}])
        self.assertEqual(disabled["items"][0]["id"], ident)
        self.assertEqual(disabled["items"][0]["path"], "mods/sample.jar.disabled")
        self.assertTrue((self.root / "mods" / "sample.jar.disabled").is_file())
        self.install(self.fabric("stage/other.jar", version="2"), "other.jar")
        self.assert_code("duplicate_mod_id", lambda: self.store.transact([{"action": "toggle", "id": ident, "enabled": True}]))

    def test_replacement_at_same_filename_requires_explicit_identity(self):
        first = self.install()
        source = self.fabric("stage/updated.jar", version="2")
        self.assert_code("path_conflict", lambda: self.install(source))
        result = self.install(source, replaces=first["items"][0]["id"])
        self.assertEqual(result["items"][0]["version"], "2")
        self.assertEqual(result["items"][0]["id"], first["items"][0]["id"])

    def test_stale_revision_and_external_local_changes_do_not_destroy_contents(self):
        first = self.install(metadata={"provider": "modrinth", "projectId": "p", "versionId": "v"})
        item = first["items"][0]
        old = (self.root / item["path"]).read_bytes()
        (self.root / item["path"]).write_bytes(old + b"local edit")
        self.assert_code("stale_revision", lambda: self.store.transact([{"action": "remove", "id": item["id"]}], first["revision"]))
        now = self.store.inventory()
        self.assertNotIn("versionId", now["items"][0])
        self.assertTrue(now["items"][0]["localModified"])
        self.assert_code("local_changed", lambda: self.store.transact([{"action": "remove", "id": item["id"]}], now["revision"]))
        self.assertEqual((self.root / item["path"]).read_bytes(), old + b"local edit")

    def test_metadata_hash_mismatch_refuses_install_and_manifest_forged_identity(self):
        source = self.fabric()
        self.assert_code("hash_mismatch", lambda: self.install(source, metadata={"hashes": {"sha256": "0" * 64}}))
        self.assertEqual(self.store.inventory()["items"], [])
        result = self.install(source, metadata={"modIds": ["forged"], "id": "fake", "path": "../bad", "name": "Catalog name", "userRequested": True})
        self.assertEqual(result["items"][0]["modIds"], ["sample"])
        self.assertNotEqual(result["items"][0]["id"], "fake")
        self.assertEqual(result["items"][0]["name"], "Sample mod")
        self.assertTrue(result["items"][0]["userRequested"])

    def test_case_disabled_and_reserved_path_aliases_are_refused(self):
        self.install()
        source = self.fabric("stage/other.jar", ident="other")
        self.assert_code("path_conflict", lambda: self.install(source, "SAMPLE.jar"))
        for filename in ("../escape.jar", "mods/escape.jar", "x\\escape.jar", "CON.jar", "nul.txt", "a.jar.", "a.jar ", "a.jar:stream", "sample.jar.disabled"):
            with self.subTest(filename=filename):
                self.assert_code("unsafe_path", lambda: self.install(source, filename))

    def test_unsafe_archive_names_and_large_metadata_are_visible_and_refused_for_install(self):
        for name in ("../escape", "a/../../escape", "CON.txt", "x:ads"):
            source = self.archive("stage/unsafe.jar", {"fabric.mod.json": {"id": "sample", "version": "1"}, name: "bad"})
            self.assert_code("unsafe_archive", lambda: self.install(source))
        source = self.archive("game/mods/huge.jar", {"fabric.mod.json": " " * (1024 * 1024 + 1)})
        result = self.store.inventory()
        self.assertTrue(any(problem["code"] == "metadata_limit" for problem in result["problems"]))
        self.assertTrue(source.exists())

    def test_running_check_at_commit_refuses_after_staging(self):
        running = {"value": False}
        self.store = content.ContentStore(self.root, self.data, "fixture", is_running=lambda: running["value"])
        source = self.fabric()
        real_copy = content.shutil.copyfile
        def copied(*args, **kwargs):
            result = real_copy(*args, **kwargs)
            running["value"] = True
            return result
        with mock.patch.object(content.shutil, "copyfile", side_effect=copied):
            self.assert_code("game_running", lambda: self.install(source))
        self.assertEqual(self.store.inventory()["items"], [])

    def test_commit_failure_restores_removed_replaced_and_manifest_state(self):
        first = self.install(metadata={"provider": "modrinth", "projectId": "p"})
        old_bytes = (self.root / "mods/sample.jar").read_bytes()
        source = self.fabric("stage/next.jar", version="2")
        real_replace = content.os.replace
        failed = {"value": False}
        def fail_manifest(src, dst):
            if Path(dst).name == "manifest.json" and not failed["value"]:
                failed["value"] = True
                raise OSError("injected disk failure after file replacement")
            return real_replace(src, dst)
        with mock.patch.object(content.os, "replace", side_effect=fail_manifest):
            self.assert_code("commit_failed", lambda: self.install(source, replaces=first["items"][0]["id"]))
        self.assertEqual((self.root / "mods/sample.jar").read_bytes(), old_bytes)
        self.assertEqual(self.store.inventory()["revision"], first["revision"])
        self.assertTrue(list(self.root.rglob("before-*")), "recoverable backups must remain")

    def test_interrupted_commit_recovers_before_inventory_and_keeps_backups(self):
        first = self.install()
        old_bytes = (self.root / "mods/sample.jar").read_bytes()
        source = self.fabric("stage/next.jar", version="2")
        real_replace = content.os.replace
        def interrupt_manifest(src, dst):
            if Path(dst).name == "manifest.json":
                raise KeyboardInterrupt("simulated process interruption")
            return real_replace(src, dst)
        with mock.patch.object(content.os, "replace", side_effect=interrupt_manifest):
            with self.assertRaises(KeyboardInterrupt):
                self.install(source, replaces=first["items"][0]["id"])
        reopened = content.ContentStore(self.root, self.data, "fixture")
        recovered = reopened.inventory()
        self.assertEqual(recovered["revision"], first["revision"])
        self.assertEqual((self.root / "mods/sample.jar").read_bytes(), old_bytes)
        self.assertTrue(list(self.root.rglob("before-*")))

    def test_pending_recovery_waits_for_stopped_owned_root(self):
        first = self.install()
        source = self.fabric('stage/recovery.jar', version='2')
        real_replace = content.os.replace
        def interrupt_manifest(src, dst):
            if Path(dst).name == 'manifest.json':
                raise KeyboardInterrupt('interrupted fixture')
            return real_replace(src, dst)
        with mock.patch.object(content.os, 'replace', side_effect=interrupt_manifest), self.assertRaises(KeyboardInterrupt):
            self.install(source, replaces=first['items'][0]['id'])
        interrupted_bytes = (self.root / 'mods/sample.jar').read_bytes()
        for options, code in (({'is_running': lambda: True}, 'game_running'), ({'can_recover': lambda: False}, 'recovery_requires_private')):
            reopened = content.ContentStore(self.root, self.data, 'fixture', **options)
            self.assert_code(code, reopened.inventory)
            self.assertEqual((self.root / 'mods/sample.jar').read_bytes(), interrupted_bytes)
            self.assertTrue(self.store.journal_path.exists())
        self.assertEqual(content.ContentStore(self.root, self.data, 'fixture').inventory()['revision'], first['revision'])

    def test_recovery_refuses_to_overwrite_files_edited_after_interruption(self):
        first = self.install()
        source = self.fabric("stage/next.jar", version="2")
        real_replace = content.os.replace
        def interrupt_manifest(src, dst):
            if Path(dst).name == "manifest.json":
                raise KeyboardInterrupt()
            return real_replace(src, dst)
        with mock.patch.object(content.os, "replace", side_effect=interrupt_manifest):
            with self.assertRaises(KeyboardInterrupt):
                self.install(source, replaces=first["items"][0]["id"])
        changed = b"user changed during downtime"
        (self.root / "mods/sample.jar").write_bytes(changed)
        self.assert_code("recovery_conflict", self.store.inventory)
        self.assertEqual((self.root / "mods/sample.jar").read_bytes(), changed)

    def test_reparse_root_content_and_source_are_never_followed(self):
        outside = self.base / "outside"
        outside.mkdir()
        source = self.fabric("outside/sample.jar")
        expected = source.read_bytes()
        link = self.root / "mods"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("symlinks unavailable")
            completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
            if completed.returncode:
                self.skipTest("junction creation unavailable")
        self.assert_code("unsafe_path", lambda: self.install(source))
        self.assertEqual(source.read_bytes(), expected)
        self.assert_code("unsafe_path", lambda: content.canonical_root(link))
        self.assert_code("unsafe_path", lambda: content.canonical_root(link / ".." / "game"))

    def test_public_staged_inspection_returns_archive_identity_without_game_mutation(self):
        source = self.fabric(depends={"helper": ">=2"})
        item = content.inspect_file(source, "mods", "download.jar")
        self.assertEqual(item["modIds"], ["sample"])
        self.assertEqual(item["depends"], [{"id": "helper", "requirement": ">=2"}])
        self.assertEqual(item["path"], "mods/download.jar")
        self.assertFalse((self.root / "mods").exists())

    def test_failed_preparation_reports_content_error_and_preserves_live_files(self):
        first = self.install()
        source = self.fabric("stage/next.jar", version="2")
        with mock.patch.object(content.shutil, "copyfile", side_effect=OSError("disk full")):
            self.assert_code("io_error", lambda: self.install(source, replaces=first["items"][0]["id"]))
        self.assertEqual(self.store.inventory()["revision"], first["revision"])

    def test_fully_committed_interruption_finishes_verified_transaction(self):
        first = self.install()
        source = self.fabric("stage/next.jar", version="2")
        with mock.patch.object(content.ContentStore, "_finish_journal", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.install(source, replaces=first["items"][0]["id"])
        result = content.ContentStore(self.root, self.data, "fixture").inventory()
        self.assertEqual(result["items"][0]["version"], "2")
        self.assertFalse(self.store.journal_path.exists())

    def test_interrupted_removal_and_addition_restore_both_files(self):
        first = self.install()
        original = (self.root / "mods/sample.jar").read_bytes()
        source = self.fabric("stage/other.jar", ident="other")
        real_replace = content.os.replace
        def interrupt_manifest(src, dst):
            if Path(dst).name == "manifest.json":
                raise KeyboardInterrupt()
            return real_replace(src, dst)
        with mock.patch.object(content.os, "replace", side_effect=interrupt_manifest):
            with self.assertRaises(KeyboardInterrupt):
                self.store.transact([{"action": "remove", "id": first["items"][0]["id"]},
                    {"action": "install", "kind": "mods", "filename": "other.jar", "source": source, "metadata": {}}])
        recovered = self.store.inventory()
        self.assertEqual(recovered["revision"], first["revision"])
        self.assertEqual((self.root / "mods/sample.jar").read_bytes(), original)
        self.assertFalse((self.root / "mods/other.jar").exists())

    def test_unpacked_resource_toggle_remove_and_recovery_keep_contents(self):
        folder = self.root / "resourcepacks" / "loose"
        folder.mkdir(parents=True)
        (folder / "readme.txt").write_text("do not lose this")
        first = self.store.inventory()
        ident = first["items"][0]["id"]
        toggled = self.store.transact([{"action": "toggle", "id": ident, "enabled": False}])
        self.assertEqual(toggled["items"][0]["id"], ident)
        self.assertEqual((folder.with_name("loose.disabled") / "readme.txt").read_text(), "do not lose this")
        real_replace = content.os.replace
        def interrupt_manifest(src, dst):
            if Path(dst).name == "manifest.json":
                raise KeyboardInterrupt()
            return real_replace(src, dst)
        with mock.patch.object(content.os, "replace", side_effect=interrupt_manifest):
            with self.assertRaises(KeyboardInterrupt):
                self.store.transact([{"action": "remove", "id": ident}])
        self.assertEqual(self.store.inventory()["revision"], toggled["revision"])
        self.assertEqual((folder.with_name("loose.disabled") / "readme.txt").read_text(), "do not lose this")

    def test_recovery_does_not_mutate_content_while_game_is_running(self):
        first = self.install()
        source = self.fabric("stage/next.jar", version="2")
        real_replace = content.os.replace
        def interrupt_manifest(src, dst):
            if Path(dst).name == "manifest.json":
                raise KeyboardInterrupt()
            return real_replace(src, dst)
        with mock.patch.object(content.os, "replace", side_effect=interrupt_manifest):
            with self.assertRaises(KeyboardInterrupt):
                self.install(source, replaces=first["items"][0]["id"])
        running = {"value": True}
        reopened = content.ContentStore(self.root, self.data, "fixture", lambda: running["value"])
        current = (self.root / "mods/sample.jar").read_bytes()
        self.assert_code("game_running", reopened.inventory)
        self.assertEqual((self.root / "mods/sample.jar").read_bytes(), current)
        running["value"] = False
        self.assertEqual(reopened.inventory()["revision"], first["revision"])

    def test_archive_parent_case_aliases_are_refused(self):
        source = self.archive("stage/alias.jar", {"fabric.mod.json": {"id": "sample", "version": "1"}, "A/one": "1", "a/two": "2"})
        self.assert_code("unsafe_archive", lambda: self.install(source))

    def test_archive_count_bound_is_checked_before_metadata_parse(self):
        source = self.archive("stage/many.jar", {str(index): "" for index in range(10001)})
        self.assert_code("metadata_limit", lambda: content.inspect_file(source, "mods"))

    def test_manifest_change_is_part_of_revision_and_unsafe_journal_cannot_escape(self):
        first = self.install(metadata={"provider": "modrinth", "projectId": "p"})
        manifest = json.loads(self.store.manifest_path.read_text())
        manifest["items"][first["items"][0]["id"]]["metadata"]["userRequested"] = True
        self.store.manifest_path.write_text(json.dumps(manifest))
        self.assertNotEqual(self.store.inventory()["revision"], first["revision"])
        outside = self.base / "precious"
        outside.write_text("keep")
        self.store.private_dir.mkdir(exist_ok=True)
        self.store.journal_path.write_text(json.dumps({"schema": 1, "root": str(self.store.root), "transaction": "a" * 32,
            "beforeManifest": manifest, "afterManifest": manifest, "operations": [
                {"path": "../precious", "before": None, "after": None}]}))
        self.assert_code("unsafe_path", self.store.inventory)
        self.assertEqual(outside.read_text(), "keep")

    def test_source_change_during_staging_refuses_without_touching_destination(self):
        source = self.fabric()
        real_copy = content.shutil.copyfile
        def changed(src, dst, **options):
            result = real_copy(src, dst, **options)
            Path(src).write_bytes(Path(src).read_bytes() + b"change")
            return result
        with mock.patch.object(content.shutil, "copyfile", side_effect=changed):
            self.assert_code("hash_mismatch", lambda: self.install(source))
        self.assertEqual(self.store.inventory()["items"], [])

    def test_private_history_junction_is_refused(self):
        source = self.fabric()
        outside = self.base / "outside"
        outside.mkdir()
        link = self.store.private_dir
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("symlinks unavailable")
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
            if result.returncode:
                self.skipTest("junction creation unavailable")
        self.assert_code("unsafe_path", lambda: self.install(source))
        self.assertEqual(list(outside.iterdir()), [])

    def test_quilt_optional_and_unsupported_constraints_remain_explicit(self):
        source = self.archive("stage/quilt.jar", {"quilt.mod.json": {"quilt_loader": {
            "id": "quilt", "version": "1", "depends": [
                {"id": "optional", "versions": "*", "optional": True},
                {"id": "conditional", "versions": "*", "unless": "other"},
                [{"id": "left"}, {"id": "right"}]]}}})
        item = content.inspect_file(source)
        self.assertEqual([value["id"] for value in item["optionalDepends"]], ["optional"])
        self.assertNotIn("optional", [value["id"] for value in item["depends"]])
        self.assertTrue(all(value["unsupported"] for value in item["depends"]))
        self.assertEqual(len(item["depends"]), 2)

    def test_zip_header_cannot_lie_about_entry_count_to_bypass_preflight_bound(self):
        source = self.archive("stage/many.jar", {str(index): "" for index in range(10001)})
        raw = bytearray(source.read_bytes())
        end = raw.rfind(b"PK\x05\x06")
        struct.pack_into("<HH", raw, end + 8, 1, 1)
        source.write_bytes(raw)
        self.assert_code("metadata_limit", lambda: content.inspect_file(source))

    def test_metadata_embedded_width_limit_cannot_be_bypassed_with_small_modules(self):
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, "w") as archive:
            archive.writestr("fabric.mod.json", '{"id":"embedded","version":"1"}')
        entries = {"fabric.mod.json": {"id": "parent", "version": "1", "jars": [{"file": f"child/{index}.jar"} for index in range(256)]}}
        entries.update({f"child/{index}.jar": nested.getvalue() for index in range(256)})
        source = self.archive("stage/wide.jar", entries)
        self.assert_code("metadata_limit", lambda: content.inspect_file(source))

    def test_structural_metadata_types_are_unknown_rather_than_invalid_ui_values(self):
        source = self.archive("stage/bad.jar", {"fabric.mod.json": {"id": "sample", "name": {"bad": True}, "version": ["bad"]}})
        item = content.inspect_file(source)
        self.assertIsInstance(item["name"], str)
        self.assertIsNone(item["version"])
        self.assertTrue(item["unknown"])
        self.assertTrue(item["metadataProblems"])

    def test_two_stores_with_same_revision_cannot_both_commit(self):
        before = self.store.inventory()
        sources = [self.fabric(f"stage/{name}.jar", ident=name) for name in ("one", "two")]
        barrier, outcomes = threading.Barrier(2), []
        def commit(index):
            store = content.ContentStore(self.root, self.data, "fixture")
            barrier.wait()
            try:
                result = store.transact([{"action": "install", "kind": "mods", "filename": f"{index}.jar", "source": sources[index], "metadata": {}}], before["revision"])
                outcomes.append(("ok", result))
            except content.ContentError as exc:
                outcomes.append((exc.code, None))
        threads = [threading.Thread(target=commit, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(result[0] for result in outcomes), ["ok", "stale_revision"])
        self.assertEqual(len(self.store.inventory()["items"]), 1)

    def test_case_only_explicit_replacement_does_not_alias_live_destination(self):
        first = self.install()
        source = self.fabric("stage/new.jar", version="2")
        self.assert_code("path_conflict", lambda: self.install(source, "SAMPLE.jar", replaces=first["items"][0]["id"]))

    def test_lock_is_shared_by_repeat_instances_for_same_canonical_root(self):
        another = content.ContentStore(str(self.root / "."), self.data, "fixture")
        self.assertIs(self.store._lock, another._lock)


if __name__ == "__main__":
    unittest.main()
