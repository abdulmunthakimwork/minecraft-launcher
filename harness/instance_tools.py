"""Bounded instance files, native-picker I/O and recoverable local operations.

The host owns profile/private-root and running-process policy. Every operation
also validates its caller's revision under the content store's canonical-root
lock. This module never invokes a shell, downloads files or executes game data.
"""
import copy
import gzip
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import threading
import uuid
from urllib.parse import quote
import zipfile

from instance_content import (ContentError, _LOCKS, _LOCKS_GUARD, _archive_names,
                              _encoded, _filename, _inspect, _no_links,
                              _sync_dir, _zip_archive, canonical_root,
                              ENTRY_LIMIT, FILE_LIMIT, METADATA_LIMIT)

KINDS = ('worlds', 'screenshots', 'servers', 'datapacks', 'resourcepacks', 'shaderpacks', 'advanced')
TREE_LIMIT = 8 * 1024 * 1024 * 1024
TEXT_LIMIT = METADATA_LIMIT
_BACKUP_ID = re.compile(r'^[0-9a-f]{32}$')


def _error(code, message):
    raise ContentError(code, message)


def _root(root, allow_missing=False):
    path = Path(canonical_root(root))
    if path.exists() and not path.is_dir():
        _error('unsafe_path', 'The installation path is not a directory.')
    if not allow_missing and not path.is_dir():
        _error('missing_root', 'The installation directory does not exist.')
    return path


def _lock(root):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(root), threading.RLock())


def _id(kind, relative):
    return hashlib.sha256((kind + '/' + relative.casefold()).encode('utf-8')).hexdigest()[:32]


def _children(folder):
    _no_links(folder)
    if not folder.exists():
        return []
    if not folder.is_dir():
        _error('unsafe_path', 'The selected content folder is not a directory.')
    children, seen = [], set()
    with os.scandir(folder) as entries:
        for entry in entries:
            _filename(entry.name, allow_disabled=True)
            if entry.name.casefold() in seen:
                _error('path_alias', 'Files share a Windows filename identity.')
            seen.add(entry.name.casefold())
            child = _no_links(folder / entry.name)
            info = child.stat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                _error('unsafe_path', 'Only regular files and directories can be managed.')
            children.append(child)
            if len(children) > ENTRY_LIMIT:
                _error('file_limit', 'The folder contains too many entries.')
    return sorted(children, key=lambda p: (p.name.casefold(), p.name))


def _tree(path, exclude=()):
    """Validate the complete lexical tree before copying, hashing or archiving."""
    path = _no_links(path)
    if not path.exists():
        return []
    records, total = [], 0
    def visit(item, relative, depth):
        nonlocal total
        _no_links(item)
        if depth > 64 or len(records) >= ENTRY_LIMIT:
            _error('file_limit', 'The directory tree exceeds the allowed depth or entry count.')
        info = item.stat()
        if item.is_dir():
            records.append((relative, item, True))
            for child in _children(item):
                if depth == 0 and child.name in exclude:
                    continue
                visit(child, (relative + '/' if relative else '') + child.name, depth + 1)
        elif stat.S_ISREG(info.st_mode):
            if info.st_size > FILE_LIMIT:
                _error('file_limit', 'A file exceeds the 512 MiB limit.')
            total += info.st_size
            if total > TREE_LIMIT:
                _error('file_limit', 'The directory exceeds the 8 GiB copy and backup limit.')
            records.append((relative, item, False))
        else:
            _error('unsafe_path', 'Only regular files and directories can be managed.')
    visit(path, '', 0)
    return records


def _fingerprint(path, exclude=()):
    records = []
    for relative, item, directory in _tree(path, exclude):
        if directory:
            records.append([relative, 'directory'])
            continue
        digest, count = hashlib.sha256(), 0
        with open(item, 'rb') as handle:
            while block := handle.read(1024 * 1024):
                count += len(block)
                if count > FILE_LIMIT:
                    _error('file_limit', 'A file grew beyond the copy limit.')
                digest.update(block)
        records.append([relative, count, digest.hexdigest()])
    return records


def _bounded_read(path, limit=TEXT_LIMIT):
    _no_links(path)
    if not path.exists():
        return b''
    if not path.is_file():
        _error('unsafe_path', 'The selected data path is not a regular file.')
    with open(path, 'rb') as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        _error('metadata_limit', 'Data exceeds the bounded metadata limit.')
    return raw


class _NBTReader:
    """Typed NBT keeps unknown fields and list ordering on write-back."""
    def __init__(self, raw):
        self.raw, self.pos, self.nodes = raw, 0, 0

    def read(self, count):
        if count < 0 or self.pos + count > len(self.raw):
            _error('invalid_nbt', 'The NBT data is truncated or has an invalid length.')
        data = self.raw[self.pos:self.pos + count]
        self.pos += count
        return data

    def number(self, form):
        return struct.unpack('>' + form, self.read(struct.calcsize('>' + form)))[0]

    def text(self):
        return self.read(self.number('H')).decode('utf-8')

    def payload(self, kind, depth=0):
        self.nodes += 1
        if depth > 32 or self.nodes > ENTRY_LIMIT:
            _error('metadata_limit', 'The NBT nesting or tag count exceeds its limit.')
        if kind in (1, 2, 3, 4, 5, 6):
            return self.number({1: 'b', 2: 'h', 3: 'i', 4: 'q', 5: 'f', 6: 'd'}[kind])
        if kind == 8:
            return self.text()
        if kind in (7, 11, 12):
            count = self.number('i')
            width = {7: 1, 11: 4, 12: 8}[kind]
            if count < 0 or count > METADATA_LIMIT // width:
                _error('metadata_limit', 'The NBT array exceeds its limit.')
            return self.read(count * width)
        if kind == 9:
            subtype, count = self.number('B'), self.number('i')
            if count < 0 or count > ENTRY_LIMIT or (subtype == 0 and count):
                _error('metadata_limit', 'The NBT list exceeds its limit.')
            return (subtype, [self.payload(subtype, depth + 1) for _ in range(count)])
        if kind == 10:
            fields = {}
            while (subtype := self.number('B')) != 0:
                name = self.text()
                if name in fields:
                    _error('invalid_nbt', 'Duplicate NBT field names cannot be edited safely.')
                fields[name] = (subtype, self.payload(subtype, depth + 1))
            return fields
        _error('invalid_nbt', 'The NBT tag type is unsupported.')


def _nbt(path):
    raw = _bounded_read(path)
    compressed = raw.startswith(b'\x1f\x8b')
    if compressed:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as handle:
            raw = handle.read(METADATA_LIMIT + 1)
        if len(raw) > METADATA_LIMIT:
            _error('metadata_limit', 'Expanded NBT metadata exceeds 1 MiB.')
    reader = _NBTReader(raw)
    if reader.number('B') != 10:
        _error('invalid_nbt', 'The NBT root must be a compound.')
    name = reader.text()
    fields = reader.payload(10)
    if reader.pos != len(raw):
        _error('invalid_nbt', 'The NBT document contains trailing data.')
    return name, fields, compressed


def _nbt_string(value):
    raw = value.encode('utf-8')
    if len(raw) > 65535:
        _error('metadata_limit', 'An NBT string exceeds its encoding limit.')
    return struct.pack('>H', len(raw)) + raw


def _nbt_payload(kind, value):
    if kind in (1, 2, 3, 4, 5, 6):
        return struct.pack('>' + {1: 'b', 2: 'h', 3: 'i', 4: 'q', 5: 'f', 6: 'd'}[kind], value)
    if kind == 8:
        return _nbt_string(value)
    if kind in (7, 11, 12):
        return struct.pack('>i', len(value) // {7: 1, 11: 4, 12: 8}[kind]) + value
    if kind == 9:
        subtype, values = value
        return bytes([subtype]) + struct.pack('>i', len(values)) + b''.join(_nbt_payload(subtype, v) for v in values)
    if kind == 10:
        return b''.join(bytes([tag[0]]) + _nbt_string(name) + _nbt_payload(*tag) for name, tag in value.items()) + b'\0'
    _error('invalid_nbt', 'The NBT tag type is unsupported.')


def _nbt_bytes(name, fields, compressed):
    raw = b'\x0a' + _nbt_string(name) + _nbt_payload(10, fields)
    if len(raw) > METADATA_LIMIT:
        _error('metadata_limit', 'Saved NBT metadata exceeds 1 MiB.')
    return gzip.compress(raw, mtime=0) if compressed else raw


def _value(fields, name, default=None, kind=None):
    tag = fields.get(name)
    return tag[1] if tag and (kind is None or tag[0] == kind) else default


def _worlds(root):
    items = []
    for path in _children(root / 'saves'):
        if not path.is_dir() or not (path / 'level.dat').exists():
            continue
        _tree(path)
        relative = path.relative_to(root).as_posix()
        item = {'id': _id('worlds', relative), 'kind': 'worlds', 'path': relative,
                'filename': path.name, 'name': path.name, 'version': None,
                'lastPlayed': None, 'metadataProblems': []}
        try:
            _, fields, _ = _nbt(path / 'level.dat')
            data = _value(fields, 'Data', {}, 10)
            version = _value(data, 'Version', {}, 10)
            item.update(name=_value(data, 'LevelName', path.name, 8),
                        version=_value(version, 'Name', None, 8),
                        lastPlayed=_value(data, 'LastPlayed', None, 4),
                        gameType=_value(data, 'GameType', None, 3),
                        dataVersion=_value(data, 'DataVersion', None, 3))
        except (ContentError, ValueError, OSError, EOFError) as exc:
            item['metadataProblems'].append({'code': getattr(exc, 'code', 'invalid_nbt'), 'message': str(exc)})
        items.append(item)
    return items


def _world(root, ident):
    for item in _worlds(root):
        if item['id'] == ident:
            return item
    _error('missing_item', 'The selected world no longer exists.')


def _image(path):
    if not path.is_file() or path.suffix.casefold() not in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
        return False
    with open(path, 'rb') as handle:
        header = handle.read(32)
    suffix = path.suffix.casefold()
    return ((suffix == '.png' and len(header) >= 24 and header.startswith(b'\x89PNG\r\n\x1a\n') and header[12:16] == b'IHDR')
            or (suffix in ('.jpg', '.jpeg') and header.startswith(b'\xff\xd8\xff'))
            or (suffix == '.gif' and header[:6] in (b'GIF87a', b'GIF89a'))
            or (suffix == '.webp' and header.startswith(b'RIFF') and header[8:12] == b'WEBP'))


def _screenshots(root, instance_id):
    items = []
    for path in _children(root / 'screenshots'):
        if not _image(path):
            continue
        relative = path.relative_to(root).as_posix()
        prefix = '/__instances/' + quote(str(instance_id), safe='') + '/' if instance_id else ''
        items.append({'id': _id('screenshots', relative), 'kind': 'screenshots', 'path': relative,
                      'filename': path.name, 'name': path.stem, 'size': path.stat().st_size,
                      'url': prefix + 'image?name=' + quote(path.name, safe='')})
    return items


def _packs(root, kind):
    return [_inspect(path, kind, path.name) for path in _children(root / kind)]


def _datapacks(root, world_id):
    world = _world(root, world_id)
    folder = root / world['path'] / 'datapacks'
    items = []
    for path in _children(folder):
        item = _inspect(path, 'resourcepacks', path.name)
        item.update(kind='datapacks', path=path.relative_to(root).as_posix())
        item['id'] = _id('datapacks', item['path'])
        items.append(item)
    return world, items


def _servers(root):
    path = root / 'servers.dat'
    if not path.exists():
        return ('', {'servers': (9, (10, []))}, True), []
    document = _nbt(path)
    records = _value(document[1], 'servers', (10, []), 9)
    if records[0] != 10:
        _error('invalid_nbt', 'The server list must contain NBT compounds.')
    items = []
    for index, record in enumerate(records[1]):
        acceptance = _value(record, 'acceptTextures', None, 1)
        items.append({'id': _id('servers', str(index) + '/' + hashlib.sha256(_nbt_payload(10, record)).hexdigest()),
                      'name': _value(record, 'name', '', 8), 'address': _value(record, 'ip', '', 8),
                      'acceptTextures': None if acceptance is None else bool(acceptance),
                      'resourcePolicy': 'prompt' if acceptance is None else ('enabled' if acceptance else 'disabled')})
    return document, items


def _settings(path, separator):
    raw = _bounded_read(path)
    text = raw.decode('utf-8-sig')
    values = {}
    for line in text.splitlines():
        if line.lstrip().startswith(('#', '!')) or separator not in line:
            continue
        key, value = line.split(separator, 1)
        key = key.strip()
        if key in values:
            _error('invalid_options', 'Duplicate settings cannot be edited safely.')
        values[key] = value.strip()
    return text, values


def _patch_settings(text, updates, separator):
    newline = '\r\n' if '\r\n' in text else '\n'
    remaining, lines = dict(updates), []
    for line in text.splitlines(keepends=True):
        body = line.rstrip('\r\n')
        key = body.split(separator, 1)[0].strip() if separator in body and not body.lstrip().startswith(('#', '!')) else None
        if key in remaining:
            ending = line[len(body):] or newline
            lines.append(key + separator + remaining.pop(key) + ending)
        else:
            lines.append(line)
    if remaining and lines and not lines[-1].endswith(('\n', '\r')):
        lines[-1] += newline
    lines.extend(key + separator + value + newline for key, value in remaining.items())
    return ''.join(lines).encode('utf-8')


def _resource_settings(root, items):
    text, values = _settings(root / 'options.txt', ':')
    try:
        selected = json.loads(values.get('resourcePacks', '["vanilla"]'))
    except ValueError as exc:
        raise ContentError('invalid_options', 'The resource pack selection is malformed.') from exc
    if not isinstance(selected, list) or len(selected) > 256 or any(not isinstance(v, str) or len(v) > 512 for v in selected):
        _error('invalid_options', 'The resource pack selection must be a bounded string list.')
    mapping = {'file/' + item['filename']: item['id'] for item in items if item.get('enabled')}
    # Minecraft writes low priority first; the workspace presents high first.
    active = [mapping[name] for name in reversed(selected) if name in mapping]
    return text, selected, active


def _renderer(root):
    for item in _packs(root, 'mods'):
        if not item.get('enabled'):
            continue
        if any(ident.casefold() == 'iris' for ident in item.get('modIds', [])):
            return 'iris', root / 'config' / 'iris.properties', '='
        if any(ident.casefold() == 'optifine' for ident in item.get('modIds', [])):
            return 'optifine', root / 'optionsshaders.txt', '='
        path = root / item['path']
        if path.is_file() and path.suffix.casefold() == '.jar':
            try:
                with _zip_archive(path) as archive:
                    names = _archive_names(archive)
                    if 'net/optifine/Config.class' in names or 'optifine/Config.class' in names:
                        return 'optifine', root / 'optionsshaders.txt', '='
            except (ContentError, OSError, zipfile.BadZipFile):
                continue
    return None, None, None


def _shader_settings(root, items):
    renderer, path, separator = _renderer(root)
    if not renderer:
        return None, None, '', {}, None
    text, values = _settings(path, separator)
    selected = values.get('shaderPack', '')
    enabled = renderer != 'iris' or values.get('enableShaders', 'false').casefold() == 'true'
    ident = next((item['id'] for item in items if item.get('enabled') and item['filename'] == selected), None) if enabled else None
    return renderer, path, text, values, ident


def _state(root, kind, params, private, running):
    paths, message, extra = [], None, {}
    writable = bool(private and not running)
    _no_links(root)
    if root.exists() and not root.is_dir():
        _error('unsafe_path', 'The installation path is not a directory.')
    if not root.exists():
        caps = {'worlds': ['import'], 'screenshots': ['import'], 'servers': ['save'],
                'datapacks': [], 'resourcepacks': ['activate'], 'shaderpacks': [], 'advanced': ['restore']}
        if kind == 'resourcepacks':
            extra['activeIds'] = []
        elif kind == 'shaderpacks':
            extra.update(activeId=None, renderer=None)
        elif kind == 'advanced':
            extra['settings'] = {'directory': str(root), 'backupLocation': 'Launcher data/workspace-backups',
                                 'copyLimitBytes': TREE_LIMIT}
        message = 'This installation has not created game files yet. Launch Minecraft or import files to get started.'
        if kind == 'datapacks':
            message += ' Create or import a world before managing its datapacks.'
        elif kind == 'shaderpacks':
            message += ' Install a compatible Iris or OptiFine renderer before selecting a shader.'
        if running:
            message += ' Stop Minecraft before changing installation files.'
        elif not private:
            message += ' Make an independent installation copy to edit these shared files.'
        revision = hashlib.sha256(_encoded({'root': str(root), 'kind': kind, 'missing': True})).hexdigest()
        return {'ok': True, 'items': [], 'capabilities': caps[kind] if writable else [],
                'revision': revision, 'message': message, **extra}
    if kind == 'worlds':
        items = _worlds(root)
        paths = [root / 'saves']
        caps = ['reveal', 'datapacks']
        if not running:
            caps += ['export', 'backup']
        if writable:
            caps += ['rename', 'copy', 'delete', 'import']
    elif kind == 'screenshots':
        items = _screenshots(root, params.get('instanceId'))
        paths = [root / 'screenshots']
        caps = ['reveal', 'export'] + (['rename', 'copy', 'delete', 'import'] if writable else [])
    elif kind == 'servers':
        _, items = _servers(root)
        paths = [root / 'servers.dat']
        caps = ['reveal', 'export'] + (['save'] if writable else [])
    elif kind == 'datapacks':
        world, items = _datapacks(root, params.get('id'))
        paths = [root / world['path']]
        extra['worldId'] = world['id']
        caps = ['reveal', 'export'] + (['import', 'rename', 'copy', 'delete'] if writable else [])
        message = 'Datapacks belong to this world; Minecraft manages their enabled state.'
    elif kind == 'resourcepacks':
        items = _packs(root, kind)
        _, _, extra['activeIds'] = _resource_settings(root, items)
        paths = [root / kind, root / 'options.txt']
        caps = ['reveal'] + (['activate'] if writable else [])
    elif kind == 'shaderpacks':
        items = _packs(root, kind)
        renderer, path, _, _, extra['activeId'] = _shader_settings(root, items)
        paths = [root / kind, root / 'mods', root / 'config' / 'iris.properties', root / 'optionsshaders.txt']
        extra['renderer'] = renderer
        caps = ['reveal'] + (['activate'] if writable and renderer else [])
        if not renderer:
            message = 'Install a compatible Iris or OptiFine renderer before selecting a shader.'
    else:
        items = []
        paths = [root]
        caps = ['reveal'] + (['backup', 'export'] if not running else []) + (['restore'] if writable else [])
        extra['settings'] = {'directory': str(root), 'backupLocation': 'Launcher data/workspace-backups',
                             'copyLimitBytes': TREE_LIMIT}
    revision_data = [[p.relative_to(root).as_posix(), _fingerprint(p, ('.launcher-tools', '.launcher-content'))] for p in paths]
    revision = hashlib.sha256(_encoded({'root': str(root), 'kind': kind, 'state': revision_data})).hexdigest()
    if running:
        message = (message + ' ' if message else '') + 'Stop Minecraft before changing installation files or backing up worlds.'
    elif not private:
        message = (message + ' ' if message else '') + 'Make an independent installation copy to edit these shared files.'
    result = {'ok': True, 'items': items, 'capabilities': caps, 'revision': revision, **extra}
    if message:
        result['message'] = message
    return result


def read_tools(root, kind, params, private=True, running=False):
    if kind not in KINDS:
        _error('unsupported_tool', 'This installation tool is unsupported.')
    if not isinstance(params, dict):
        _error('invalid_change', 'Tool parameters must be an object.')
    root = _root(root, allow_missing=True)
    with _lock(root):
        _recover(root, writable=bool(private and not running))
        return _state(root, kind, params, private, running)


def read_logs(root, *, instance_id=None, launch_status=None, running=False, redact=None, diagnosis_enabled=True):
    """Read a selected instance log tail without recovery or any file writes.

    A matching launch_status is a trusted host snapshot, never request data.
    Its host-owned logPath can live outside the installation. Instance paths
    remain fixed at logs/latest.log with the usual no-link validation.
    """
    from crash_diagnostics import analyze
    root = _root(root, allow_missing=True)
    status = launch_status if isinstance(launch_status, dict) else {}
    selected = instance_id is not None and status.get('configurationId') == instance_id
    context = {key: status.get(key) for key in ('startedAt', 'exitCode')} if selected else {}
    context['running'] = bool(running) and selected
    path = root / 'logs' / 'latest.log'
    source = 'logs/latest.log'
    if selected and isinstance(status.get('logPath'), str) and status['logPath']:
        path = Path(status['logPath'])
        if not path.is_absolute():
            _error('unsafe_path', 'The host log path must be absolute.')
        source = 'Launcher output'
    _no_links(path)
    text, truncated = '', False
    if path.exists():
        if not path.is_file():
            _error('unsafe_path', 'The selected log path is not a regular file.')
        with open(path, 'rb') as handle:
            info = os.fstat(handle.fileno())
            limit = 1024 * 1024
            start = max(0, info.st_size - limit)
            partial = False
            if start:
                handle.seek(start - 1)
                partial = handle.read(1) != b'\n'
            else:
                handle.seek(start)
            raw = handle.read(limit)
        truncated = bool(start)
        if partial:
            # The first bytes can be half a UTF-8 character or half an error.
            raw = raw.partition(b'\n')[2]
        text = raw.decode('utf-8', 'replace')
        context['logModifiedAt'] = info.st_mtime
    if callable(redact):
        text = redact(text)
    text = re.sub(r'''(?i)(access[_-]?token|authorization|client[_-]?token)(["']?\s*[:= ]+\s*)(?:bearer\s+)?["']?[^"'\s,]+''', r'\1\2[redacted]', text)
    context.update(source=source, sourceTruncated=truncated)
    diagnosis = analyze(text, context) if diagnosis_enabled else {
        'summary': 'Local crash explanations are turned off in Settings.',
        'state': 'unknown', 'scope': 'unknown', 'severity': 'info', 'findings': []}
    return {'ok': True, 'text': text, 'running': context['running'], 'capabilities': ['export'],
            'source': source, 'sourceTruncated': truncated, 'diagnosis': diagnosis,
            'diagnosisEnabled': bool(diagnosis_enabled)}


def _atomic(path, raw):
    path = _no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.tmp-' + uuid.uuid4().hex)
    try:
        with open(temporary, 'xb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        _no_links(path)
        os.replace(temporary, path)
        _sync_dir(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _destination(folder, name):
    _filename(name)
    _no_links(folder)
    for child in _children(folder):
        if child.name.casefold() == name.casefold():
            _error('path_conflict', 'The destination filename already exists.')
    return _no_links(folder / name)


def _picker(pick_path, body, options):
    if not callable(pick_path):
        _error('picker_unavailable', 'Native file selection is unavailable.')
    selected = pick_path(options)
    if isinstance(selected, dict):
        selected = selected.get('path')
    if not selected:
        return None
    path = Path(canonical_root(selected))
    if body.get('outputPath') and canonical_root(body['outputPath']) != str(path):
        _error('unsafe_path', 'The requested output was not selected in the native picker.')
    return path


def _backup_folder(data_dir, instance_id, root):
    _filename(instance_id)
    data = Path(canonical_root(data_dir))
    folder = _no_links(data / 'workspace-backups' / instance_id)
    if folder.is_relative_to(root):
        _error('unsafe_path', 'Recovery archives must be outside the installation directory.')
    return folder


def _archive(root, source, destination, exclude=()):
    destination = _no_links(destination)
    records = _tree(source, exclude)
    snapshot = _fingerprint(source, exclude)
    if (source.is_dir() and destination.is_relative_to(source)) or destination == source:
        _error('unsafe_path', 'An export cannot be placed inside its source.')
    if destination.exists():
        _error('path_conflict', 'The export destination already exists.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name('.' + destination.name + '.tmp-' + uuid.uuid4().hex)
    try:
        with zipfile.ZipFile(temporary, 'x', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for _, path, directory in records:
                relative = path.relative_to(root).as_posix()
                if relative == '.':
                    continue
                archive.write(path, relative + '/' if directory else relative)
        with open(temporary, 'r+b') as handle:
            os.fsync(handle.fileno())
        if _fingerprint(source, exclude) != snapshot:
            _error('local_changed', 'Files changed while the archive was being created; the live source was preserved.')
        _no_links(destination)
        if destination.exists():
            _error('path_conflict', 'The export destination changed during the operation.')
        os.replace(temporary, destination)
        _sync_dir(destination.parent)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def _backup(root, source, data_dir, instance_id):
    folder = _backup_folder(data_dir, instance_id, root)
    ident = uuid.uuid4().hex
    destination = folder / (ident + '.zip')
    _archive(root, source, destination, ('.launcher-tools', '.launcher-content') if source == root else ())
    return {'backupId': ident, 'backupPath': str(destination)}


def _journal_path(root):
    return _no_links(root / '.launcher-tools' / 'journal.json')


def _snapshot(records):
    return hashlib.sha256(_encoded(records)).hexdigest()


def _level_snapshot(records, raw):
    updated = copy.deepcopy(records)
    level = next((entry for entry in updated if entry[0] == 'level.dat'), None)
    if level is None or len(level) != 3:
        _error('invalid_journal', 'World recovery requires regular level metadata.')
    level[1:] = [len(raw), hashlib.sha256(raw).hexdigest()]
    return updated


def _relative_path(root, relative):
    if not isinstance(relative, str) or relative.startswith('/') or '\\' in relative:
        _error('unsafe_path', 'A safe relative journal path is required.')
    for part in relative.split('/'):
        _filename(part, allow_disabled=True)
    path = _no_links(root / relative)
    if not path.is_relative_to(root) or path == root:
        _error('unsafe_path', 'The journal path is outside its installation.')
    return path


def _recover(root, writable=True):
    journal = _journal_path(root)
    if not journal.exists():
        return
    if not writable:
        _error('recovery_required', 'Stop Minecraft and use the private installation owner to finish pending recovery.')
    try:
        data = json.loads(_bounded_read(journal, 2 * METADATA_LIMIT))
        if (not isinstance(data, dict) or data.get('schema') not in (1, 2) or
                data.get('root') != str(root) or data.get('action') not in ('rename', 'delete')):
            _error('invalid_journal', 'The recovery journal identity is invalid.')
        source = _relative_path(root, data['source'])
        destination = _relative_path(root, data['destination'])
        parts = source.relative_to(root).parts
        managed = ((len(parts) == 2 and parts[0] in ('saves', 'screenshots')) or
                   (len(parts) == 4 and parts[0] == 'saves' and parts[2] == 'datapacks'))
        if not managed or source == destination:
            _error('invalid_journal', 'The recovery source is not a managed world, screenshot or world datapack.')
        if data['action'] == 'rename' and destination.parent != source.parent:
            _error('invalid_journal', 'Recovery renames must remain in their original content folder.')
        if data['action'] == 'delete' and (destination.parent != root / '.launcher-tools' or
                                         not re.fullmatch(r'deleted-[0-9a-f]{32}', destination.name)):
            _error('invalid_journal', 'The deletion recovery destination is invalid.')
        original = data.get('originalLevel')
        if original is not None and (data['action'] != 'rename' or len(parts) != 2 or
                                     parts[0] != 'saves' or not isinstance(original, str)):
            _error('invalid_journal', 'Only a world rename can restore level metadata.')
        if data['schema'] == 1:
            _error('recovery_conflict', 'The pending legacy operation has no content verification; preserve its files and journal for review.')
        before, after = data.get('beforeSnapshot'), data.get('afterSnapshot')
        if any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value) for value in (before, after)):
            _error('invalid_journal', 'Recovery content verification is invalid.')
        raw = None
        if original is not None:
            import base64
            raw = base64.b64decode(original, validate=True)
            if len(raw) > METADATA_LIMIT or hashlib.sha256(raw).hexdigest() != data.get('originalLevelSha256'):
                _error('invalid_journal', 'Recovery metadata is invalid or exceeds its limit.')
        elif before != after:
            _error('invalid_journal', 'Recovery cannot restore changed content without its original metadata.')
        if source.exists() and destination.exists():
            _error('recovery_conflict', 'Both recovery paths exist; preserve them for manual review.')
        current = destination if destination.exists() else source
        if not current.exists():
            _error('recovery_conflict', 'Both recovery paths are missing; preserve the pending journal for review.')
        records = _fingerprint(current)
        observed = _snapshot(records)
        if observed not in (before, after):
            _error('recovery_conflict', 'Recovery files changed outside the pending operation; preserve them and its journal for review.')
        if raw is not None and _snapshot(_level_snapshot(records, raw)) != before:
            _error('invalid_journal', 'Recovery metadata does not match the original content verification.')
        if current == destination:
            # Publication did not finish. Restore the previous name and bytes.
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(destination, source)
            _sync_dir(source.parent)
        if _snapshot(_fingerprint(source)) != observed:
            _error('recovery_conflict', 'Recovery files changed during the operation; preserve them and its journal for review.')
        if raw is not None and observed != before:
            _atomic(source / 'level.dat', raw)
        journal.unlink()
        _sync_dir(journal.parent)
    except (ValueError, KeyError, TypeError) as exc:
        raise ContentError('invalid_journal', 'The recovery journal cannot be read safely.') from exc


def _rename_or_delete(root, source, destination, action, new_level=None):
    import base64
    journal = _journal_path(root)
    original = _bounded_read(source / 'level.dat') if new_level is not None else None
    before = _fingerprint(source)
    if original is not None and _level_snapshot(before, original) != before:
        _error('local_changed', 'World metadata changed before the operation; the live source was preserved.')
    after = _level_snapshot(before, new_level) if new_level is not None else before
    data = {'schema': 2, 'root': str(root), 'action': action,
            'beforeSnapshot': _snapshot(before), 'afterSnapshot': _snapshot(after),
            'source': source.relative_to(root).as_posix(), 'destination': destination.relative_to(root).as_posix()}
    if original is not None:
        data['originalLevel'] = base64.b64encode(original).decode('ascii')
        data['originalLevelSha256'] = hashlib.sha256(original).hexdigest()
    _atomic(journal, _encoded(data))
    try:
        if _fingerprint(source) != before:
            _error('local_changed', 'Files changed before the operation; the live source was preserved.')
        if new_level is not None:
            _atomic(source / 'level.dat', new_level)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _no_links(source)
        _no_links(destination)
        if destination.exists():
            _error('path_conflict', 'The operation destination already exists.')
        if _fingerprint(source) != after:
            _error('local_changed', 'Files changed during the operation; the live source was preserved.')
        os.replace(source, destination)
        _sync_dir(source.parent)
        journal.unlink()
        _sync_dir(journal.parent)
    except BaseException:
        _recover(root)
        raise
    if action == 'delete':
        # The durable external archive is the published recovery artifact.
        if destination.is_dir():
            shutil.rmtree(destination)
        else:
            destination.unlink()


def _copy(source, destination):
    _tree(source)
    destination = _no_links(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / ('.launcher-copy-' + uuid.uuid4().hex)
    try:
        if source.is_dir():
            shutil.copytree(source, staging, symlinks=False)
        else:
            shutil.copy2(source, staging)
        _tree(staging)
        if _fingerprint(source) != _fingerprint(staging):
            _error('local_changed', 'The source changed while it was being copied.')
        _no_links(destination)
        if destination.exists():
            _error('path_conflict', 'The copy destination changed during the operation.')
        os.replace(staging, destination)
        _sync_dir(destination.parent)
    finally:
        if staging.exists():
            shutil.rmtree(staging) if staging.is_dir() else staging.unlink()


def _validated_archive(source):
    _no_links(source)
    if source.stat().st_size > FILE_LIMIT:
        _error('file_limit', 'The selected archive exceeds 512 MiB.')
    with _zip_archive(source) as archive:
        names = _archive_names(archive)
        total = sum(entry.file_size for entry in names.values())
        if total > TREE_LIMIT or any(entry.file_size > FILE_LIMIT or entry.flag_bits & 1 for entry in names.values()):
            _error('file_limit', 'The archive exceeds its expansion limit or contains encrypted files.')
        for name, entry in names.items():
            if (name.endswith(('level.dat', 'pack.mcmeta')) and entry.file_size > METADATA_LIMIT):
                _error('metadata_limit', 'Archive metadata exceeds 1 MiB.')
        return list(names)


def _extract(source, staging):
    names = _validated_archive(source)
    staging.mkdir(parents=True)
    total = 0
    with _zip_archive(source) as archive:
        _archive_names(archive)
        for name in names:
            info = archive.getinfo(name)
            destination = _relative_path(staging, name.rstrip('/'))
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            count = 0
            with archive.open(info) as source_file, open(destination, 'xb') as output:
                while block := source_file.read(1024 * 1024):
                    count += len(block)
                    total += len(block)
                    if count > FILE_LIMIT or total > TREE_LIMIT or count > info.file_size:
                        _error('file_limit', 'The archive exceeded its declared expansion size.')
                    output.write(block)
            if count != info.file_size:
                _error('unsafe_archive', 'The archive entry length is inconsistent.')


def _import(root, kind, body, folder, pick_path):
    source = _picker(pick_path, body, {'kind': 'file', 'title': 'Import ' + kind,
                                     'filters': ['.zip', '.png', '.jpg', '.jpeg']})
    if source is None:
        return {'ok': True, 'cancelled': True, 'message': 'File selection was cancelled.'}
    if source.is_relative_to(root):
        _error('unsafe_path', 'Select a source outside this installation for import.')
    staging = root / '.launcher-tools' / ('import-' + uuid.uuid4().hex)
    _no_links(staging)
    try:
        if kind == 'worlds':
            if source.is_dir():
                content = source
            else:
                _extract(source, staging)
                candidates = [p.parent for _, p, directory in _tree(staging) if not directory and p.name == 'level.dat']
                if len(candidates) != 1:
                    _error('unsupported_archive', 'Select an archive containing exactly one world.')
                content = candidates[0]
            if not (content / 'level.dat').is_file():
                _error('invalid_world', 'The selected folder is not a Minecraft world.')
            _nbt(content / 'level.dat')
            name = body.get('name') or (source.name if source.is_dir() else source.stem)
        elif kind == 'screenshots':
            if not _image(source):
                _error('unsupported_content', 'Select a supported local image.')
            content, name = source, body.get('name') or source.name
            if not Path(name).suffix:
                name += source.suffix
        else:
            content, name = source, body.get('name') or source.name
            if source.is_dir():
                _tree(source)
                _bounded_read(source / 'pack.mcmeta')
            else:
                _validated_archive(source)
            inspected = _inspect(source, 'resourcepacks', source.name)
            if not inspected.get('hashes') or inspected.get('metadataProblems'):
                _error('unsupported_content', 'The selected datapack cannot be inspected safely.')
        destination = _destination(folder, name)
        _copy(content, destination)
        return {'ok': True, 'path': destination.relative_to(root).as_posix(), 'message': 'The selected local files were copied into this installation.'}
    finally:
        if staging.exists():
            _tree(staging)
            shutil.rmtree(staging)


def _save_servers(root, body, baseline, data_dir, instance_id):
    incoming = body.get('items')
    if not isinstance(incoming, list) or len(incoming) > 256:
        _error('invalid_change', 'Provide at most 256 server entries.')
    (name, fields, compressed), existing = _servers(root)
    records = _value(fields, 'servers', (10, []), 9)[1]
    mapping = {item['id']: record for item, record in zip(existing, records)}
    output, seen = [], set()
    for item in incoming:
        if not isinstance(item, dict):
            _error('invalid_change', 'Each server entry must be an object.')
        title, address, ident = item.get('name'), item.get('address'), item.get('id')
        if ident is not None and not isinstance(ident, str):
            _error('invalid_change', 'Existing server IDs must be strings.')
        if not isinstance(title, str) or not title.strip() or len(title) > 512 or any(ord(c) < 32 for c in title):
            _error('invalid_change', 'Enter a bounded nonempty server name.')
        _server_address(address)
        if ident is not None and (ident not in mapping or ident in seen):
            _error('missing_item', 'The existing server identity is missing or duplicated.')
        seen.add(ident) if ident else None
        record = copy.deepcopy(mapping[ident]) if ident else {}
        record['name'], record['ip'] = (8, title), (8, address)
        if 'acceptTextures' in item:
            acceptance = item['acceptTextures']
            if acceptance is not None and not isinstance(acceptance, bool):
                _error('invalid_change', 'Server resource acceptance must be true, false or prompt.')
            if acceptance is None:
                record.pop('acceptTextures', None)
            else:
                record['acceptTextures'] = (1, int(acceptance))
        elif 'resourcePolicy' in item:
            policy = item['resourcePolicy']
            if policy not in ('prompt', 'enabled', 'disabled'):
                _error('invalid_change', 'The server resource policy is invalid.')
            if policy == 'prompt':
                record.pop('acceptTextures', None)
            else:
                record['acceptTextures'] = (1, int(policy == 'enabled'))
        output.append(record)
    fields['servers'] = (9, (10, output))
    raw = _nbt_bytes(name, fields, compressed)
    history = _backup(root, root / 'servers.dat', data_dir, instance_id) if (root / 'servers.dat').exists() else {}
    _atomic(root / 'servers.dat', raw)
    return {'ok': True, **history, 'message': 'The installation server list was saved.'}


def _server_address(address):
    if (not isinstance(address, str) or not address or len(address) > 512 or
            any(c.isspace() or ord(c) < 32 or c in '/\\?#@' for c in address)):
        _error('invalid_change', 'Enter a server host or host:port without spaces.')
    port = None
    if address.startswith('['):
        match = re.fullmatch(r'\[([^\]]+)\](?::([0-9]+))?', address)
        if not match:
            _error('invalid_change', 'An IPv6 server address requires matching brackets and a valid port.')
        try:
            ipaddress.IPv6Address(match.group(1))
        except ValueError as exc:
            raise ContentError('invalid_change', 'The IPv6 server address is invalid.') from exc
        port = match.group(2)
    elif address.count(':') > 1:
        try:
            ipaddress.IPv6Address(address)
        except ValueError as exc:
            raise ContentError('invalid_change', 'The server address is invalid.') from exc
    else:
        host, separator, port_value = address.partition(':')
        port = port_value if separator else None
        try:
            hostname = host.rstrip('.').encode('idna').decode('ascii')
        except UnicodeError as exc:
            raise ContentError('invalid_change', 'The server hostname is invalid.') from exc
        if len(hostname) > 253 or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in hostname.split('.')):
            _error('invalid_change', 'The server hostname is invalid.')
    if port is not None and (not port.isascii() or not port.isdigit() or len(port) > 5 or not 1 <= int(port) <= 65535):
        _error('invalid_change', 'The server port must be between 1 and 65535.')


def _activate(root, kind, body, baseline, data_dir, instance_id):
    mapping = {item['id']: item for item in baseline['items']}
    if kind == 'resourcepacks':
        ids = body.get('ids')
        if not isinstance(ids, list) or len(ids) > 256 or any(not isinstance(v, str) for v in ids) or len(set(ids)) != len(ids):
            _error('invalid_change', 'Provide an ordered list of unique resource pack IDs.')
        if any(ident not in mapping or not mapping[ident].get('enabled') for ident in ids):
            _error('missing_item', 'Selected resource packs must exist and be enabled.')
        text, selected, _ = _resource_settings(root, baseline['items'])
        installed = {'file/' + item['filename'] for item in baseline['items']}
        retained = [name for name in selected if name not in installed]
        if 'vanilla' not in retained:
            retained.insert(0, 'vanilla')
        selection = retained + ['file/' + mapping[ident]['filename'] for ident in reversed(ids)]
        path = root / 'options.txt'
        raw = _patch_settings(text, {'resourcePacks': json.dumps(selection, ensure_ascii=False, separators=(',', ':'))}, ':')
    else:
        ident = body.get('id')
        if ident is not None and (ident not in mapping or not mapping[ident].get('enabled')):
            _error('missing_item', 'The selected shader must exist and be enabled.')
        renderer, path, text, _, _ = _shader_settings(root, baseline['items'])
        if not renderer:
            _error('renderer_required', 'Install a compatible Iris or OptiFine renderer before selecting a shader.')
        updates = {'shaderPack': mapping[ident]['filename'] if ident else 'OFF'}
        if renderer == 'iris':
            updates['enableShaders'] = 'true' if ident else 'false'
        raw = _patch_settings(text, updates, '=')
    history = _backup(root, path, data_dir, instance_id) if path.exists() else {}
    _atomic(path, raw)
    return {'ok': True, **history, 'message': 'The selected packs were saved to the installation settings.'}


def _restore(root, data_dir, instance_id, ident):
    if not isinstance(ident, str) or not _BACKUP_ID.fullmatch(ident):
        _error('unsafe_path', 'Select a launcher recovery archive by its ID.')
    source = _backup_folder(data_dir, instance_id, root) / (ident + '.zip')
    _no_links(source)
    if not source.is_file():
        _error('missing_item', 'The recovery archive no longer exists.')
    names = _validated_archive(source)
    for name in names:
        destination = _relative_path(root, name.rstrip('/'))
        if destination.exists():
            _error('path_conflict', 'Restore would replace existing files; move them aside before restoring.')
    staging = root / '.launcher-tools' / ('restore-' + uuid.uuid4().hex)
    moved = []
    try:
        _extract(source, staging)
        # Merge directories, never replace an existing file or directory.
        for relative, path, directory in _tree(staging):
            if not relative:
                continue
            destination = _relative_path(root, relative)
            if directory:
                if not destination.exists():
                    destination.mkdir(parents=True)
                    moved.append((destination, True))
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    _error('path_conflict', 'A restore destination changed during the operation.')
                os.replace(path, destination)
                moved.append((destination, False))
        return {'ok': True, 'message': 'The recovery archive was restored without replacing existing files.'}
    except BaseException:
        for destination, directory in reversed(moved):
            if destination.exists():
                destination.rmdir() if directory else destination.unlink()
        raise
    finally:
        if staging.exists():
            _tree(staging)
            shutil.rmtree(staging)


def mutate_tools(root, data_dir, instance_id, body, open_path=None, pick_path=None):
    if not isinstance(body, dict) or body.get('kind') not in KINDS or not isinstance(body.get('action'), str):
        _error('invalid_change', 'Provide a supported tool kind and action.')
    root = _root(root, allow_missing=True)
    kind, action = body['kind'], body['action']
    if body.get('id') is not None and not isinstance(body['id'], str):
        _error('invalid_change', 'Selected tool item IDs must be strings.')
    running, private = body.get('running', False), body.get('private', True)
    if not isinstance(running, bool) or not isinstance(private, bool):
        _error('invalid_change', 'Tool state flags must be booleans.')
    params = {'id': body.get('worldId'), 'instanceId': instance_id} if kind == 'datapacks' else {'instanceId': instance_id}
    with _lock(root):
        _recover(root, writable=bool(private and not running))
        baseline = _state(root, kind, params, private, running)
        if not isinstance(body.get('revision'), str) or body['revision'] != baseline['revision']:
            _error('stale_revision', 'Installation files changed; refresh this tool before continuing.')
        if action not in baseline['capabilities']:
            if running:
                _error('game_running', 'Stop Minecraft before performing this operation.')
            if not private:
                _error('private_root_required', 'Make an independent installation copy before editing these files.')
            _error('unsupported_action', 'This action is unavailable for the selected installation tool.')
        item = next((i for i in baseline['items'] if i['id'] == body.get('id')), None)
        if body.get('id') is not None and item is None and not (kind == 'advanced' and action == 'restore') and action != 'activate':
            _error('missing_item', 'The selected item no longer exists.')
        if kind == 'advanced':
            source, folder = root, root
        elif kind == 'servers':
            source, folder = root / 'servers.dat', root
        elif kind == 'datapacks':
            world = _world(root, body.get('worldId'))
            folder = root / world['path'] / 'datapacks'
            source = root / item['path'] if item else folder
        else:
            folder = root / ('saves' if kind == 'worlds' else kind)
            source = root / item['path'] if item else folder
        if action not in ('import', 'save', 'activate', 'reveal', 'backup', 'export', 'restore') and item is None:
            _error('missing_item', 'The selected item no longer exists.')
        if action == 'reveal':
            _no_links(source)
            if not source.exists():
                _error('missing_item', 'The selected folder or file does not exist.')
            if not callable(open_path):
                _error('opener_unavailable', 'Native folder access is unavailable.')
            response = open_path(str(source))
            if response is False or (isinstance(response, dict) and (response.get('ok') is False or response.get('error'))):
                _error('open_failed', 'The operating system could not open the selected path.')
            return {'ok': True, 'path': str(source), 'revision': baseline['revision'], 'message': 'The selected path was opened.'}
        if action == 'backup':
            result = {'ok': True, **_backup(root, source, data_dir, instance_id), 'message': 'A recoverable backup was created.'}
        elif action == 'export':
            destination = _picker(pick_path, body, {'kind': 'file', 'title': 'Export ' + kind,
                                                  'save': True, 'defaultName': source.name if source.is_file() and kind in ('screenshots', 'servers') else (source.name + '.zip'),
                                                  'filters': [source.suffix] if source.is_file() else ['.zip']})
            if destination is None:
                return {'ok': True, 'cancelled': True, 'revision': baseline['revision'], 'message': 'File selection was cancelled.'}
            if destination.is_relative_to(root):
                _error('unsafe_path', 'Export files outside the installation directory.')
            if source.is_file() and kind in ('screenshots', 'servers'):
                _copy(source, destination)
            else:
                _archive(root, source, destination, ('.launcher-tools', '.launcher-content') if source == root else ())
            result = {'ok': True, 'outputPath': str(destination), 'message': 'The selected files were exported.'}
        elif action == 'import':
            result = _import(root, kind, body, folder, pick_path)
        elif action == 'save':
            result = _save_servers(root, body, baseline, data_dir, instance_id)
        elif action == 'activate':
            result = _activate(root, kind, body, baseline, data_dir, instance_id)
        elif action == 'restore':
            result = _restore(root, data_dir, instance_id, body.get('id'))
        elif action == 'datapacks':
            if not item:
                _error('missing_item', 'Select a world to inspect its datapacks.')
            return _state(root, 'datapacks', {'id': item['id']}, private, running)
        elif action == 'copy':
            name = body.get('name') or ((source.stem + ' copy' + source.suffix) if source.is_file() else source.name + ' copy')
            destination = _destination(folder, name)
            _copy(source, destination)
            result = {'ok': True, 'path': destination.relative_to(root).as_posix(), 'message': 'An independent local copy was created.'}
        elif action == 'rename':
            name = body.get('name')
            if kind == 'screenshots' and isinstance(name, str):
                if not Path(name).suffix:
                    name += source.suffix
                if Path(name).suffix.casefold() != source.suffix.casefold():
                    _error('invalid_change', 'Keep the screenshot image extension when renaming.')
            destination = _destination(folder, name)
            level = None
            if kind == 'worlds':
                root_name, fields, compressed = _nbt(source / 'level.dat')
                data = _value(fields, 'Data', {}, 10)
                if 'Data' not in fields:
                    fields['Data'] = (10, data)
                data['LevelName'] = (8, name)
                level = _nbt_bytes(root_name, fields, compressed)
            history = _backup(root, source, data_dir, instance_id)
            _rename_or_delete(root, source, destination, 'rename', level)
            result = {'ok': True, **history, 'path': destination.relative_to(root).as_posix(), 'message': 'The selected item was renamed; its original is retained in recovery history.'}
        elif action == 'delete':
            history = _backup(root, source, data_dir, instance_id)
            destination = root / '.launcher-tools' / ('deleted-' + uuid.uuid4().hex)
            _rename_or_delete(root, source, destination, 'delete')
            result = {'ok': True, **history, 'message': 'The selected item was deleted; a recoverable archive was retained.'}
        else:
            _error('unsupported_action', 'This tool action is unsupported.')
        result['revision'] = _state(root, kind, params, private, running)['revision']
        return result
