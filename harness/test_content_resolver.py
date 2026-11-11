import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from content_resolver import matches_requirement, resolve_plan, validate_inventory
from instance_content import inspect_file


CONTEXT = {'minecraft': '1.20.1', 'loader': 'fabric', 'javaMajor': 17}


def version(project, ident=None, dependencies=None, **extra):
    ident = ident or project + '-v'
    result = {'id': ident, 'project_id': project, 'name': project, 'version_number': '1.0.0',
              'game_versions': ['1.20.1'], 'loaders': ['fabric'], 'version_type': 'release',
              'dependencies': dependencies or [], 'files': [{'filename': ident + '.jar',
              'url': 'https://cdn.modrinth.com/data/' + project + '/versions/' + ident + '/' + ident + '.jar',
              'size': 7, 'hashes': {'sha512': hashlib.sha512(ident.encode()).hexdigest()}, 'primary': True}]}
    result.update(extra)
    return result


def dependency(project=None, ident=None, relation='required', **extra):
    return dict({'project_id': project, 'version_id': ident, 'dependency_type': relation}, **extra)


def local(ident, modids, **extra):
    result = {'id': ident, 'kind': 'mods', 'filename': ident + '.jar', 'path': 'mods/' + ident + '.jar',
              'modIds': modids, 'version': '1.0.0', 'enabled': True, 'depends': {}, 'conflicts': {}, 'hashes': {}}
    result.update(extra)
    return result


class FakeProvider:
    name = 'modrinth'

    def __init__(self, records, projects=None):
        self.records = {item['id']: item for item in records}
        self.projects = projects or {}
        self.calls = []

    def version(self, ident):
        self.calls.append(('version', ident))
        return copy.deepcopy(self.records[ident])

    def project(self, ident):
        return copy.deepcopy(self.projects.get(ident, {'id': ident, 'project_type': 'mod', 'client_side': 'optional'}))

    def versions(self, ident, minecraft, loaders):
        self.calls.append(('versions', ident))
        return [copy.deepcopy(item) for item in self.records.values() if item['project_id'] == ident]


class ResolverTests(unittest.TestCase):
    def test_original_project_logo_follows_exact_dependency_plan_and_reuse_metadata(self):
        root = version('root')
        provider = FakeProvider([root], {'root': {'id': 'root', 'title': 'Original project', 'project_type': 'mod',
            'icon_url': 'https://cdn.modrinth.com/data/root/icon.png'}})
        plan = resolve_plan(provider, CONTEXT, [{'versionId': 'root-v'}], [])
        self.assertFalse(plan['conflicts'])
        self.assertEqual(plan['files'][0].get('iconUrl'), 'https://cdn.modrinth.com/data/root/icon.png')
        self.assertEqual(plan['files'][0]['metadata'].get('iconUrl'), 'https://cdn.modrinth.com/data/root/icon.png')
        existing = local('same-file', [], hashes=root['files'][0]['hashes'])
        reused = resolve_plan(provider, CONTEXT, [{'versionId': 'root-v'}], [existing])
        self.assertEqual(reused['reuseMetadata']['same-file'].get('iconUrl'), 'https://cdn.modrinth.com/data/root/icon.png')

    def test_untrusted_optional_logo_does_not_block_compatible_plan(self):
        provider = FakeProvider([version('root')], {'root': {'id': 'root', 'project_type': 'mod',
            'icon_url': 'https://untrusted.test/icon.png'}})
        plan = resolve_plan(provider, CONTEXT, [{'versionId': 'root-v'}], [])
        self.assertFalse(plan['conflicts'])
        self.assertNotIn('iconUrl', plan['files'][0]['metadata'])

    def test_same_hash_reuse_retains_catalog_dependency_and_compatibility_evidence(self):
        root = version('root', dependencies=[dependency('helper', 'helper-v'), dependency('bundled', 'bundled-v', 'embedded')])
        existing = local('same-file', [], hashes=root['files'][0]['hashes'])
        plan = self.plan([root, version('helper')], installed=[existing])
        self.assertFalse(plan['conflicts'])
        metadata = plan['reuseMetadata']['same-file']
        self.assertEqual(metadata.get('providerDependencies'), [dependency('helper', 'helper-v'), dependency('bundled', 'bundled-v', 'embedded')])
        self.assertEqual(metadata.get('gameVersions'), ['1.20.1'])
        self.assertEqual(metadata.get('loaders'), ['fabric'])
        self.assertEqual(metadata.get('embeddedProjects'), ['bundled'])
        self.assertEqual(metadata.get('embeddedVersions'), ['bundled-v'])

    def nested_item(self, descriptor):
        def archive_bytes(metadata):
            metadata = copy.deepcopy(metadata)
            children = metadata.pop('children', [])
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w') as archive:
                if children:
                    metadata['jars'] = [{'file': 'nested/' + str(index) + '.jar'} for index in range(len(children))]
                archive.writestr('fabric.mod.json', json.dumps(dict(schemaVersion=1, **metadata)))
                for index, child in enumerate(children):
                    archive.writestr('nested/' + str(index) + '.jar', archive_bytes(child))
            return stream.getvalue()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / (descriptor['id'] + '.jar')
        path.write_bytes(archive_bytes(descriptor))
        return inspect_file(path)

    def plan(self, records, selections=None, installed=None, **options):
        provider = FakeProvider(records)
        result = resolve_plan(provider, CONTEXT, selections or [{'versionId': records[0]['id']}], installed or [], **options)
        return result

    def codes(self, plan):
        return {conflict['code'] for conflict in plan['conflicts']}

    def test_exact_pinned_required_graph(self):
        root = version('root', dependencies=[dependency('dep', 'dep-old')])
        plan = self.plan([root, version('dep', 'dep-new'), version('dep', 'dep-old')])
        self.assertEqual({file['versionId'] for file in plan['files']}, {'root-v', 'dep-old'})
        self.assertFalse(plan['conflicts'])
        dep = next(file for file in plan['files'] if file['projectId'] == 'dep')
        self.assertEqual(dep['requiredBy'], ['root-v'])
        self.assertFalse(dep['userRequested'])

    def test_project_only_requirement_filters_context_and_channel(self):
        root = version('root', dependencies=[dependency('dep')])
        plan = self.plan([root, version('dep', 'wrong', game_versions=['1.21']),
                          version('dep', 'beta', version_type='beta'), version('dep', 'release')])
        self.assertEqual({file['versionId'] for file in plan['files']}, {'root-v', 'release'})
        self.assertFalse(plan['conflicts'])

    def test_pinned_context_project_mismatch_missing_reference_are_visible(self):
        cases = [(version('root', loaders=['forge']), 'loader_mismatch'),
                 (version('root', dependencies=[dependency('different', 'dep-v')]), 'project_mismatch'),
                 (version('root', dependencies=[dependency('missing', 'missing-v')]), 'missing_dependency')]
        for root, code in cases:
            with self.subTest(code=code):
                self.assertIn(code, self.codes(self.plan([root, version('dep')])))

    def test_optional_choice_and_embedded_evidence(self):
        root = version('root', dependencies=[dependency('optional', relation='optional'), dependency('bundled', relation='embedded')])
        default = self.plan([root, version('optional')])
        self.assertEqual(len(default['files']), 1)
        self.assertTrue(default['warnings'])
        selected = self.plan([root, version('optional')], include_optional=True)
        self.assertEqual(len(selected['files']), 2)
        self.assertFalse(selected['conflicts'])

    def test_local_alias_disabled_requirement_reused_and_enabled(self):
        root = version('root', depends={'local_alias': '>=1.0.0 <2.0.0'})
        installed = local('local-file', ['actual'], provides=['local_alias'], enabled=False)
        plan = self.plan([root], installed=[installed])
        self.assertFalse(plan['conflicts'])
        self.assertEqual(plan['reuse'], ['local-file'])
        self.assertEqual(plan['enable'], ['local-file'])

    def test_local_incompatible_unknown_requirement_and_environment(self):
        for requirement, code in [('>=2.0', 'requirement_mismatch'), ('banana', 'unsupported_requirement')]:
            with self.subTest(requirement=requirement):
                plan = self.plan([version('root', depends={'api': requirement})], installed=[local('api-file', ['api'])])
                self.assertIn(code, self.codes(plan))
        plan = self.plan([version('root', depends={'minecraft': '>=1.21', 'java': '>=21', 'fabricloader': '>=0.16'})])
        self.assertIn('requirement_mismatch', self.codes(plan))
        self.assertIn('unresolved_environment', self.codes(plan))

    def test_both_directions_known_incompatibility(self):
        root = version('root', modIds=['root'], conflicts={'other': '*'})
        installed = local('other-file', ['other'])
        self.assertIn('incompatible', self.codes(self.plan([root], installed=[installed])))
        installed['conflicts'] = {'root': '*'}
        root['conflicts'] = {}
        self.assertIn('incompatible', self.codes(self.plan([root], installed=[installed])))

    def test_cycle_and_graph_limits(self):
        root = version('root', dependencies=[dependency('dep', 'dep-v')])
        dep = version('dep', dependencies=[dependency('root', 'root-v')])
        self.assertIn('dependency_cycle', self.codes(self.plan([root, dep])))
        records = [version('n' + str(index), dependencies=[dependency('n' + str(index + 1), 'n' + str(index + 1) + '-v')]) for index in range(34)]
        records[-1]['dependencies'] = []
        self.assertIn('graph_limit', self.codes(self.plan(records)))
        roots = [version('x' + str(index)) for index in range(257)]
        self.assertIn('graph_limit', self.codes(self.plan(roots, selections=[{'versionId': item['id']} for item in roots])))

    def test_missing_hash_and_unsafe_file_are_conflicts(self):
        for changes in ({'hashes': {}}, {'url': 'https://example.test/file.jar'}, {'filename': 'CON.jar'}):
            root = version('root')
            root['files'][0].update(changes)
            with self.subTest(changes=changes):
                self.assertIn('invalid_file', self.codes(self.plan([root])))

    def test_duplicate_mod_ids_and_embedded_ids_cross_provider(self):
        root = version('root', modIds=['root'], embeddedModIds=['api'])
        installed = local('foreign', ['api'], provider='other', projectId='foreign')
        self.assertIn('duplicate_identity', self.codes(self.plan([root], installed=[installed])))

    def test_same_hash_retry_reuses_and_enables(self):
        root = version('root')
        installed = local('present', [], hashes=root['files'][0]['hashes'], enabled=False)
        plan = self.plan([root], installed=[installed])
        self.assertEqual(plan['files'], [])
        self.assertEqual(plan['reuse'], ['present'])
        self.assertEqual(plan['enable'], ['present'])

    def test_whole_update_set_honors_pinned_dependency_in_both_orders(self):
        root = version('root', 'root-new', dependencies=[dependency('dep', 'dep-old')])
        records = [root, version('dep', 'dep-old'), version('dep', 'dep-new')]
        for selections in ([{'versionId': 'root-new'}, {'versionId': 'dep-new'}], [{'versionId': 'dep-new'}, {'versionId': 'root-new'}]):
            with self.subTest(selections=selections):
                self.assertIn('version_conflict', self.codes(self.plan(records, selections=selections)))

    def test_replacement_validates_remaining_reverse_dependents(self):
        root = version('api', version_number='2.0.0', modIds=['api'])
        old = local('api-old', ['api'], provider='modrinth', projectId='api', versionId='old')
        dependent = local('dependent', ['dependent'], depends={'api': '<2.0.0'})
        plan = self.plan([root], installed=[old, dependent])
        self.assertIn('requirement_mismatch', self.codes(plan))
        self.assertEqual(plan['remove'], ['api-old'])
        self.assertEqual(plan['files'][0]['replaces'], 'api-old')

    def test_numeric_fabric_and_maven_constraints_are_careful(self):
        for actual, requirement, expected in [('1.2.3', '>=1.2 <2.0', True), ('2.0', '[1.0,2.0)', False),
                                               ('1.2.9', '1.2.x', True), ('1.3', '~1.2.0', False),
                                               ('1.9', '^1.2.0', True), ('1.2', ['<1.0', '>=1.2'], True),
                                               ('1.2.3', '1.2.3', True)]:
            with self.subTest(actual=actual, requirement=requirement):
                self.assertEqual(matches_requirement(actual, requirement), expected)
        for actual, requirement in [('banana', '>=1.0'), ('1.0', 'latest'), ('1.0', '(,1.0],[2.0,)')]:
            with self.subTest(actual=actual, requirement=requirement), self.assertRaises(ValueError):
                matches_requirement(actual, requirement)

    def test_live_fabric_semantic_versions_include_build_and_prerelease_bounds(self):
        cases = [('6.0.4+06488ac19c', '>=6.0.4', True), ('6.0.4+other', '=6.0.4+06488ac19c', True),
                 ('6.0.3+06488ac19c', '>=6.0.4', False), ('26.2', '>=26.1-rc.2', True),
                 ('26.1-rc.1', '>=26.1-rc.2', False), ('26.2-alpha.1', '~26.2-', True),
                 ('26.3-alpha.1', '~26.2-', False), ('26.2-', '>=26.2-', True),
                 ('26.2.0-alpha', '>=26.2.0-', True), ('4.12.4+26.3', '>=4.12.4+26.2', True),
                 ('0.9.2+mc26.2', '=0.9.2+mc26.2', True), ('26.3-alpha.2', '>=26.2 <26.3-alpha.3', True),
                 ('26.3-alpha.3', '>=26.2 <26.3-alpha.3', False), ('1.21-4.5', '<=1.21-4.5', True),
                 ('1.21-4.10', '<=1.21-4.5', False), ('1.0-beta', '>=1.0', False),
                 ('1.2.0-beta', '1.2.x', True)]
        for actual, requirement, expected in cases:
            with self.subTest(actual=actual, requirement=requirement):
                self.assertEqual(matches_requirement(actual, requirement), expected)

    def test_fabric_caret_and_tilde_follow_loader_rules_including_zero_major(self):
        for actual, requirement, expected in [('0.9.0', '^0.2.0', True), ('1.0', '^0.2.0', False),
                                               ('1.0.9', '~1', True), ('1.1', '~1', False),
                                               ('1.3-alpha', '~1.2', False)]:
            with self.subTest(actual=actual, requirement=requirement):
                self.assertEqual(matches_requirement(actual, requirement), expected)

    def test_malformed_semantic_requirements_remain_explicitly_unsupported(self):
        for requirement in ('>=1.0-alpha..1', '>=1.0-beta_1', '>=1..2', '>=1.2.x', '1.x.3', '1.2.x-alpha', '>='):
            with self.subTest(requirement=requirement), self.assertRaises(ValueError):
                matches_requirement('1.2.3', requirement)

    def test_maven_qualifiers_remain_explicitly_unsupported_in_numeric_range_scope(self):
        # Maven considers "final" a release alias and build data participates
        # in ordering; neither can be checked by Fabric's SemVer comparator.
        for actual, requirement in [('1.0-final', '[1.0]'), ('1.0+mc26.2', '[1.0,2.0)'),
                                     ('1.0', '[1.0-rc.1,2.0)')]:
            with self.subTest(actual=actual, requirement=requirement), self.assertRaises(ValueError):
                matches_requirement(actual, requirement)

    def test_neutral_store_relations_reuse_enabled_local_provider(self):
        root = version('root', depends=[{'id': 'api', 'requirement': '>=1.0'}])
        plan = self.plan([root], installed=[local('api-file', ['api'])])
        self.assertFalse(plan['conflicts'])
        self.assertEqual(plan['reuse'], ['api-file'])

    def test_disabled_local_requirement_checks_its_entire_graph(self):
        root = version('root', modIds=['root'], depends=[{'id': 'api', 'requirement': '*'}])
        api = local('api-file', ['api'], enabled=False, depends=[{'id': 'helper', 'requirement': '[1,2)'}])
        helper = local('helper-file', ['helper'], enabled=False, conflicts=[{'id': 'root', 'requirement': '*'}])
        plan = self.plan([root], installed=[api, helper])
        self.assertEqual(set(plan['enable']), {'api-file', 'helper-file'})
        self.assertIn('incompatible', self.codes(plan))

    def test_local_cycle_is_jointly_satisfied(self):
        root = version('root', modIds=['root'], depends={'api': '*'})
        api = local('api-file', ['api'], depends={'root': '*'})
        self.assertFalse(self.plan([root], installed=[api])['conflicts'])

    def test_satisfied_final_cycle_still_enforces_every_version_and_missing_edge(self):
        one = local('one', ['one'], version='2.0.0', depends={'two': '>=3.0'})
        two = local('two', ['two'], version='3.1.0', depends={'one': '>=2.0'})
        provider = FakeProvider([])
        self.assertFalse(validate_inventory(provider, CONTEXT, [one, two])['conflicts'])
        two['version'] = '2.9.0'
        result = validate_inventory(provider, CONTEXT, [one, two])
        self.assertIn('requirement_mismatch', self.codes(result))
        two['version'] = '3.1.0'
        two['depends']['missing'] = '*'
        self.assertIn('missing_dependency', self.codes(validate_inventory(provider, CONTEXT, [one, two])))

    def test_shared_embedded_modules_are_not_duplicate_root_mods(self):
        one = local('one-file', ['one', 'fabric-api-base'], rootModIds=['one'], embeddedModIds=['fabric-api-base'])
        two = local('two-file', ['two', 'fabric-api-base'], rootModIds=['two'], embeddedModIds=['fabric-api-base'])
        self.assertFalse(validate_inventory(FakeProvider([]), CONTEXT, [one, two])['conflicts'])
        two['rootModIds'] = ['one']
        self.assertIn('duplicate_identity', self.codes(validate_inventory(FakeProvider([]), CONTEXT, [one, two])))

    def test_exact_reuse_preserves_root_and_embedded_mod_classification(self):
        root = version('root')
        existing = local('present', ['root', 'fabric-api-base'], hashes=root['files'][0]['hashes'],
                         rootModIds=['root'], embeddedModIds=['fabric-api-base'])
        other = local('other', ['other', 'fabric-api-base'], rootModIds=['other'], embeddedModIds=['fabric-api-base'])
        self.assertFalse(self.plan([root], installed=[existing, other])['conflicts'])

    def test_unused_nested_version_does_not_require_its_own_helper(self):
        items = [self.nested_item({'id': name, 'version': '1.0', 'depends': {'shared_api': '=1.0'},
                    'children': [{'id': 'shared_api', 'version': embedded_version, 'depends': {'helper': '=' + embedded_version}}]})
                 for name, embedded_version in (('one', '1.0'), ('two', '2.0'))]
        items.append(self.nested_item({'id': 'helper', 'version': '1.0'}))
        self.assertFalse(validate_inventory(FakeProvider([]), CONTEXT, items)['conflicts'])

    def test_nested_versions_must_satisfy_all_root_demands_together(self):
        items = [self.nested_item({'id': name, 'version': '1.0', 'depends': {'shared_api': '=' + embedded_version},
                    'children': [{'id': 'shared_api', 'version': embedded_version}]})
                 for name, embedded_version in (('one', '1.0'), ('two', '2.0'))]
        result = validate_inventory(FakeProvider([]), CONTEXT, items)
        self.assertTrue(result['conflicts'])
        self.assertIn('version_conflict', self.codes(result))

    def test_two_nested_versions_in_one_file_preserve_independent_relations(self):
        bundle = self.nested_item({'id': 'bundle', 'version': '1.0', 'depends': {'shared_api': '=1.0'},
            'children': [{'id': 'shared_api', 'version': '1.0', 'depends': {'helper': '=1.0'}},
                         {'id': 'shared_api', 'version': '2.0', 'depends': {'helper': '=2.0'}}]})
        helper = self.nested_item({'id': 'helper', 'version': '1.0'})
        self.assertFalse(validate_inventory(FakeProvider([]), CONTEXT, [bundle, helper])['conflicts'])

    def test_nested_selection_backtracks_when_newest_candidate_lacks_helper(self):
        bundle = self.nested_item({'id': 'bundle', 'version': '1.0', 'depends': {'shared_api': '*'},
            'children': [{'id': 'shared_api', 'version': '2.0', 'depends': {'helper': '=2.0'}},
                         {'id': 'shared_api', 'version': '1.0', 'depends': {'helper': '=1.0'}}]})
        helper = self.nested_item({'id': 'helper', 'version': '1.0'})
        self.assertFalse(validate_inventory(FakeProvider([]), CONTEXT, [bundle, helper])['conflicts'])

    def test_selected_nested_candidate_missing_helper_still_blocks(self):
        bundle = self.nested_item({'id': 'bundle', 'version': '1.0', 'depends': {'shared_api': '=1.0'},
            'children': [{'id': 'shared_api', 'version': '1.0', 'depends': {'missing': '*'}}]})
        result = validate_inventory(FakeProvider([]), CONTEXT, [bundle])
        self.assertIn('missing_dependency', self.codes(result))

    def test_direct_root_version_cannot_be_replaced_by_bundled_candidate(self):
        bundle = self.nested_item({'id': 'bundle', 'version': '1.0', 'depends': {'shared_api': '=1.0'},
            'children': [{'id': 'shared_api', 'version': '1.0'}]})
        direct = self.nested_item({'id': 'shared_api', 'version': '2.0'})
        self.assertTrue(validate_inventory(FakeProvider([]), CONTEXT, [bundle, direct])['conflicts'])

    def test_selecting_nested_descendant_also_checks_its_parent_dependencies(self):
        bundle = self.nested_item({'id': 'bundle', 'version': '1.0', 'depends': {'deep_api': '*'},
            'children': [{'id': 'intermediate', 'version': '1.0', 'depends': {'missing': '*'},
                          'children': [{'id': 'deep_api', 'version': '1.0'}]}]})
        self.assertIn('missing_dependency', self.codes(validate_inventory(FakeProvider([]), CONTEXT, [bundle])))

    def test_unselected_unknown_nested_version_does_not_hide_supported_candidate(self):
        bundle = self.nested_item({'id': 'bundle', 'version': '1.0', 'depends': {'shared_api': '>=1.0'},
            'children': [{'id': 'shared_api', 'version': 'unknown build format'}, {'id': 'shared_api', 'version': '1.0'}]})
        self.assertFalse(validate_inventory(FakeProvider([]), CONTEXT, [bundle])['conflicts'])

    def test_unused_nested_breaks_do_not_apply_but_selected_breaks_do(self):
        bundle = self.nested_item({'id': 'bundle', 'version': '1.0', 'depends': {'shared_api': '=1.0'},
            'children': [{'id': 'shared_api', 'version': '1.0'},
                         {'id': 'shared_api', 'version': '2.0', 'breaks': {'helper': '*'}}]})
        helper = self.nested_item({'id': 'helper', 'version': '1.0'})
        self.assertFalse(validate_inventory(FakeProvider([]), CONTEXT, [bundle, helper])['conflicts'])
        bundle = self.nested_item({'id': 'bundle', 'version': '1.0', 'depends': {'shared_api': '=2.0'},
            'children': [{'id': 'shared_api', 'version': '2.0', 'breaks': {'helper': '*'}}]})
        self.assertIn('incompatible', self.codes(validate_inventory(FakeProvider([]), CONTEXT, [bundle, helper])))

    def test_canonical_inventory_enables_needed_disabled_root_and_excludes_unused_nested_edges(self):
        from content_resolver import canonical_inventory
        bundle = self.nested_item({'id': 'bundle', 'version': '1.0', 'depends': {'shared_api': '=1.0'},
            'children': [{'id': 'shared_api', 'version': '1.0', 'depends': {'helper': '=1.0'}},
                         {'id': 'shared_api', 'version': '2.0', 'depends': {'unused': '*'}}]})
        helper = self.nested_item({'id': 'helper', 'version': '1.0'})
        helper['enabled'] = False
        original = copy.deepcopy([bundle, helper])
        result = canonical_inventory(CONTEXT, [bundle, helper])
        self.assertFalse(result['conflicts'])
        self.assertEqual(result['enable'], [helper['id']])
        selected = next(item for item in result['items'] if item['id'] == bundle['id'])
        self.assertEqual(selected['providedVersions']['shared_api'], '1.0')
        self.assertNotIn('unused', [relation['id'] for relation in selected['depends']])
        self.assertEqual([bundle, helper], original)

    def test_canonical_inventory_cannot_reenable_an_explicitly_blocked_root(self):
        from content_resolver import canonical_inventory
        consumer = self.nested_item({'id': 'consumer', 'version': '1.0', 'depends': {'helper': '*'}})
        helper = self.nested_item({'id': 'helper', 'version': '1.0'})
        helper['enabled'] = False
        result = canonical_inventory(CONTEXT, [consumer, helper], blocked=[helper['id']])
        self.assertIn('missing_dependency', self.codes(result))
        self.assertEqual(result['enable'], [])
        self.assertEqual([item['id'] for item in result['items']], [consumer['id']])

    def test_compatible_enabled_nested_candidate_precedes_newer_disabled_archive(self):
        from content_resolver import canonical_inventory
        enabled = self.nested_item({'id': 'enabled', 'version': '1.0', 'depends': {'shared_api': '>=1.0'},
            'children': [{'id': 'shared_api', 'version': '1.0'}]})
        disabled = self.nested_item({'id': 'disabled', 'version': '1.0',
            'children': [{'id': 'shared_api', 'version': '2.0'}]})
        disabled['enabled'] = False
        self.assertFalse(validate_inventory(FakeProvider([]), CONTEXT, [enabled, disabled])['conflicts'])
        result = canonical_inventory(CONTEXT, [enabled, disabled])
        self.assertEqual(result['enable'], [])
        self.assertEqual([item['id'] for item in result['items']], [enabled['id']])

    def test_fabric_recommendation_stays_optional_when_loaded_by_quilt(self):
        consumer = self.nested_item({'id': 'consumer', 'version': '1.0', 'recommends': {'api': '>=2.0'}})
        api = self.nested_item({'id': 'api', 'version': '1.0'})
        result = validate_inventory(FakeProvider([]), dict(CONTEXT, loader='quilt'), [consumer, api])
        self.assertFalse(result['conflicts'])
        self.assertIn('optional_local', {warning['code'] for warning in result['warnings']})

    def test_local_depth_bound_is_reported(self):
        root = version('root', depends={'a0': '*'})
        items = [local('a' + str(index), ['a' + str(index)], enabled=False,
                       depends={'a' + str(index + 1): '*'}) for index in range(34)]
        items[-1]['depends'] = {}
        self.assertIn('graph_limit', self.codes(self.plan([root], installed=items)))

    def test_embedded_provider_satisfies_another_roots_required_project(self):
        bundled = version('bundler', dependencies=[dependency('api', 'api-embedded', relation='embedded')])
        consumer = version('consumer', dependencies=[dependency('api', 'api-embedded')])
        plan = self.plan([bundled, consumer], selections=[{'versionId': 'bundler-v'}, {'versionId': 'consumer-v'}])
        self.assertFalse(plan['conflicts'])
        self.assertEqual({file['versionId'] for file in plan['files']}, {'bundler-v', 'consumer-v'})

    def test_installed_embedded_provider_satisfies_required_project(self):
        root = version('root', dependencies=[dependency('api', 'api-v')])
        installed = local('bundle-file', ['bundle', 'api'], embeddedProjects=['api'], embeddedVersions=['api-v'])
        plan = self.plan([root], installed=[installed])
        self.assertFalse(plan['conflicts'])
        self.assertEqual(plan['reuse'], ['bundle-file'])
        self.assertEqual(plan['reuseMetadata']['bundle-file']['requiredBy'], ['root-v'])

    def test_pinned_installed_requirement_checks_exact_hash_before_reuse(self):
        dep = version('dep')
        root = version('root', dependencies=[dependency('dep', 'dep-v')])
        existing = local('dep-file', ['dep'], provider='modrinth', projectId='dep', versionId='dep-v',
                         hashes=dep['files'][0]['hashes'], enabled=False)
        plan = self.plan([root, dep], installed=[existing])
        self.assertFalse(plan['conflicts'])
        self.assertEqual(plan['enable'], ['dep-file'])
        self.assertEqual([file['versionId'] for file in plan['files']], ['root-v'])

    def test_release_channels_are_enforced_for_explicit_selections(self):
        root = version('root', version_type='beta')
        plan = self.plan([root], selections=[{'versionId': 'root-v', 'releaseChannels': ['release']}])
        self.assertIn('release_channel_mismatch', self.codes(plan))

    def test_filename_collision_includes_disabled_arbitrary_files(self):
        root = version('root')
        existing = local('unrelated-file', [], enabled=False, filename='ROOT-V.jar.disabled')
        self.assertIn('path_conflict', self.codes(self.plan([root], installed=[existing])))

    def test_incompatible_provider_relations_respect_exact_version(self):
        root = version('root', dependencies=[dependency('other', 'other-old', relation='incompatible')])
        installed = local('other-file', [], provider='modrinth', projectId='other', versionId='other-old')
        self.assertIn('incompatible', self.codes(self.plan([root], installed=[installed])))
        installed['versionId'] = 'other-new'
        self.assertFalse(self.plan([root], installed=[installed])['conflicts'])

    def test_context_kind_resourcepacks_and_shader_renderer_condition(self):
        resource = version('textures', loaders=['minecraft'])
        provider = FakeProvider([resource], {'textures': {'project_type': 'resourcepack'}})
        plan = resolve_plan(provider, dict(CONTEXT, kind='resourcepacks'), [{'versionId': 'textures-v'}], [])
        self.assertFalse(plan['conflicts'])
        self.assertEqual(plan['files'][0]['kind'], 'resourcepacks')
        shader = version('shader', loaders=['iris', 'optifine'])
        provider = FakeProvider([shader], {'shader': {'project_type': 'shader'}})
        plan = resolve_plan(provider, dict(CONTEXT, kind='shaderpacks'), [{'versionId': 'shader-v'}], [])
        self.assertFalse(plan['conflicts'])
        self.assertEqual(len(plan['files']), 1)
        self.assertIn('renderer_required', {warning['code'] for warning in plan['warnings']})

    def test_unsupported_catalog_project_type_does_not_become_mod(self):
        provider = FakeProvider([version('pack')], {'pack': {'project_type': 'modpack'}})
        plan = resolve_plan(provider, CONTEXT, [{'versionId': 'pack-v'}], [])
        self.assertIn('unsupported_content', self.codes(plan))

    def test_unsupported_constraint_is_not_hidden_by_true_alternative(self):
        with self.assertRaises(ValueError):
            matches_requirement('1.0', ['*', 'banana'])
        with self.assertRaises(ValueError):
            matches_requirement('1.0', '* || banana')

    def test_unsupported_requirement_without_target_is_explicit(self):
        root = version('root', depends={'missing': 'banana'})
        self.assertIn('unsupported_requirement', self.codes(self.plan([root])))

    def test_reverse_provider_requirement_survives_whole_update_set(self):
        api = version('api', 'api-new')
        old = local('api-old', [], provider='modrinth', projectId='api', versionId='api-old')
        consumer = local('consumer', ['consumer'], providerDependencies=[dependency('api', 'api-old')])
        self.assertIn('missing_dependency', self.codes(self.plan([api], installed=[old, consumer])))

    def test_installed_context_metadata_is_checked_before_compatibility_claim(self):
        existing = local('wrong', ['wrong'], gameVersions=['1.21'], loaders=['forge'])
        plan = self.plan([version('root')], installed=[existing])
        self.assertIn('minecraft_mismatch', self.codes(plan))
        self.assertIn('loader_mismatch', self.codes(plan))

    def test_final_inventory_validation_checks_archive_only_local_requirements(self):
        result = validate_inventory(FakeProvider([]), CONTEXT,
                                    [local('staged', ['staged'], depends=[{'id': 'missing', 'requirement': '>=1.0'}])])
        self.assertIn('missing_dependency', {problem['code'] for problem in result['conflicts']})

    def test_fml_environment_uses_forge_or_explicit_neoforge_fml_version(self):
        item = local('sample', ['sample'], depends=[{'id': 'javafml', 'requirement': '[47,)'}])
        context = dict(CONTEXT, loader='forge', loaderVersion='46.0.0')
        self.assertIn('requirement_mismatch', self.codes(validate_inventory(FakeProvider([]), context, [item])))
        context['loaderVersion'] = '47.1.0'
        self.assertFalse(validate_inventory(FakeProvider([]), context, [item])['conflicts'])
        item['depends'][0]['requirement'] = '[4,)'
        context.update(loader='neoforge', loaderVersion='21.1.0')
        self.assertIn('unresolved_environment', self.codes(validate_inventory(FakeProvider([]), context, [item])))
        context['fmlVersion'] = '4.0.42'
        self.assertFalse(validate_inventory(FakeProvider([]), context, [item])['conflicts'])

    def test_server_only_archive_relations_do_not_apply_to_client_installs(self):
        one = local('one', ['one'], metadataFormats=['forge'],
            depends=[{'id': 'missing-server-module', 'requirement': '*', 'side': 'SERVER'}, {'id': 'two', 'requirement': '*', 'side': 'SERVER'}],
            optionalDepends=[{'id': 'two', 'requirement': '>=2', 'side': 'SERVER'}],
            conflicts=[{'id': 'two', 'requirement': '*', 'side': 'SERVER'}])
        two = local('two', ['two'], depends=[{'id': 'one', 'requirement': '*', 'side': 'SERVER'}])
        result = validate_inventory(FakeProvider([]), dict(CONTEXT, loader='forge'), [one, two])
        self.assertFalse(result['conflicts'])

    def test_server_only_local_fabric_mod_is_not_client_compatible(self):
        item = local('server-only', ['server-only'], environment='server', metadataFormats=['fabric'])
        self.assertIn('client_unsupported', self.codes(validate_inventory(FakeProvider([]), CONTEXT, [item])))

    def test_local_runtime_format_compatibility_is_checked(self):
        provider = FakeProvider([])
        fabric = local('fabric', ['fabric'], metadataFormats=['fabric'])
        self.assertIn('loader_mismatch', self.codes(validate_inventory(provider, dict(CONTEXT, loader='forge'), [fabric])))
        self.assertFalse(validate_inventory(provider, dict(CONTEXT, loader='quilt'), [fabric])['conflicts'])
        forge = local('forge', ['forge'], metadataFormats=['forge'])
        self.assertFalse(validate_inventory(provider, dict(CONTEXT, loader='neoforge'), [forge])['conflicts'])
        self.assertIn('loader_mismatch', self.codes(validate_inventory(provider, dict(CONTEXT, loader='neoforge', minecraft='1.20.2'), [forge])))
        multiple = local('both', ['both'], rootMetadataFormats=['fabric', 'forge'])
        self.assertFalse(validate_inventory(provider, dict(CONTEXT, loader='forge'), [multiple])['conflicts'])

    def test_final_inventory_uses_embedded_modules_individual_versions(self):
        bundle = local('bundle', ['bundle', 'api'], version='9.0.0', providedVersions={'bundle': '9.0.0', 'api': '1.2.0'})
        consumer = local('consumer', ['consumer'], depends=[{'id': 'api', 'requirement': '[1.0,2.0)'}])
        self.assertFalse(validate_inventory(FakeProvider([]), CONTEXT, [bundle, consumer])['conflicts'])
        bundle['providedVersions']['api'] = '3.0.0'
        self.assertIn('requirement_mismatch', {problem['code'] for problem in validate_inventory(FakeProvider([]), CONTEXT, [bundle, consumer])['conflicts']})

    def test_final_inventory_does_not_enable_disabled_dependency_silently(self):
        consumer = local('consumer', ['consumer'], depends={'api': '*'})
        api = local('api', ['api'], enabled=False)
        result = validate_inventory(FakeProvider([]), CONTEXT, [consumer, api])
        self.assertIn('disabled_dependency', {problem['code'] for problem in result['conflicts']})

    def test_project_only_edge_uses_required_pin_from_other_root(self):
        one = version('one', dependencies=[dependency('api')])
        two = version('two', dependencies=[dependency('api', 'api-old')])
        records = [one, two, version('api', 'api-new', date_published='2026-11-10'), version('api', 'api-old', date_published='2026-11-09')]
        for selections in ([{'versionId': 'one-v'}, {'versionId': 'two-v'}], [{'versionId': 'two-v'}, {'versionId': 'one-v'}]):
            plan = self.plan(records, selections=selections)
            self.assertFalse(plan['conflicts'])
            self.assertEqual({file['versionId'] for file in plan['files']}, {'one-v', 'two-v', 'api-old'})

    def test_deep_later_pin_overrides_an_unpinned_candidate(self):
        root = version('root', dependencies=[dependency('api'), dependency('bridge')])
        bridge = version('bridge', dependencies=[dependency('api', 'api-old')])
        records = [root, version('api', 'api-new', date_published='2026-11-10'), version('api', 'api-old'), bridge]
        plan = self.plan(records)
        self.assertFalse(plan['conflicts'])
        self.assertEqual({file['versionId'] for file in plan['files']}, {'root-v', 'bridge-v', 'api-old'})
        api = next(file for file in plan['files'] if file['projectId'] == 'api')
        self.assertEqual(api['requiredBy'], ['bridge-v', 'root-v'])

    def test_final_inventory_marks_unknown_archive_metadata_unresolved(self):
        unknown = local('unknown', [], unknown=True, metadataProblems=['Malformed fabric.mod.json'])
        result = validate_inventory(FakeProvider([]), CONTEXT, [unknown])
        self.assertIn('unresolved_metadata', {warning['code'] for warning in result['warnings']})

    def test_malformed_provider_version_is_a_visible_conflict(self):
        for changes in ({'game_versions': None}, {'loaders': 'fabric'}, {'dependencies': [None]}, {'files': None}):
            with self.subTest(changes=changes):
                root = version('root', **changes)
                self.assertIn('invalid_metadata', self.codes(self.plan([root])))

    def test_malformed_selection_members_and_ids_are_visible_conflicts(self):
        for selection in ('bad', None, 42, {'versionId': {}}, {'versionId': []}, {'versionId': 'root-v', 'projectId': []}):
            with self.subTest(selection=selection):
                result = resolve_plan(FakeProvider([version('root')]), CONTEXT, [selection], [])
                self.assertIn('invalid_selection', self.codes(result))
                self.assertFalse(result['files'])

    def test_obsolete_candidate_required_subtree_is_not_installed(self):
        root = version('root', dependencies=[dependency('api'), dependency('bridge')])
        bridge = version('bridge', dependencies=[dependency('api', 'api-old')])
        api_new = version('api', 'api-new', dependencies=[dependency('old-helper')], date_published='2026-11-10')
        records = [root, api_new, version('api', 'api-old'), bridge, version('old-helper')]
        plan = self.plan(records)
        self.assertFalse(plan['conflicts'])
        self.assertEqual({file['versionId'] for file in plan['files']}, {'root-v', 'bridge-v', 'api-old'})

    def test_exact_pins_conflict_even_when_no_root_explicitly_selects_dependency(self):
        root = version('root', dependencies=[dependency('api', 'api-one'), dependency('bridge', 'bridge-v')])
        bridge = version('bridge', dependencies=[dependency('api', 'api-two')])
        plan = self.plan([root, bridge, version('api', 'api-one'), version('api', 'api-two')])
        self.assertIn('version_conflict', self.codes(plan))

    def test_unsupported_archive_relation_never_disappears(self):
        staged = local('staged', ['staged'], depends=[{'id': 'api', 'requirement': '*', 'unsupported': True}])
        result = validate_inventory(FakeProvider([]), CONTEXT, [staged, local('api', ['api'])])
        self.assertIn('unsupported_requirement', {problem['code'] for problem in result['conflicts']})

    def test_selection_cannot_switch_content_category(self):
        provider = FakeProvider([version('mod')])
        plan = resolve_plan(provider, dict(CONTEXT, kind='resourcepacks'), [{'versionId': 'mod-v'}], [])
        self.assertIn('kind_mismatch', self.codes(plan))

    def test_archive_optional_dependencies_are_visible_choices(self):
        root = version('root', optionalDepends=[{'id': 'recommended', 'requirement': '>=1.0'}])
        result = self.plan([root])
        self.assertFalse(result['conflicts'])
        self.assertIn('optional_local', {warning['code'] for warning in result['warnings']})

    def test_provider_optional_choice_does_not_require_unrelated_fabric_recommendations(self):
        consumer = self.nested_item({'id': 'consumer', 'version': '1.0', 'depends': {'api': '*'},
            'recommends': {'missing-addon': '*'}, 'children': [{'id': 'api', 'version': '1.0'}]})
        root = version('newmod', dependencies=[dependency('optional', relation='optional')])
        result = self.plan([root, version('optional')], installed=[consumer], include_optional=True)
        self.assertFalse(result['conflicts'], result['conflicts'])
        self.assertEqual({file['projectId'] for file in result['files']}, {'newmod', 'optional'})
        self.assertIn(consumer['id'], result['reuse'])
        self.assertTrue(any(warning.get('code') == 'optional_local' and warning.get('dependency') == 'missing-addon' for warning in result['warnings']))

    def test_provider_optional_choice_keeps_present_fabric_recommendation_advisory(self):
        consumer = self.nested_item({'id': 'consumer', 'version': '1.0', 'recommends': {'api': '>=2'}})
        api = self.nested_item({'id': 'api', 'version': '1.0'})
        result = self.plan([version('newmod')], installed=[consumer, api], include_optional=True)
        self.assertFalse(result['conflicts'], result['conflicts'])
        self.assertTrue(any(warning.get('code') == 'optional_local' and warning.get('dependency') == 'api' for warning in result['warnings']))

    def test_provider_optional_choice_keeps_disabled_fabric_recommendation_disabled(self):
        consumer = self.nested_item({'id': 'consumer', 'version': '1.0', 'suggests': {'api': '*'}})
        api = self.nested_item({'id': 'api', 'version': '1.0'})
        api['enabled'] = False
        result = self.plan([version('newmod')], installed=[consumer, api], include_optional=True)
        self.assertFalse(result['conflicts'], result['conflicts'])
        self.assertNotIn(api['id'], result['enable'])

    def test_optional_provider_reference_and_hard_archive_dependency_still_require_resolution(self):
        root = version('newmod', dependencies=[dependency('missing', relation='optional')])
        self.assertIn('missing_dependency', self.codes(self.plan([root], include_optional=True)))
        consumer = self.nested_item({'id': 'consumer', 'version': '1.0', 'depends': {'missing': '*'}})
        self.assertIn('missing_dependency', self.codes(self.plan([version('newmod')], installed=[consumer], include_optional=True)))

    def test_provider_optional_choice_preserves_native_present_optional_constraints(self):
        for format, requirement in (('quilt', '>=2'), ('forge', '[2,3)')):
            with self.subTest(format=format):
                consumer = local('consumer', ['consumer'], metadataFormats=[format],
                    optionalDepends=[{'id': 'api', 'requirement': requirement}])
                result = self.plan([version('newmod')], installed=[consumer, local('api', ['api'], version='1.0')], include_optional=True)
                self.assertIn('requirement_mismatch', self.codes(result))

    def test_nested_quilt_module_does_not_make_parent_fabric_recommendation_mandatory(self):
        child = io.BytesIO()
        with zipfile.ZipFile(child, 'w') as archive:
            archive.writestr('quilt.mod.json', json.dumps({'quilt_loader': {'id': 'child', 'version': '1.0'}}))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / 'outer.jar'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('fabric.mod.json', json.dumps({'schemaVersion': 1, 'id': 'outer', 'version': '1.0',
                'depends': {'child': '*'}, 'recommends': {'api': '>=2'}, 'jars': [{'file': 'nested/child.jar'}]}))
            archive.writestr('nested/child.jar', child.getvalue())
        outer = inspect_file(path)
        api = self.nested_item({'id': 'api', 'version': '1.0'})
        result = validate_inventory(FakeProvider([]), dict(CONTEXT, loader='quilt'), [outer, api])
        self.assertFalse(result['conflicts'], result['conflicts'])
        self.assertTrue(any(warning.get('code') == 'optional_local' and warning.get('dependency') == 'api' for warning in result['warnings']))

    def test_present_optional_forge_module_must_satisfy_its_constraint(self):
        consumer = local('consumer', ['consumer'], metadataFormats=['forge'],
                         optionalDepends=[{'id': 'api', 'requirement': '[2,3)'}])
        api = local('api', ['api'], version='1.0')
        result = validate_inventory(FakeProvider([]), CONTEXT, [consumer, api])
        self.assertIn('requirement_mismatch', {problem['code'] for problem in result['conflicts']})

    def test_fabric_recommendation_mismatch_stays_a_visible_recommendation(self):
        consumer = local('consumer', ['consumer'], metadataFormats=['fabric'],
                         optionalDepends=[{'id': 'api', 'requirement': '>=2'}])
        result = validate_inventory(FakeProvider([]), CONTEXT, [consumer, local('api', ['api'])])
        self.assertFalse(result['conflicts'])
        self.assertIn('optional_local', {warning['code'] for warning in result['warnings']})

    def test_maven_unbounded_end_must_have_an_exclusive_bracket(self):
        for requirement in ('[,1]', '[1,]'):
            with self.subTest(requirement=requirement), self.assertRaises(ValueError):
                matches_requirement('1', requirement)

    def test_unsupported_incompatibility_is_visible_even_without_target(self):
        item = local('local', ['local'], conflicts=[{'id': 'missing', 'requirement': 'banana'}])
        result = validate_inventory(FakeProvider([]), CONTEXT, [item])
        self.assertIn('unsupported_requirement', {problem['code'] for problem in result['conflicts']})

    def test_unsupported_incompatibility_group_is_not_silently_ignored(self):
        item = local('local', ['local'], conflicts=[{'id': None, 'requirement': '*', 'unsupported': True}])
        result = validate_inventory(FakeProvider([]), CONTEXT, [item])
        self.assertIn('unsupported_requirement', {problem['code'] for problem in result['conflicts']})

    def test_reused_dependency_retains_and_adds_reverse_ownership_metadata(self):
        root = version('root', dependencies=[dependency('api', 'api-v')])
        api = version('api')
        existing = local('api-file', ['api'], hashes=api['files'][0]['hashes'], provider='modrinth',
                         projectId='api', versionId='api-v', userRequested=False, requiredBy=['previous-root'])
        result = self.plan([root, api], installed=[existing])
        self.assertEqual(result['reuseMetadata']['api-file']['requiredBy'], ['previous-root', 'root-v'])
        self.assertFalse(result['reuseMetadata']['api-file']['userRequested'])
        self.assertNotIn('modIds', result['reuseMetadata']['api-file'])

    def test_direct_reuse_promotes_existing_dependency_to_user_requested(self):
        root = version('root')
        existing = local('present', ['root'], hashes=root['files'][0]['hashes'], userRequested=False)
        result = self.plan([root], installed=[existing])
        self.assertTrue(result['reuseMetadata']['present']['userRequested'])
        self.assertEqual(result['reuseMetadata']['present']['projectId'], 'root')


if __name__ == '__main__':
    unittest.main()
