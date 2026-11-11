"""Repair contracts exercise real archives, inventory and atomic transactions."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
import zipfile

from content_provider import ModrinthProvider, ProviderError
from installation_service import InstallationService, ServiceError
from instance_content import ContentStore


def archive(mod_id, version='1.0.0', depends=None, **extra):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as zipped:
        zipped.writestr('fabric.mod.json', json.dumps(dict(
            id=mod_id, name=mod_id, version=version, depends=depends or {}, **extra)))
    return stream.getvalue()


def required(project, version=None, relation='required'):
    return {'project_id': project, 'version_id': version, 'dependency_type': relation}


class FixtureResponse(io.BytesIO):
    def __init__(self, url, raw):
        super().__init__(raw)
        self.url = url
        self.headers = {'Content-Length': str(len(raw))}

    def geturl(self):
        return self.url


class RepairProvider(ModrinthProvider):
    """Only remote metadata/transport is fake; acquisition verification is real."""
    def __init__(self):
        super().__init__(transport=self.transport_fixture)
        self.records, self.bodies, self.identifications = {}, {}, {}
        self.unavailable, self.failed_downloads = set(), set()

    def add(self, project, raw=None, *, ident=None, dependencies=None, **extra):
        ident = ident or project + '-v'
        raw = raw if raw is not None else archive(project)
        filename = ident + '.jar'
        url = 'https://cdn.modrinth.com/data/' + project + '/versions/' + ident + '/' + filename
        record = {'id': ident, 'project_id': project, 'name': project,
                  'version_number': '1.0.0', 'game_versions': ['1.20.1'],
                  'loaders': ['fabric'], 'version_type': 'release',
                  'date_published': '2026-01-01T00:00:00Z', 'modIds': [project],
                  'dependencies': dependencies or [], 'files': [{
                      'filename': filename, 'url': url, 'size': len(raw), 'primary': True,
                      'hashes': {'sha512': hashlib.sha512(raw).hexdigest(),
                                 'sha1': hashlib.sha1(raw).hexdigest()}}]}
        record.update(extra)
        self.records[ident], self.bodies[url] = record, raw
        self.identifications[hashlib.sha512(raw).hexdigest()] = ident
        return record

    def version(self, ident):
        if ident in self.unavailable:
            raise ProviderError('provider_http', 'Fixture metadata is unavailable')
        return copy.deepcopy(self.records[ident])

    def project(self, ident):
        if ident in self.unavailable:
            raise ProviderError('provider_http', 'Fixture metadata is unavailable')
        return {'id': ident, 'title': ident, 'project_type': 'mod', 'client_side': 'optional'}

    def versions(self, project, minecraft, loaders):
        return [self.version(ident) for ident, record in self.records.items()
                if record['project_id'] == project]

    def identify(self, hashes):
        if 'identify' in self.unavailable:
            raise ProviderError('provider_http', 'Fixture identification is unavailable')
        ident = self.identifications.get(hashes.get('sha512'))
        return self.version(ident) if ident else None

    def transport_fixture(self, request, timeout=30):
        if request.full_url in self.failed_downloads:
            raise OSError('Fixture acquisition failure')
        return FixtureResponse(request.full_url, self.bodies[request.full_url])


class DependencyRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.game, self.state, self.data = (self.base / name for name in ('game', 'state', 'data'))
        for path in (self.game, self.state, self.data):
            path.mkdir()
        self.root = self.game / 'instances' / 'repair'
        self.root.mkdir(parents=True)
        self.provider = RepairProvider()
        self.profile = {'name': 'Repair fixture', 'type': 'custom',
                        'gameDir': str(self.root), 'lastVersionId': '1.20.1'}
        self.service = InstallationService(str(self.game), str(self.state), str(self.data),
            profiles=lambda: {'repair': self.profile}, save_profile=lambda *_: self.fail('Unexpected profile write'),
            context=lambda _: {'minecraft': '1.20.1', 'loader': 'fabric', 'loaderVersion': '0.16.5'},
            provider=self.provider)
        self.store = ContentStore(self.root, self.data, 'repair')

    def install(self, project, *, ident=None, raw=None, managed=True, enabled=True, ownership=None, filename=None):
        ident = ident or project + '-v'
        record = self.provider.records.get(ident)
        raw = raw if raw is not None else self.provider.bodies[record['files'][0]['url']]
        filename = filename or ident + '.jar'
        source = self.base / ('source-' + filename)
        source.write_bytes(raw)
        metadata = {'provider': 'modrinth', 'projectId': project, 'versionId': ident} if managed else {}
        if ownership is not None:
            metadata.update(ownership)
        result = self.store.transact([{'action': 'install', 'kind': 'mods', 'filename': filename,
                                     'source': str(source), 'metadata': metadata}])
        item = next(item for item in result['items'] if item['filename'] == filename)
        if not enabled:
            item_id = item['id']
            result = self.store.transact([{'action': 'toggle', 'id': item_id, 'enabled': False}])
            item = next(item for item in result['items'] if item['id'] == item_id)
        return item

    def repair(self, **extra):
        body = {'kind': 'mods', 'repairDependencies': True, 'revision': self.store.inventory()['revision']}
        body.update(extra)
        try:
            return self.service.post('repair', 'plan', body)
        except ServiceError as error:
            if error.code == 'invalid_selection' and 'Select a compatible content version' in str(error):
                self.fail('Repair mode incorrectly requires a catalog selection')
            raise

    def apply(self, plan):
        ack = self.service.post('repair', 'apply', {'planId': plan['planId']})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = self.service.get('repair', 'job', {'jobId': ack['jobId']})['job']
            if job['status'] in ('failed', 'succeeded'):
                return job
            time.sleep(.01)
        self.fail('Disposable repair job did not finish')

    def tree(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes() for path in (self.root / 'mods').glob('*')}

    def chain(self):
        self.provider.add('main', archive('main', depends={'helper': '*'}), dependencies=[required('helper', 'helper-v')])
        self.provider.add('helper', archive('helper', depends={'leaf': '*'}), dependencies=[required('leaf', 'leaf-v')])
        self.provider.add('leaf')
        return self.install('main', ownership={'userRequested': True, 'requiredBy': []})

    # Catches missing repair dispatch and silent root upgrades; real downloads/commit must add only libraries.
    def test_managed_root_repairs_required_transitive_libraries_atomically(self):
        root = self.chain()
        before = self.tree()
        plan = self.repair()
        self.assertEqual(plan['dependencyRepair'], {'checked': 1, 'unidentified': []})
        self.assertFalse(plan['conflicts'], plan)
        self.assertEqual({file['versionId'] for file in plan['files']}, {'helper-v', 'leaf-v'})
        self.assertEqual(plan['remove'], [])
        self.assertEqual(self.tree(), before)
        self.assertEqual(plan['reuseMetadata'][root['id']]['userRequested'], True)
        result = self.apply(plan)
        self.assertEqual(result['status'], 'succeeded', result)
        items = {item['projectId']: item for item in self.store.inventory()['items']}
        self.assertEqual(set(items), {'main', 'helper', 'leaf'})
        self.assertEqual((self.root / 'mods' / 'main-v.jar').read_bytes(), before['mods/main-v.jar'])
        self.assertFalse(items['helper']['userRequested'])
        self.assertEqual(items['helper']['requiredBy'], ['main-v'])
        history = list((self.root / '.launcher-content' / 'history').glob('*/transaction.json'))
        self.assertEqual(len(history), 2)  # One fixture install, one complete repair.

    # Catches assuming real catalog version records expose the archive's module IDs.
    def test_real_catalog_shape_defers_missing_module_identity_until_verified_apply(self):
        self.chain()
        for record in self.provider.records.values():
            record.pop('modIds', None)
        plan = self.repair()
        self.assertFalse(plan['conflicts'], plan)
        self.assertEqual({file['versionId'] for file in plan['files']}, {'helper-v', 'leaf-v'})
        self.assertIn('archive_requirement_pending', {warning.get('code') for warning in plan['warnings']})
        result = self.apply(plan)
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual({item['modIds'][0] for item in self.store.inventory()['items']}, {'main', 'helper', 'leaf'})

    # Catches deferral allowing provider-required bytes that cannot satisfy the actual root requirement.
    def test_deferred_module_requirement_still_blocks_commit_when_archive_does_not_provide_it(self):
        self.provider.add('main', archive('main', depends={'needed_module': '*'}),
                          dependencies=[required('helper', 'helper-v')])
        self.provider.add('helper', archive('different_module'))
        for record in self.provider.records.values():
            record.pop('modIds', None)
        self.install('main')
        before, revision = self.tree(), self.store.inventory()['revision']
        plan = self.repair()
        self.assertFalse(plan['conflicts'], plan)
        result = self.apply(plan)
        self.assertEqual(result['code'], 'archive_dependency_conflict', result)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.store.inventory()['revision'], revision)

    # Catches seeding dependencies as user choices and acquiring the same shared artifact twice.
    def test_shared_library_deduplicates_and_preserves_seeded_root_ownership(self):
        self.provider.add('first', dependencies=[required('shared', 'shared-v')])
        self.provider.add('second', dependencies=[required('shared', 'shared-v')])
        self.provider.add('shared')
        first = self.install('first', ownership={'userRequested': False, 'requiredBy': ['original-owner']})
        self.install('second', ownership={'userRequested': True, 'requiredBy': []})
        plan = self.repair()
        self.assertFalse(plan['conflicts'], plan)
        self.assertEqual([file['versionId'] for file in plan['files']], ['shared-v'])
        self.assertEqual(plan['files'][0]['requiredBy'], ['first-v', 'second-v'])
        self.assertFalse(plan['reuseMetadata'][first['id']]['userRequested'])
        self.assertEqual(plan['reuseMetadata'][first['id']]['requiredBy'], ['original-owner'])
        self.assertEqual(self.apply(plan)['status'], 'succeeded')
        items = {item['projectId']: item for item in self.store.inventory()['items']}
        self.assertEqual(items['first']['requiredBy'], ['original-owner'])
        self.assertFalse(items['first']['userRequested'])

    # Catches guessing provider identity or missing in-memory evidence at verified apply.
    def test_hash_identified_local_root_is_reused_and_catalog_evidence_commits(self):
        self.provider.add('main', archive('actual_main', depends={'helper': '*'}),
                          dependencies=[required('helper', 'helper-v')], modIds=['catalog_guess'])
        self.provider.add('helper')
        root = self.install('main', managed=False, filename='my-local.jar')
        before = self.tree()
        plan = self.repair(ids=[root['id']])
        self.assertFalse(plan['conflicts'], plan)
        self.assertEqual([file['versionId'] for file in plan['files']], ['helper-v'])
        self.assertEqual(self.tree(), before)
        self.assertNotIn('projectId', self.store.inventory()['items'][0])
        result = self.apply(plan)
        self.assertEqual(result['status'], 'succeeded', result)
        root = next(item for item in self.store.inventory()['items'] if item['filename'] == 'my-local.jar')
        self.assertEqual(root['projectId'], 'main')
        self.assertEqual(root['modIds'], ['actual_main'])

    # Catches applying reuse metadata too late: new helper must recognize already local anchor as its exact provider.
    def test_new_library_can_depend_on_hash_identified_local_existing_library(self):
        self.provider.add('main', dependencies=[required('helper', 'helper-v')])
        self.provider.add('helper', dependencies=[required('anchor', 'anchor-v')])
        self.provider.add('anchor')
        self.install('main')
        self.install('anchor', managed=False, filename='local-anchor.jar')
        plan = self.repair()
        self.assertFalse(plan['conflicts'], plan)
        self.assertEqual([file['versionId'] for file in plan['files']], ['helper-v'])
        result = self.apply(plan)
        self.assertEqual(result['status'], 'succeeded', result)

    # Catches substituting a release primary when identify refers to another exact artifact in that release.
    def test_identified_non_primary_root_keeps_exact_local_artifact(self):
        record = self.provider.add('main', archive('primary_root'), dependencies=[required('helper', 'helper-v')])
        local_raw = archive('alternate_root')
        alternate = dict(record['files'][0], filename='alternate.jar', primary=False,
                         url='https://cdn.modrinth.com/data/main/alternate.jar', size=len(local_raw),
                         hashes={'sha512': hashlib.sha512(local_raw).hexdigest(), 'sha1': hashlib.sha1(local_raw).hexdigest()})
        record['files'].append(alternate)
        self.provider.identifications[alternate['hashes']['sha512']] = 'main-v'
        self.provider.add('helper')
        self.install('main', raw=local_raw, managed=False, filename='alternate-local.jar')
        before = self.tree()
        plan = self.repair()
        self.assertFalse(plan['conflicts'], plan)
        self.assertEqual([file['versionId'] for file in plan['files']], ['helper-v'])
        self.assertEqual(self.apply(plan)['status'], 'succeeded')
        self.assertEqual((self.root / 'mods' / 'alternate-local.jar').read_bytes(), before['mods/alternate-local.jar'])
        self.assertEqual(self.provider.records['main-v']['files'][0]['filename'], 'main-v.jar')
        repeated = self.repair()
        self.assertFalse(repeated['conflicts'], repeated)
        self.assertEqual(repeated['files'], [])
        self.assertEqual(repeated['remove'], [])
        self.assertEqual(self.apply(repeated)['status'], 'succeeded')
        root = next(item for item in self.store.inventory()['items'] if item['filename'] == 'alternate-local.jar')
        self.assertEqual(root['projectId'], 'main')
        self.assertEqual(root['versionId'], 'main-v')
        self.assertEqual((self.root / 'mods' / 'alternate-local.jar').read_bytes(), before['mods/alternate-local.jar'])

    # Catches forged identify responses granting identities despite disagreeing file hashes.
    def test_identification_with_one_forged_supported_hash_is_unidentified(self):
        record = self.provider.add('main')
        root = self.install('main', managed=False)
        record['files'][0]['hashes']['sha1'] = '0' * 40
        plan = self.repair()
        self.assertEqual(plan['files'], [])
        self.assertEqual(plan['dependencyRepair']['checked'], 1)
        self.assertEqual([item['id'] for item in plan['dependencyRepair']['unidentified']], [root['id']])
        self.assertNotIn('projectId', self.store.inventory()['items'][0])

    # Catches missing pinned-root integrity checks and offering an upgrade disguised as repair.
    def test_managed_root_hash_mismatch_blocks_application(self):
        record = self.provider.add('main')
        self.install('main', raw=archive('main', version='2.0.0'))
        before = self.tree()
        plan = self.repair()
        self.assertIn('root_hash_mismatch', {problem['code'] for problem in plan['conflicts']})
        with self.assertRaises(ServiceError) as caught:
            self.apply(plan)
        self.assertEqual(caught.exception.code, 'dependency_conflict')
        self.assertEqual(self.tree(), before)
        self.assertEqual(record['id'], 'main-v')

    # Catches replacement of an installed dependency with an exact demanded version.
    def test_conflicting_installed_dependency_version_blocks_repair(self):
        self.provider.add('main', dependencies=[required('helper', 'helper-new')])
        self.provider.add('helper', ident='helper-old')
        self.provider.add('helper', archive('helper', version='2.0.0'), ident='helper-new')
        self.install('main')
        helper = self.install('helper', ident='helper-old')
        before = self.tree()
        plan = self.repair()
        self.assertIn('installed_replacement', {problem['code'] for problem in plan['conflicts']})
        self.assertIn(helper['id'], plan['remove'])
        with self.assertRaises(ServiceError):
            self.apply(plan)
        self.assertEqual(self.tree(), before)

    # Catches enabling a disabled selection solely because it was explicitly selected.
    def test_disabled_roots_are_excluded_but_required_disabled_library_is_enabled(self):
        self.provider.add('main', dependencies=[required('helper', 'helper-v')])
        self.provider.add('helper')
        self.provider.add('unrelated', dependencies=[required('missing', 'missing-v')])
        main = self.install('main')
        helper = self.install('helper', enabled=False, ownership={'userRequested': False, 'requiredBy': ['old-owner']})
        unrelated = self.install('unrelated', enabled=False)
        empty = self.repair(ids=[unrelated['id']])
        self.assertEqual(empty['dependencyRepair']['checked'], 0)
        self.assertEqual(empty['files'], [])
        self.assertEqual(empty['enable'], [])
        plan = self.repair(ids=[main['id'], unrelated['id']])
        self.assertEqual(plan['dependencyRepair']['checked'], 1)
        self.assertEqual(plan['enable'], [helper['id']])
        self.assertFalse(plan['conflicts'], plan)
        result = self.apply(plan)
        self.assertEqual(result['status'], 'succeeded', result)
        items = {item['projectId']: item for item in self.store.inventory()['items']}
        self.assertTrue(items['helper']['enabled'])
        self.assertFalse(items['unrelated']['enabled'])
        self.assertFalse(items['helper']['userRequested'])

    # Catches accidental optional acquisition, even if a caller requests the catalog optional toggle.
    def test_optional_dependencies_are_excluded_from_repair(self):
        self.provider.add('main', dependencies=[required('optional', 'optional-v', 'optional')])
        self.provider.add('optional')
        self.install('main')
        plan = self.repair(includeOptional=True)
        self.assertFalse(plan['conflicts'], plan)
        self.assertEqual(plan['files'], [])

    # Catches empty/malformed IDs becoming a broad all-mods operation, and stale/missing revision acceptance.
    def test_invalid_scope_and_revision_never_broaden_to_all_roots(self):
        self.chain()
        for ids in ([], 'main-v', None, [None], ['missing-id'], ['x' * 513], ['a'] * 257):
            with self.subTest(ids=repr(ids)[:60]):
                with self.assertRaises(ServiceError) as caught:
                    self.repair(ids=ids, selections=[{'versionId': 'main-v'}])
                self.assertEqual(caught.exception.status, 400)
        for revision in (None, '', 0, 'stale'):
            with self.subTest(revision=revision):
                with self.assertRaises(ServiceError) as caught:
                    self.repair(revision=revision, selections=[{'versionId': 'main-v'}])
                self.assertIn(caught.exception.status, (400, 409))
        with self.assertRaises(ServiceError):
            self.service.post('repair', 'plan', {'kind': 'mods', 'repairDependencies': True,
                                               'selections': [{'versionId': 'main-v'}]})
        self.assertEqual(len(self.store.inventory()['items']), 1)

    # Catches a repair-all scope being accidentally used for a single-file request.
    def test_selected_scope_resolves_only_that_roots_provider_dependencies(self):
        main = self.chain()
        self.provider.add('other', dependencies=[required('other_helper', 'other_helper-v')])
        self.provider.add('other_helper')
        self.install('other')
        plan = self.repair(ids=[main['id']])
        self.assertEqual(plan['dependencyRepair']['checked'], 1)
        self.assertEqual({file['versionId'] for file in plan['files']}, {'helper-v', 'leaf-v'})

    # Catches no roots being an invalid-selection exception and unknown archives losing useful diagnostics.
    def test_empty_or_unknown_installation_returns_truthful_diagnostic_plan(self):
        empty = self.repair()
        self.assertEqual(empty['dependencyRepair'], {'checked': 0, 'unidentified': []})
        self.assertEqual(empty['files'], [])
        self.provider.add('unknown')
        root = self.install('unknown', managed=False, raw=archive('unmatched'))
        plan = self.repair()
        self.assertEqual(plan['files'], [])
        self.assertEqual([item['id'] for item in plan['dependencyRepair']['unidentified']], [root['id']])
        self.assertTrue(plan['warnings'])
        self.assertFalse(plan['conflicts'], plan)

    # Catches treating unknown local hard requirements as a safe/complete repair.
    def test_unknown_root_hard_requirement_blocks_otherwise_known_repair(self):
        self.chain()
        self.provider.add('unknown')
        unknown = self.install('unknown', managed=False, raw=archive('unmatched', depends={'helper': '*'}))
        for record in self.provider.records.values():
            record.pop('modIds', None)
        plan = self.repair()
        self.assertIn('missing_dependency', {problem['code'] for problem in plan['conflicts']})
        self.assertTrue(any(problem.get('itemId') == unknown['id'] for problem in plan['conflicts']))
        with self.assertRaises(ServiceError):
            self.apply(plan)

    # Catches using catalog aliases to override actual archive IDs or archive-side client exclusion.
    def test_actual_root_metadata_remains_authoritative_during_review(self):
        self.provider.add('main', archive('main', environment='server'), modIds=['imaginary'])
        self.install('main', managed=False)
        plan = self.repair()
        self.assertIn('client_unsupported', {problem['code'] for problem in plan['conflicts']})
        with self.assertRaises(ServiceError):
            self.apply(plan)

    # Catches provider failures being mistaken for a valid empty repair with Apply enabled.
    def test_provider_lookup_failure_is_retryable_and_preserves_installation(self):
        self.chain()
        before = self.tree()
        self.provider.unavailable.add('main-v')
        try:
            failed = self.repair()
        except ServiceError as error:
            self.assertIn(error.status, (409, 502, 503))
        else:
            self.assertTrue(failed['conflicts'], failed)
            with self.assertRaises(ServiceError):
                self.apply(failed)
        self.provider.unavailable.clear()
        retry = self.repair()
        self.assertFalse(retry['conflicts'], retry)
        self.assertEqual(self.tree(), before)

    # Catches failed verified transport acquisition partially committing a library or ownership changes.
    def test_download_hash_failure_preserves_all_existing_files_and_metadata(self):
        self.chain()
        plan = self.repair()
        before, revision = self.tree(), self.store.inventory()['revision']
        self.provider.bodies[self.provider.records['leaf-v']['files'][0]['url']] = b'wrong download bytes'
        result = self.apply(plan)
        self.assertEqual(result['status'], 'failed', result)
        self.assertIn(result['code'], ('hash_mismatch', 'size_mismatch'))
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.store.inventory()['revision'], revision)

    # Catches trusting provider dependencies over an acquired archive's undisclosed hard requirements.
    def test_acquired_archive_validation_failure_preserves_existing_installation(self):
        self.provider.add('main', dependencies=[required('helper', 'helper-v')])
        self.provider.add('helper', archive('helper', depends={'undisclosed': '*'}))
        self.install('main')
        plan = self.repair()
        self.assertFalse(plan['conflicts'], plan)
        before, revision = self.tree(), self.store.inventory()['revision']
        result = self.apply(plan)
        self.assertEqual(result['code'], 'archive_dependency_conflict', result)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.store.inventory()['revision'], revision)

    # Catches abandoning already committed new files after a later atomic transaction operation fails.
    def test_commit_failure_rolls_back_preceding_library_install(self):
        self.chain()
        plan = self.repair()
        before, revision = self.tree(), self.store.inventory()['revision']
        replace = os.replace
        failed = False

        def fail_leaf_commit(source, destination):
            nonlocal failed
            if not failed and Path(destination).name == 'leaf-v.jar' and Path(destination).parent.name == 'mods':
                failed = True
                raise OSError('Fixture commit failure after helper installation')
            return replace(source, destination)

        with mock.patch('instance_content.os.replace', side_effect=fail_leaf_commit):
            result = self.apply(plan)
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['code'], 'commit_failed', result)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.store.inventory()['revision'], revision)
        receipts = list((self.root / '.launcher-content' / 'history').glob('*/result.json'))
        self.assertIn('rolled_back', {json.loads(path.read_text())['status'] for path in receipts})


if __name__ == '__main__':
    unittest.main()
