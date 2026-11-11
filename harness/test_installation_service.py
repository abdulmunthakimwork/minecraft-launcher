import os
import io
import json
import hashlib
import threading
import time
import zipfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import installation_service

from installation_service import InstallationService, ServiceError
from instance_content import ContentStore
from test_content_resolver import FakeProvider, version, dependency


def jar(mod_id, depends=None):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('fabric.mod.json', json.dumps({'id': mod_id, 'version': '1.0.0', 'name': mod_id, 'depends': depends or {}}))
    return data.getvalue()


class DownloadProvider(FakeProvider):
    def __init__(self, bodies, relations=None):
        records = []
        for ident, raw in bodies.items():
            record = version(ident, dependencies=(relations or {}).get(ident))
            record['files'][0].update(size=len(raw), hashes={'sha512': hashlib.sha512(raw).hexdigest()})
            records.append(record)
        super().__init__(records)
        self.bodies = bodies
        self.downloads = []
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()

    def download(self, file, destination):
        self.entered.set()
        if not self.release.wait(4):
            raise RuntimeError('Fixture timeout')
        self.downloads.append(file['versionId'])
        Path(destination).write_bytes(self.bodies[file['projectId']])
        return destination


class InstallationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.game = self.base / 'game'
        self.game.mkdir()
        self.state = self.base / 'state'
        self.state.mkdir()
        self.data = self.base / 'data'
        self.data.mkdir()
        self.private = self.game / 'instances' / 'private'
        self.private.mkdir(parents=True)
        self.profiles = {
            'private': {'name': 'Same name', 'type': 'custom', 'gameDir': str(self.private), 'lastVersionId': '1.20.1'},
            'default': {'name': 'Same name', 'type': 'latest-release', 'lastVersionId': 'latest-release'},
        }
        self.running = False
        self.saved = []
        self.service = InstallationService(
            str(self.game), str(self.state), str(self.data),
            profiles=lambda: self.profiles,
            save_profile=self.save,
            context=lambda p: {'minecraft': '1.20.1', 'loader': 'fabric', 'loaderVersion': '0.16.5', 'versionId': p['lastVersionId']},
            versions=lambda: [{'id': '1.20.1', 'type': 'release'}],
            running=lambda: self.running,
        )

    def save(self, ident, profile):
        self.saved.append((ident, dict(profile)))
        self.profiles[ident] = dict(profile)
        return self.profiles

    def test_same_names_resolve_by_canonical_profile_id(self):
        out = self.service.get('private')
        self.assertEqual(out['id'], 'private')
        self.assertTrue(out['ownership']['private'])
        self.assertEqual(out['profile']['gameDir'], str(self.private))

    def missing_profile_root(self):
        missing = self.game / 'new-installations' / 'not-launched'
        self.profiles['private']['gameDir'] = str(missing)
        return missing

    def test_unlaunched_installation_inventory_version_change_and_copy(self):
        missing = self.missing_profile_root()
        baseline = self.service.get('private', 'content')
        self.assertEqual(baseline['items'], [])
        self.assertFalse(missing.parent.exists())
        result = self.service.post('private', 'general', {'patch': {'minecraft': '1.21.1', 'loader': 'vanilla', 'loaderVersion': ''}})
        self.assertEqual(result['profile']['lastVersionId'], '1.21.1')
        copied = self.service.post('private', 'copy', {})
        self.assertTrue(Path(copied['profile']['gameDir']).is_dir())
        self.assertEqual(self.service.get(copied['instanceId'], 'content')['items'], [])
        self.assertFalse(missing.parent.exists())

    def test_first_import_creates_private_root_but_shared_import_is_blocked(self):
        import base64
        missing = self.missing_profile_root()
        self.provider({})
        raw = jar('local')
        body = {'kind': 'mods', 'files': [{'name': 'local.jar', 'data': base64.b64encode(raw).decode()}]}
        self.profiles['other'] = dict(self.profiles['private'])
        with self.assertRaises(ServiceError) as caught:
            self.service.post('private', 'import', body)
        self.assertEqual(caught.exception.code, 'shared_directory')
        self.assertFalse(missing.exists())
        del self.profiles['other']
        result = self.service.post('private', 'import', body)
        self.assertEqual(len(result['items']), 1)
        self.assertEqual((missing / 'mods/local.jar').read_bytes(), raw)

    def test_content_and_tools_refresh_report_current_global_running_and_ownership(self):
        self.service.launch_status = lambda: {'running': self.running, 'configurationId': 'other'}
        self.running = True
        self.profiles['other'] = dict(self.profiles['private'])
        for resource, params in [('content', {})] + [('tools', {'kind': kind}) for kind in ('worlds', 'screenshots', 'servers', 'notes', 'logs', 'advanced')]:
            with self.subTest(resource=resource, params=params):
                result = self.service.get('private', resource, params)
                self.assertTrue(result['gameRunning'])
                self.assertFalse(result['ownership']['private'])
                if params.get('kind') == 'logs':
                    self.assertFalse(result['running'], 'Stop must remain bound to this installation')
        self.running = False
        del self.profiles['other']
        for resource, params in [('content', {}), ('tools', {'kind': 'logs'})]:
            result = self.service.get('private', resource, params)
            self.assertFalse(result['gameRunning'])
            self.assertTrue(result['ownership']['private'])

    def test_unknown_profile_does_not_fall_back(self):
        with self.assertRaises(ServiceError):
            self.service.get('absent')

    def test_default_root_is_read_only_for_content_changes(self):
        out = self.service.get('default')
        self.assertFalse(out['ownership']['private'])
        with self.assertRaises(ServiceError):
            self.service.post('default', 'remove', {'ids': ['missing']})

    def test_two_profiles_in_same_root_cannot_claim_ownership(self):
        self.profiles['other'] = dict(self.profiles['private'])
        out = self.service.get('private')
        self.assertFalse(out['ownership']['private'])
        self.assertIn('other', out['ownership']['sharedWith'])

    def test_running_game_blocks_profile_and_content_mutations(self):
        self.running = True
        with self.assertRaises(ServiceError):
            self.service.post('private', 'general', {'patch': {'name': 'New'}})
        with self.assertRaises(ServiceError):
            self.service.post('private', 'remove', {'ids': []})
        self.assertFalse(self.saved)

    def test_general_save_preserves_other_profile_fields(self):
        self.profiles['private']['extra'] = {'keep': True}
        out = self.service.post('private', 'general', {'patch': {'name': 'Renamed', 'resolution': {'width': 1280, 'height': 720}}})
        self.assertTrue(out['ok'])
        self.assertEqual(self.profiles['private']['extra'], {'keep': True})
        self.assertEqual(self.profiles['default']['name'], 'Same name')

    def test_general_rejects_arbitrary_or_malformed_fields(self):
        for patch in ({'unknown': 'x'}, {'name': ''}, {'resolution': {'width': -1, 'height': 720}}, {'name': ['x']}):
            with self.subTest(patch=patch), self.assertRaises(ServiceError):
                self.service.post('private', 'general', {'patch': patch})
        self.assertFalse(self.saved)

    def test_independent_copy_preserves_source_and_pins_default(self):
        (self.game / 'mods').mkdir()
        (self.game / 'mods' / 'unknown.jar').write_bytes(b'original')
        (self.game / 'saves' / 'world').mkdir(parents=True)
        (self.game / 'saves' / 'world' / 'level.dat').write_bytes(b'world')
        out = self.service.post('default', 'copy', {'name': 'Private copy'})
        copied = self.profiles[out['instanceId']]
        self.assertEqual(copied['lastVersionId'], '1.20.1')
        self.assertEqual(copied['type'], 'custom')
        self.assertNotEqual(copied['gameDir'], str(self.game))
        self.assertEqual((Path(copied['gameDir']) / 'saves' / 'world' / 'level.dat').read_bytes(), b'world')
        self.assertEqual((self.game / 'mods' / 'unknown.jar').read_bytes(), b'original')
        self.assertNotIn('gameDir', self.profiles['default'])

    def test_copy_uses_unique_names_and_destinations(self):
        first = self.service.post('private', 'copy', {'name': 'Copy'})
        second = self.service.post('private', 'copy', {'name': 'Copy'})
        a, b = self.profiles[first['instanceId']], self.profiles[second['instanceId']]
        self.assertNotEqual(a['gameDir'], b['gameDir'])
        self.assertNotEqual(a['name'], b['name'])

    def test_notes_save_is_per_instance_and_rejects_large_input(self):
        revision = self.service.get('private', 'tools', {'kind': 'notes'}).get('revision')
        self.service.post('private', 'tools', {'kind': 'notes', 'action': 'save', 'text': 'My pack notes', 'revision': revision})
        self.assertEqual(self.service.get('private', 'tools', {'kind': 'notes'})['text'], 'My pack notes')
        self.assertEqual(self.service.get('default', 'tools', {'kind': 'notes'})['text'], '')
        with self.assertRaises(ServiceError):
            self.service.post('private', 'tools', {'kind': 'notes', 'action': 'save', 'text': 'x' * (1024 * 1024 + 1)})

    def test_notes_revisions_prevent_a_stale_tab_overwriting_saved_text(self):
        first = self.service.get('private', 'tools', {'kind': 'notes'})
        stale = self.service.get('private', 'tools', {'kind': 'notes'})
        self.assertIsInstance(first.get('revision'), str)
        saved = self.service.post('private', 'tools', {'kind': 'notes', 'action': 'save',
                                  'text': 'Saved from the first tab', 'revision': first['revision']})
        self.assertNotEqual(saved['revision'], stale['revision'])
        with self.assertRaises(ServiceError) as error:
            self.service.post('private', 'tools', {'kind': 'notes', 'action': 'save',
                              'text': 'A stale second draft', 'revision': stale['revision']})
        self.assertEqual((error.exception.status, error.exception.code), (409, 'stale_revision'))
        current = self.service.get('private', 'tools', {'kind': 'notes'})
        self.assertEqual(current['text'], 'Saved from the first tab')
        self.assertEqual(current['revision'], saved['revision'])

    def test_notes_save_requires_a_revision_and_noop_preserves_saved_bytes(self):
        before = self.service.get('private', 'tools', {'kind': 'notes'})
        with self.assertRaises(ServiceError) as error:
            self.service.post('private', 'tools', {'kind': 'notes', 'action': 'save', 'text': 'Unbound'})
        self.assertEqual(error.exception.code, 'stale_revision')
        saved = self.service.post('private', 'tools', {'kind': 'notes', 'action': 'save',
                                  'text': 'Pack notes', 'revision': before['revision']})
        path = Path(self.service._settings_path('private'))
        content, stamp = path.read_bytes(), path.stat().st_mtime_ns
        repeated = self.service.post('private', 'tools', {'kind': 'notes', 'action': 'save',
                                     'text': 'Pack notes', 'revision': saved['revision']})
        self.assertEqual(repeated['revision'], saved['revision'])
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_notes_revision_cannot_be_replayed_on_another_installation(self):
        one = self.service.get('private', 'tools', {'kind': 'notes'})
        two = self.service.get('default', 'tools', {'kind': 'notes'})
        self.assertNotEqual(one.get('revision'), two.get('revision'))
        with self.assertRaises(ServiceError) as error:
            self.service.post('default', 'tools', {'kind': 'notes', 'action': 'save',
                              'text': 'Wrong installation', 'revision': one['revision']})
        self.assertEqual(error.exception.code, 'stale_revision')
        self.assertEqual(self.service.get('default', 'tools', {'kind': 'notes'})['text'], '')

    def test_notes_acknowledgment_cannot_adopt_an_external_edit_as_its_saved_revision(self):
        before = self.service.get('private', 'tools', {'kind': 'notes'})
        real_write = installation_service._atomic_json
        def outside_edit(path, value):
            real_write(path, value)
            Path(path).write_text('{"notes":"External edit after the save"}', encoding='utf-8')
        with mock.patch.object(installation_service, '_atomic_json', side_effect=outside_edit):
            saved = self.service.post('private', 'tools', {'kind': 'notes', 'action': 'save',
                                      'text': 'Submitted note', 'revision': before['revision']})
        current = self.service.get('private', 'tools', {'kind': 'notes'})
        self.assertEqual(saved['text'], 'Submitted note')
        self.assertNotEqual(saved['revision'], current['revision'])
        with self.assertRaises(ServiceError) as error:
            self.service.post('private', 'tools', {'kind': 'notes', 'action': 'save',
                              'text': 'Another edit', 'revision': saved['revision']})
        self.assertEqual(error.exception.code, 'stale_revision')
        self.assertEqual(self.service.get('private', 'tools', {'kind': 'notes'})['text'], 'External edit after the save')

    def test_corrupt_notes_are_reported_and_never_overwritten_as_empty(self):
        path = Path(self.service._settings_path('private'))
        path.parent.mkdir(parents=True)
        for raw in (b'{"notes":"Recoverable old text",', b'[]', b'{"notes":42}', b'\xff'):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                with self.assertRaises(ServiceError) as read_error:
                    self.service.get('private', 'tools', {'kind': 'notes'})
                self.assertEqual(read_error.exception.code, 'invalid_settings')
                with self.assertRaises(ServiceError):
                    self.service.post('private', 'tools', {'kind': 'notes', 'action': 'save',
                                      'text': 'Replacement', 'revision': 'stale'})
                self.assertEqual(path.read_bytes(), raw)

    def test_unreadable_notes_are_reported_without_claiming_an_empty_read(self):
        path = Path(self.service._settings_path('private'))
        path.parent.mkdir(parents=True)
        path.write_text('{"notes":"Keep these notes"}', encoding='utf-8')
        with mock.patch('builtins.open', side_effect=PermissionError('fixture denied')):
            with self.assertRaises(ServiceError) as error:
                self.service.get('private', 'tools', {'kind': 'notes'})
        self.assertEqual(error.exception.code, 'settings_unavailable')
        self.assertEqual(path.read_text(), '{"notes":"Keep these notes"}')

    def test_idle_job_is_truthful(self):
        out = self.service.get('private', 'job')
        self.assertEqual(out['job']['status'], 'idle')

    def test_profiles_with_dynamic_latest_version_require_copy(self):
        self.profiles['private']['lastVersionId'] = 'latest-release'
        self.assertFalse(self.service.get('private')['ownership']['private'])

    def provider(self, bodies, relations=None):
        self.service.provider = DownloadProvider(bodies, relations)
        return self.service.provider

    def planned(self, selection='main-v'):
        return self.service.post('private', 'plan', {'kind': 'mods', 'selections': [{'versionId': selection}]})

    def job(self, plan):
        ack = self.service.post('private', 'apply', {'planId': plan['planId']})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            job = self.service.get('private', 'job', {'jobId': ack['jobId']})['job']
            if job['status'] in ('failed', 'succeeded'):
                return job
            time.sleep(.01)
        self.fail('Operation did not finish')

    def test_actual_archive_dependency_blocks_whole_commit(self):
        self.provider({'main': jar('main', {'unlisted': '*'})})
        plan = self.planned()
        self.assertFalse(plan['conflicts'])
        result = self.job(plan)
        self.assertEqual(result['code'], 'archive_dependency_conflict')
        self.assertEqual(self.service.get('private', 'content')['items'], [])

    def test_required_downloads_commit_together_and_reopen_retains_relations(self):
        self.provider({'main': jar('main', {'helper': '*'}), 'helper': jar('helper')}, {'main': [dependency('helper', 'helper-v')]})
        result = self.job(self.planned())
        self.assertEqual(result['status'], 'succeeded', result)
        items = self.service.get('private', 'content')['items']
        self.assertEqual({item['modIds'][0] for item in items}, {'main', 'helper'})
        main = next(item for item in items if item['projectId'] == 'main')
        self.assertEqual(main['providerDependencies'][0]['project_id'], 'helper')

    def test_multiple_catalog_picks_share_one_dependency_and_one_atomic_commit(self):
        provider = self.provider({'first': jar('first', {'helper': '*'}), 'second': jar('second', {'helper': '*'}),
            'helper': jar('helper')}, {'first': [dependency('helper', 'helper-v')], 'second': [dependency('helper', 'helper-v')]})
        store = ContentStore(self.private, self.data, 'private')
        with mock.patch.object(self.service, '_store', return_value=store), mock.patch.object(store, 'transact', wraps=store.transact) as transact:
            plan = self.service.post('private', 'plan', {'kind': 'mods', 'selections': [
                {'projectId': 'first', 'versionId': 'first-v'}, {'projectId': 'second', 'versionId': 'second-v'}],
                'revision': store.inventory()['revision']})
            self.assertFalse(plan['conflicts'], plan)
            self.assertEqual({file['versionId'] for file in plan['files']}, {'first-v', 'second-v', 'helper-v'})
            helper = next(file for file in plan['files'] if file['versionId'] == 'helper-v')
            self.assertEqual(helper['requiredBy'], ['first-v', 'second-v'])
            self.assertFalse(helper['userRequested'])
            transact.assert_not_called()
            result = self.job(plan)
            self.assertEqual(result['status'], 'succeeded', result)
            transact.assert_called_once()
        self.assertEqual(len(provider.downloads), 3)
        self.assertEqual(len(set(provider.downloads)), 3)
        self.assertEqual(len(store.inventory()['items']), 3)
        self.assertEqual({item['projectId'] for item in result['result']['summary']['installed']}, {'first', 'second', 'helper'})

    def test_content_job_reports_verified_downloads_and_committed_outcomes(self):
        self.provider({'main': jar('main', {'helper': '*'}), 'helper': jar('helper')}, {'main': [dependency('helper', 'helper-v')]})
        result = self.job(self.planned())
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual((result['completed'], result['total']), (2, 2))
        summary = result['result']['summary']
        self.assertEqual({item['projectId'] for item in summary['installed']}, {'main', 'helper'})
        self.assertEqual(summary['reused'], [])
        self.assertEqual(summary['enabled'], [])
        self.assertEqual(summary['removed'], [])
        installed = self.service.get('private', 'content')['items']
        self.assertEqual({item['id'] for item in summary['installed']}, {item['id'] for item in installed})
        reused = self.job(self.planned())
        self.assertEqual(reused['status'], 'succeeded', reused)
        self.assertEqual((reused['completed'], reused['total']), (0, 0))
        self.assertEqual(reused['result']['summary']['installed'], [])
        self.assertEqual({item['projectId'] for item in reused['result']['summary']['reused']}, {'main', 'helper'})

    def test_repeated_identical_reviews_reuse_one_unapplied_plan_without_downloading(self):
        provider = self.provider({'main': jar('main')})
        try:
            plans = [self.planned()['planId'] for _ in range(40)]
        except ServiceError as error:
            self.fail('Repeated unchanged reviews must stay usable: ' + str(error))
        self.assertEqual(len(set(plans)), 1)
        self.assertEqual(provider.downloads, [])
        self.assertEqual(self.service.get('private', 'content')['items'], [])

    def test_review_reuse_keeps_optional_policy_and_changed_inventory_distinct(self):
        self.provider({'main': jar('main'), 'optional': jar('optional')}, {'main': [dependency('optional', 'optional-v', 'optional')]})
        ordinary = self.planned()
        include_optional = self.service.post('private', 'plan', {'kind': 'mods', 'selections': [{'versionId': 'main-v'}], 'includeOptional': True})
        self.assertNotEqual(ordinary['planId'], include_optional['planId'])
        self.assertEqual(len(include_optional['files']), 2)
        self.assertEqual(self.planned()['planId'], ordinary['planId'])
        self.assertEqual(self.job(ordinary)['status'], 'succeeded')
        changed = self.planned()
        self.assertNotEqual(changed['planId'], ordinary['planId'])
        self.assertEqual(changed['files'], [])

    def test_old_hot_reload_reviews_still_reuse_without_consuming_slots(self):
        self.provider({'main': jar('main')})
        plan = self.planned()
        installation_service._PLANS[plan['planId']].pop('includeOptional')
        installation_service._PLANS[plan['planId']].pop('updates')
        self.assertEqual(self.planned()['planId'], plan['planId'])

    def test_failed_applied_reviews_do_not_block_later_download_retries(self):
        provider = self.provider({'main': jar('main')})
        with mock.patch.object(provider, 'download', side_effect=RuntimeError('Network unavailable')):
            for _ in range(33):
                try:
                    plan = self.planned()
                except ServiceError as error:
                    self.fail('Finished job receipts must not consume review slots: ' + str(error))
                result = self.job(plan)
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['error'], 'Network unavailable')
        self.assertEqual(self.service.get('private', 'content')['items'], [])

    def test_distinct_review_cache_evicts_oldest_and_preserves_files_and_job_receipts(self):
        provider = self.provider({'receipt': jar('receipt'), 'main': jar('main')})
        receipt_plan = self.planned('receipt-v')
        receipt_job = self.job(receipt_plan)
        self.assertEqual(receipt_job['status'], 'succeeded')
        baseline = self.service.get('private', 'content')
        downloaded = list(provider.downloads)
        for index in range(33):
            record = version('main', 'main-' + str(index) + '-v')
            record['files'][0].update(size=len(provider.bodies['main']),
                                      hashes={'sha512': hashlib.sha512(provider.bodies['main']).hexdigest()})
            provider.records[record['id']] = record
        try:
            plans = [self.planned('main-' + str(index) + '-v') for index in range(33)]
        except ServiceError as error:
            self.fail('Changing versions must keep the review usable: ' + str(error))
        self.assertEqual(len({plan['planId'] for plan in plans}), 33)
        self.assertEqual(self.service.get('private', 'content'), baseline)
        self.assertEqual(provider.downloads, downloaded)
        with self.assertRaises(ServiceError) as caught:
            self.service.post('private', 'apply', {'planId': plans[0]['planId']})
        self.assertEqual(caught.exception.code, 'expired_plan')
        self.assertEqual(self.planned('main-32-v')['planId'], plans[-1]['planId'])
        recovered = self.planned('main-0-v')
        self.assertNotEqual(recovered['planId'], plans[0]['planId'])
        replay = self.service.post('private', 'apply', {'planId': receipt_plan['planId']})
        self.assertEqual(replay['jobId'], receipt_job['id'])
        self.assertEqual(provider.downloads, downloaded)
        self.assertEqual(self.service.get('private', 'content'), baseline)

    def test_download_progress_counts_verified_files_while_next_file_is_pending(self):
        provider = self.provider({'main': jar('main', {'helper': '*'}), 'helper': jar('helper')}, {'main': [dependency('helper', 'helper-v')]})
        plan = self.planned()
        pending, release = threading.Event(), threading.Event()
        download = provider.download
        calls = []

        def controlled_download(content, destination):
            calls.append(content['filename'])
            if len(calls) == 2:
                pending.set()
                if not release.wait(5):
                    raise RuntimeError('Fixture download release timed out')
            return download(content, destination)

        with mock.patch.object(provider, 'download', side_effect=controlled_download):
            ack = self.service.post('private', 'apply', {'planId': plan['planId']})
            try:
                self.assertTrue(pending.wait(3))
                progress = self.service.get('private', 'job', {'jobId': ack['jobId']})['job']
                self.assertEqual((progress['completed'], progress['total']), (1, 2))
                self.assertEqual(progress['phase'], 'downloading')
                self.assertEqual(progress['currentFile'], calls[-1])
                self.assertEqual(self.service.get('private', 'content')['items'], [])
            finally:
                release.set()
                result = self.job(plan)
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual((result['completed'], result['total']), (2, 2))

    def test_apply_and_independent_copy_retain_original_project_logo_metadata(self):
        provider = self.provider({'main': jar('main')})
        icon = 'https://cdn.modrinth.com/data/main/icon.png'
        provider.projects['main'] = {'id': 'main', 'project_type': 'mod', 'client_side': 'optional', 'icon_url': icon}
        plan = self.planned()
        self.assertEqual(plan['files'][0]['iconUrl'], icon)
        # Older saved plans may carry provider artwork only in their public
        # file record; apply must not silently drop that provenance.
        installation_service._PLANS[plan['planId']]['proposal']['files'][0]['metadata'].pop('iconUrl')
        self.assertEqual(self.job(plan)['status'], 'succeeded')
        item = self.service.get('private', 'content')['items'][0]
        self.assertEqual(item['iconUrl'], icon)
        copied = self.service.post('private', 'copy', {})
        self.assertEqual(self.service.get(copied['instanceId'], 'content')['items'][0]['iconUrl'], icon)

    def test_content_jobs_retain_authoritative_pack_kind_pending_and_after_completion(self):
        for kind, project_type, loaders in [('resourcepacks', 'resourcepack', ['minecraft']),
                                            ('shaderpacks', 'shader', ['iris'])]:
            with self.subTest(kind=kind):
                data = io.BytesIO()
                with zipfile.ZipFile(data, 'w') as archive:
                    if kind == 'resourcepacks':
                        archive.writestr('pack.mcmeta', json.dumps({'pack': {'pack_format': 15, 'description': 'Fixture resources'}}))
                    else:
                        archive.writestr('shaders/fixture.vsh', 'void main() {}')
                project_id = 'resource' if kind == 'resourcepacks' else 'shader'
                version_id = project_id + '-v'
                provider = self.provider({project_id: data.getvalue()})
                provider.projects[project_id] = {'id': project_id, 'project_type': project_type, 'client_side': 'optional'}
                provider.records[version_id]['loaders'] = loaders
                provider.records[version_id]['files'][0]['filename'] = 'pack.zip'
                plan = self.service.post('private', 'plan', {'kind': kind, 'selections': [{'versionId': version_id}]})
                self.assertFalse(plan['conflicts'], plan)
                provider.release.clear()
                ack = self.service.post('private', 'apply', {'planId': plan['planId']})
                try:
                    self.assertTrue(provider.entered.wait(3))
                    pending = self.service.get('private', 'job', {'jobId': ack['jobId']})['job']
                    self.assertEqual(pending.get('kind'), kind)
                    self.assertEqual(self.service.get('private')['latestJob'].get('kind'), kind)
                finally:
                    provider.release.set()
                    completed = self.job(plan)
                self.assertEqual(completed['status'], 'succeeded', completed)
                self.assertEqual(completed.get('kind'), kind)
                self.assertEqual(self.service.get('private')['latestJob'].get('kind'), kind)

    def test_apply_replay_returns_same_background_job(self):
        provider = self.provider({'main': jar('main')})
        provider.release.clear()
        plan = self.planned()
        first = self.service.post('private', 'apply', {'planId': plan['planId']})
        self.assertTrue(provider.entered.wait(2))
        try:
            second = self.service.post('private', 'apply', {'planId': plan['planId']})
            self.assertEqual(first['jobId'], second['jobId'])
            self.assertEqual(self.service.get('private')['latestJob']['id'], first['jobId'])
            with self.assertRaises(ServiceError):
                self.service.post('private', 'general', {'patch': {'name': 'Race'}})
        finally:
            provider.release.set()
        self.assertEqual(self.job(plan)['status'], 'succeeded')
        self.assertEqual(provider.downloads, ['main-v'])

    def test_local_content_change_during_download_preserves_files(self):
        provider = self.provider({'main': jar('main')})
        provider.release.clear()
        plan = self.planned()
        self.service.post('private', 'apply', {'planId': plan['planId']})
        self.assertTrue(provider.entered.wait(2))
        (self.private / 'mods').mkdir(exist_ok=True)
        original = self.private / 'mods' / 'external.jar'
        original.write_bytes(jar('external'))
        provider.release.set()
        result = self.job(plan)
        self.assertEqual(result['code'], 'stale_revision')
        self.assertEqual([item['filename'] for item in self.service.get('private', 'content')['items']], ['external.jar'])

    def test_remove_dependency_offers_explicit_dependent_set(self):
        self.provider({'main': jar('main', {'helper': '*'}), 'helper': jar('helper')}, {'main': [dependency('helper', 'helper-v')]})
        self.assertEqual(self.job(self.planned())['status'], 'succeeded')
        inventory = self.service.get('private', 'content')
        helper = next(i for i in inventory['items'] if i['projectId'] == 'helper')
        with self.assertRaises(ServiceError) as caught:
            self.service.post('private', 'remove', {'ids': [helper['id']], 'revision': inventory['revision']})
        self.assertEqual(len(caught.exception.details['dependents']), 1)
        ids = caught.exception.details['suggestedIds']
        result = self.service.post('private', 'remove', {'ids': ids, 'revision': inventory['revision']})
        self.assertEqual(result['items'], [])

    def batch_fixture(self, disabled=(), missing=False):
        self.provider({})
        store = ContentStore(self.private, self.data, 'private')
        records = [('helper', {}), ('first', {'helper': '*'}), ('second', {'helper': '*'}), ('other', {})]
        if missing:
            records = [('first', {'absent': '*'})]
        for ident, depends in records:
            source = self.base / (ident + '.jar')
            source.write_bytes(jar(ident, depends))
            store.transact([{'action': 'install', 'kind': 'mods', 'filename': source.name, 'source': str(source)}])
        items = {item['modIds'][0]: item for item in store.inventory()['items']}
        if disabled:
            store.transact([{'action': 'toggle', 'id': items[ident]['id'], 'enabled': False} for ident in disabled])
        return store, {item['modIds'][0]: item for item in store.inventory()['items']}

    def test_bulk_preview_separates_dependent_additions_and_does_not_commit(self):
        store, items = self.batch_fixture()
        snapshot = store.inventory()
        with mock.patch.object(self.service, '_store', return_value=store), mock.patch.object(store, 'transact', wraps=store.transact) as transact:
            for action, flags in [('remove', {}), ('toggle', {'enabled': False})]:
                with self.subTest(action=action):
                    preview = self.service.post('private', 'preview-change', {'action': action, 'kind': 'mods',
                        'ids': [items['helper']['id']], 'revision': snapshot['revision'], **flags})
                    self.assertEqual([item['name'] for item in preview['selected']], ['helper'])
                    self.assertEqual({item['name'] for item in preview['additional']}, {'first', 'second'})
                    self.assertEqual({item['name'] for item in preview['affected']}, {'helper', 'first', 'second'})
                    self.assertTrue(preview['requiresExpansion'])
                    self.assertFalse(preview['conflicts'])
                    self.assertEqual(preview['applyBody']['revision'], snapshot['revision'])
                    self.assertTrue(preview['applyBody']['includeDependents' if action == 'remove' else 'disableDependents'])
            transact.assert_not_called()
        self.assertEqual(store.inventory()['revision'], snapshot['revision'])
        preview = self.service.post('private', 'preview-change', {'action': 'remove', 'kind': 'mods',
            'ids': [items['helper']['id']], 'revision': snapshot['revision']})
        applied = self.service.post('private', 'remove', preview['applyBody'])
        self.assertEqual([item['name'] for item in applied['items']], ['other'])
        self.assertEqual({item['name'] for item in applied['summary']['removed']}, {'helper', 'first', 'second'})

    def test_bulk_enable_preview_applies_one_transaction_and_keeps_other_files_unchanged(self):
        store, items = self.batch_fixture(disabled=('helper', 'first', 'second'))
        snapshot = store.inventory()
        original_other = (self.private / 'mods/other.jar').read_bytes()
        with mock.patch.object(self.service, '_store', return_value=store), mock.patch.object(store, 'transact', wraps=store.transact) as transact:
            preview = self.service.post('private', 'preview-change', {'action': 'toggle', 'kind': 'mods',
                'ids': [items['first']['id'], items['second']['id']], 'enabled': True, 'revision': snapshot['revision']})
            self.assertEqual({item['name'] for item in preview['additional']}, {'helper'})
            transact.assert_not_called()
            result = self.service.post('private', 'toggle', preview['applyBody'])
            transact.assert_called_once()
            changes = transact.call_args.args[0]
            self.assertEqual(len(changes), 3)
            self.assertEqual(len({item['id'] for item in changes}), 3)
        self.assertTrue(all(item['enabled'] for item in result['items']))
        self.assertEqual({item['name'] for item in result['summary']['enabled']}, {'first', 'second', 'helper'})
        self.assertEqual((self.private / 'mods/other.jar').read_bytes(), original_other)

    def test_bulk_mixed_states_report_unchanged_and_noop_does_not_transact(self):
        store, items = self.batch_fixture(disabled=('other',))
        snapshot = store.inventory()
        with mock.patch.object(self.service, '_store', return_value=store), mock.patch.object(store, 'transact', wraps=store.transact) as transact:
            preview = self.service.post('private', 'preview-change', {'action': 'toggle', 'kind': 'mods',
                'ids': [items['helper']['id'], items['other']['id']], 'enabled': True, 'revision': snapshot['revision']})
            self.assertEqual([item['name'] for item in preview['unchanged']], ['helper'])
            self.assertEqual([item['name'] for item in preview['changed']], ['other'])
            result = self.service.post('private', 'toggle', preview['applyBody'])
            self.assertEqual(len(transact.call_args.args[0]), 1)
            after = result['revision']
            transact.reset_mock()
            noop = self.service.post('private', 'preview-change', {'action': 'toggle', 'kind': 'mods',
                'ids': [items['helper']['id'], items['other']['id']], 'enabled': True, 'revision': after})
            self.assertEqual(noop['changed'], [])
            result = self.service.post('private', 'toggle', noop['applyBody'])
            self.assertEqual(result['revision'], after)
            transact.assert_not_called()

    def test_bulk_preview_reports_genuine_missing_dependency_without_changes(self):
        store, items = self.batch_fixture(disabled=('first',), missing=True)
        snapshot = store.inventory()
        preview = self.service.post('private', 'preview-change', {'action': 'toggle', 'kind': 'mods',
            'ids': [items['first']['id']], 'enabled': True, 'revision': snapshot['revision']})
        self.assertIn('missing_dependency', {item['code'] for item in preview['conflicts']})
        with self.assertRaises(ServiceError) as caught:
            self.service.post('private', 'toggle', preview['applyBody'])
        self.assertEqual(caught.exception.code, 'dependency_conflict')
        self.assertEqual(store.inventory()['revision'], snapshot['revision'])

    def test_bulk_preview_and_apply_reject_invalid_and_cross_category_selections(self):
        store, items = self.batch_fixture()
        pack_source = self.base / 'pack.zip'
        with zipfile.ZipFile(pack_source, 'w') as archive:
            archive.writestr('pack.mcmeta', json.dumps({'pack': {'pack_format': 15, 'description': 'Pack'}}))
        store.transact([{'action': 'install', 'kind': 'resourcepacks', 'filename': 'pack.zip', 'source': str(pack_source)}])
        snapshot = store.inventory()
        pack = next(item for item in snapshot['items'] if item['kind'] == 'resourcepacks')
        for resource in ('toggle', 'remove', 'preview-change'):
            for ids in ([], [''], [items['other']['id'], items['other']['id']], ['x' * 129], ['x' + str(i) for i in range(1025)]):
                with self.subTest(resource=resource, ids=len(ids)), self.assertRaises(ServiceError) as caught:
                    self.service.post('private', resource, {'action': 'remove' if resource == 'remove' else 'toggle', 'kind': 'mods', 'ids': ids, 'enabled': False, 'revision': snapshot['revision']})
                self.assertEqual(caught.exception.code, 'invalid_selection')
            with self.subTest(resource=resource, mixed=True), self.assertRaises(ServiceError) as caught:
                self.service.post('private', resource, {'action': 'remove' if resource == 'remove' else 'toggle', 'kind': 'mods',
                    'ids': [items['other']['id'], pack['id']], 'enabled': False, 'revision': snapshot['revision']})
            self.assertEqual(caught.exception.code, 'kind_mismatch')
        self.assertEqual(store.inventory()['revision'], snapshot['revision'])

    def assert_bulk_review_wrong_operation(self, reviewed, applied):
        store, items = self.batch_fixture()
        snapshot = store.inventory()
        preview = self.service.post('private', 'preview-change', {'action': reviewed, 'kind': 'mods',
            'ids': [items['other']['id']], 'enabled': False, 'revision': snapshot['revision']})
        with self.assertRaises(ServiceError) as caught:
            self.service.post('private', applied, preview['applyBody'])
        self.assertEqual(caught.exception.code, 'action_mismatch')
        self.assertEqual(store.inventory()['revision'], snapshot['revision'])

    def test_bulk_toggle_review_cannot_remove_files(self):
        self.assert_bulk_review_wrong_operation('toggle', 'remove')

    def test_bulk_remove_review_cannot_toggle_files(self):
        self.assert_bulk_review_wrong_operation('remove', 'toggle')

    def test_bulk_review_revalidates_revision_root_and_context_before_apply(self):
        store, items = self.batch_fixture()
        body = {'action': 'toggle', 'kind': 'mods', 'ids': [items['other']['id']], 'enabled': False,
            'revision': store.inventory()['revision']}
        preview = self.service.post('private', 'preview-change', body)
        original_context = self.service.context_callback
        self.service.context_callback = lambda profile: dict(original_context(profile), loaderVersion='0.99.0')
        with self.assertRaises(ServiceError) as caught:
            self.service.post('private', 'toggle', preview['applyBody'])
        self.assertEqual(caught.exception.code, 'changed_installation')
        self.service.context_callback = original_context
        other_root = self.game / 'instances/other'
        other_root.mkdir()
        self.profiles['private']['gameDir'] = str(other_root)
        with self.assertRaises(ServiceError) as caught:
            self.service.post('private', 'toggle', preview['applyBody'])
        self.assertEqual(caught.exception.code, 'changed_installation')
        self.profiles['private']['gameDir'] = str(self.private)
        (self.private / 'mods/new.jar').write_bytes(jar('new'))
        with self.assertRaises(ServiceError) as caught:
            self.service.post('private', 'toggle', preview['applyBody'])
        self.assertEqual(caught.exception.code, 'stale_revision')
        self.assertTrue((self.private / 'mods/other.jar').exists())

    def test_bulk_resource_files_change_availability_without_activating_configuration(self):
        self.provider({})
        store = ContentStore(self.private, self.data, 'private')
        for kind in ('resourcepacks', 'shaderpacks'):
            with self.subTest(kind=kind):
                source = self.base / (kind + '.zip')
                with zipfile.ZipFile(source, 'w') as archive:
                    if kind == 'resourcepacks':
                        archive.writestr('pack.mcmeta', json.dumps({'pack': {'pack_format': 15, 'description': 'Home'}}))
                    else:
                        archive.writestr('shaders/test.vsh', 'void main(){}')
                store.transact([{'action': 'install', 'kind': kind, 'filename': source.name, 'source': str(source)}])
                snapshot = store.inventory()
                item = next(item for item in snapshot['items'] if item['kind'] == kind)
                options = self.private / 'options.txt'
                options.write_text('resourcePacks:["vanilla"]\n', encoding='utf-8')
                iris = self.private / 'config/iris.properties'
                iris.parent.mkdir(exist_ok=True)
                iris.write_text('enableShaders=false\n', encoding='utf-8')
                preview = self.service.post('private', 'preview-change', {'action': 'toggle', 'kind': kind,
                    'ids': [item['id']], 'enabled': False, 'revision': snapshot['revision']})
                self.assertFalse(preview['activationChanged'])
                self.service.post('private', 'toggle', preview['applyBody'])
                self.assertEqual(options.read_text(encoding='utf-8'), 'resourcePacks:["vanilla"]\n')
                self.assertEqual(iris.read_text(encoding='utf-8'), 'enableShaders=false\n')

    def test_bulk_preview_obeys_running_busy_and_private_directory_guards(self):
        store, items = self.batch_fixture()
        body = {'action': 'remove', 'kind': 'mods', 'ids': [items['other']['id']], 'revision': store.inventory()['revision']}
        self.running = True
        with self.assertRaises(ServiceError) as caught:
            self.service.post('private', 'preview-change', body)
        self.assertEqual(caught.exception.code, 'game_running')
        self.running = False
        self.service.external_busy = lambda: True
        with self.assertRaises(ServiceError) as caught:
            self.service.post('private', 'preview-change', body)
        self.assertEqual(caught.exception.code, 'operation_running')
        self.service.external_busy = lambda: False
        self.profiles['other'] = dict(self.profiles['private'])
        with self.assertRaises(ServiceError) as caught:
            self.service.post('private', 'preview-change', body)
        self.assertEqual(caught.exception.code, 'shared_directory')

    def test_enabling_disabled_mod_offers_installed_dependencies(self):
        store = ContentStore(str(self.private), str(self.data), 'private')
        for name, raw in [('main.jar', jar('main', {'helper': '*'})), ('helper.jar', jar('helper'))]:
            staged = self.base / name
            staged.write_bytes(raw)
            store.transact([{'action': 'install', 'kind': 'mods', 'filename': name, 'source': str(staged)}])
        items = store.inventory()['items']
        store.transact([{'action': 'toggle', 'id': item['id'], 'enabled': False} for item in items])
        main = next(item for item in items if item['modIds'] == ['main'])
        with self.assertRaises(ServiceError) as caught:
            self.service.post('private', 'toggle', {'id': main['id'], 'enabled': True})
        self.assertEqual(len(caught.exception.details['dependents']), 1)
        out = self.service.post('private', 'toggle', {'id': main['id'], 'enabled': True, 'includeDependencies': True})
        self.assertTrue(all(item['enabled'] for item in out['items']))

    def test_local_import_retry_reuses_identical_archive(self):
        import base64
        self.provider({})
        body = {'kind': 'mods', 'files': [{'name': 'local.jar', 'data': base64.b64encode(jar('local')).decode()}]}
        first = self.service.post('private', 'import', body)
        second = self.service.post('private', 'import', body)
        self.assertEqual(len(second['items']), 1)
        self.assertEqual(first['revision'], second['revision'])

    def nested_selection_fixture(self, alternate=False, passive_bundle=False):
        self.provider({})
        store = ContentStore(self.private, self.data, 'private')
        for ident, api_version, helper in (('root_a', '1.0.0', 'dep_used'), ('root_b', '2.0.0', 'dep_unused')):
            if alternate and ident == 'root_b':
                api_version, helper = '1.0.0', 'dep_used'
            nested = io.BytesIO()
            with zipfile.ZipFile(nested, 'w') as archive:
                archive.writestr('fabric.mod.json', json.dumps({'id': 'shared_api', 'version': api_version, 'depends': {helper: '*'}}))
            source = self.base / (ident + '.jar')
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('fabric.mod.json', json.dumps({'id': ident, 'version': '1.0.0',
                    'depends': {} if passive_bundle and ident == 'root_a' else {'shared_api': '=1.0.0'},
                    'jars': [{'file': 'nested/api.jar'}]}))
                archive.writestr('nested/api.jar', nested.getvalue())
            store.transact([{'action': 'install', 'kind': 'mods', 'filename': source.name, 'source': str(source)}])
        for ident in ('dep_used', 'dep_unused'):
            source = self.base / (ident + '.jar')
            source.write_bytes(jar(ident))
            store.transact([{'action': 'install', 'kind': 'mods', 'filename': source.name, 'source': str(source)}])
        return store, {item['filename'][:-4]: item for item in store.inventory()['items']}

    def test_removing_unused_nested_candidate_dependency_does_not_expand_dependents(self):
        store, items = self.nested_selection_fixture()
        result = self.service.post('private', 'remove', {'ids': [items['dep_unused']['id']], 'revision': store.inventory()['revision']})
        self.assertEqual({item['filename'] for item in result['items']}, {'root_a.jar', 'root_b.jar', 'dep_used.jar'})

    def test_enabling_root_uses_selected_nested_dependencies(self):
        store, items = self.nested_selection_fixture()
        store.transact([{'action': 'toggle', 'id': items[ident]['id'], 'enabled': False} for ident in ('root_b', 'dep_unused')])
        result = self.service.post('private', 'toggle', {'id': items['root_b']['id'], 'enabled': True,
            'revision': store.inventory()['revision']})
        returned = {item['modIds'][0]: item for item in result['items']}
        self.assertTrue(returned['root_b']['enabled'])
        self.assertFalse(returned['dep_unused']['enabled'])

    def test_removing_nested_library_source_uses_remaining_compatible_candidate(self):
        store, items = self.nested_selection_fixture(alternate=True)
        result = self.service.post('private', 'remove', {'ids': [items['root_a']['id']],
            'revision': store.inventory()['revision']})
        self.assertEqual({item['filename'] for item in result['items']}, {'root_b.jar', 'dep_used.jar', 'dep_unused.jar'})

    def test_enable_review_includes_disabled_helper_of_enabled_bundle(self):
        store, items = self.nested_selection_fixture(passive_bundle=True)
        store.transact([{'action': 'toggle', 'id': items[ident]['id'], 'enabled': False}
                        for ident in ('root_b', 'dep_used', 'dep_unused')])
        with self.assertRaises(ServiceError) as caught:
            self.service.post('private', 'toggle', {'id': items['root_b']['id'], 'enabled': True})
        self.assertEqual(caught.exception.code, 'dependent_content')
        self.assertEqual(caught.exception.details['suggestedIds'], sorted([items['root_b']['id'], items['dep_used']['id']]))
        result = self.service.post('private', 'toggle', {'id': items['root_b']['id'], 'enabled': True, 'includeDependencies': True})
        returned = {item['modIds'][0]: item for item in result['items']}
        self.assertTrue(returned['dep_used']['enabled'])
        self.assertFalse(returned['dep_unused']['enabled'])

    def test_enable_review_follows_exact_provider_only_disabled_dependency(self):
        self.provider({})
        store = ContentStore(self.private, self.data, 'private')
        for ident in ('main', 'helper'):
            source = self.base / (ident + '.jar')
            source.write_bytes(jar(ident))
            metadata = {'provider': 'modrinth', 'projectId': ident, 'versionId': ident + '-v'}
            if ident == 'main':
                metadata['providerDependencies'] = [{'project_id': None, 'version_id': 'helper-v', 'dependency_type': 'required'}]
            store.transact([{'action': 'install', 'kind': 'mods', 'filename': source.name, 'source': str(source), 'metadata': metadata}])
        items = {item['modIds'][0]: item for item in store.inventory()['items']}
        store.transact([{'action': 'toggle', 'id': item['id'], 'enabled': False} for item in items.values()])
        with self.assertRaises(ServiceError) as caught:
            self.service.post('private', 'toggle', {'id': items['main']['id'], 'enabled': True})
        self.assertEqual(caught.exception.code, 'dependent_content')
        self.assertEqual(caught.exception.details['suggestedIds'], sorted(item['id'] for item in items.values()))
        result = self.service.post('private', 'toggle', {'id': items['main']['id'], 'enabled': True, 'includeDependencies': True})
        self.assertTrue(all(item['enabled'] for item in result['items']))

    def test_enable_review_resolves_mixed_provider_and_local_dependency_chain(self):
        self.provider({})
        store = ContentStore(self.private, self.data, 'private')
        for ident in ('main', 'provider_helper', 'local_helper'):
            source = self.base / (ident + '.jar')
            source.write_bytes(jar(ident, {'local_helper': '*'} if ident == 'provider_helper' else {}))
            metadata = {'provider': 'modrinth', 'projectId': ident, 'versionId': ident + '-v'}
            if ident == 'main':
                metadata['providerDependencies'] = [{'project_id': None, 'version_id': 'provider_helper-v', 'dependency_type': 'required'}]
            store.transact([{'action': 'install', 'kind': 'mods', 'filename': source.name, 'source': str(source), 'metadata': metadata}])
        items = {item['modIds'][0]: item for item in store.inventory()['items']}
        store.transact([{'action': 'toggle', 'id': item['id'], 'enabled': False} for item in items.values()])
        with self.assertRaises(ServiceError) as caught:
            self.service.post('private', 'toggle', {'id': items['main']['id'], 'enabled': True})
        self.assertEqual(caught.exception.code, 'dependent_content')
        self.assertEqual(caught.exception.details['suggestedIds'], sorted(item['id'] for item in items.values()))
        result = self.service.post('private', 'toggle', {'id': items['main']['id'], 'enabled': True, 'includeDependencies': True})
        self.assertTrue(all(item['enabled'] for item in result['items']))

    def test_remove_review_preserves_substitute_bundle_when_another_dependency_is_lost(self):
        self.provider({})
        store = ContentStore(self.private, self.data, 'private')
        records = [('bundle_a', {}, [('shared_api', '2.0.0'), ('lost_helper', '1.0.0')]),
                   ('bundle_b', {'shared_api': '>=1'}, [('shared_api', '1.0.0')]),
                   ('consumer', {'lost_helper': '*'}, [])]
        for ident, depends, bundled in records:
            source = self.base / (ident + '.jar')
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('fabric.mod.json', json.dumps({'id': ident, 'version': '1.0.0', 'depends': depends,
                    'jars': [{'file': 'nested/' + child + '.jar'} for child, version in bundled]}))
                for child, version in bundled:
                    nested = io.BytesIO()
                    with zipfile.ZipFile(nested, 'w') as module:
                        module.writestr('fabric.mod.json', json.dumps({'id': child, 'version': version}))
                    archive.writestr('nested/' + child + '.jar', nested.getvalue())
            store.transact([{'action': 'install', 'kind': 'mods', 'filename': source.name, 'source': str(source)}])
        items = {item['filename'][:-4]: item for item in store.inventory()['items']}
        with self.assertRaises(ServiceError) as caught:
            self.service.post('private', 'remove', {'ids': [items['bundle_a']['id']]})
        self.assertEqual(caught.exception.code, 'dependent_content')
        self.assertEqual(caught.exception.details['suggestedIds'], sorted([items['bundle_a']['id'], items['consumer']['id']]))
        result = self.service.post('private', 'remove', {'ids': caught.exception.details['suggestedIds']})
        self.assertEqual([item['filename'] for item in result['items']], ['bundle_b.jar'])

    def candidate_action_fixture(self, records, disabled=()):
        self.provider({})
        store = ContentStore(self.private, self.data, 'private')
        for record in records:
            ident = record['id']
            source = self.base / (ident + '.jar')
            bundled = record.get('bundled', [])
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('fabric.mod.json', json.dumps({'id': ident, 'version': '1.0.0',
                    'depends': record.get('depends', {}), 'jars': [{'file': f'nested/{index}.jar'} for index in range(len(bundled))]}))
                for index, child in enumerate(bundled):
                    nested = io.BytesIO()
                    with zipfile.ZipFile(nested, 'w') as module:
                        module.writestr('fabric.mod.json', json.dumps(child))
                    archive.writestr(f'nested/{index}.jar', nested.getvalue())
            metadata = {}
            if record.get('managed') or record.get('providerDependencies'):
                metadata = {'provider': 'modrinth', 'projectId': ident, 'versionId': ident + '-v',
                            'providerDependencies': record.get('providerDependencies', [])}
            store.transact([{'action': 'install', 'kind': 'mods', 'filename': source.name, 'source': str(source), 'metadata': metadata}])
        items = {item['filename'][:-4]: item for item in store.inventory()['items']}
        if disabled:
            store.transact([{'action': 'toggle', 'id': items[ident]['id'], 'enabled': False} for ident in disabled])
        return store, items

    def test_disable_review_resolves_surviving_local_then_provider_dependencies(self):
        store, items = self.candidate_action_fixture([
            {'id': 'bundle_a', 'bundled': [{'id': 'api', 'version': '2.0.0'}, {'id': 'lost_helper', 'version': '1.0.0'}]},
            {'id': 'bundle_b', 'depends': {'api': '>=1'}, 'bundled': [{'id': 'api', 'version': '1.0.0', 'depends': {'local_helper': '*'}}]},
            {'id': 'consumer', 'depends': {'lost_helper': '*'}},
            {'id': 'local_helper', 'providerDependencies': [{'project_id': None, 'version_id': 'provider_helper-v', 'dependency_type': 'required'}]},
            {'id': 'provider_helper', 'managed': True},
        ])
        with self.assertRaises(ServiceError) as caught:
            self.service.post('private', 'toggle', {'id': items['bundle_a']['id'], 'enabled': False})
        self.assertEqual(caught.exception.details['suggestedIds'], sorted([items['bundle_a']['id'], items['consumer']['id']]))
        result = self.service.post('private', 'toggle', {'id': items['bundle_a']['id'], 'enabled': False, 'disableDependents': True})
        returned = {item['modIds'][0]: item for item in result['items']}
        for ident in ('bundle_b', 'local_helper', 'provider_helper'):
            self.assertTrue(returned[ident]['enabled'])
            self.assertEqual(returned[ident]['hashes'], items[ident]['hashes'])

    def test_disable_review_uses_enabled_substitute_before_disabled_newer_bundle(self):
        store, items = self.candidate_action_fixture([
            {'id': 'bundle_a', 'bundled': [{'id': 'api', 'version': '3.0.0'}, {'id': 'lost_helper', 'version': '1.0.0'}]},
            {'id': 'consumer_b', 'depends': {'api': '>=1'}},
            {'id': 'consumer_c', 'depends': {'lost_helper': '*'}},
            {'id': 'bundle_d', 'bundled': [{'id': 'api', 'version': '1.0.0'}]},
            {'id': 'bundle_x', 'bundled': [{'id': 'api', 'version': '2.0.0'}]},
        ], disabled=['bundle_x'])
        with self.assertRaises(ServiceError) as caught:
            self.service.post('private', 'toggle', {'id': items['bundle_a']['id'], 'enabled': False})
        self.assertEqual(caught.exception.details['suggestedIds'], sorted([items['bundle_a']['id'], items['consumer_c']['id']]))
        result = self.service.post('private', 'toggle', {'id': items['bundle_a']['id'], 'enabled': False, 'disableDependents': True})
        returned = {item['modIds'][0]: item for item in result['items']}
        self.assertTrue(returned['consumer_b']['enabled'])
        self.assertTrue(returned['bundle_d']['enabled'])
        self.assertFalse(returned['bundle_x']['enabled'])

    def test_native_resolution_limits_are_accepted(self):
        self.service.post('private', 'general', {'patch': {'resolution': {'width': 300, 'height': 260}}})
        with self.assertRaises(ServiceError):
            self.service.post('private', 'general', {'patch': {'resolution': {'width': 10000, 'height': 720}}})

    def test_disabled_update_stays_disabled(self):
        old = self.base / 'old.jar'
        old.write_bytes(jar('main'))
        store = ContentStore(str(self.private), str(self.data), 'private')
        snapshot = store.transact([{'action': 'install', 'kind': 'mods', 'filename': 'old.jar', 'source': str(old),
                                   'metadata': {'provider': 'modrinth', 'projectId': 'main', 'versionId': 'old', 'userRequested': True}}])
        store.transact([{'action': 'toggle', 'id': snapshot['items'][0]['id'], 'enabled': False}])
        self.provider({'main': jar('main', {'minecraft': '>=1.20'})})
        result = self.job(self.planned())
        self.assertEqual(result['status'], 'succeeded', result)
        item = self.service.get('private', 'content')['items'][0]
        self.assertFalse(item['enabled'])
        self.assertTrue(item['filename'].endswith('.disabled'))
        self.assertEqual(item['versionId'], 'main-v')
        summary = result['result']['summary']
        self.assertEqual([record['filename'] for record in summary['installed']], [item['filename']])
        self.assertEqual([record['filename'] for record in summary['removed']], ['old.jar.disabled'])
        self.assertEqual(summary['enabled'], [])

    def test_disabled_required_reuse_commits_owner_metadata(self):
        raw = jar('helper')
        old = self.base / 'helper.jar'
        old.write_bytes(raw)
        store = ContentStore(str(self.private), str(self.data), 'private')
        snapshot = store.transact([{'action': 'install', 'kind': 'mods', 'filename': 'helper-v.jar', 'source': str(old),
                                   'metadata': {'provider': 'modrinth', 'projectId': 'helper', 'versionId': 'helper-v', 'userRequested': False}}])
        store.transact([{'action': 'toggle', 'id': snapshot['items'][0]['id'], 'enabled': False}])
        self.provider({'main': jar('main', {'helper': '*'}), 'helper': raw}, {'main': [dependency('helper', 'helper-v')]})
        result = self.job(self.planned())
        self.assertEqual(result['status'], 'succeeded', result)
        items = self.service.get('private', 'content')['items']
        helper = next(i for i in items if i.get('projectId') == 'helper')
        self.assertTrue(helper['enabled'])
        self.assertIn('main-v', helper['requiredBy'])
        self.assertNotIn('helper-v', self.service.provider.downloads)
        self.assertEqual([item['projectId'] for item in result['result']['summary']['enabled']], ['helper'])
        self.assertEqual([item['projectId'] for item in result['result']['summary']['reused']], ['helper'])

    def test_independent_copy_retains_managed_provider_evidence(self):
        self.provider({'main': jar('main')})
        self.assertEqual(self.job(self.planned())['status'], 'succeeded')
        copied = self.service.post('private', 'copy', {})
        item = self.service.get(copied['instanceId'], 'content')['items'][0]
        self.assertEqual(item['projectId'], 'main')
        self.assertEqual(item['releaseChannels'], ['release'])

    def test_logs_are_selected_redacted_and_exported_without_overwrite(self):
        (self.private / 'logs').mkdir()
        (self.private / 'logs/latest.log').write_text('accessToken="private-secret"\nAuthorization: Bearer token-value\nhello')
        foreign = self.base / 'other.log'
        foreign.write_text('other installation')
        self.service.launch_status = lambda: {'configurationId': 'default', 'logPath': str(foreign)}
        data = self.service.get('private', 'tools', {'kind': 'logs'})
        self.assertNotIn('private-secret', data['text'])
        self.assertNotIn('token-value', data['text'])
        self.assertNotIn('other installation', data['text'])
        output = self.base / 'export.log'
        self.service.pick_path = lambda options: str(output)
        self.service.post('private', 'tools', {'kind': 'logs', 'action': 'export'})
        self.assertEqual(output.read_bytes().decode('utf-8'), data['text'])

    def test_direct_version_change_with_mods_requires_copy(self):
        self.provider({'main': jar('main')})
        self.assertEqual(self.job(self.planned())['status'], 'succeeded')
        with self.assertRaises(ServiceError):
            self.service.post('private', 'general', {'patch': {'lastVersionId': '1.21.1'}})

    def test_advanced_backup_history_binds_instance(self):
        (self.private / 'options.txt').write_text('original')
        baseline = self.service.get('private', 'tools', {'kind': 'advanced'})
        backup = self.service.post('private', 'tools', {'kind': 'advanced', 'action': 'backup', 'revision': baseline['revision']})
        history = self.service.get('private', 'tools', {'kind': 'advanced'})
        self.assertEqual(history['backups'][0]['id'], backup['backupId'])
        self.assertEqual(self.service.get('default', 'tools', {'kind': 'advanced'})['backups'], [])

    def test_advanced_exposes_existing_content_history_without_changing_it(self):
        source = self.base / 'synthetic.jar'
        source.write_bytes(jar('history_fixture'))
        store = ContentStore(str(self.private), str(self.data), 'private')
        store.transact([{'action': 'install', 'kind': 'mods', 'filename': 'fixture.jar', 'source': str(source)}])
        history = self.private / '.launcher-content/history'
        before = {str(path.relative_to(history)): path.read_bytes() for path in history.rglob('*') if path.is_file()}
        data = self.service.get('private', 'tools', {'kind': 'advanced'})
        self.assertEqual(data.get('contentRecoveryPath'), os.path.normcase(str(history.resolve())))
        self.assertEqual({str(path.relative_to(history)): path.read_bytes() for path in history.rglob('*') if path.is_file()}, before)

    def test_advanced_missing_content_history_never_creates_a_directory(self):
        missing = self.base / 'never-created'
        self.profiles['private']['gameDir'] = str(missing)
        data = self.service.get('private', 'tools', {'kind': 'advanced'})
        self.assertNotIn('contentRecoveryPath', data)
        self.assertFalse(missing.exists())
        history = self.private / '.launcher-content/history'
        self.profiles['private']['gameDir'] = str(self.private)
        data = self.service.get('private', 'tools', {'kind': 'advanced'})
        self.assertNotIn('contentRecoveryPath', data)
        self.assertFalse(history.exists())

    def test_advanced_refuses_linked_content_history_paths(self):
        import instance_content
        history = self.private / '.launcher-content/history'
        history.mkdir(parents=True)
        (history / 'retained.txt').write_text('synthetic artifact')
        for linked in (history, history.parent):
            with self.subTest(linked=linked), mock.patch.object(instance_content, '_reparse',
                    side_effect=lambda path: os.path.samefile(path, linked)):
                try:
                    data = self.service.get('private', 'tools', {'kind': 'advanced'})
                except installation_service.ContentError as error:
                    self.assertEqual(error.code, 'unsafe_path')
                else:
                    self.assertNotIn('contentRecoveryPath', data)
        self.assertEqual((history / 'retained.txt').read_text(), 'synthetic artifact')

    def test_log_stop_failure_is_not_wrapped_as_a_successful_tool_action(self):
        self.service.launch_status = lambda: {'configurationId': 'private', 'running': True}
        self.service.launch_stop = lambda: {'running': True, 'error': 'Minecraft could not be stopped'}
        out = self.service.post('private', 'tools', {'kind': 'logs', 'action': 'stop'})
        self.assertFalse(out['ok'])
        self.assertEqual(out['error'], 'Minecraft could not be stopped')
        self.assertTrue(out['status']['running'])

    def test_copy_aborts_if_external_world_file_changes(self):
        (self.private / 'saves/world').mkdir(parents=True)
        level = self.private / 'saves/world/level.dat'
        level.write_bytes(b'before')
        original_copy = installation_service.shutil.copytree
        def edit_after_copy(source, destination, *args, **kwargs):
            result = original_copy(source, destination, *args, **kwargs)
            if os.path.samefile(source, self.private / 'saves'):
                level.write_bytes(b'after external edit')
            return result
        with mock.patch.object(installation_service.shutil, 'copytree', side_effect=edit_after_copy), self.assertRaises(ServiceError):
            self.service.post('private', 'copy', {})
        self.assertEqual(level.read_bytes(), b'after external edit')
        self.assertEqual(set(self.profiles), {'private', 'default'})


if __name__ == '__main__':
    unittest.main()
