"""Real disposable roots exercise file changes; native pickers are injected."""
import gzip
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from instance_content import ContentError, ContentStore

try:
    from instance_tools import read_tools, mutate_tools
except ImportError:
    def read_tools(*args, **kwargs):
        raise AssertionError('Instance tools have not been implemented')
    def mutate_tools(*args, **kwargs):
        raise AssertionError('Instance tools have not been implemented')


def nbt_text(value):
    raw = value.encode('utf-8')
    return struct.pack('>H', len(raw)) + raw


def nbt_tag(kind, name, value):
    if kind == 8:
        raw = nbt_text(value)
    elif kind == 1:
        raw = struct.pack('>b', value)
    elif kind == 3:
        raw = struct.pack('>i', value)
    elif kind == 4:
        raw = struct.pack('>q', value)
    elif kind == 10:
        raw = b''.join(nbt_tag(*entry) for entry in value) + b'\0'
    elif kind == 9:
        raw = bytes([10]) + struct.pack('>i', len(value))
        raw += b''.join(b''.join(nbt_tag(*entry) for entry in row) + b'\0' for row in value)
    else:
        raise AssertionError('Unexpected fixture tag')
    return bytes([kind]) + nbt_text(name) + raw


class InstanceToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'game'
        self.root.mkdir()
        self.data = self.base / 'data'
        self.data.mkdir()

    def world(self, folder='Original', title='My local world'):
        path = self.root / 'saves' / folder
        path.mkdir(parents=True)
        raw = nbt_tag(10, '', [(10, 'Data', [
            (8, 'LevelName', title), (4, 'LastPlayed', 1700000000000),
            (3, 'GameType', 1), (3, 'DataVersion', 3465),
            (10, 'Version', [(8, 'Name', '1.20.1'), (3, 'Id', 3465)]),
            (8, 'UnknownMetadata', 'preserve me'),
        ])])
        (path / 'level.dat').write_bytes(gzip.compress(raw))
        (path / 'region').mkdir()
        (path / 'region' / 'r.0.0.mca').write_bytes(b'region contents')
        return path

    def mutate(self, kind, action, **extra):
        params = {'id': extra.get('worldId')} if kind == 'datapacks' else {}
        baseline = read_tools(self.root, kind, params)
        return mutate_tools(self.root, self.data, 'fixture',
                            {'kind': kind, 'action': action, 'revision': baseline['revision'], **extra})

    def interrupted_rename(self):
        """Leave the real published journal as if the process stopped after moving."""
        world = self.world()
        original = (world / 'level.dat').read_bytes()
        destination = world.with_name('Unpublished rename')
        journal = self.root / '.launcher-tools' / 'journal.json'
        real_unlink = Path.unlink
        def stop_publication(path, *args, **kwargs):
            if path.name == 'journal.json' and destination.exists():
                raise OSError('fixture interrupted publication')
            return real_unlink(path, *args, **kwargs)
        def process_stopped(root, **kwargs):
            if journal.exists():
                raise SystemExit('fixture process stopped')
        item = read_tools(self.root, 'worlds', {})['items'][0]
        with patch.object(Path, 'unlink', new=stop_publication), \
                patch('instance_tools._recover', side_effect=process_stopped), self.assertRaises(SystemExit):
            self.mutate('worlds', 'rename', id=item['id'], name=destination.name)
        self.assertFalse(world.exists())
        self.assertTrue(destination.exists())
        self.assertTrue(journal.exists())
        return world, destination, original, journal

    def screenshot(self, name='local.png'):
        folder = self.root / 'screenshots'
        folder.mkdir(exist_ok=True)
        # A one-pixel PNG, suitable for the same signature gate as the server.
        raw = bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489')
        (folder / name).write_bytes(raw)
        return folder / name

    def test_restore_deleted_world_merges_existing_saves_directory(self):
        world = self.world()
        item = read_tools(self.root, 'worlds', {})['items'][0]
        deleted = self.mutate('worlds', 'delete', id=item['id'])
        self.assertFalse(world.exists())
        self.assertTrue((self.root / 'saves').exists())
        restored = self.mutate('advanced', 'restore', id=deleted['backupId'])
        self.assertTrue(restored['ok'])
        self.assertEqual((world / 'region/r.0.0.mca').read_bytes(), b'region contents')

    def pack(self, kind, name):
        folder = self.root / kind
        folder.mkdir(exist_ok=True)
        with zipfile.ZipFile(folder / name, 'w') as archive:
            archive.writestr('pack.mcmeta', json.dumps({'pack': {'pack_format': 15, 'description': name}}))
        return folder / name

    def test_unlaunched_installation_tools_are_empty_without_creating_root(self):
        self.root.rmdir()
        expected = {'worlds': ['import'], 'screenshots': ['import'], 'servers': ['save'],
                    'datapacks': [], 'resourcepacks': ['activate'], 'shaderpacks': [], 'advanced': ['restore']}
        for kind, capabilities in expected.items():
            with self.subTest(kind=kind):
                result = read_tools(self.root, kind, {})
                self.assertEqual(result['items'], [])
                self.assertEqual(result['capabilities'], capabilities)
                self.assertTrue(result.get('message'))
                self.assertEqual(result['revision'], read_tools(self.root, kind, {})['revision'])
                self.assertFalse(self.root.exists())

    def test_first_server_save_creates_private_root_and_consumes_empty_revision(self):
        self.root.rmdir()
        baseline = read_tools(self.root, 'servers', {})
        body = {'kind': 'servers', 'action': 'save', 'revision': baseline['revision'],
                'items': [{'name': 'Home server', 'address': 'localhost:25565'}]}
        result = mutate_tools(self.root, self.data, 'fixture', body)
        self.assertTrue((self.root / 'servers.dat').is_file())
        inventory = read_tools(self.root, 'servers', {})
        self.assertEqual([(i['name'], i['address']) for i in inventory['items']], [('Home server', 'localhost:25565')])
        self.assertEqual(result['revision'], inventory['revision'])
        self.assertNotEqual(baseline['revision'], result['revision'])
        with self.assertRaises(ContentError) as caught:
            mutate_tools(self.root, self.data, 'fixture', body)
        self.assertEqual(caught.exception.code, 'stale_revision')

    def test_first_world_import_creates_private_root_after_picker_selection(self):
        source = self.world()
        external = self.base / 'Imported world'
        os.replace(source, external)
        (self.root / 'saves').rmdir()
        self.root.rmdir()
        baseline = read_tools(self.root, 'worlds', {})
        result = mutate_tools(self.root, self.data, 'fixture',
            {'kind': 'worlds', 'action': 'import', 'revision': baseline['revision']},
            pick_path=lambda options: str(external))
        self.assertTrue(result['ok'])
        self.assertEqual((self.root / 'saves' / external.name / 'region' / 'r.0.0.mca').read_bytes(), b'region contents')
        self.assertEqual((external / 'region' / 'r.0.0.mca').read_bytes(), b'region contents')
        self.assertNotEqual(baseline['revision'], result['revision'])

    def test_cancelled_first_import_keeps_installation_directory_absent(self):
        self.root.rmdir()
        baseline = read_tools(self.root, 'worlds', {})
        result = mutate_tools(self.root, self.data, 'fixture',
            {'kind': 'worlds', 'action': 'import', 'revision': baseline['revision']},
            pick_path=lambda options: '')
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['revision'], baseline['revision'])
        self.assertFalse(self.root.exists())

    def test_first_tool_writes_remain_blocked_for_shared_or_running_installations(self):
        self.root.rmdir()
        for flags, code in (({'private': False}, 'private_root_required'), ({'running': True}, 'game_running')):
            with self.subTest(flags=flags):
                baseline = read_tools(self.root, 'servers', {}, **flags)
                self.assertEqual(baseline['capabilities'], [])
                with self.assertRaises(ContentError) as caught:
                    mutate_tools(self.root, self.data, 'fixture',
                        {'kind': 'servers', 'action': 'save', 'revision': baseline['revision'],
                         'items': [{'name': 'Server', 'address': 'localhost'}], **flags})
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(self.root.exists())

    def test_invalid_first_server_save_preserves_absent_installation_directory(self):
        self.root.rmdir()
        baseline = read_tools(self.root, 'servers', {})
        with self.assertRaises(ContentError) as caught:
            mutate_tools(self.root, self.data, 'fixture',
                {'kind': 'servers', 'action': 'save', 'revision': baseline['revision'],
                 'items': [{'name': 'Server', 'address': 'invalid host'}]})
        self.assertEqual(caught.exception.code, 'invalid_change')
        self.assertFalse(self.root.exists())

    def test_worlds_report_real_metadata_and_revision_changes_with_region_bytes(self):
        world = self.world()
        (self.root / 'saves' / 'Not a world').mkdir()
        result = read_tools(self.root, 'worlds', {})
        self.assertEqual(len(result['items']), 1)
        item = result['items'][0]
        self.assertEqual(item['name'], 'My local world')
        self.assertEqual(item['filename'], 'Original')
        self.assertEqual(item['version'], '1.20.1')
        self.assertEqual(item['lastPlayed'], 1700000000000)
        (world / 'region' / 'r.0.0.mca').write_bytes(b'edited region')
        self.assertNotEqual(result['revision'], read_tools(self.root, 'worlds', {})['revision'])

    def test_bounded_malformed_world_metadata_does_not_invent_a_version(self):
        path = self.world()
        (path / 'level.dat').write_bytes(gzip.compress(b'x' * (1024 * 1024 + 1)))
        item = read_tools(self.root, 'worlds', {})['items'][0]
        self.assertEqual(item['name'], 'Original')
        self.assertIsNone(item['version'])
        self.assertTrue(item['metadataProblems'])

    def test_world_copy_keeps_original_and_rename_has_external_recovery_archive(self):
        original = self.world()
        item = read_tools(self.root, 'worlds', {})['items'][0]
        self.mutate('worlds', 'copy', id=item['id'], name='Independent')
        self.assertEqual((original / 'region' / 'r.0.0.mca').read_bytes(), b'region contents')
        self.assertEqual((self.root / 'saves' / 'Independent' / 'region' / 'r.0.0.mca').read_bytes(), b'region contents')
        result = self.mutate('worlds', 'rename', id=item['id'], name='Renamed world')
        self.assertFalse(original.exists())
        renamed = self.root / 'saves' / 'Renamed world'
        self.assertTrue(renamed.exists())
        self.assertIn(b'Renamed world', gzip.decompress((renamed / 'level.dat').read_bytes()))
        backup = Path(result['backupPath'])
        self.assertTrue(backup.is_relative_to(self.data / 'workspace-backups' / 'fixture'))
        with zipfile.ZipFile(backup) as archive:
            self.assertEqual(archive.read('saves/Original/region/r.0.0.mca'), b'region contents')

    def test_world_deletion_is_recoverable_and_restore_refuses_existing_paths(self):
        original = self.world()
        ident = read_tools(self.root, 'worlds', {})['items'][0]['id']
        removed = self.mutate('worlds', 'delete', id=ident)
        self.assertFalse(original.exists())
        restored = self.mutate('advanced', 'restore', id=removed['backupId'])
        self.assertTrue(restored['ok'])
        self.assertEqual((original / 'region' / 'r.0.0.mca').read_bytes(), b'region contents')
        with self.assertRaises(ContentError):
            self.mutate('advanced', 'restore', id=removed['backupId'])

    def test_backup_contains_world_mod_settings_and_unique_history(self):
        self.world()
        (self.root / 'options.txt').write_bytes(b'music:0.2\n')
        (self.root / 'mods').mkdir()
        (self.root / 'mods' / 'user.jar').write_bytes(b'user file')
        a = self.mutate('advanced', 'backup')
        b = self.mutate('advanced', 'backup')
        self.assertNotEqual(a['backupPath'], b['backupPath'])
        self.assertFalse(Path(a['backupPath']).is_relative_to(self.root))
        with zipfile.ZipFile(a['backupPath']) as archive:
            self.assertEqual(archive.read('options.txt'), b'music:0.2\n')
            self.assertEqual(archive.read('mods/user.jar'), b'user file')
            self.assertIn('saves/Original/level.dat', archive.namelist())

    def test_stale_revision_never_deletes_locally_changed_world(self):
        original = self.world()
        baseline = read_tools(self.root, 'worlds', {})
        (original / 'level.dat').write_bytes(b'changed by another application')
        with self.assertRaises(ContentError) as caught:
            mutate_tools(self.root, self.data, 'fixture', {'kind': 'worlds', 'action': 'delete',
                'id': baseline['items'][0]['id'], 'revision': baseline['revision']})
        self.assertEqual(caught.exception.code, 'stale_revision')
        self.assertTrue(original.exists())

    def test_screenshot_inventory_uses_real_images_and_safe_instance_urls(self):
        self.screenshot('A screenshot.png')
        (self.root / 'screenshots' / 'fake.png').write_text('not an image')
        (self.root / 'screenshots' / 'text.txt').write_text('text')
        result = read_tools(self.root, 'screenshots', {'instanceId': 'stable profile'})
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['url'], '/__instances/stable%20profile/image?name=A%20screenshot.png')

    def test_screenshot_rename_preserves_extension_and_delete_keeps_bytes(self):
        source = self.screenshot()
        original = source.read_bytes()
        ident = read_tools(self.root, 'screenshots', {})['items'][0]['id']
        self.mutate('screenshots', 'rename', id=ident, name='New title')
        renamed = self.root / 'screenshots' / 'New title.png'
        self.assertEqual(renamed.read_bytes(), original)
        ident = read_tools(self.root, 'screenshots', {})['items'][0]['id']
        result = self.mutate('screenshots', 'delete', id=ident)
        with zipfile.ZipFile(result['backupPath']) as archive:
            self.assertEqual(archive.read('screenshots/New title.png'), original)

    def test_export_uses_native_picker_and_cancellation_has_no_file_effect(self):
        self.world()
        before = read_tools(self.root, 'worlds', {})
        body = {'kind': 'worlds', 'action': 'export', 'id': before['items'][0]['id'], 'revision': before['revision']}
        result = mutate_tools(self.root, self.data, 'fixture', body, pick_path=lambda options: '')
        self.assertTrue(result['cancelled'])
        self.assertEqual(list(self.base.glob('*.zip')), [])
        exported = self.base / 'picked.zip'
        mutate_tools(self.root, self.data, 'fixture', body, pick_path=lambda options: str(exported))
        with zipfile.ZipFile(exported) as archive:
            self.assertEqual(archive.read('saves/Original/region/r.0.0.mca'), b'region contents')
        other = self.base / 'unpicked.zip'
        with self.assertRaises(ContentError):
            mutate_tools(self.root, self.data, 'fixture', {**body, 'outputPath': str(other)}, pick_path=lambda options: str(exported))
        self.assertFalse(other.exists())

    def test_import_world_copies_selected_directory_and_rejects_archive_traversal(self):
        source_root = self.base / 'outside'
        source_root.mkdir()
        source = source_root / 'Imported'
        source.mkdir()
        (source / 'level.dat').write_bytes(gzip.compress(nbt_tag(10, '', [(10, 'Data', [(8, 'LevelName', 'Imported')])])))
        before = read_tools(self.root, 'worlds', {})
        body = {'kind': 'worlds', 'action': 'import', 'revision': before['revision']}
        mutate_tools(self.root, self.data, 'fixture', body, pick_path=lambda options: str(source))
        self.assertTrue((source / 'level.dat').exists())
        self.assertTrue((self.root / 'saves' / 'Imported' / 'level.dat').exists())
        bad = self.base / 'bad.zip'
        with zipfile.ZipFile(bad, 'w') as archive:
            archive.writestr('../escaped', 'bad')
            archive.writestr('level.dat', 'world')
        body['revision'] = read_tools(self.root, 'worlds', {})['revision']
        with self.assertRaises(ContentError):
            mutate_tools(self.root, self.data, 'fixture', body, pick_path=lambda options: str(bad))
        self.assertFalse((self.base / 'escaped').exists())

    def test_servers_edit_preserves_unknown_tags_order_and_acceptance(self):
        raw = nbt_tag(10, '', [(8, 'rootUnknown', 'root original'), (9, 'servers', [
            [(8, 'name', 'One'), (8, 'ip', 'one.example:25565'), (1, 'acceptTextures', 1), (8, 'icon', 'original icon')],
            [(8, 'name', 'Two'), (8, 'ip', '[::1]:25565'), (1, 'acceptTextures', 0), (4, 'unknownLong', 123456789)],
        ])])
        (self.root / 'servers.dat').write_bytes(gzip.compress(raw))
        before = read_tools(self.root, 'servers', {})
        self.assertEqual([i['resourcePolicy'] for i in before['items']], ['enabled', 'disabled'])
        second, first = before['items'][1], before['items'][0]
        first = {**first, 'name': 'Edited One', 'address': 'edited.example'}
        self.mutate('servers', 'save', items=[second, first])
        after = read_tools(self.root, 'servers', {})
        self.assertEqual([i['name'] for i in after['items']], ['Two', 'Edited One'])
        self.assertEqual([i['acceptTextures'] for i in after['items']], [False, True])
        contents = gzip.decompress((self.root / 'servers.dat').read_bytes())
        self.assertIn(b'original icon', contents)
        self.assertIn(b'root original', contents)
        self.assertIn(struct.pack('>q', 123456789), contents)

    def test_servers_reject_invalid_address_without_overwriting_data(self):
        (self.root / 'servers.dat').write_bytes(gzip.compress(nbt_tag(10, '', [(9, 'servers', [])])))
        original = (self.root / 'servers.dat').read_bytes()
        with self.assertRaises(ContentError):
            self.mutate('servers', 'save', items=[{'name': 'Bad', 'address': 'bad host'}])
        self.assertEqual((self.root / 'servers.dat').read_bytes(), original)

    def test_servers_reject_invalid_ports_and_brackets_before_saving(self):
        for address in ('example.com:70000', 'example.com:0', '[::1', 'bad:host:123', 'example.com:notaport'):
            with self.subTest(address=address), self.assertRaises(ContentError):
                self.mutate('servers', 'save', items=[{'name': 'Server', 'address': address}])
        self.assertFalse((self.root / 'servers.dat').exists())

    def test_invalid_selected_id_does_not_export_the_entire_world_folder(self):
        self.world()
        baseline = read_tools(self.root, 'worlds', {})
        target = self.base / 'should-not-exist.zip'
        with self.assertRaises(ContentError):
            mutate_tools(self.root, self.data, 'fixture', {'kind': 'worlds', 'action': 'export',
                'id': 'missing', 'revision': baseline['revision']}, pick_path=lambda options: str(target))
        self.assertFalse(target.exists())

    def test_modification_during_backup_prevents_world_deletion(self):
        world = self.world()
        baseline = read_tools(self.root, 'worlds', {})
        real_write = zipfile.ZipFile.write
        changed = False
        def change_after_archive(archive, filename, *args, **kwargs):
            nonlocal changed
            result = real_write(archive, filename, *args, **kwargs)
            if Path(filename).name == 'r.0.0.mca' and not changed:
                changed = True
                (world / 'region' / 'r.0.0.mca').write_bytes(b'external update during backup')
            return result
        with patch.object(zipfile.ZipFile, 'write', new=change_after_archive), self.assertRaises(ContentError):
            mutate_tools(self.root, self.data, 'fixture', {'kind': 'worlds', 'action': 'delete',
                'id': baseline['items'][0]['id'], 'revision': baseline['revision']})
        self.assertEqual((world / 'region' / 'r.0.0.mca').read_bytes(), b'external update during backup')

    def test_interrupted_world_rename_is_recovered_before_inventory(self):
        world, destination, original, journal = self.interrupted_rename()
        result = read_tools(self.root, 'worlds', {})
        self.assertEqual([i['filename'] for i in result['items']], ['Original'])
        self.assertEqual((world / 'level.dat').read_bytes(), original)
        self.assertFalse(destination.exists())
        self.assertFalse(journal.exists())

    def test_interrupted_recovery_preserves_newer_world_metadata(self):
        world, destination, _, journal = self.interrupted_rename()
        newer = b'newer metadata written by Minecraft'
        (destination / 'level.dat').write_bytes(newer)
        with self.assertRaises(ContentError) as caught:
            read_tools(self.root, 'worlds', {})
        self.assertEqual(caught.exception.code, 'recovery_conflict')
        self.assertEqual((destination / 'level.dat').read_bytes(), newer)
        self.assertFalse(world.exists())
        self.assertTrue(journal.exists())

    def test_interrupted_recovery_preserves_newer_region_contents(self):
        world, destination, _, journal = self.interrupted_rename()
        metadata = (destination / 'level.dat').read_bytes()
        (destination / 'region' / 'r.0.0.mca').write_bytes(b'newer chunks')
        with self.assertRaises(ContentError) as caught:
            read_tools(self.root, 'worlds', {})
        self.assertEqual(caught.exception.code, 'recovery_conflict')
        self.assertEqual((destination / 'region' / 'r.0.0.mca').read_bytes(), b'newer chunks')
        self.assertEqual((destination / 'level.dat').read_bytes(), metadata)
        self.assertFalse(world.exists())
        self.assertTrue(journal.exists())

    def test_invalid_recovery_metadata_is_rejected_before_moving_world(self):
        world, destination, _, journal = self.interrupted_rename()
        metadata = (destination / 'level.dat').read_bytes()
        data = json.loads(journal.read_text())
        data['originalLevel'] = 'invalid base64!'
        journal.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(ContentError):
            read_tools(self.root, 'worlds', {})
        self.assertTrue(destination.exists())
        self.assertFalse(world.exists())
        self.assertEqual((destination / 'level.dat').read_bytes(), metadata)
        self.assertTrue(journal.exists())

    def test_legacy_recovery_journal_preserves_unverified_world(self):
        import base64
        world = self.world()
        original = (world / 'level.dat').read_bytes()
        destination = world.with_name('Legacy rename')
        os.replace(world, destination)
        newer = b'newer world bytes'
        (destination / 'level.dat').write_bytes(newer)
        journal = self.root / '.launcher-tools' / 'journal.json'
        journal.parent.mkdir()
        journal.write_text(json.dumps({'schema': 1, 'root': os.path.normcase(str(self.root)),
            'action': 'rename', 'source': 'saves/Original', 'destination': 'saves/Legacy rename',
            'originalLevel': base64.b64encode(original).decode('ascii')}), encoding='utf-8')
        with self.assertRaises(ContentError) as caught:
            read_tools(self.root, 'worlds', {})
        self.assertEqual(caught.exception.code, 'recovery_conflict')
        self.assertTrue(destination.exists())
        self.assertFalse(world.exists())
        self.assertEqual((destination / 'level.dat').read_bytes(), newer)
        self.assertTrue(journal.exists())

    def test_native_reveal_reports_failed_host_open_without_claiming_success(self):
        self.world()
        baseline = read_tools(self.root, 'worlds', {})
        with self.assertRaises(ContentError):
            mutate_tools(self.root, self.data, 'fixture', {'kind': 'worlds', 'action': 'reveal',
                'id': baseline['items'][0]['id'], 'revision': baseline['revision']}, open_path=lambda path: {'error': 'failed'})

    def test_running_read_does_not_mutate_pending_recovery_files(self):
        world = self.world()
        destination = world.with_name('Interrupted')
        os.replace(world, destination)
        folder = self.root / '.launcher-tools'
        folder.mkdir()
        (folder / 'journal.json').write_text(json.dumps({'schema': 1, 'root': os.path.normcase(str(self.root)),
            'action': 'rename', 'source': 'saves/Original', 'destination': 'saves/Interrupted'}), encoding='utf-8')
        with self.assertRaises(ContentError):
            read_tools(self.root, 'worlds', {}, running=True)
        self.assertTrue(destination.exists())
        self.assertFalse(world.exists())

    def test_pending_journal_cannot_move_unrelated_installation_files(self):
        (self.root / 'options.txt').write_bytes(b'keep settings')
        folder = self.root / '.launcher-tools'
        folder.mkdir()
        (folder / 'journal.json').write_text(json.dumps({'schema': 1, 'root': os.path.normcase(str(self.root)),
            'action': 'rename', 'source': 'malicious destination', 'destination': 'options.txt'}), encoding='utf-8')
        with self.assertRaises(ContentError):
            read_tools(self.root, 'worlds', {})
        self.assertEqual((self.root / 'options.txt').read_bytes(), b'keep settings')

    def test_archive_import_rejects_case_aliased_directories(self):
        source = self.base / 'alias.zip'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('World/level.dat', b'world')
            archive.writestr('world/region/r.0.0.mca', b'region')
        baseline = read_tools(self.root, 'worlds', {})
        with self.assertRaises(ContentError):
            mutate_tools(self.root, self.data, 'fixture', {'kind': 'worlds', 'action': 'import',
                'revision': baseline['revision']}, pick_path=lambda options: str(source))
        self.assertFalse((self.root / 'saves').exists())

    def test_optifine_shader_selection_preserves_unrelated_properties(self):
        self.pack('shaderpacks', 'Shader.zip')
        mods = self.root / 'mods'
        mods.mkdir()
        with zipfile.ZipFile(mods / 'OptiFine.jar', 'w') as archive:
            archive.writestr('net/optifine/Config.class', b'class data, never executed')
        config = self.root / 'optionsshaders.txt'
        config.write_bytes(b'shaderPack=OFF\nrenderResMul=0.7\n')
        baseline = read_tools(self.root, 'shaderpacks', {})
        self.assertEqual(baseline['renderer'], 'optifine')
        self.mutate('shaderpacks', 'activate', id=baseline['items'][0]['id'])
        self.assertIn(b'renderResMul=0.7\n', config.read_bytes())
        self.assertIn(b'shaderPack=Shader.zip\n', config.read_bytes())

    def test_resource_activation_uses_inventory_ids_and_preserves_vanilla_options(self):
        self.pack('resourcepacks', 'First.zip')
        self.pack('resourcepacks', 'Second.zip')
        options = self.root / 'options.txt'
        options.write_text('music:0.3\nresourcePacks:["vanilla","file/First.zip"]\ncustomSetting:unchanged\n', encoding='utf-8')
        inventory = ContentStore(self.root, self.data, 'fixture').inventory()['items']
        ids = {i['filename']: i['id'] for i in inventory}
        before = read_tools(self.root, 'resourcepacks', {})
        self.assertEqual(before['activeIds'], [ids['First.zip']])
        self.mutate('resourcepacks', 'activate', ids=[ids['Second.zip'], ids['First.zip']])
        text = options.read_text(encoding='utf-8')
        self.assertIn('music:0.3\n', text)
        self.assertIn('customSetting:unchanged\n', text)
        line = next(l for l in text.splitlines() if l.startswith('resourcePacks:'))
        self.assertEqual(json.loads(line.split(':', 1)[1]), ['vanilla', 'file/First.zip', 'file/Second.zip'])
        self.assertEqual(read_tools(self.root, 'resourcepacks', {})['activeIds'], [ids['Second.zip'], ids['First.zip']])

    def test_resource_activation_refuses_disabled_missing_and_stale_selection(self):
        self.pack('resourcepacks', 'Off.zip.disabled')
        disabled = read_tools(self.root, 'resourcepacks', {})['items'][0]['id']
        for ids in ([disabled], ['missing'], [disabled, disabled]):
            with self.subTest(ids=ids), self.assertRaises(ContentError):
                self.mutate('resourcepacks', 'activate', ids=ids)
        self.assertFalse((self.root / 'options.txt').exists())

    def test_shader_activation_requires_actual_renderer_and_preserves_config(self):
        self.pack('shaderpacks', 'Shader.zip')
        before = read_tools(self.root, 'shaderpacks', {})
        self.assertNotIn('activate', before['capabilities'])
        self.assertTrue(before['message'])
        mods = self.root / 'mods'
        mods.mkdir()
        with zipfile.ZipFile(mods / 'renderer.jar', 'w') as archive:
            archive.writestr('fabric.mod.json', '{"id":"iris","version":"1.7.0","name":"Iris"}')
        config = self.root / 'config'
        config.mkdir()
        iris = config / 'iris.properties'
        iris.write_text('# User settings\nenableShaders=false\nshaderPack=OFF\nmaxShadowRenderDistance=12\n', encoding='utf-8')
        before = read_tools(self.root, 'shaderpacks', {})
        self.assertIn('activate', before['capabilities'])
        self.mutate('shaderpacks', 'activate', id=before['items'][0]['id'])
        text = iris.read_text(encoding='utf-8')
        self.assertIn('shaderPack=Shader.zip', text)
        self.assertIn('enableShaders=true', text)
        self.assertIn('maxShadowRenderDistance=12', text)
        self.assertEqual(read_tools(self.root, 'shaderpacks', {})['activeId'], before['items'][0]['id'])

    def test_datapacks_are_scoped_to_selected_world(self):
        world = self.world()
        self.world('Other', 'Other world')
        (world / 'datapacks').mkdir()
        with zipfile.ZipFile(world / 'datapacks' / 'local.zip', 'w') as archive:
            archive.writestr('pack.mcmeta', '{"pack":{"pack_format":15,"description":"Local world pack"}}')
        ident = next(i['id'] for i in read_tools(self.root, 'worlds', {})['items'] if i['filename'] == 'Original')
        result = read_tools(self.root, 'datapacks', {'id': ident})
        self.assertEqual([i['name'] for i in result['items']], ['Local world pack'])
        self.assertTrue(result['items'][0]['path'].startswith('saves/Original/datapacks/'))
        with self.assertRaises(ContentError):
            read_tools(self.root, 'datapacks', {})

    def test_world_datapack_import_requires_actual_pack_metadata(self):
        world = self.world()
        ident = read_tools(self.root, 'worlds', {})['items'][0]['id']
        archive_path = self.base / 'not-a-datapack.zip'
        with zipfile.ZipFile(archive_path, 'w') as archive:
            archive.writestr('ordinary.txt', 'unrelated file')
        baseline = read_tools(self.root, 'datapacks', {'id': ident})
        with self.assertRaises(ContentError):
            mutate_tools(self.root, self.data, 'fixture', {'kind': 'datapacks', 'action': 'import',
                'worldId': ident, 'revision': baseline['revision']}, pick_path=lambda options: str(archive_path))
        self.assertFalse((world / 'datapacks' / 'not-a-datapack.zip').exists())

    def test_invalid_server_and_shader_ids_have_validated_errors(self):
        self.pack('shaderpacks', 'Shader.zip')
        mods = self.root / 'mods'
        mods.mkdir()
        with zipfile.ZipFile(mods / 'Iris.jar', 'w') as archive:
            archive.writestr('fabric.mod.json', '{"id":"iris","version":"1.7.0"}')
        for bad_id in ([], {}):
            with self.subTest(kind='servers', bad_id=bad_id), self.assertRaises(ContentError):
                self.mutate('servers', 'save', items=[{'name': 'Name', 'address': 'localhost', 'id': bad_id}])
            with self.subTest(kind='shaderpacks', bad_id=bad_id), self.assertRaises(ContentError):
                self.mutate('shaderpacks', 'activate', id=bad_id)

    def test_running_and_shared_capabilities_and_explicit_flags_refuse_mutation(self):
        self.world()
        result = read_tools(self.root, 'worlds', {}, private=False, running=True)
        self.assertIn('reveal', result['capabilities'])
        self.assertNotIn('delete', result['capabilities'])
        self.assertNotIn('backup', result['capabilities'])
        for flags in ({'running': True}, {'private': False}):
            with self.subTest(flags=flags), self.assertRaises(ContentError):
                mutate_tools(self.root, self.data, 'fixture', {'kind': 'worlds', 'action': 'delete',
                    'id': result['items'][0]['id'], 'revision': result['revision'], **flags})

    def test_linked_content_cannot_escape_export_or_deletion(self):
        original = self.world()
        outside = self.base / 'outside.txt'
        outside.write_bytes(b'keep outside')
        link = original / 'linked.txt'
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest('Symlink creation is unavailable')
        with self.assertRaises(ContentError):
            read_tools(self.root, 'worlds', {})
        self.assertEqual(outside.read_bytes(), b'keep outside')

    def test_invalid_names_and_missing_revision_never_change_original(self):
        original = self.world()
        before = read_tools(self.root, 'worlds', {})
        for name in ('../escaped', 'CON', 'trailing.', 'a:b', 'Original'):
            with self.subTest(name=name), self.assertRaises(ContentError):
                self.mutate('worlds', 'rename', id=before['items'][0]['id'], name=name)
        with self.assertRaises(ContentError):
            mutate_tools(self.root, self.data, 'fixture', {'kind': 'worlds', 'action': 'delete', 'id': before['items'][0]['id']})
        self.assertTrue(original.exists())

    def test_disk_failure_during_rename_rolls_back_world_and_retains_backup(self):
        original = self.world()
        before = read_tools(self.root, 'worlds', {})
        real_replace = os.replace
        def fail_destination(source, destination):
            if Path(destination).name == 'New title' and Path(source) == original:
                raise OSError('fixture disk failure')
            return real_replace(source, destination)
        with patch('os.replace', side_effect=fail_destination), self.assertRaises(OSError):
            self.mutate('worlds', 'rename', id=before['items'][0]['id'], name='New title')
        self.assertEqual((original / 'region' / 'r.0.0.mca').read_bytes(), b'region contents')
        self.assertIn(b'My local world', gzip.decompress((original / 'level.dat').read_bytes()))
        self.assertTrue(list((self.data / 'workspace-backups' / 'fixture').glob('*.zip')))


if __name__ == '__main__':
    unittest.main()
