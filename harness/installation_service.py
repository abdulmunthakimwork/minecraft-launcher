"""Installation-scoped workspace service; host callbacks own launcher state."""
import base64
import contextlib
import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

import installer
import download_control
from instance_content import ContentStore, ContentError, canonical_root
from content_provider import ModrinthProvider, ProviderError, validate_file, validate_url, MAX_FILE_BYTES
from content_resolver import resolve_plan, validate_inventory

# Keep operation ownership and jobs through the host's in-place hot reload.
if '_COORDINATOR' not in globals():
    _COORDINATOR = threading.RLock()
    _BUSY = set()
    _PLANS = {}
    _JOBS = {}
    _LATEST = {}
if '_JOBS_LOCK' not in globals():
    _JOBS_LOCK = threading.RLock()


def _job_update(job, **values):
    with _JOBS_LOCK:
        job.update(values)


def _job_snapshot(job):
    with _JOBS_LOCK:
        out = {key: copy.deepcopy(value) for key, value in job.items() if not key.startswith('_')}
        owned = job.get('_control')
    if owned:
        out.update(owned.snapshot())
        if out['status'] in ('queued', 'running') and out['controlStatus'] in ('paused', 'cancelling'):
            out['status'] = out['controlStatus']
    return out


class ServiceError(RuntimeError):
    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status, self.code, self.details = status, code, details


@contextlib.contextmanager
def coordinator():
    with _COORDINATOR:
        yield


def busy():
    with _JOBS_LOCK:
        return bool(_BUSY)


def _atomic_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + '.' + uuid.uuid4().hex + '.tmp'
    try:
        with open(temp, 'w', encoding='utf-8') as file:
            json.dump(value, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.remove(temp)


def _text(value, name, maximum=4096, empty=True):
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value or (not empty and not value.strip()):
        raise ServiceError(400, 'invalid_field', 'Invalid ' + name)
    return value


class _RepairProvider:
    """A proposal-local view pins identified artifacts without changing a provider."""
    def __init__(self, provider, versions):
        self.provider, self.pinned = provider, versions
        self.name = getattr(provider, 'name', 'modrinth')

    def version(self, ident):
        if ident in self.pinned:
            return copy.deepcopy(self.pinned[ident])
        return self.provider.version(ident)

    def __getattr__(self, name):
        return getattr(self.provider, name)


class InstallationService:
    def __init__(self, game_dir, state_dir, data_dir, *, profiles, save_profile,
                 context, versions=None, running=None, external_busy=None,
                 prepare_loader=None, open_path=None, pick_path=None,
                 launch_status=None, launch_stop=None, provider=None, redact=None, diagnosis_enabled=None):
        self.game_dir = os.path.abspath(game_dir)
        self.state_dir = os.path.abspath(state_dir)
        self.data_dir = os.path.abspath(data_dir)
        self.profiles, self.save_profile = profiles, save_profile
        self.context_callback, self.versions = context, versions or (lambda: [])
        self.running = running or (lambda: False)
        self.external_busy = external_busy or (lambda: False)
        self.prepare_loader = prepare_loader
        self.open_path, self.pick_path = open_path, pick_path
        self.launch_status, self.launch_stop = launch_status, launch_stop
        self.provider = provider or ModrinthProvider()
        self.redact = redact or (lambda text: text)
        self.diagnosis_enabled = diagnosis_enabled or (lambda: True)

    def _profile(self, ident):
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', ident):
            raise ServiceError(400, 'invalid_instance', 'Invalid installation ID')
        profile = self.profiles().get(ident)
        if not isinstance(profile, dict):
            raise ServiceError(404, 'missing_instance', 'This installation no longer exists')
        return dict(profile)

    def _key(self, ident):
        return (os.path.normcase(self.data_dir), ident)

    def _root(self, profile):
        target = profile.get('gameDir') or self.game_dir
        if not isinstance(target, str):
            raise ServiceError(400, 'invalid_directory', 'Invalid game directory')
        try:
            return str(canonical_root(os.path.abspath(target)))
        except ContentError as error:
            raise ServiceError(409, 'unsafe_directory', str(error)) from error

    def _context(self, profile):
        out = dict(self.context_callback(profile) or {})
        if not out.get('minecraft'):
            raise ServiceError(409, 'missing_version', 'Select a Minecraft version before managing content')
        out['loader'] = {'fabric-loader': 'fabric', 'quilt-loader': 'quilt'}.get(out.get('loader'), out.get('loader') or 'vanilla')
        return out

    def _ownership(self, ident, profile):
        root = self._root(profile)
        normalized = os.path.normcase(root)
        shared = []
        for other_id, other in self.profiles().items():
            if other_id == ident or not isinstance(other, dict):
                continue
            try:
                candidate = self._root(other)
            except ServiceError:
                continue
            if os.path.normcase(candidate) == normalized:
                shared.append(other_id)
        defaults = {os.path.normcase(os.path.realpath(p)) for p in (self.game_dir, self.state_dir, self.data_dir)}
        dynamic = str(profile.get('lastVersionId') or '').startswith('latest') or profile.get('type') in ('latest-release', 'latest-snapshot')
        private = bool(profile.get('gameDir')) and normalized not in defaults and not shared and not dynamic
        reason = ('This installation follows the latest Minecraft version. Make a pinned independent copy to manage mods.' if dynamic else
                  'This game directory is shared. Make an independent copy before changing its content.' if shared or normalized in defaults or not profile.get('gameDir') else '')
        return {'root': root, 'private': private, 'sharedWith': shared, 'reason': reason}

    def _ensure_mutable(self, ident, profile=None, private=True):
        profile = profile or self._profile(ident)
        if self.running():
            raise ServiceError(409, 'game_running', 'Close Minecraft before changing this installation')
        with _JOBS_LOCK:
            instance_busy = self._key(ident) in _BUSY
        if self.external_busy() or instance_busy:
            raise ServiceError(409, 'operation_running', 'Wait for the current installation operation to finish')
        ownership = self._ownership(ident, profile)
        if private and not ownership['private']:
            raise ServiceError(409, 'shared_directory', ownership['reason'], ownership)
        return profile, ownership

    def _store(self, ident, profile=None):
        profile = profile or self._profile(ident)
        return ContentStore(self._root(profile), self.data_dir, ident, is_running=self.running,
                            can_recover=lambda: self._ownership(ident, self._profile(ident))['private'], allow_missing=True)

    def get(self, ident, resource='', params=None):
        params = params or {}
        profile = self._profile(ident)
        if not resource:
            return {'ok': True, 'id': ident, 'profile': dict(profile, id=ident),
                    'context': self._context(profile), 'ownership': self._ownership(ident, profile),
                    'versions': self.versions(), 'running': bool(self.running()),
                    'loaderChoices': ['vanilla', 'fabric', 'quilt', 'forge', 'neoforge'],
                    'settings': self._settings(ident), 'latestJob': self.get(ident, 'job')['job']}
        if resource == 'content':
            return dict(self._store(ident, profile).inventory(), ok=True,
                        gameRunning=bool(self.running()), ownership=self._ownership(ident, profile))
        if resource == 'catalog':
            kind = self._kind(params.get('kind'))
            context = self._context(profile)
            return dict(self.provider.search(str(params.get('query') or ''), kind,
                                             context['minecraft'], self._loaders(context, kind),
                                             offset=max(0, int(params.get('offset') or 0))), ok=True)
        if resource == 'versions':
            context = self._context(profile)
            kind = self._kind(params.get('kind'))
            project = _text(params.get('projectId'), 'project', 128, False)
            return {'ok': True, 'versions': self.provider.versions(project, context['minecraft'], self._loaders(context, kind))}
        if resource == 'job':
            with _JOBS_LOCK:
                job_id = params.get('jobId') or _LATEST.get(self._key(ident))
                job = _JOBS.get(job_id)
                if not job:
                    return {'ok': True, 'job': {'status': 'idle'}}
                if job.get('_key') != self._key(ident):
                    raise ServiceError(404, 'missing_job', 'No operation for this installation')
                return {'ok': True, 'job': _job_snapshot(job)}
        if resource == 'tools':
            return dict(self._tools_get(ident, profile, params),
                        gameRunning=bool(self.running()), ownership=self._ownership(ident, profile))
        raise ServiceError(404, 'unknown_action', 'Unknown installation resource')

    @staticmethod
    def _kind(value):
        if value not in ('mods', 'resourcepacks', 'shaderpacks'):
            raise ServiceError(400, 'invalid_kind', 'Choose mods, resource packs or shaders')
        return value

    @staticmethod
    def _loaders(context, kind):
        if kind == 'resourcepacks':
            return ['minecraft']
        if kind == 'shaderpacks':
            return []
        loader = context.get('loader')
        return [] if loader in ('vanilla', None, '') else [loader]

    def _settings_path(self, ident):
        self._profile(ident)
        return os.path.join(self.data_dir, 'installation-settings', ident + '.json')

    def _settings(self, ident):
        try:
            path = canonical_root(self._settings_path(ident))
            with open(path, encoding='utf-8-sig') as file:
                raw = file.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError('settings limit')
            out = json.loads(raw)
            if not isinstance(out, dict):
                raise ValueError('settings object required')
            return out
        except FileNotFoundError:
            return {}
        except OSError as error:
            raise ServiceError(409, 'settings_unavailable', 'Installation notes and settings could not be read. Existing data was preserved; restore access and retry.') from error
        except (ValueError, UnicodeError) as error:
            raise ServiceError(409, 'invalid_settings', 'Installation notes and settings are unreadable. Existing data was preserved; repair the saved settings before editing.') from error

    def _notes(self, ident):
        settings = self._settings(ident)
        text = settings.get('notes', '')
        if not isinstance(text, str) or len(text) > 1024 * 1024 or '\x00' in text:
            raise ServiceError(409, 'invalid_settings', 'Saved installation notes are invalid. Existing data was preserved; repair the saved settings before editing.')
        return settings, text, self._notes_revision(ident, text)

    def _notes_revision(self, ident, text):
        encoded = json.dumps([self._key(ident), text], ensure_ascii=True, separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()

    def post(self, ident, resource, body=None):
        body = body or {}
        if not isinstance(body, dict):
            raise ServiceError(400, 'invalid_request', 'Invalid installation request')
        if resource == 'job/control':
            self._profile(ident)
            with _JOBS_LOCK:
                job = _JOBS.get(body.get('jobId'))
                if not job or job.get('_key') != self._key(ident) or not job.get('_control'):
                    raise ServiceError(404, 'missing_job', 'No operation for this installation')
                owned = job['_control']
            try:
                owned.request(body.get('action'))
            except download_control.ControlError as error:
                raise ServiceError(400 if error.code == 'invalid_control' else 409, error.code, str(error)) from error
            return {'ok': True, 'job': _job_snapshot(job)}
        with _COORDINATOR:
            profile = self._profile(ident)
            if resource == 'general':
                return self._general(ident, profile, body)
            if resource == 'copy':
                return self._copy(ident, profile, body)
            if resource == 'tools' and body.get('kind') == 'notes':
                text = _text(body.get('text'), 'notes', 1024 * 1024)
                if body.get('action') != 'save':
                    raise ServiceError(400, 'unknown_action', 'Unknown notes action')
                settings, current, revision = self._notes(ident)
                if not isinstance(body.get('revision'), str) or body['revision'] != revision:
                    raise ServiceError(409, 'stale_revision', 'Saved notes changed. Your draft was preserved; reload the saved notes before applying a new edit.')
                if text != current:
                    settings['notes'] = text
                    _atomic_json(self._settings_path(ident), settings)
                return {'ok': True, 'text': text, 'revision': self._notes_revision(ident, text)}
            if resource == 'tools' and body.get('kind') == 'logs' and body.get('action') == 'stop' and self.launch_stop:
                status = self.launch_status() if self.launch_status else {}
                if status.get('configurationId') != ident:
                    raise ServiceError(409, 'different_instance', 'This installation has no running Minecraft process')
                result = self.launch_stop()
                return dict({'ok': not bool(result.get('error')), 'status': result},
                            **({'error': result['error']} if result.get('error') else {}))
            if resource == 'tools' and body.get('kind') == 'logs' and body.get('action') == 'export':
                if not self.pick_path:
                    raise ServiceError(409, 'picker_unavailable', 'Native file selection is unavailable')
                target = self.pick_path({'save': True, 'defaultName': 'Minecraft-' + ident + '.log', 'filters': ['.log']})
                if not target:
                    return {'ok': True, 'cancelled': True, 'message': 'Log export was cancelled.'}
                target = str(canonical_root(target))
                root = self._root(profile)
                try:
                    inside = os.path.commonpath([root, target]) == root
                except ValueError:
                    inside = False
                if inside:
                    raise ServiceError(409, 'unsafe_destination', 'Export the log outside the installation folder')
                raw = self._tools_get(ident, profile, {'kind': 'logs'})['text'].encode('utf-8')
                with open(target, 'xb') as fh:
                    fh.write(raw)
                    fh.flush()
                    os.fsync(fh.fileno())
                return {'ok': True, 'message': 'The log was exported.', 'path': target}
            if resource == 'apply':
                saved = _PLANS.get(body.get('planId'))
                if saved and saved['_key'] == self._key(ident) and saved.get('jobId'):
                    return {'ok': True, 'jobId': saved['jobId']}
            if resource == 'tools':
                if body.get('action') != 'reveal' and not (body.get('action') == 'export' and body.get('kind') in ('screenshots', 'servers')):
                    self._ensure_mutable(ident, profile, private=body.get('action') not in ('export', 'backup'))
                return self._tools_post(ident, profile, body)
            self._ensure_mutable(ident, profile)
            if resource == 'plan':
                return self._plan(ident, profile, body)
            if resource == 'apply':
                return self._apply(ident, profile, body)
            if resource == 'import':
                return self._import(ident, profile, body)
            if resource == 'preview-change':
                action = body.get('action')
                if action not in ('toggle', 'remove'):
                    raise ServiceError(400, 'unknown_action', 'Choose enable, disable or remove')
                self._kind(body.get('kind'))
                return self._change(ident, profile, action, body, preview=True)
            if resource in ('toggle', 'remove'):
                return self._change(ident, profile, resource, body)
            raise ServiceError(404, 'unknown_action', 'Unknown installation action')

    def _general(self, ident, profile, body):
        self._ensure_mutable(ident, profile, private=False)
        patch = body.get('patch')
        allowed = {'name', 'icon', 'lastVersionId', 'minecraft', 'loader', 'loaderVersion', 'gameDir', 'javaDir', 'javaArgs', 'resolution'}
        if not isinstance(patch, dict) or set(patch) - allowed:
            raise ServiceError(400, 'invalid_patch', 'Unsupported installation setting')
        patch = dict(patch)
        for key in ('name', 'icon', 'lastVersionId', 'minecraft', 'loader', 'loaderVersion', 'gameDir', 'javaDir', 'javaArgs'):
            if key in patch:
                _text(patch[key], key, 8192 if key == 'javaArgs' else 4096, empty=key not in ('name', 'lastVersionId', 'minecraft'))
        if profile.get('type') in ('latest-release', 'latest-snapshot'):
            for key in ('name', 'icon', 'lastVersionId', 'minecraft', 'loader', 'loaderVersion'):
                if key in patch and patch[key] not in ('', profile.get(key), self._context(profile).get(key)):
                    raise ServiceError(409, 'default_profile', 'Make a custom installation to change the latest version profile')
        if 'resolution' in patch:
            resolution = patch['resolution']
            if resolution is not None and (not isinstance(resolution, dict) or
                    any(not isinstance(resolution.get(key), int) or isinstance(resolution.get(key), bool) or not (300 if key == 'width' else 260) <= resolution[key] <= 9999 for key in ('width', 'height'))):
                raise ServiceError(400, 'invalid_resolution', 'Enter a valid window width and height')
        context = self._context(profile)
        if patch.get('lastVersionId') and patch['lastVersionId'] != profile.get('lastVersionId'):
            if not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', patch['lastVersionId']):
                raise ServiceError(400, 'invalid_version', 'Select a valid Minecraft version')
            if any(item['kind'] == 'mods' for item in self._store(ident, profile).inventory()['items']):
                raise ServiceError(409, 'content_version_change', 'Make an independent copy before changing the version of an installation with mods')
        minecraft = patch.pop('minecraft', context['minecraft'])
        loader = patch.pop('loader', context['loader'])
        loader_version = patch.pop('loaderVersion', context.get('loaderVersion') or '')
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', minecraft) or loader_version and not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', loader_version):
            raise ServiceError(400, 'invalid_version', 'Select a valid Minecraft and loader version')
        if loader not in ('vanilla', 'fabric', 'quilt', 'forge', 'neoforge'):
            raise ServiceError(400, 'invalid_loader', 'Unsupported mod loader')
        if minecraft != context['minecraft'] or loader != context['loader'] or loader_version != (context.get('loaderVersion') or ''):
            items = self._store(ident, profile).inventory()['items']
            if any(item['kind'] == 'mods' for item in items):
                raise ServiceError(409, 'content_version_change', 'Make an independent copy before changing the Minecraft or loader version of an installation with mods')
            if loader == 'vanilla':
                patch['lastVersionId'] = minecraft
            elif self.prepare_loader:
                patch['lastVersionId'] = self.prepare_loader(minecraft, loader, loader_version)
            else:
                raise ServiceError(409, 'loader_unavailable', 'Loader preparation is unavailable')
        if patch.get('gameDir'):
            canonical_root(os.path.abspath(patch['gameDir']))
        updated = dict(profile, **patch)
        self.save_profile(ident, updated)
        return self.get(ident)

    def _copy(self, ident, profile, body):
        self._ensure_mutable(ident, profile, private=False)
        name = _text(body.get('name') or (profile.get('name') or 'Installation') + ' copy', 'copy name', 128, False).strip()
        taken = {p.get('name') for p in self.profiles().values() if isinstance(p, dict)}
        base, index = name, 2
        while name in taken:
            name = base + ' (' + str(index) + ')'
            index += 1
        new_id = uuid.uuid4().hex
        instances_root = str(canonical_root(os.path.join(self.game_dir, 'instances')))
        os.makedirs(instances_root, exist_ok=True)
        directory = os.path.join(instances_root, new_id)
        source = self._root(profile)
        original_context = self._context(profile)
        entries = ('mods', 'resourcepacks', 'shaderpacks', 'config', 'saves', 'screenshots', 'options.txt', 'servers.dat')
        entries += tuple(self._copy_version_entries(source, original_context.get('versionId')))
        before = self._copy_snapshot(source, entries)
        os.mkdir(directory)
        try:
            for entry in entries:
                original = os.path.join(source, entry)
                if not os.path.lexists(original):
                    continue
                self._safe_tree(original)
                destination = os.path.join(directory, entry)
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                if os.path.isdir(original):
                    shutil.copytree(original, destination)
                else:
                    shutil.copy2(original, destination)
            if before != self._copy_snapshot(source, entries) or before != self._copy_snapshot(directory, entries):
                raise ServiceError(409, 'source_changed', 'Installation files changed during copying. The original was preserved; try again after the changes finish.')
            if original_context != self._context(self._profile(ident)) or source != self._root(self._profile(ident)):
                raise ServiceError(409, 'changed_installation', 'The original installation changed during copying')
            context = self._context(profile)
            updated = dict(profile, name=name, type='custom', gameDir=directory,
                           lastVersionId=context.get('versionId') if context.get('versionId') and not str(context['versionId']).startswith('latest') else context['minecraft'])
            updated.pop('id', None)
            # Reconcile copied bytes with the original managed catalog evidence.
            # The copy has its own root manifest and keeps actual parsed identity.
            evidence = self._store(ident, profile).inventory()['items']
            copied_store = ContentStore(directory, self.data_dir, new_id, is_running=self.running)
            changes = []
            for item in evidence:
                name = item['filename'][:-9] if item['filename'].lower().endswith('.disabled') else item['filename']
                metadata = {key: item[key] for key in ('provider', 'projectId', 'versionId', 'providerDependencies', 'releaseChannels', 'gameVersions', 'loaders', 'embeddedProjects', 'embeddedVersions', 'userRequested', 'requiredBy', 'iconUrl') if key in item}
                if metadata and not item.get('directory'):
                    changes.append({'action': 'install', 'kind': item['kind'], 'filename': name,
                                    'source': os.path.join(directory, item['path']), 'metadata': metadata})
            if changes:
                copied_store.transact(changes)
            self.save_profile(new_id, updated)
        except Exception:
            if os.path.commonpath([os.path.realpath(directory), os.path.realpath(instances_root)]) != os.path.realpath(instances_root):
                raise ServiceError(409, 'unsafe_directory', 'Copy cleanup escaped the installation directory')
            shutil.rmtree(directory)
            raise
        return {'ok': True, 'instanceId': new_id, 'profile': updated}

    def _copy_version_entries(self, source, version_id):
        """Keep the selected instance-local metadata chain, with shared assets shared."""
        entries, seen = [], set()
        pending = [version_id] if version_id else []
        while pending:
            ident = pending.pop()
            if not installer._safe_version_id(ident):
                raise ServiceError(409, 'invalid_metadata', 'The installation has an invalid inherited version ID')
            if ident in seen:
                continue
            seen.add(ident)
            if len(seen) > 32:
                raise ServiceError(409, 'invalid_metadata', 'The installation version metadata chain is too deep')
            path = installer._version_metadata_path(self.state_dir, ident, [self.game_dir, source])
            if not path:
                continue
            try:
                with open(path, 'rb') as file:
                    raw = file.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise ValueError('metadata is too large')
                doc = json.loads(raw.decode('utf-8-sig'))
                if not isinstance(doc, dict):
                    raise ValueError('metadata is not an object')
            except (OSError, ValueError) as error:
                raise ServiceError(409, 'invalid_metadata', 'The installation version metadata cannot be copied: ' + str(error)) from error
            for dependency in (doc.get('inheritsFrom'), doc.get('jar')):
                if dependency and dependency != ident:
                    pending.append(dependency)
            if installer._version_metadata_path(self.state_dir, ident, [self.game_dir]):
                continue
            relative = os.path.join('versions', ident, ident + '.json')
            expected = os.path.join(source, relative)
            if str(canonical_root(expected)) != os.path.normcase(os.path.realpath(path)):
                raise ServiceError(409, 'unsafe_directory', 'The installation version metadata escaped its source directory')
            entries.append(relative)
        return entries

    @classmethod
    def _copy_snapshot(cls, root, entries):
        # Compare copied bytes, rather than assuming a successful filesystem
        # copy means the source remained stable while it was read.
        out = {}
        for entry in entries:
            source = os.path.join(root, entry)
            if not os.path.lexists(source):
                continue
            cls._safe_tree(source)
            paths = [source]
            if os.path.isdir(source):
                paths.extend(os.path.join(folder, name) for folder, dirs, files in os.walk(source) for name in dirs + files)
            for path in paths:
                key = os.path.relpath(path, root)
                if os.path.isdir(path):
                    out[key] = ('directory',)
                    continue
                stat = os.stat(path)
                digest = hashlib.sha256()
                with open(path, 'rb') as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b''):
                        digest.update(chunk)
                out[key] = (stat.st_size, digest.hexdigest())
        return out

    @staticmethod
    def _safe_tree(root):
        def check(path):
            if os.path.islink(path) or (hasattr(os.path, 'isjunction') and os.path.isjunction(path)):
                raise ServiceError(409, 'linked_file', 'Linked folders cannot be copied as independent installation data')
            attrs = getattr(os.lstat(path), 'st_file_attributes', 0)
            if attrs & 0x400:
                raise ServiceError(409, 'linked_file', 'Reparse points cannot be copied as independent installation data')
        check(root)
        if os.path.isdir(root):
            for folder, dirs, files in os.walk(root, followlinks=False):
                check(folder)
                for name in dirs + files:
                    check(os.path.join(folder, name))

    def _tools_get(self, ident, profile, params):
        kind = params.get('kind')
        if kind == 'notes':
            _, text, revision = self._notes(ident)
            return {'ok': True, 'text': text, 'revision': revision, 'capabilities': ['save']}
        if kind == 'logs':
            status = self.launch_status() if self.launch_status else {}
            from instance_tools import read_logs
            result = read_logs(self._root(profile), instance_id=ident,
                               launch_status=status, running=bool(self.running()), redact=self.redact,
                               diagnosis_enabled=self.diagnosis_enabled())
            if result['running'] and self.launch_stop:
                result['capabilities'].append('stop')
            return result
        from instance_tools import read_tools
        result = read_tools(self._root(profile), kind, dict(params, instanceId=ident), private=self._ownership(ident, profile)['private'], running=bool(self.running()))
        if kind == 'advanced':
            result.update(settings=dict(profile), ownership=self._ownership(ident, profile))
            folder = str(canonical_root(os.path.join(self.data_dir, 'workspace-backups', ident)))
            backups = []
            if os.path.isdir(folder):
                for name in os.listdir(folder):
                    if not re.fullmatch(r'[a-f0-9]{32}\.zip', name):
                        continue
                    path = str(canonical_root(os.path.join(folder, name)))
                    stat = os.stat(path)
                    backups.append({'id': name[:-4], 'name': 'Backup ' + time.strftime('%Y-%m-%d %H:%M', time.localtime(stat.st_mtime)), 'size': stat.st_size, 'createdAt': stat.st_mtime})
            result['backups'] = sorted(backups, key=lambda item: item['createdAt'], reverse=True)
            root = self._root(profile)
            try:
                history = str(canonical_root(os.path.join(root, '.launcher-content', 'history')))
                if os.path.commonpath([root, history]) == root and os.path.isdir(history):
                    result['contentRecoveryPath'] = history
            except ContentError:
                # Recovery artifacts may have been moved or linked externally.
                # Keep the safe Advanced tools available without exposing that path.
                pass
        return result

    def _tools_post(self, ident, profile, body):
        if body.get('kind') == 'logs' and body.get('action') == 'stop' and self.launch_stop:
            result = self.launch_stop()
            return dict({'ok': not bool(result.get('error')), 'status': result},
                        **({'error': result['error']} if result.get('error') else {}))
        from instance_tools import mutate_tools
        return mutate_tools(self._root(profile), self.data_dir, ident,
                            dict(body, private=self._ownership(ident, profile)['private'], running=bool(self.running())),
                            open_path=self.open_path, pick_path=self.pick_path)

    def _plan(self, ident, profile, body):
        kind = self._kind(body.get('kind'))
        context = dict(self._context(profile), kind=kind)
        snapshot = self._store(ident, profile).inventory()
        revision = body.get('revision')
        repair = body.get('repairDependencies', False)
        if not isinstance(repair, bool):
            raise ServiceError(400, 'invalid_field', 'Dependency repair must be a boolean')
        if repair and (not isinstance(revision, str) or not revision):
            raise ServiceError(400, 'invalid_revision', 'Refresh the installed list before checking dependencies')
        if revision and revision != snapshot['revision']:
            raise ServiceError(409, 'stale_revision', 'Content changed. Refresh the installed list and try again.')
        if kind == 'mods' and context['loader'] == 'vanilla':
            raise ServiceError(409, 'loader_required', 'Choose a compatible mod loader in General before installing mods')
        selections = body.get('selections')
        if repair:
            if kind != 'mods' or body.get('updates'):
                raise ServiceError(400, 'invalid_selection', 'Dependency repair requires installed mods')
            selections, proposal = self._repair_dependencies(context, snapshot, body)
        elif body.get('updates'):
            chosen = body.get('ids')
            items = [item for item in snapshot['items'] if item['kind'] == kind and (not chosen or item['id'] in chosen)]
            if 'releaseChannels' in body:
                channels = body['releaseChannels']
                if not isinstance(channels, list) or not channels or any(channel not in ('release', 'beta', 'alpha') for channel in channels):
                    raise ServiceError(400, 'invalid_channels', 'Choose release, beta or alpha update channels')
                items = [dict(item, releaseChannels=channels) for item in items]
            selections = self.provider.updates(items, context['minecraft'], self._loaders(context, kind))
            if 'releaseChannels' in body:
                selections = [dict(selection, releaseChannels=body['releaseChannels']) for selection in selections]
            if not selections:
                return {'ok': True, 'planId': None, 'revision': snapshot['revision'],
                        'files': [], 'reuse': [], 'enable': [], 'remove': [], 'conflicts': [],
                        'warnings': ['No compatible updates were found for these files.']}
        if not repair and (not isinstance(selections, list) or not selections or len(selections) > 256):
            raise ServiceError(400, 'invalid_selection', 'Select a compatible content version')
        include_optional = bool(body.get('includeOptional')) and not repair
        if not repair:
            proposal = resolve_plan(self.provider, context, selections, snapshot['items'], include_optional)
        now = time.time()
        for old_id, old in list(_PLANS.items()):
            if old['created'] < now - 900:
                _PLANS.pop(old_id, None)
        identity = {'_key': self._key(ident), 'root': self._root(profile),
                    'profileVersion': profile.get('lastVersionId'), 'revision': snapshot['revision'],
                    'context': context, 'selections': selections, 'proposal': proposal,
                    'includeOptional': include_optional, 'updates': bool(body.get('updates'))}
        if repair:
            identity['repairDependencies'] = True
            identity['repairIds'] = body.get('ids')
        # Going Back and reviewing an unchanged selection must not consume a new
        # review slot. Re-resolve first so changed remote evidence stays distinct.
        for old_id, old in _PLANS.items():
            if not old.get('jobId') and all(old.get(key, False if key in ('includeOptional', 'updates') else None) == value
                                           for key, value in identity.items()):
                return dict(proposal, ok=True, planId=old_id, revision=snapshot['revision'])
        pending = [old_id for old_id, old in _PLANS.items()
                   if old['_key'] == self._key(ident) and not old.get('jobId')]
        if len(pending) >= 32:
            # Reviews hold no changed files. Retire the oldest unused reviews
            # so browsing versions stays usable; applying one requires review
            # again through the existing expired-plan recovery contract.
            pending.sort(key=lambda old_id: _PLANS[old_id]['created'])
            for old_id in pending[:len(pending) - 31]:
                _PLANS.pop(old_id, None)
        plan_id = uuid.uuid4().hex
        _PLANS[plan_id] = dict(identity, created=now)
        return dict(proposal, ok=True, planId=plan_id, revision=snapshot['revision'])

    def _repair_dependencies(self, context, snapshot, body):
        installed = copy.deepcopy(snapshot['items'])
        mods = {item['id']: item for item in installed if item['kind'] == 'mods'}
        chosen = body.get('ids') if 'ids' in body else None
        if 'ids' in body:
            if (not isinstance(chosen, list) or not 1 <= len(chosen) <= 256 or
                    any(not isinstance(value, str) or not value or len(value) > 512 or value not in mods for value in chosen) or
                    len(set(chosen)) != len(chosen)):
                raise ServiceError(400, 'invalid_selection', 'Select existing mod file IDs before checking dependencies')
        roots = [item for item in mods.values() if item.get('enabled', True) and (chosen is None or item['id'] in chosen)]
        if len(roots) > 256:
            raise ServiceError(400, 'invalid_selection', 'Check at most 256 installed mods at a time')
        pinned, selections, unidentified, conflicts = {}, [], [], []
        provider_name = getattr(self.provider, 'name', 'modrinth')
        for item in roots:
            managed = item.get('provider') == provider_name and bool(item.get('projectId')) and bool(item.get('versionId'))
            try:
                version = self.provider.version(item['versionId']) if managed else self.provider.identify(item.get('hashes') or {})
            except (ProviderError, KeyError, OSError, ValueError) as error:
                raise ServiceError(502, 'provider_unavailable', 'Dependency metadata could not be checked. Retry this check.',
                                   {'itemId': item['id'], 'error': str(error)}) from error
            files = version.get('files') if isinstance(version, dict) else None
            valid_identity = (isinstance(version, dict) and isinstance(version.get('id'), str) and
                              0 < len(version['id']) <= 512 and isinstance(version.get('project_id'), str) and
                              0 < len(version['project_id']) <= 512)
            if managed and (not valid_identity or version['id'] != item['versionId'] or version['project_id'] != item['projectId']):
                raise ServiceError(502, 'provider_unavailable', 'Pinned mod metadata is unavailable or disagrees with its installed identity. Retry this check.',
                                   {'itemId': item['id']})
            candidates = files if isinstance(files, list) else []
            matching = []
            for file in candidates:
                try:
                    hashes = validate_file(file)
                except ProviderError:
                    continue
                current = item.get('hashes') or {}
                if hashes and all(current.get(key, '').lower() == digest for key, digest in hashes.items()):
                    matching.append(file)
            if not valid_identity or len(matching) != 1:
                if managed:
                    conflicts.append({'code': 'root_hash_mismatch', 'message': 'Installed mod bytes do not match one verified file in the pinned provider version. Repair cannot replace this mod.',
                                      'itemId': item['id'], 'versionId': item['versionId']})
                else:
                    unidentified.append({key: item[key] for key in ('id', 'filename', 'name') if key in item})
                continue
            version = copy.deepcopy(version)
            version['files'] = [dict(copy.deepcopy(matching[0]), primary=True)]
            # Exact on-disk roots supply their own module identities and hard
            # requirements; catalog names/aliases cannot manufacture those.
            for field in ('modIds', 'provides', 'embeddedModIds', 'depends', 'conflicts', 'optionalDepends', 'warnings'):
                version.pop(field, None)
            previous = pinned.get(version['id'])
            if previous is not None and previous != version:
                conflicts.append({'code': 'duplicate_identity', 'message': 'Several installed artifacts claim the same provider version. Choose one exact artifact before repairing.',
                                  'versionId': version['id'], 'itemId': item['id']})
                continue
            pinned[version['id']] = version
            selections.append({'projectId': version['project_id'], 'versionId': version['id']})
            evidence = {'provider': provider_name, 'projectId': version['project_id'], 'versionId': version['id'],
                        'providerDependencies': copy.deepcopy(version.get('dependencies') or []),
                        'gameVersions': copy.deepcopy(version.get('game_versions') or []),
                        'loaders': copy.deepcopy(version.get('loaders') or [])}
            item.update(ContentStore._metadata(evidence, item['hashes']))
        view = _RepairProvider(self.provider, pinned)
        proposal = (resolve_plan(view, context, selections, installed, False) if selections else
                    {'files': [], 'reuse': [], 'enable': [], 'remove': [], 'conflicts': [], 'warnings': [], 'reuseMetadata': {}})
        for item in roots:
            metadata = proposal['reuseMetadata'].get(item['id'])
            if metadata is not None:
                for field in ('userRequested', 'requiredBy'):
                    if field in item:
                        metadata[field] = copy.deepcopy(item[field])
                    else:
                        metadata.pop(field, None)
        if proposal['remove']:
            conflicts.append({'code': 'installed_replacement', 'message': 'A required version would replace an installed mod. Resolve that version conflict before repairing dependencies.',
                              'itemIds': list(proposal['remove'])})
        # Preserve every physical archive field through final-set validation,
        # including roots the catalog resolver represents with proposal nodes.
        prospective = []
        for item in installed:
            if item['id'] in proposal['enable']:
                item['enabled'] = True
            metadata = proposal['reuseMetadata'].get(item['id'])
            if metadata:
                item.update(ContentStore._metadata(metadata, item['hashes']))
            prospective.append(item)
        prospective.extend(dict(file, id=file['versionId'], enabled=True) for file in proposal['files'])
        validation = validate_inventory(view, context, prospective)
        for problem in conflicts + validation['conflicts']:
            if problem not in proposal['conflicts']:
                proposal['conflicts'].append(problem)
        self._defer_repair_archive_checks(proposal, roots, pinned, prospective, provider_name)
        for item in unidentified:
            proposal['warnings'].append({'code': 'unidentified_root', 'message': 'This installed mod could not be identified by its exact file hashes; catalog dependencies were not inferred.', **item})
        for warning in validation['warnings']:
            if warning not in proposal['warnings']:
                proposal['warnings'].append(warning)
        proposal['dependencyRepair'] = {'checked': len(roots), 'unidentified': unidentified}
        return selections, proposal

    @staticmethod
    def _defer_repair_archive_checks(proposal, roots, pinned, prospective, provider_name):
        # A provider release identifies exact bytes, not the module IDs inside
        # those bytes. Defer only a checked root's missing module requirement
        # when its required provider graph will acquire an unknown-ID archive.
        unknown = {file['versionId']: file for file in proposal['files'] if file.get('kind') == 'mods' and
                   not any(file.get(field) for field in ('modIds', 'provides', 'embeddedModIds'))}
        if not unknown:
            return
        pending_by_root = {}
        for root in roots:
            if root.get('versionId') not in pinned:
                continue
            reached, queue = set(), [root]
            while queue:
                item = queue.pop()
                marker = item.get('versionId') or item['id']
                if marker in reached:
                    continue
                reached.add(marker)
                for relation in item.get('providerDependencies') or []:
                    if relation.get('dependency_type', 'required') != 'required':
                        continue
                    exact, project = relation.get('version_id'), relation.get('project_id')
                    queue.extend(target for target in prospective if target.get('provider') == provider_name and
                                 ((exact and target.get('versionId') == exact) or
                                  (not exact and project and target.get('projectId') == project)))
            files = [unknown[ident]['filename'] for ident in sorted(reached & unknown.keys())]
            if files:
                pending_by_root[root['id']] = files
        direct = {}
        deferred = set()
        for problem in proposal['conflicts']:
            if problem.get('code') != 'missing_dependency' or not problem.get('dependency') or not problem.get('itemId'):
                continue
            key = (problem['itemId'], problem['dependency'])
            direct.setdefault(problem['dependency'], set()).add(key)
            if problem['itemId'] in pending_by_root:
                deferred.add(key)
        retained = []
        for problem in proposal['conflicts']:
            dependency = problem.get('dependency')
            if problem.get('code') == 'missing_dependency' and dependency:
                if (problem.get('itemId'), dependency) in deferred:
                    continue
                # Canonical candidate selection also emits an aggregate error.
                # Remove it only if every attributed root requirement was
                # deferred; an unknown/unselected root remains blocking.
                attributed = direct.get(dependency)
                if not problem.get('itemId') and attributed and attributed <= deferred:
                    continue
            retained.append(problem)
        proposal['conflicts'] = retained
        for item_id, dependency in sorted(deferred):
            proposal['warnings'].append({'code': 'archive_requirement_pending',
                'message': 'Required module identities will be verified from downloaded archives before commit; unmet requirements will preserve existing content.',
                'itemId': item_id, 'dependency': dependency, 'files': pending_by_root[item_id]})

    def _apply(self, ident, profile, body):
        plan_id = body.get('planId')
        saved = _PLANS.get(plan_id)
        if not saved or saved['_key'] != self._key(ident) or saved['created'] < time.time() - 900:
            raise ServiceError(409, 'expired_plan', 'This content review expired. Resolve it again before installing.')
        if saved.get('jobId'):
            return {'ok': True, 'jobId': saved['jobId']}
        if saved['root'] != self._root(profile) or saved['profileVersion'] != profile.get('lastVersionId') or not self._same_context(saved, profile):
            raise ServiceError(409, 'changed_installation', 'This installation changed. Resolve the content again.')
        if saved['proposal'].get('conflicts'):
            raise ServiceError(409, 'dependency_conflict', 'Resolve the listed compatibility problems before installing', saved['proposal']['conflicts'])
        if self._store(ident, profile).inventory()['revision'] != saved['revision']:
            raise ServiceError(409, 'stale_revision', 'Installed content changed after this review. Resolve it again.')
        job_id = self._start_job(ident, 'Installing content', lambda job: self._apply_job(ident, saved, job),
                                 kind=saved['context']['kind'])
        saved['jobId'] = job_id
        return {'ok': True, 'jobId': job_id}

    def _same_context(self, saved, profile):
        return {key: value for key, value in saved['context'].items() if key != 'kind'} == self._context(profile)

    def _start_job(self, ident, message, worker, kind=None):
        key = self._key(ident)
        with _JOBS_LOCK:
            if key in _BUSY:
                raise ServiceError(409, 'operation_running', 'An operation is already running for this installation')
            _BUSY.add(key)
        job_id = uuid.uuid4().hex
        owned = download_control.JobControl(job_id)
        job = {'id': job_id, '_key': key, 'status': 'queued', 'phase': 'preparing', 'message': message,
               'error': None, 'result': None, 'startedAt': time.time(), '_control': owned}
        if kind is not None:
            job['kind'] = kind
        with _JOBS_LOCK:
            _JOBS[job_id], _LATEST[key] = job, job_id

        def execute():
            try:
                with download_control.bind(owned):
                    owned.checkpoint()
                    _job_update(job, status='running')
                    result = worker(job)
                    owned.checkpoint()
                    owned.finish('done')
                    _job_update(job, status='succeeded', phase='complete', message='Content updated', result=result)
            except download_control.DownloadCancelled as error:
                owned.finish('cancelled')
                _job_update(job, status='cancelled', phase='cancelled', message=str(error), error=None,
                            code='download_cancelled')
            except Exception as error:
                owned.finish('error')
                _job_update(job, status='failed', phase='failed', message=str(error), error=str(error),
                            code=getattr(error, 'code', 'operation_failed'), details=getattr(error, 'details', None))
            finally:
                with _JOBS_LOCK:
                    _BUSY.discard(key)
                    _job_update(job, finishedAt=time.time())
        threading.Thread(target=execute, name='installation-' + job_id[:8], daemon=True).start()
        return job_id

    def _apply_job(self, ident, saved, job):
        from instance_content import inspect_file
        from content_resolver import validate_inventory
        stage = tempfile.mkdtemp(prefix='content-', dir=self.data_dir)
        owned = job['_control']
        try:
            changes, inspected = [], []
            files = saved['proposal'].get('files') or []
            _job_update(job, completed=0, total=len(files), currentFile=None, bytesDone=0,
                        bytesTotal=sum(content.get('size') or 0 for content in files))
            completed_bytes = 0
            for index, content in enumerate(files):
                owned.checkpoint()
                filename = str(content.get('filename') or 'content')
                _job_update(job, phase='downloading', message='Downloading ' + filename, completed=index,
                           total=len(files), currentFile=filename)
                destination = os.path.join(stage, str(index) + '.download')
                self._acquire(content, destination, owned,
                              lambda got: _job_update(job, bytesDone=completed_bytes + got, currentBytes=got))
                owned.checkpoint()
                _job_update(job, phase='verifying', message='Verifying ' + filename)
                kind = content.get('kind') or saved['context']['kind']
                item = inspect_file(destination, kind, content['filename'])
                metadata = {k: v for k, v in (content.get('metadata') or {}).items()
                            if k not in ('modIds', 'depends', 'conflicts', 'providedVersions', 'optionalDepends', 'metadataFormats', 'rootMetadataFormats', 'environment', 'warnings', 'unknown', 'metadataProblems', 'name', 'version', 'enabled', 'hashes')}
                metadata.update(provider=content.get('provider') or 'modrinth', projectId=content.get('projectId'),
                                versionId=content.get('versionId'), userRequested=content.get('userRequested', True),
                                requiredBy=content.get('requiredBy') or [])
                if content.get('iconUrl'):
                    metadata['iconUrl'] = content['iconUrl']
                item.update(metadata)
                replacements = content.get('replaces') or []
                if isinstance(replacements, str):
                    replacements = [replacements]
                # Updating a disabled file must not silently enable it.
                original = next((i for i in self._store(ident).inventory()['items'] if i['id'] in replacements), None)
                if original and not original.get('enabled', True):
                    item['enabled'] = False
                    metadata['enabled'] = False
                inspected.append(item)
                changes.append({'action': 'install', 'kind': kind, 'filename': content['filename'],
                                'source': destination, 'metadata': dict(metadata, hashes=content.get('hashes') or {}),
                                'replaces': content.get('replaces') or []})
                if not item.get('enabled', True):
                    changes.append({'action': 'toggle', 'id': item['id'], 'enabled': False})
                completed_bytes += os.path.getsize(destination)
                _job_update(job, completed=index + 1, bytesDone=completed_bytes)
            with _COORDINATOR:
                owned.checkpoint()
                profile = self._profile(ident)
                if self.running() or self.external_busy():
                    raise ServiceError(409, 'game_running', 'Close Minecraft before committing content changes')
                if saved['root'] != self._root(profile) or saved['profileVersion'] != profile.get('lastVersionId') or not self._same_context(saved, profile):
                    raise ServiceError(409, 'changed_installation', 'The installation changed during download')
                if not self._ownership(ident, profile)['private']:
                    raise ServiceError(409, 'shared_directory', 'The installation directory is no longer private')
                store = self._store(ident, profile)
                snapshot = store.inventory()
                if snapshot['revision'] != saved['revision']:
                    raise ServiceError(409, 'stale_revision', 'Local content changed during download; existing files were preserved')
                remove = set(saved['proposal'].get('remove') or [])
                for content in files:
                    replaces = content.get('replaces')
                    remove.update(replaces if isinstance(replaces, list) else [replaces] if replaces else [])
                prospective = [dict(item) for item in snapshot['items'] if item['id'] not in remove]
                for item in prospective:
                    if item['id'] in (saved['proposal'].get('enable') or []):
                        item['enabled'] = True
                    metadata = (saved['proposal'].get('reuseMetadata') or {}).get(item['id'])
                    if metadata:
                        item.update(ContentStore._metadata(metadata, item['hashes']))
                prospective.extend(inspected)
                validation = validate_inventory(self.provider, saved['context'], prospective)
                if validation.get('conflicts'):
                    raise ServiceError(409, 'archive_dependency_conflict', 'Verified files have incompatible or missing dependencies; existing content was preserved', validation['conflicts'])
                changes.extend({'action': 'toggle', 'id': item_id, 'enabled': True} for item_id in saved['proposal'].get('enable') or [])
                for item in snapshot['items']:
                    metadata = (saved['proposal'].get('reuseMetadata') or {}).get(item['id'])
                    if metadata and item['id'] not in remove:
                        name = item['filename'][:-9] if item['filename'].lower().endswith('.disabled') else item['filename']
                        changes.append({'action': 'install', 'kind': item['kind'], 'filename': name,
                                        'source': os.path.join(saved['root'], item['path']), 'metadata': metadata})
                replaced_ids = set()
                for change in changes:
                    replaces = change.get('replaces') or []
                    replaced_ids.update(replaces if isinstance(replaces, list) else [replaces])
                changes.extend({'action': 'remove', 'id': item_id} for item_id in saved['proposal'].get('remove') or [] if item_id not in replaced_ids)
                _job_update(job, phase='committing', message='Verifying and applying content changes',
                           completed=len(files), total=len(files), currentFile=None)
                with owned.noninterruptible('Verifying and applying content changes'):
                    result = store.transact(changes, expected_revision=saved['revision'])
                    owned.finish('done')
                final_items = {item['id']: item for item in result['items']}

                def records(items):
                    return [{key: item[key] for key in ('id', 'name', 'filename', 'version', 'projectId', 'versionId')
                             if key in item} for item in items]

                installed_ids = {item['id'] for item in inspected}
                reuse_ids = set(saved['proposal'].get('reuse') or []) - remove
                enabled_ids = set(saved['proposal'].get('enable') or [])
                summary = {
                    'installed': records(item for item in result['items'] if item['id'] in installed_ids),
                    'reused': records(item for item in result['items'] if item['id'] in reuse_ids),
                    'enabled': records(final_items[item['id']] for item in snapshot['items']
                                       if item['id'] in enabled_ids and not item.get('enabled', True)
                                       and final_items.get(item['id'], {}).get('enabled')),
                    'removed': records(item for item in snapshot['items'] if item['id'] in remove),
                }
                return dict(result, summary=summary, warnings=validation.get('warnings') or [])
        finally:
            if os.path.commonpath([os.path.realpath(stage), os.path.realpath(self.data_dir)]) != os.path.realpath(self.data_dir):
                raise ServiceError(409, 'unsafe_stage', 'Content staging cleanup escaped the data directory')
            shutil.rmtree(stage)

    def _acquire(self, content, destination, owned, progress):
        hashes = validate_file(content)
        digest = hashes.get('sha512') or hashes.get('sha256') or hashes.get('sha1')
        cache_root = os.path.join(self.data_dir, 'download-cache', 'content')
        cache = os.path.join(cache_root, digest)
        canonical_root(cache_root)
        os.makedirs(cache_root, exist_ok=True)
        canonical_root(cache)
        canonical_root(cache + '.part')
        if not download_control._verified(cache, content['size'], hashes):
            if hasattr(self.provider, 'transport'):
                try:
                    download_control.download(content['url'], cache, size=content['size'], hashes=hashes,
                        control=owned, progress=progress, opener=self.provider.transport, max_bytes=MAX_FILE_BYTES,
                        validate_response=lambda response: validate_url(response.geturl()))
                except (download_control.DownloadCancelled, ProviderError):
                    raise
                except download_control.DownloadError as error:
                    raise ProviderError(error.code, str(error)) from error
                except (OSError, ValueError, RuntimeError) as error:
                    raise ProviderError('download_failed', str(error)) from error
            else:
                # A provider adapter without a streaming boundary cannot safely
                # claim to pause its opaque acquisition call.
                try:
                    with owned.noninterruptible('Acquiring content through this provider'):
                        self.provider.download(content, cache)
                except Exception:
                    if os.path.isfile(cache) and not download_control._verified(cache, content['size'], hashes):
                        os.remove(cache)
                    raise
                if not download_control._verified(cache, content['size'], hashes):
                    os.remove(cache)
                    raise ProviderError('hash_mismatch', 'Acquired content failed its verification hash check.')
        owned.checkpoint()
        with owned.noninterruptible('Staging a verified content file'):
            shutil.copyfile(cache, destination)
        progress(content['size'])

    def _change(self, ident, profile, action, body, preview=False):
        from content_resolver import validate_inventory, canonical_inventory
        if 'action' in body and body['action'] != action:
            raise ServiceError(400, 'action_mismatch', 'This review is for a different operation. Review the selected files again.')
        context = self._context(profile)
        if (('root' in body and body['root'] != self._root(profile)) or
                ('profileVersion' in body and body['profileVersion'] != profile.get('lastVersionId')) or
                ('context' in body and body['context'] != context)):
            raise ServiceError(409, 'changed_installation', 'The installation changed. Review these files again before applying.')
        if action == 'toggle' and not isinstance(body.get('enabled'), bool):
            raise ServiceError(400, 'invalid_state', 'Choose enabled or disabled')
        store = self._store(ident, profile)
        snapshot = store.inventory()
        if body.get('revision') and body['revision'] != snapshot['revision']:
            raise ServiceError(409, 'stale_revision', 'Content changed. Refresh before changing these files.')
        ids = body.get('ids') if 'ids' in body or action == 'remove' else [body.get('id')]
        if (not isinstance(ids, list) or not ids or len(ids) > 1024 or
                any(not isinstance(item_id, str) or not item_id or len(item_id) > 128 or '\x00' in item_id for item_id in ids) or
                len(set(ids)) != len(ids)):
            raise ServiceError(400, 'invalid_selection', 'Select installed content to change')
        selected = {item['id']: item for item in snapshot['items'] if item['id'] in ids}
        if len(selected) != len(ids):
            raise ServiceError(404, 'missing_content', 'Selected content is no longer installed')
        kind = self._kind(body['kind']) if 'kind' in body else next(iter(selected.values()))['kind']
        if any(item['kind'] != kind for item in selected.values()):
            raise ServiceError(400, 'kind_mismatch', 'Select files from one content category at a time')
        requested_ids = list(ids)
        requested = dict(selected)
        affected = set(selected)
        enabling = action == 'toggle' and body.get('enabled') is True
        graph_items = [dict(item, enabled=True) if enabling and item['id'] in selected else dict(item)
                       for item in snapshot['items']]
        canonical = canonical_inventory(context, graph_items)
        edges = {item['id']: set() for item in snapshot['items']}
        edges.update(self._dependency_edges(canonical['items'], context))
        provider_edges = self._dependency_edges(snapshot['items'], context, local=False)
        for item_id, targets in provider_edges.items():
            edges[item_id].update(targets)
        if enabling:
            # A chosen nested module may live in an already enabled bundle.
            # Its disabled helpers still belong in the explicit enable review.
            affected.update(canonical['enable'])
        if not enabling:
            remaining = canonical_inventory(context, snapshot['items'], blocked=list(selected))
            # A surviving bundled candidate may replace the chosen source.
            # Only request dependent edits when the remaining set needs them.
            if not remaining['conflicts'] and not remaining['enable']:
                edges = {item['id']: set() for item in snapshot['items']}
        changed = True
        while changed:
            previous = set(affected)
            if enabling:
                already_enabled = {item['id'] for item in snapshot['items'] if item.get('enabled', True)}
                canonical, enabled_roots = self._selected_dependencies(snapshot['items'], context, already_enabled | affected)
                affected.update(enabled_roots - already_enabled)
                edges = {item['id']: set() for item in snapshot['items']}
                edges.update(self._dependency_edges(canonical['items'], context))
                for item_id, targets in provider_edges.items():
                    edges[item_id].update(targets)
                for item_id in list(affected):
                    affected.update(target for target in edges[item_id] if not next(i for i in snapshot['items'] if i['id'] == target).get('enabled', True))
            else:
                for item_id, targets in edges.items():
                    if item_id in affected or not targets.intersection(affected):
                        continue
                    original = next(item for item in snapshot['items'] if item['id'] == item_id)
                    if not original.get('enabled', True):
                        continue
                    # Isolate this root's requirements from other broken roots.
                    # Other surviving archives remain available as dependencies,
                    # so a compatible bundled substitute can keep it installed.
                    unavailable = affected | {item['id'] for item in snapshot['items'] if not item.get('enabled', True)}
                    remaining, _ = self._selected_dependencies(snapshot['items'], context, {item_id}, blocked=unavailable)
                    if remaining['conflicts']:
                        affected.add(item_id)
            changed = previous != affected
        expanded = affected - set(selected)
        permitted = (body.get('includeDependencies') is True if enabling else
                     body.get('disableDependents') is True if action == 'toggle' else body.get('includeDependents') is True)
        if expanded and not permitted and not preview:
            raise ServiceError(409, 'dependent_content', 'Review the affected files before changing this content.',
                               {'dependents': [{'id': i['id'], 'name': i.get('name') or i['filename']} for i in snapshot['items'] if i['id'] in expanded],
                                'suggestedIds': sorted(affected)})
        if permitted or preview:
            ids = sorted(affected)
            selected = {item['id']: item for item in snapshot['items'] if item['id'] in affected}
        prospective = [dict(item) for item in snapshot['items'] if action != 'remove' or item['id'] not in selected]
        changes = []
        if action == 'toggle':
            for item in prospective:
                if item['id'] in selected:
                    item['enabled'] = body['enabled']
            changes = [{'action': 'toggle', 'id': item_id, 'enabled': body['enabled']} for item_id in ids
                       if bool(selected[item_id].get('enabled', True)) != body['enabled']]
        else:
            changes = [{'action': 'remove', 'id': item_id} for item_id in ids]
        validation_items = prospective if enabling else [item for item in prospective if item['id'] not in selected]
        validation = validate_inventory(self.provider, context, validation_items)
        changed_ids = {change['id'] for change in changes}

        def records(entries):
            return [{key: item[key] for key in ('id', 'name', 'filename', 'kind', 'enabled', 'version', 'iconUrl') if key in item}
                    for item in entries]

        unchanged = [item for item in snapshot['items'] if item['id'] in selected and item['id'] not in changed_ids]
        if preview:
            apply_body = {'action': action, 'kind': kind, 'ids': requested_ids, 'revision': snapshot['revision'],
                          'root': self._root(profile), 'profileVersion': profile.get('lastVersionId'), 'context': context}
            if action == 'toggle':
                apply_body['enabled'] = body['enabled']
            if expanded:
                apply_body['includeDependencies' if enabling else 'disableDependents' if action == 'toggle' else 'includeDependents'] = True
            warnings = list(validation.get('warnings') or [])
            if kind in ('resourcepacks', 'shaderpacks'):
                warnings.append({'code': 'file_state_only', 'message': 'This changes file availability. Active resource-pack order and shader configuration stay as saved.'})
            return {'ok': True, 'action': action, 'kind': kind, 'revision': snapshot['revision'],
                    'selected': records(item for item in snapshot['items'] if item['id'] in requested),
                    'affected': records(item for item in snapshot['items'] if item['id'] in affected),
                    'additional': records(item for item in snapshot['items'] if item['id'] in expanded),
                    'changed': records(item for item in snapshot['items'] if item['id'] in changed_ids),
                    'unchanged': records(unchanged), 'requiresExpansion': bool(expanded),
                    'activationChanged': False, 'conflicts': validation.get('conflicts') or [],
                    'warnings': warnings, 'applyBody': apply_body}
        if validation.get('conflicts'):
            raise ServiceError(409, 'dependency_conflict', 'This change would leave incompatible or missing dependencies. Existing content was preserved.', validation['conflicts'])
        result = store.transact(changes, expected_revision=body.get('revision') or snapshot['revision']) if changes else snapshot
        summary = {'installed': [], 'reused': [], 'enabled': [], 'disabled': [], 'removed': [], 'unchanged': records(unchanged)}
        if action == 'remove':
            summary['removed'] = records(item for item in snapshot['items'] if item['id'] in changed_ids)
        else:
            summary['enabled' if body['enabled'] else 'disabled'] = records(item for item in result['items'] if item['id'] in changed_ids)
        return dict(result, ok=True, summary=summary)

    @classmethod
    def _selected_dependencies(cls, items, context, enabled, blocked=()):
        """Resolve alternating archive/provider file requirements to a fixed set."""
        from content_resolver import canonical_inventory
        blocked = set(blocked)
        available = [item for item in items if item['id'] not in blocked]
        provider_edges = cls._dependency_edges(available, context, local=False)
        selected = set(enabled) - blocked
        # Each iteration adds at least one available file, or returns. No
        # acquisition occurs here; the file set and graph bounds are finite.
        for _ in range(len(available) + 1):
            preview = [dict(item, enabled=item['id'] in selected) for item in items]
            canonical = canonical_inventory(context, preview, blocked=list(blocked))
            active = {item['id'] for item in canonical['items']}
            required = selected | active | set(canonical['enable'])
            for ident in selected | active:
                required.update(provider_edges.get(ident, set()))
            if required == selected:
                return canonical, selected
            selected = required
        raise ServiceError(409, 'graph_limit', 'Dependency selection exceeded the available installation file set')

    @staticmethod
    def _dependency_edges(items, context, local=True):
        from content_resolver import _relations
        edges = {item['id']: set() for item in items}
        for item in items:
            relations = _relations(item.get('depends'), context) if local else []
            for relation in relations:
                if str(relation.get('side', '')).lower() == 'server':
                    continue
                ident = relation.get('id') or relation.get('modId')
                targets = [target for target in items if target['id'] != item['id'] and ident in target.get('modIds', [])]
                if targets:
                    target = next((target for target in targets if target.get('enabled', True)), targets[0])
                    edges[item['id']].add(target['id'])
            for relation in item.get('providerDependencies') or []:
                if relation.get('dependency_type', 'required') != 'required':
                    continue
                project, version = relation.get('project_id'), relation.get('version_id')
                if (version and version in item.get('embeddedVersions', [])) or (project and project in item.get('embeddedProjects', [])):
                    continue
                targets = [target for target in items if target['id'] != item['id'] and
                           ((version and version in target.get('embeddedVersions', [])) or (project and project in target.get('embeddedProjects', [])) or
                            (target.get('provider') == item.get('provider') and ((version and version == target.get('versionId')) or (not version and project == target.get('projectId')))))]
                if targets:
                    target = next((target for target in targets if target.get('enabled', True)), targets[0])
                    edges[item['id']].add(target['id'])
        return edges

    def _import(self, ident, profile, body):
        from instance_content import inspect_file
        from content_resolver import validate_inventory
        kind = self._kind(body.get('kind'))
        store = self._store(ident, profile)
        snapshot = store.inventory()
        if body.get('revision') and body['revision'] != snapshot['revision']:
            raise ServiceError(409, 'stale_revision', 'Refresh the installed list before importing')
        files = body.get('files')
        if body.get('path'):
            path = _text(body['path'], 'source file', 4096, False)
            files = [{'name': os.path.basename(path), '_source': path}]
        if not isinstance(files, list) or not 1 <= len(files) <= 64:
            raise ServiceError(400, 'invalid_files', 'Select local content files to import')
        stage = tempfile.mkdtemp(prefix='local-content-', dir=self.data_dir)
        try:
            changes, inspected = [], []
            for index, entry in enumerate(files):
                name = _text(entry.get('name'), 'filename', 240, False)
                destination = os.path.join(stage, str(index) + '.download')
                if entry.get('_source'):
                    self._safe_tree(entry['_source'])
                    if os.path.getsize(entry['_source']) > 512 * 1024 * 1024 or not os.path.isfile(entry['_source']):
                        raise ServiceError(400, 'file_too_large', 'Local content file is too large')
                    shutil.copyfile(entry['_source'], destination)
                else:
                    encoded = _text(entry.get('data'), 'file data', 48 * 1024 * 1024, False)
                    try:
                        data = base64.b64decode(encoded, validate=True)
                    except (ValueError, TypeError) as error:
                        raise ServiceError(400, 'invalid_file', 'Local file data is invalid') from error
                    if len(data) > 32 * 1024 * 1024:
                        raise ServiceError(400, 'file_too_large', 'Use the file picker for content larger than 32 MiB')
                    Path(destination).write_bytes(data)
                item = inspect_file(destination, kind, name)
                if not any(existing['kind'] == kind and existing['hashes'] == item['hashes'] for existing in snapshot['items'] + inspected):
                    inspected.append(item)
                changes.append({'action': 'install', 'kind': kind, 'filename': name, 'source': destination, 'metadata': {'userRequested': True}})
            validation = validate_inventory(self.provider, self._context(profile), snapshot['items'] + inspected)
            if validation.get('conflicts'):
                raise ServiceError(409, 'dependency_conflict', 'These files have incompatible or missing dependencies; nothing was imported', validation['conflicts'])
            return dict(store.transact(changes, expected_revision=snapshot['revision']), ok=True)
        finally:
            if os.path.commonpath([os.path.realpath(stage), os.path.realpath(self.data_dir)]) != os.path.realpath(self.data_dir):
                raise ServiceError(409, 'unsafe_stage', 'Local staging cleanup escaped the data directory')
            shutil.rmtree(stage)
