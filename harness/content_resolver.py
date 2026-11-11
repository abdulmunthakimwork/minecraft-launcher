"""Pure dependency proposals; no live-file mutations or acquisition.

Known metadata is checked in both directions against the final enabled set.
Fabric semantic predicates and one numeric Maven interval are supported deliberately;
unrecognized grammar, prerequisites, or unresolved identities are conflicts.
Catalog metadata cannot prove unseen archive mod IDs: staging/ContentStore must
inspect downloaded archives and validate their complete identities before commit.
"""
import copy
from functools import cmp_to_key
import re

try:
    from content_provider import ProviderError, optional_icon_url, validate_file
except ImportError:
    from .content_provider import ProviderError, optional_icon_url, validate_file

MAX_NODES = 256
MAX_DEPTH = 32


def _numeric(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+(?:\.\d+){0,7}', value):
        raise ValueError('Only numeric version components can be compared: %r' % value)
    return tuple(int(part) for part in value.split('.'))


def _semantic(value):
    # Fabric Loader 0.16.10 SemanticVersionImpl: an arbitrary numeric core,
    # optional (including empty) prerelease, and comparison-neutral build data.
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError('Unsupported semantic version: %r' % value)
    without_build, build_separator, build = value.partition('+')
    core, separator, prerelease = without_build.partition('-')
    components = _numeric(core)
    if separator and not re.fullmatch(r'|[-0-9A-Za-z]+(?:\.[-0-9A-Za-z]+)*', prerelease):
        raise ValueError('Invalid semantic prerelease: %r' % value)
    return components, prerelease if separator else None, build if build_separator else None


def _compare(left, right):
    width = max(len(left), len(right))
    left, right = left + (0,) * (width - len(left)), right + (0,) * (width - len(right))
    return (left > right) - (left < right)


def _semantic_compare(left, right):
    compared = _compare(left[0], right[0])
    if compared or left[1] == right[1]:
        return compared
    if left[1] is None:
        return 1
    if right[1] is None:
        return -1
    left_parts = left[1].split('.') if left[1] else []
    right_parts = right[1].split('.') if right[1] else []
    for first, second in zip(left_parts, right_parts):
        if first == second:
            continue
        first_numeric = bool(re.fullmatch(r'0|[1-9][0-9]*', first))
        second_numeric = bool(re.fullmatch(r'0|[1-9][0-9]*', second))
        if first_numeric and second_numeric:
            return (int(first) > int(second)) - (int(first) < int(second))
        if first_numeric != second_numeric:
            return -1 if first_numeric else 1
        return (first > second) - (first < second)
    return (len(left_parts) > len(right_parts)) - (len(left_parts) < len(right_parts))


def _maven_numeric(actual):
    # Apache Maven 3.9.9 ComparableVersion gives qualifiers/build text its own
    # ordering. Keep this documented numeric subset exact, never use SemVer to
    # guess a Maven qualifier's order or release aliases such as "final".
    if actual[1] is not None or actual[2] is not None:
        raise ValueError('Maven qualifiers are outside the supported numeric interval grammar.')
    return actual[0]


def _predicate(expression):
    expression = expression.strip()
    if expression == '*':
        return lambda actual: True
    interval = re.fullmatch(r'([\[(])\s*([^,]*)\s*,\s*([^,]*)\s*([\])])', expression)
    if interval:
        lower = _numeric(interval[2].strip()) if interval[2].strip() else None
        upper = _numeric(interval[3].strip()) if interval[3].strip() else None
        if lower is None and upper is None:
            raise ValueError('Unbounded Maven interval is not supported.')
        if (lower is None and interval[1] != '(') or (upper is None and interval[4] != ')'):
            raise ValueError('Unbounded Maven endpoint must have an exclusive bracket.')
        if lower is not None and upper is not None and _compare(lower, upper) > 0:
            raise ValueError('Maven interval is reversed.')
        return lambda actual: ((lower is None or _compare(_maven_numeric(actual), lower) >= (0 if interval[1] == '[' else 1)) and
                               (upper is None or _compare(_maven_numeric(actual), upper) <= (0 if interval[4] == ']' else -1)))
    exact_interval = re.fullmatch(r'\[(\d+(?:\.\d+)*)\]', expression)
    if exact_interval:
        wanted = _numeric(exact_interval[1])
        return lambda actual: _compare(_maven_numeric(actual), wanted) == 0
    if '||' in expression:
        branches = [_predicate(branch) for branch in expression.split('||')]
        return lambda actual: any(branch(actual) for branch in branches)
    if any(char in expression for char in '[],()'):
        raise ValueError('Unsupported compound Maven requirement: ' + expression)
    expressions = re.sub(r'(>=|<=|==|!=|>|<|=|~|\^)\s+', r'\1', expression).split()
    if not expressions:
        raise ValueError('Unsupported version requirement: ' + expression)
    predicates = []
    for token in expressions:
        match = re.fullmatch(r'(>=|<=|==|!=|>|<|=|~|\^)?\s*(.+)', token.strip())
        operator, text = match[1] or '=', match[2]
        if text == '*':
            if operator != '=':
                raise ValueError('Wildcard comparator is unsupported.')
            predicates.append(lambda actual: True)
            continue
        core = text.split('+', 1)[0].split('-', 1)[0]
        if re.search(r'[xX*]', core):
            if (operator not in ('=', '==') or '-' in text.split('+', 1)[0] or
                    not re.fullmatch(r'\d+(?:\.\d+)*\.[xX*](?:\.[xX*])*', core)):
                raise ValueError('Only a trailing numeric wildcard is supported.')
            prefix = _numeric(re.split(r'\.[xX*]', core, maxsplit=1)[0])
            predicates.append(lambda actual, prefix=prefix: (actual[0] + (0,) * len(prefix))[:len(prefix)] == prefix)
            continue
        wanted = _semantic(text)
        if operator in ('~', '^'):
            width = 2 if operator == '~' else 1
            prefix = (wanted[0] + (0,) * width)[:width]
            predicates.append(lambda actual, wanted=wanted, prefix=prefix, width=width:
                              _semantic_compare(actual, wanted) >= 0 and (actual[0] + (0,) * width)[:width] == prefix)
        else:
            def compare(actual, wanted=wanted, operator=operator):
                result = _semantic_compare(actual, wanted)
                return {'=': result == 0, '==': result == 0, '!=': result != 0,
                        '>': result > 0, '>=': result >= 0, '<': result < 0, '<=': result <= 0}[operator]
            predicates.append(compare)
    return lambda actual: all(predicate(actual) for predicate in predicates)


def matches_requirement(actual, requirement):
    """Raise ValueError for unsupported grammar rather than assume compatibility."""
    if isinstance(requirement, list):
        if not requirement:
            raise ValueError('Empty version requirement alternatives.')
        # Parse every alternative first, so an unsupported branch never disappears.
        predicates = [_predicate(branch) if isinstance(branch, str) else _invalid_requirement(branch) for branch in requirement]
        if all(branch.strip() == '*' for branch in requirement):
            return True
        actual = _semantic(str(actual))
        return any(predicate(actual) for predicate in predicates)
    if not isinstance(requirement, str) or not requirement.strip():
        raise ValueError('Missing version requirement.')
    predicate = _predicate(requirement)
    return True if requirement.strip() == '*' else predicate(_semantic(str(actual)))


def _invalid_requirement(value):
    raise ValueError('Invalid version requirement: %r' % value)


def _format_loaders(format, context):
    return {'fabric': ('fabric', 'quilt'), 'quilt': ('quilt',), 'forge': ('forge', 'neoforge') if context.get('minecraft') == '1.20.1' else ('forge',),
            'neoforge': ('neoforge',)}.get(format, ())


def _relations(value, context=None):
    if isinstance(value, dict):
        relations = [{'id': ident, 'requirement': requirement} for ident, requirement in value.items()]
    elif isinstance(value, list):
        relations = [relation if isinstance(relation, dict) else {'id': str(relation), 'requirement': '*'} for relation in value]
    else:
        relations = []
    return [relation for relation in relations if str(relation.get('side', '')).upper() != 'SERVER' and
            (context is None or not relation.get('sourceFormat') or context.get('loader') in _format_loaders(relation['sourceFormat'], context))]


def _ids(item):
    result = set()
    for key in ('modIds', 'provides', 'embeddedModIds'):
        values = item.get(key) or []
        if isinstance(values, str):
            values = [values]
        for value in values:
            if isinstance(value, dict):
                value = value.get('id')
            if value:
                result.add(str(value))
    return result


def _same_hash(left, right):
    first, second = left.get('hashes') or {}, right.get('hashes') or {}
    shared = set(first).intersection(second)
    return bool(shared) and all(str(first[key]).lower() == str(second[key]).lower() for key in shared)


def resolve_plan(provider, context, selections, installed, include_optional=False):
    plan = {'files': [], 'reuse': [], 'enable': [], 'remove': [], 'conflicts': [], 'warnings': [], 'reuseMetadata': {}}
    state = _Resolver(provider, context, installed, plan, include_optional)
    state.resolve(selections)
    return plan


def validate_inventory(provider, context, installed):
    """Validate an already constructed final set; never silently enable files."""
    plan = {'files': [], 'reuse': [], 'enable': [], 'remove': [], 'conflicts': [], 'warnings': [], 'reuseMetadata': {}}
    state = _Resolver(provider, context, installed, plan, False)
    state.finalize()
    for ident in plan['enable']:
        state.issue('disabled_dependency', 'The final set requires a file that is still disabled.', itemId=ident)
    return {'conflicts': plan['conflicts'], 'warnings': plan['warnings']}


def canonical_inventory(context, installed, blocked=()):
    """Return the pure selected set for prospective file dependency operations.

    Callers first apply prospective root enabled flags/removals to their copy of
    ``installed``. Explicit disable/remove target file IDs belong in ``blocked``
    so they cannot be reenabled. Other required disabled roots are returned in
    ``enable``. Respect ``conflicts`` before committing any derived operation.
    """
    plan = {'files': [], 'reuse': [], 'enable': [], 'remove': [], 'conflicts': [], 'warnings': [], 'reuseMetadata': {}}
    if not isinstance(blocked, (list, tuple, set, frozenset)) or any(not isinstance(ident, str) for ident in blocked):
        raise ValueError('Blocked content file IDs must be strings.')
    plan['remove'] = sorted(set(blocked))
    state = _Resolver(None, context, installed, plan, False)
    state.finalize()
    return {'items': copy.deepcopy(state.final_inventory), 'enable': plan['enable'],
            'conflicts': plan['conflicts'], 'warnings': plan['warnings']}


class _Resolver:
    def __init__(self, provider, context, installed, plan, include_optional):
        self.provider, self.context, self.plan = provider, dict(context), plan
        self.name = getattr(provider, 'name', 'modrinth')
        self.installed = copy.deepcopy(installed)
        self.include_optional = include_optional
        self.versions, self.projects, self.nodes, self.selected = {}, {}, {}, {}
        self.active, self.visited, self.roots = [], set(), set()
        self.channel_by_project = {}
        self.explicit_projects = {}
        self.pending_projects = []
        self.project_requirements = []
        self._messages = set()
        self.final_inventory = []

    def issue(self, code, message, **details):
        if self.active:
            details.setdefault('sourceVersionId', self.active[-1])
        marker = repr((code, message, sorted(details.items())))
        if marker not in self._messages:
            self._messages.add(marker)
            self.plan['conflicts'].append(dict(code=code, message=message, **details))

    def warning(self, code, message, **details):
        self.plan['warnings'].append(dict(code=code, message=message, **details))

    def fetch(self, kind, ident):
        cache = self.versions if kind == 'version' else self.projects
        if ident not in cache:
            try:
                value = getattr(self.provider, kind)(ident)
                if not isinstance(value, dict) or (kind == 'version' and not value.get('id')):
                    raise ValueError('Reference returned no version metadata.')
                if kind == 'version' and not self.valid_version(value):
                    self.issue('invalid_metadata', 'Provider version metadata has malformed fields.', versionId=ident)
                    cache[ident] = None
                    return None
                cache[ident] = copy.deepcopy(value)
            except (ProviderError, KeyError, OSError, ValueError) as exc:
                self.issue('missing_dependency', 'Provider reference could not be resolved.', reference=ident, error=str(exc))
                cache[ident] = None
        return cache[ident]

    def valid_version(self, value):
        if not isinstance(value.get('id'), str) or not isinstance(value.get('project_id'), str):
            return False
        for key in ('game_versions', 'loaders'):
            if not isinstance(value.get(key), list) or any(not isinstance(entry, str) for entry in value[key]):
                return False
        for key in ('dependencies', 'files'):
            if not isinstance(value.get(key, []), list) or any(not isinstance(entry, dict) for entry in value.get(key, [])):
                return False
        return True

    def kind(self, version, project):
        project_type = project.get('project_type') or version.get('project_type')
        return {'mod': 'mods', 'resourcepack': 'resourcepacks', 'shader': 'shaderpacks'}.get(project_type, self.context.get('kind', 'mods'))

    def compatible(self, version, project, report=True):
        ident = version.get('id')
        success = True
        if project.get('project_type') not in (None, 'mod', 'resourcepack', 'shader'):
            if report:
                self.issue('unsupported_content', 'This catalog project type requires a different supported installation flow.', versionId=ident)
            success = False
        if self.context.get('minecraft') not in version.get('game_versions', []):
            if report:
                self.issue('minecraft_mismatch', 'Selected version does not support this Minecraft version.', versionId=ident)
            success = False
        kind = self.kind(version, project)
        if ident in self.roots and self.context.get('kind') and self.context['kind'] != kind:
            self.issue('kind_mismatch', 'Selected version belongs to another content category.', versionId=ident, kind=kind)
        expected = ['minecraft'] if kind == 'resourcepacks' else [self.context.get('loader')]
        if kind != 'shaderpacks' and not set(expected).intersection(version.get('loaders', [])):
            if report:
                self.issue('loader_mismatch', 'Selected version does not support this installation loader.', versionId=ident)
            success = False
        if project.get('client_side') == 'unsupported' or version.get('client_side') == 'unsupported':
            if report:
                self.issue('client_unsupported', 'Provider metadata excludes this file from client installations.', versionId=ident)
            success = False
        return success

    def resolve(self, selections):
        if not isinstance(selections, list) or not selections:
            self.issue('invalid_selection', 'Select at least one exact content version.')
            return
        if not self.context.get('minecraft') or self.context.get('loader') not in ('fabric', 'quilt', 'forge', 'neoforge', 'vanilla', 'minecraft'):
            self.issue('invalid_context', 'A pinned Minecraft version and normalized loader are required.')
            return
        if len(selections) > MAX_NODES:
            self.issue('graph_limit', 'Dependency graph exceeds 256 nodes or 32 levels.')
            return
        # All explicit selections are pinned before traversal: ordering cannot mask
        # a different version demanded by a dependency or another update.
        for selection in selections:
            if not isinstance(selection, dict):
                self.issue('invalid_selection', 'Every selection must be an exact content version object.')
                continue
            ident = selection.get('versionId')
            if not isinstance(ident, str) or not ident or len(ident) > 512:
                self.issue('invalid_selection', 'Selection must pin an exact provider version.')
                continue
            project_selection = selection.get('projectId')
            if project_selection is not None and (not isinstance(project_selection, str) or not project_selection or len(project_selection) > 512):
                self.issue('invalid_selection', 'Selected provider project must be a bounded identity string.')
                continue
            version = self.fetch('version', ident)
            if not version:
                continue
            project_id = version.get('project_id')
            if selection.get('projectId') and selection['projectId'] != project_id:
                self.issue('project_mismatch', 'Pinned version belongs to a different provider project.', versionId=ident)
            if not project_id:
                self.issue('missing_dependency', 'Pinned version has no provider project.', versionId=ident)
                continue
            if project_id in self.selected and self.selected[project_id] != ident:
                self.issue('version_conflict', 'Several exact versions of one project were selected.', projectId=project_id)
            self.selected[project_id] = ident
            self.roots.add(ident)
            channels = selection.get('releaseChannels')
            if channels is not None and (not isinstance(channels, list) or not channels or
                                          any(channel not in ('release', 'beta', 'alpha') for channel in channels)):
                self.issue('invalid_selection', 'Release channels must be a nonempty supported list.', versionId=ident)
                channels = None
            self.channel_by_project[project_id] = list(channels) if channels else [version.get('version_type', 'release')]
            if channels and version.get('version_type', 'release') not in channels:
                self.issue('release_channel_mismatch', 'Pinned version is outside the selected release channels.', versionId=ident)
        self.explicit_projects = dict(self.selected)
        for ident in sorted(self.roots):
            self.visit(ident, None, 1)
        while self.pending_projects:
            project, parent, depth = self.pending_projects.pop(0)
            parent_node = self.nodes.get(parent)
            if parent_node and parent not in self.roots and self.selected.get(parent_node['version'].get('project_id')) != parent:
                continue
            if self.embedded_provider(project, None, parent):
                continue
            ident = self.choose(project, parent)
            if ident:
                self.visit(ident, parent, depth)
        self.prune_candidates()
        self.finalize()

    def choose(self, project_id, parent):
        if project_id in self.selected:
            return self.selected[project_id]
        # A known installed project is already a provider. Its disk metadata is
        # checked against final requirements and need not be replaced by newest.
        candidates = [item for item in self.installed if item.get('provider') == self.name and item.get('projectId') == project_id]
        if len(candidates) > 1:
            self.issue('duplicate_identity', 'Several installed files provide the required project.', projectId=project_id)
            return None
        if candidates and candidates[0].get('versionId'):
            item = candidates[0]
            ident = item['versionId']
            metadata = self.fetch('version', ident)
            project = self.fetch('project', project_id)
            if metadata and project and self.compatible(metadata, project, report=False):
                self.selected[project_id] = ident
                return ident
        try:
            records = self.provider.versions(project_id, self.context['minecraft'], [self.context['loader']])
        except (ProviderError, KeyError, OSError, ValueError) as exc:
            self.issue('missing_dependency', 'Required project versions could not be resolved.', projectId=project_id, error=str(exc))
            return None
        project = self.fetch('project', project_id)
        channels = self.channel_by_project.get(project_id, ['release'])
        records = [record for record in records if isinstance(record, dict) and self.valid_version(record) and record.get('project_id') == project_id and
                   record.get('version_type', 'release') in channels and project is not None and
                   self.compatible(record, project, report=False)]
        records.sort(key=lambda record: record.get('date_published', ''), reverse=True)
        if not records:
            self.issue('missing_dependency', 'No compatible required project version is available in the allowed release channels.', projectId=project_id, requiredBy=parent)
            return None
        record = records[0]
        self.versions[record['id']] = record
        self.selected[project_id] = record['id']
        return record['id']

    def embedded_provider(self, project, exact, required_by=None):
        def matches(projects, versions):
            return exact in versions if exact else bool(project and project in projects)
        for ident in self.roots | set(self.nodes):
            version = self.versions.get(ident) or {}
            relations = [relation for relation in version.get('dependencies') or []
                         if isinstance(relation, dict) and relation.get('dependency_type') == 'embedded']
            if matches({relation.get('project_id') for relation in relations},
                       {relation.get('version_id') for relation in relations}):
                return True
        for item in self.installed:
            if item.get('provider') not in (None, self.name):
                continue
            if matches(item.get('embeddedProjects') or [], item.get('embeddedVersions') or []):
                self.reuse(item, {'requiredBy': [required_by]} if required_by else None)
                return True
        return False

    def visit(self, ident, parent, depth):
        if depth > MAX_DEPTH or (ident not in self.nodes and len(self.nodes) >= MAX_NODES):
            self.issue('graph_limit', 'Dependency graph exceeds 256 nodes or 32 levels.', versionId=ident)
            return
        if ident in self.active:
            self.issue('dependency_cycle', 'Required dependency graph contains a cycle.', cycle=self.active + [ident])
            return
        if ident in self.nodes and parent and parent not in self.nodes[ident]['requiredBy']:
            self.nodes[ident]['requiredBy'].append(parent)
        if ident in self.visited:
            return
        version = self.fetch('version', ident)
        if not version:
            return
        project_id = version.get('project_id')
        project = self.fetch('project', project_id)
        if project is None:
            return
        self.compatible(version, project)
        if project_id in self.selected and self.selected[project_id] != ident:
            if project_id in self.explicit_projects:
                self.issue('version_conflict', 'Required exact version conflicts with the selected version.', projectId=project_id, versionId=ident, selectedVersionId=self.selected[project_id])
            else:
                self.selected[project_id] = ident
        else:
            self.selected[project_id] = ident
        kind = self.kind(version, project)
        node = dict(version=version, project=project, kind=kind, requiredBy=[parent] if parent else [],
                    userRequested=ident in self.roots, embeddedProjects=set(), embeddedVersions=set())
        self.nodes[ident] = node
        self.active.append(ident)
        try:
            for relation in version.get('dependencies') or []:
                if not isinstance(relation, dict):
                    self.issue('missing_dependency', 'Malformed dependency metadata.', versionId=ident)
                    continue
                relation_type = relation.get('dependency_type', 'required')
                dep_project, dep_version = relation.get('project_id'), relation.get('version_id')
                if relation_type == 'embedded':
                    if dep_project:
                        node['embeddedProjects'].add(dep_project)
                    if dep_version:
                        node['embeddedVersions'].add(dep_version)
                    self.warning('embedded', 'Provider reports a dependency bundled in this file.', versionId=ident, projectId=dep_project)
                    continue
                if relation_type == 'incompatible':
                    continue
                if relation_type == 'optional' and not self.include_optional:
                    self.warning('optional', 'Optional dependency is available for review.', versionId=ident, projectId=dep_project, dependencyVersionId=dep_version)
                    continue
                if relation_type not in ('required', 'optional'):
                    self.issue('unsupported_relation', 'Provider dependency relation is unsupported.', versionId=ident, relation=relation_type)
                    continue
                if not dep_version and not dep_project:
                    self.issue('missing_dependency', 'Required dependency has no provider reference.', versionId=ident)
                    continue
                if self.embedded_provider(dep_project, dep_version, ident):
                    self.warning('embedded_reuse', 'A bundled dependency satisfies this required reference.',
                                 versionId=ident, projectId=dep_project, dependencyVersionId=dep_version)
                    continue
                if dep_version:
                    dependency = self.fetch('version', dep_version)
                    if dependency and dep_project and dependency.get('project_id') != dep_project:
                        self.issue('project_mismatch', 'Pinned dependency belongs to a different project.', versionId=dep_version, projectId=dep_project)
                        continue
                else:
                    self.pending_projects.append((dep_project, ident, depth + 1))
                    continue
                if dep_version:
                    self.visit(dep_version, ident, depth + 1)
        finally:
            self.active.pop()
            self.visited.add(ident)

    def prune_candidates(self):
        """Only versions reachable from the final project choices may install.

        An unpinned newest candidate is a suggestion. A later required exact pin
        replaces it; its obsolete subtree and errors cannot become live changes.
        Both reachable exact requirements remain and conflict if they disagree.
        """
        reachable, queue = set(), list(self.roots)
        while queue:
            ident = queue.pop()
            if ident in reachable or ident not in self.nodes:
                continue
            reachable.add(ident)
            for relation in self.nodes[ident]['version'].get('dependencies') or []:
                kind = relation.get('dependency_type', 'required')
                if kind != 'required' and not (kind == 'optional' and self.include_optional):
                    continue
                exact = relation.get('version_id')
                target = exact or self.selected.get(relation.get('project_id'))
                if target:
                    queue.append(target)
        self.nodes = {ident: node for ident, node in self.nodes.items() if ident in reachable}
        self.plan['conflicts'][:] = [problem for problem in self.plan['conflicts']
                                    if not problem.get('sourceVersionId') or problem['sourceVersionId'] in reachable]
        for node in self.nodes.values():
            node['requiredBy'][:] = []
        for parent, node in self.nodes.items():
            for relation in node['version'].get('dependencies') or []:
                kind = relation.get('dependency_type', 'required')
                if kind != 'required' and not (kind == 'optional' and self.include_optional):
                    continue
                target = relation.get('version_id') or self.selected.get(relation.get('project_id'))
                if target in self.nodes and parent not in self.nodes[target]['requiredBy']:
                    self.nodes[target]['requiredBy'].append(parent)

    def finalize(self):
        final = []
        for ident, node in self.nodes.items():
            version, project = node['version'], node['project']
            source_files = version.get('files') or []
            primary = next((file for file in source_files if isinstance(file, dict) and file.get('primary')), None)
            if primary is None and len(source_files) == 1:
                primary = source_files[0]
            if primary is None:
                self.issue('invalid_file', 'Provider does not identify one unambiguous primary file.', versionId=ident)
                continue
            try:
                hashes = validate_file(primary)
            except ProviderError as exc:
                self.issue('invalid_file', str(exc), versionId=ident)
                continue
            metadata = {'provider': self.name, 'projectId': version.get('project_id'), 'versionId': ident,
                        'name': project.get('title') or version.get('name') or version.get('project_id'),
                        'version': version.get('version_number'), 'modIds': sorted(_ids(version)),
                        'depends': copy.deepcopy(_relations(version.get('depends'))),
                        'conflicts': copy.deepcopy(_relations(version.get('conflicts'))),
                        'optionalDepends': copy.deepcopy(_relations(version.get('optionalDepends'))),
                        'warnings': copy.deepcopy(_relations(version.get('warnings'))),
                        'providerDependencies': copy.deepcopy(version.get('dependencies') or []),
                        'gameVersions': copy.deepcopy(version.get('game_versions') or []),
                        'loaders': copy.deepcopy(version.get('loaders') or []),
                        'releaseChannels': self.channel_by_project.get(version.get('project_id'), ['release']),
                        'userRequested': node['userRequested'], 'requiredBy': sorted(node['requiredBy']),
                        'embeddedProjects': sorted(node['embeddedProjects']), 'embeddedVersions': sorted(node['embeddedVersions'])}
            icon_url = optional_icon_url(project.get('icon_url'))
            if icon_url:
                metadata['iconUrl'] = icon_url
            file = dict(copy.deepcopy(primary), **metadata, kind=node['kind'], hashes=hashes, metadata=copy.deepcopy(metadata))
            exact = [item for item in self.installed if item.get('kind') == node['kind'] and _same_hash(item, primary)]
            if len(exact) > 1:
                self.issue('duplicate_identity', 'Several installed files match selected file hashes.', versionId=ident)
            item = dict(metadata, id=ident, kind=node['kind'], enabled=True, hashes=hashes, filename=file['filename'])
            if exact:
                existing = exact[0]
                self.reuse(existing, metadata)
                # Reuse actual local IDs, names and dependency metadata as well as
                # catalog evidence, so known embedded aliases are not discarded.
                item['id'] = existing['id']
                item['modIds'] = sorted(set(metadata['modIds']) | _ids(existing))
                item['providedVersions'] = copy.deepcopy(existing.get('providedVersions') or existing.get('modVersions') or {})
                for key in ('rootModIds', 'embeddedModIds', 'modCandidates'):
                    if key in existing:
                        item[key] = copy.deepcopy(existing[key])
                item['metadataFormats'] = copy.deepcopy(existing.get('metadataFormats') or [])
                item['optionalDepends'] += _relations(existing.get('optionalDepends'))
                item['warnings'] += _relations(existing.get('warnings'))
                item['depends'] += _relations(existing.get('depends'))
                item['conflicts'] += _relations(existing.get('conflicts'))
                item['userRequested'] = bool(existing.get('userRequested') or metadata['userRequested'])
            else:
                old = [existing for existing in self.installed if existing.get('kind') == node['kind'] and
                       existing.get('provider') == self.name and existing.get('projectId') == metadata['projectId']]
                if old:
                    file['replaces'] = old[0]['id'] if len(old) == 1 else [existing['id'] for existing in old]
                    for existing in old:
                        self.add('remove', existing['id'])
                    item['userRequested'] = bool(metadata['userRequested'] or any(existing.get('userRequested') for existing in old))
                    file['userRequested'] = file['metadata']['userRequested'] = item['userRequested']
                self.plan['files'].append(file)
            final.append(item)
            if node['kind'] == 'mods' and not item['modIds']:
                self.warning('identity_pending', 'Archive mod IDs will be checked from staged content before commit.', versionId=ident)
            if node['kind'] == 'shaderpacks':
                self.warning('renderer_required', 'Shader file installation does not activate a renderer; supported renderers are reported by provider metadata.', versionId=ident, renderers=version.get('loaders', []))
        reused = set(self.plan['reuse'])
        removed = set(self.plan['remove'])
        represented = {item['id'] for item in final}
        final.extend(dict(item, enabled=True) for item in self.installed if item['id'] in reused and item['id'] not in represented)
        final.extend(item for item in self.installed if item['id'] not in reused | removed and item.get('enabled', True))
        self.final_inventory = final
        if len(final) > MAX_NODES:
            self.issue('graph_limit', 'Dependency graph exceeds 256 nodes or 32 levels.')
            return
        final = self.select_candidates(final, removed)
        # Local requirements can enable a disabled installed provider; iterate so
        # that provider's own dependencies and reverse conflicts are checked too.
        index = 0
        while index < len(final):
            if len(final) > MAX_NODES + len(self.installed):
                self.issue('graph_limit', 'Dependency graph exceeds limits.')
                break
            item = final[index]
            index += 1
            for relation in _relations(item.get('depends'), self.context):
                self.check_requirement(item, relation, final, removed)
            for relation in _relations(item.get('optionalDepends'), self.context):
                ident = relation.get('id')
                self.warning('optional_local', 'Archive metadata declares an optional module or recommendation.',
                             itemId=item['id'], dependency=ident, requirement=relation.get('requirement', '*'))
                present = any(ident in _ids(target) for target in final)
                formats = [relation['sourceFormat']] if relation.get('sourceFormat') else item.get('metadataFormats') or []
                strict_when_present = any(format in ('forge', 'neoforge', 'quilt') for format in formats)
                # Provider optional acquisition does not turn an installed
                # archive's soft recommendations into mandatory modules.
                if present and strict_when_present:
                    self.check_requirement(item, relation, final, removed)
        self.check_duplicates(final)
        self.check_incompatibilities(final)
        self.check_provider_requirements(final)
        self.check_final_graph(final)
        self.check_installed_context(final)
        self.final_inventory = final

    def select_candidates(self, final, removed):
        """Select one Fabric/Quilt candidate per ID, with conditional child edges.

        Fabric Loader's ModSolver (fda9a7b8) makes a nested candidate's
        dependencies conditional on selecting it, and permits at most one
        candidate for each mod ID. Top-level files remain mandatory. This
        bounded search never acquires content or treats failed choices as valid.
        """
        if self.context.get('loader') not in ('fabric', 'quilt') or not any(item.get('modCandidates') for item in final):
            return final
        files = {item['id']: item for item in final}
        files.update((item['id'], item) for item in self.installed if item['id'] not in files and item['id'] not in removed)
        candidates, roots, structural = {}, {}, set()
        for file_id, item in files.items():
            records = [record for record in item.get('modCandidates', [])
                       if self.context['loader'] in _format_loaders(record.get('sourceFormat'), self.context)]
            native_quilt = self.context['loader'] == 'quilt' and any(record.get('root') and record.get('sourceFormat') == 'quilt' for record in records)
            records = [record for record in records if not (native_quilt and record.get('root') and record.get('sourceFormat') != 'quilt')]
            if not any(record.get('root') for record in records):
                records = [{'key': 'legacy', 'root': True, 'parents': [], 'modIds': sorted(_ids(item)),
                            'providedVersions': {ident: (item.get('providedVersions') or item.get('modVersions') or {}).get(ident, item.get('version')) for ident in _ids(item)},
                            **{field: copy.deepcopy(item.get(field) or []) for field in ('depends', 'conflicts', 'optionalDepends', 'warnings')}}]
            else:
                structural.add(file_id)
            roots[file_id] = []
            for record in records:
                key = file_id + '/' + record['key']
                candidate = dict(copy.deepcopy(record), key=key, fileId=file_id,
                                 parents=[file_id + '/' + parent for parent in record.get('parents', [])])
                candidates[key] = candidate
                if record.get('root'):
                    roots[file_id].append(key)
        if len(candidates) > MAX_NODES:
            self.issue('graph_limit', 'Embedded candidate selection exceeds 256 modules.')
            return final
        initial = {key for item in final for key in roots[item['id']]}
        failures, attempts = [], 0

        def owners(selected):
            result = {}
            for key in sorted(selected):
                for ident in candidates[key]['modIds']:
                    if ident in result and result[ident] != key:
                        return None
                    result[ident] = key
            return result

        def activate(selected, key, active=()):
            if key in selected:
                return [selected]
            if key not in candidates or key in active or len(active) > MAX_DEPTH:
                return []
            candidate = candidates[key]
            if candidate.get('environment') == 'server':
                return []
            choices = [selected]
            if not candidate['root']:
                parents = candidate['parents']
                if not any(parent in selected for parent in parents):
                    choices = [choice for parent in parents for choice in activate(selected, parent, active + (key,))]
            result = []
            for choice in choices:
                added = choice | {key}
                if owners(added) is None:
                    continue
                # Enabling a disabled archive enables all its top-level mods.
                pending = [added]
                if candidate['root']:
                    for other in roots[candidate['fileId']]:
                        pending = [next_choice for current in pending for next_choice in activate(current, other, active + (key,))]
                result.extend(pending)
            return result

        def fail(code, message, **details):
            failures.append((code, message, details))
            return None

        def version_order(first, second):
            first_enabled = bool(files[candidates[first]['fileId']].get('enabled', True))
            second_enabled = bool(files[candidates[second]['fileId']].get('enabled', True))
            if first_enabled != second_enabled:
                return -1 if first_enabled else 1
            try:
                left = candidates[first]['providedVersions'].get(candidates[first].get('modId'), candidates[first].get('version'))
                right = candidates[second]['providedVersions'].get(candidates[second].get('modId'), candidates[second].get('version'))
                comparison = _semantic_compare(_semantic(str(left)), _semantic(str(right)))
                if comparison:
                    return -comparison
            except ValueError:
                pass
            return (first > second) - (first < second)

        def search(selected):
            nonlocal attempts
            attempts += 1
            if attempts > 4096:
                return fail('graph_limit', 'Embedded candidate selection exceeded its bounded search limit.')
            selected_by_id = owners(selected)
            if selected_by_id is None:
                return fail('version_conflict', 'Several mandatory root modules provide the same mod ID.')
            required = {}
            for key in sorted(selected):
                candidate = candidates[key]
                relations = list(_relations(candidate.get('depends'), self.context))
                for relation in _relations(candidate.get('optionalDepends'), self.context):
                    ident = relation.get('id') or relation.get('modId')
                    if candidate.get('sourceFormat') == 'quilt' and ident in selected_by_id:
                        relations.append(relation)
                for relation in relations:
                    ident = relation.get('id') or relation.get('modId')
                    requirement = relation.get('requirement', relation.get('versionRange', '*'))
                    try:
                        if relation.get('unsupported'):
                            raise ValueError('Archive dependency relation has unsupported grammar.')
                        matches_requirement('0', requirement)  # Validate every branch before choosing a target.
                    except ValueError as exc:
                        return fail('unsupported_requirement', str(exc), itemId=candidate['fileId'], dependency=ident, requirement=requirement)
                    if not ident:
                        return fail('missing_dependency', 'Local dependency metadata has no identity.', itemId=candidate['fileId'])
                    environment = self.environment(ident)
                    if environment is not NotImplemented:
                        if environment is None:
                            return fail('unresolved_environment', 'Required environment version is unknown.', dependency=ident)
                        try:
                            if environment is False or not matches_requirement(str(environment), requirement):
                                return fail('requirement_mismatch', 'Required environment version is incompatible.', dependency=ident, requirement=requirement)
                        except ValueError as exc:
                            return fail('unsupported_requirement', str(exc), dependency=ident, requirement=requirement)
                        continue
                    required.setdefault(ident, []).append((candidate['fileId'], requirement))
            unresolved = []
            for ident, requirements in required.items():
                def compatible(key):
                    version = candidates[key]['providedVersions'].get(ident)
                    return all(matches_requirement(str(version or ''), requirement) for _, requirement in requirements)
                try:
                    if ident in selected_by_id:
                        if not compatible(selected_by_id[ident]):
                            return fail('version_conflict', 'No single selected module version satisfies all dependency requirements.', dependency=ident,
                                        requirements=[requirement for _, requirement in requirements])
                        continue
                    available = [key for key, candidate in candidates.items() if ident in candidate['modIds']]
                    choices, unknown_versions = [], []
                    for key in available:
                        try:
                            if compatible(key):
                                choices.append(key)
                        except ValueError as exc:
                            unknown_versions.append(str(exc))
                except ValueError as exc:
                    return fail('unsupported_requirement', str(exc), dependency=ident)
                if not choices:
                    if unknown_versions:
                        return fail('unsupported_requirement', unknown_versions[0], dependency=ident)
                    return fail('version_conflict' if available else 'missing_dependency',
                                'No single available module version satisfies all dependency requirements.' if available else 'Required local module could not be resolved.',
                                dependency=ident, requirements=[requirement for _, requirement in requirements])
                unresolved.append((len(choices), ident, sorted(choices, key=cmp_to_key(version_order))))
            # A selected candidate's breaks constrain the same consistent set.
            for key in selected:
                for relation in _relations(candidates[key].get('conflicts'), self.context):
                    ident = relation.get('id') or relation.get('modId')
                    target = selected_by_id.get(ident)
                    if target and target != key:
                        try:
                            if relation.get('unsupported'):
                                raise ValueError('Archive incompatibility relation has unsupported grammar.')
                            if matches_requirement(str(candidates[target]['providedVersions'].get(ident) or ''), relation.get('requirement', '*')):
                                return fail('incompatible', 'Selected embedded module versions are incompatible.', dependency=ident)
                        except ValueError as exc:
                            return fail('unsupported_requirement', str(exc), dependency=ident)
            if not unresolved:
                return selected
            _, _, choices = min(unresolved)
            for key in choices:
                for added in activate(selected, key):
                    resolved = search(added)
                    if resolved is not None:
                        return resolved
            return None

        selected = search(initial)
        if selected is None:
            code, message, details = failures[-1] if failures else ('version_conflict', 'Embedded candidates cannot be selected consistently with their parent archives.', {})
            self.issue(code, message, **details)
            selected = initial
        active_files = {candidates[key]['fileId'] for key in selected}
        for file_id in sorted(active_files - {item['id'] for item in final}):
            item = files[file_id]
            self.reuse(item)
            final.append(dict(item, enabled=True))
        result = []
        for item in final:
            if item['id'] not in structural:
                result.append(item)
                continue
            chosen = [candidates[key] for key in sorted(selected) if candidates[key]['fileId'] == item['id']]
            effective = copy.deepcopy(item)
            effective['modIds'] = sorted({ident for candidate in chosen for ident in candidate['modIds']})
            effective['rootModIds'] = sorted({ident for candidate in chosen if candidate['root'] for ident in candidate['modIds']})
            effective['embeddedModIds'] = sorted({ident for candidate in chosen if not candidate['root'] for ident in candidate['modIds']})
            effective['providedVersions'] = {ident: version for candidate in chosen for ident, version in candidate['providedVersions'].items()}
            effective['metadataFormats'] = sorted({candidate['sourceFormat'] for candidate in chosen})
            for field in ('depends', 'conflicts', 'optionalDepends', 'warnings'):
                effective[field] = [copy.deepcopy(relation) for candidate in chosen for relation in candidate.get(field, [])]
            result.append(effective)
        return result

    def add(self, key, ident):
        if ident not in self.plan[key]:
            self.plan[key].append(ident)

    def reuse(self, item, metadata=None):
        self.add('reuse', item['id'])
        provider_fields = ('provider', 'projectId', 'versionId', 'releaseChannels',
                           'providerDependencies', 'gameVersions', 'loaders', 'embeddedProjects', 'embeddedVersions')
        fields = provider_fields + ('userRequested', 'requiredBy', 'iconUrl')
        current = self.plan['reuseMetadata'].setdefault(item['id'], {key: copy.deepcopy(item[key]) for key in fields if key in item})
        if metadata:
            for key in provider_fields:
                if key in metadata:
                    current[key] = copy.deepcopy(metadata[key])
            icon_url = optional_icon_url(metadata.get('iconUrl'))
            if icon_url:
                current['iconUrl'] = icon_url
            current['userRequested'] = bool(current.get('userRequested', item.get('userRequested', True)) or metadata.get('userRequested'))
            current['requiredBy'] = sorted(set(current.get('requiredBy') or []) | set(metadata.get('requiredBy') or []))
        else:
            current.setdefault('userRequested', bool(item.get('userRequested', True)))
            current.setdefault('requiredBy', copy.deepcopy(item.get('requiredBy') or []))
        if not item.get('enabled', True):
            self.add('enable', item['id'])

    def environment(self, ident):
        if ident == 'minecraft':
            return self.context.get('minecraft')
        if ident in ('java', 'javaMajor'):
            return self.context.get('javaMajor')
        if ident in ('javafml', 'lowcodefml'):
            if self.context.get('loader') not in ('forge', 'neoforge'):
                return False
            explicit = self.context.get('fmlVersion') or (self.context.get('loaderVersions') or {}).get(ident)
            return explicit or (self.context.get('loaderVersion') if self.context.get('loader') == 'forge' else None)
        loader_ids = {'fabricloader': 'fabric', 'fabric-loader': 'fabric', 'quilt_loader': 'quilt',
                      'quiltloader': 'quilt', 'forge': 'forge', 'neoforge': 'neoforge'}
        if ident in loader_ids:
            if self.context.get('loader') != loader_ids[ident]:
                return False
            return self.context.get('loaderVersion') or (self.context.get('loaderVersions') or {}).get(loader_ids[ident])
        return NotImplemented

    def check_requirement(self, item, relation, final, removed):
        ident = relation.get('id') or relation.get('modId')
        requirement = relation.get('requirement', relation.get('versionRange', '*'))
        if relation.get('unsupported'):
            self.issue('unsupported_requirement', 'Archive dependency relation has unsupported grammar.', itemId=item['id'], dependency=ident, requirement=requirement)
            return
        try:
            if isinstance(requirement, list):
                if not requirement:
                    raise ValueError('Empty version requirement alternatives.')
                for branch in requirement:
                    _predicate(branch) if isinstance(branch, str) else _invalid_requirement(branch)
            else:
                _predicate(requirement) if isinstance(requirement, str) else _invalid_requirement(requirement)
        except ValueError as exc:
            self.issue('unsupported_requirement', str(exc), itemId=item['id'], dependency=ident, requirement=requirement)
            return
        if not ident:
            self.issue('missing_dependency', 'Local dependency metadata has no identity.', itemId=item['id'])
            return
        environment = self.environment(ident)
        if environment is not NotImplemented:
            if environment is None:
                self.issue('unresolved_environment', 'Required environment version is unknown.', itemId=item['id'], dependency=ident)
                return
            if environment is False:
                self.issue('requirement_mismatch', 'Required loader is not installed.', itemId=item['id'], dependency=ident)
                return
            targets = [{'id': ident, 'version': str(environment)}]
        else:
            targets = [target for target in final if ident in _ids(target)]
            if not targets:
                disabled = [target for target in self.installed if target['id'] not in removed and not target.get('enabled', True) and ident in _ids(target)]
                if len(disabled) == 1:
                    target = disabled[0]
                    self.reuse(target)
                    target = dict(target, enabled=True)
                    final.append(target)
                    targets = [target]
            if not targets:
                self.issue('missing_dependency', 'Required local module could not be resolved.', itemId=item['id'], dependency=ident)
                return
            for target in targets:
                existing = next((existing for existing in self.installed if existing['id'] == target['id']), None)
                if existing:
                    self.reuse(existing, {'requiredBy': [item['id']]})
        try:
            results = [matches_requirement(str((target.get('providedVersions') or target.get('modVersions') or {}).get(ident, target.get('version') or '')), requirement) for target in targets]
        except ValueError as exc:
            self.issue('unsupported_requirement', str(exc), itemId=item['id'], dependency=ident, requirement=requirement)
            return
        if not any(results):
            self.issue('requirement_mismatch', 'Required module version is incompatible.', itemId=item['id'], dependency=ident, requirement=requirement)

    def check_duplicates(self, final):
        identities, projects, paths = {}, {}, {}
        for item in final:
            if item.get('kind') == 'mods':
                for ident in (set(item['rootModIds']) if 'rootModIds' in item else _ids(item)):
                    if ident in identities and identities[ident] != item['id']:
                        self.issue('duplicate_identity', 'Several enabled files provide the same mod identity.', modId=ident, itemIds=[identities[ident], item['id']])
                    identities[ident] = item['id']
            if item.get('provider') and item.get('projectId'):
                key = (item['provider'], item['projectId'])
                if key in projects and projects[key] != item['id']:
                    self.issue('duplicate_identity', 'Several enabled files provide the same provider project.', projectId=item['projectId'])
                    self.issue('version_conflict', 'Required graph selects several files from one provider project.', projectId=item['projectId'])
                projects[key] = item['id']
            key = (item.get('kind'), str(item.get('filename', '')).casefold().removesuffix('.disabled'))
            if key[1] and key in paths and paths[key] != item['id']:
                self.issue('path_conflict', 'Selected filename aliases another enabled content file.', filename=item.get('filename'))
            paths[key] = item['id']
        # A disabled file still owns its normalized destination; only an explicit
        # same-project replacement may remove it before install.
        for file in self.plan['files']:
            key = (file['kind'], file['filename'].casefold().removesuffix('.disabled'))
            for existing in self.installed:
                existing_key = (existing.get('kind'), str(existing.get('filename', '')).casefold().removesuffix('.disabled'))
                if key == existing_key and existing['id'] not in self.plan['remove']:
                    self.issue('path_conflict', 'Selected filename aliases an existing content file.', filename=file['filename'], itemId=existing['id'])
        for item in final:
            for project in item.get('embeddedProjects') or []:
                if (self.name, project) in projects and projects[(self.name, project)] != item['id']:
                    self.issue('duplicate_identity', 'A bundled provider project is also installed separately.', projectId=project, itemId=item['id'])

    def check_incompatibilities(self, final):
        for item in final:
            for relation in _relations(item.get('warnings'), self.context):
                ident = relation.get('id') or relation.get('modId')
                requirement = relation.get('requirement', '*')
                for target in final:
                    if item['id'] == target['id'] or ident not in _ids(target):
                        continue
                    try:
                        discouraged = matches_requirement(str((target.get('providedVersions') or target.get('modVersions') or {}).get(ident, target.get('version') or '')), requirement)
                    except ValueError as exc:
                        self.warning('unsupported_advisory', str(exc), itemId=item['id'], dependency=ident)
                        continue
                    if discouraged:
                        self.warning('discouraged_content', 'Archive metadata advises against using these modules together.', itemId=item['id'], otherItemId=target['id'], modId=ident)
            for relation in _relations(item.get('conflicts'), self.context):
                ident = relation.get('id') or relation.get('modId')
                requirement = relation.get('requirement', '*')
                try:
                    if relation.get('unsupported'):
                        raise ValueError('Archive incompatibility relation has unsupported grammar.')
                    if isinstance(requirement, list):
                        if not requirement:
                            raise ValueError('Empty incompatibility alternatives.')
                        for branch in requirement:
                            _predicate(branch) if isinstance(branch, str) else _invalid_requirement(branch)
                    else:
                        _predicate(requirement) if isinstance(requirement, str) else _invalid_requirement(requirement)
                except ValueError as exc:
                    self.issue('unsupported_requirement', str(exc), itemId=item['id'], dependency=ident)
                    continue
                for target in final:
                    if item['id'] == target['id'] or ident not in _ids(target):
                        continue
                    try:
                        incompatible = matches_requirement(str((target.get('providedVersions') or target.get('modVersions') or {}).get(ident, target.get('version') or '')), requirement)
                    except ValueError as exc:
                        self.issue('unsupported_requirement', str(exc), itemId=item['id'], dependency=ident)
                        continue
                    if incompatible:
                        self.issue('incompatible', 'Known module incompatibility is present in the proposed enabled set.', itemId=item['id'], otherItemId=target['id'], modId=ident)
            for relation in item.get('providerDependencies') or []:
                if relation.get('dependency_type') != 'incompatible':
                    continue
                for target in final:
                    if target['id'] == item['id']:
                        continue
                    exact = relation.get('version_id')
                    project = relation.get('project_id')
                    if target.get('provider') == self.name and ((exact and exact == target.get('versionId')) or
                       (not exact and project == target.get('projectId'))):
                        self.issue('incompatible', 'Provider reports incompatible content in the proposed enabled set.', itemId=item['id'], otherItemId=target['id'])

    def check_provider_requirements(self, final):
        for item in final:
            for relation in item.get('providerDependencies') or []:
                if relation.get('dependency_type', 'required') != 'required':
                    continue
                project, exact = relation.get('project_id'), relation.get('version_id')
                if any((project and project in target.get('embeddedProjects', [])) or (exact and exact in target.get('embeddedVersions', [])) for target in final):
                    continue
                targets = [target for target in final if target.get('provider') == self.name and
                           ((exact and target.get('versionId') == exact) or (not exact and target.get('projectId') == project))]
                if not targets:
                    self.issue('missing_dependency', 'A remaining installed file loses a required provider dependency.', itemId=item['id'], projectId=project, versionId=exact)

    def check_final_graph(self, final):
        graph = {item['id']: set() for item in final}
        for item in final:
            for relation in _relations(item.get('depends'), self.context):
                ident = relation.get('id') or relation.get('modId')
                if self.environment(ident) is NotImplemented:
                    graph[item['id']].update(target['id'] for target in final if ident in _ids(target))
            for relation in item.get('providerDependencies') or []:
                if relation.get('dependency_type', 'required') != 'required':
                    continue
                project, exact = relation.get('project_id'), relation.get('version_id')
                graph[item['id']].update(target['id'] for target in final if target.get('provider') == self.name and
                                        ((exact and target.get('versionId') == exact) or
                                         (not exact and target.get('projectId') == project)))
        if len(graph) > MAX_NODES:
            self.issue('graph_limit', 'Dependency graph exceeds 256 nodes or 32 levels.')
            return
        seen_depth, active = {}, []
        def visit(ident, depth):
            # Loader dependencies constrain a jointly enabled set; satisfied
            # cycles are valid. Acquisition recursion is guarded in visit().
            if ident in active:
                return
            if depth > MAX_DEPTH:
                self.issue('graph_limit', 'Dependency graph exceeds 256 nodes or 32 levels.', itemId=ident)
                return
            if depth <= seen_depth.get(ident, 0):
                return
            seen_depth[ident] = depth
            active.append(ident)
            for target in graph[ident]:
                visit(target, depth + 1)
            active.pop()
        for ident in graph:
            visit(ident, 1)

    def check_installed_context(self, final):
        for item in final:
            formats = item.get('rootMetadataFormats') or item.get('metadataFormats') or []
            runtime_formats = [format for format in formats if _format_loaders(format, self.context)]
            if item.get('kind') == 'mods' and runtime_formats and not any(self.context.get('loader') in _format_loaders(format, self.context) for format in runtime_formats):
                self.issue('loader_mismatch', 'Local archive metadata does not support this installation loader.', itemId=item['id'])
            if item.get('environment') == 'server' and (not runtime_formats or ('fabric' in runtime_formats and self.context.get('loader') in ('fabric', 'quilt'))):
                self.issue('client_unsupported', 'Local archive metadata excludes this file from client installations.', itemId=item['id'])
            if item.get('unknown') or item.get('metadataProblems'):
                self.warning('unresolved_metadata', 'Archive metadata is incomplete; its full dependency compatibility is unresolved.',
                             itemId=item['id'], problems=item.get('metadataProblems') or [])
            games = item.get('gameVersions')
            loaders = item.get('loaders')
            if games and self.context['minecraft'] not in games:
                self.issue('minecraft_mismatch', 'Existing enabled content does not support this Minecraft version.', itemId=item['id'])
            wanted = 'minecraft' if item.get('kind') == 'resourcepacks' else self.context['loader']
            if loaders and item.get('kind') != 'shaderpacks' and wanted not in loaders:
                self.issue('loader_mismatch', 'Existing enabled content does not support this installation loader.', itemId=item['id'])
