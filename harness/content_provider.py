"""Bounded Modrinth metadata and verified acquisition, with injectable transport.

The transport boundary accepts a urllib Request and returns a binary response.
The default opener checks *every* redirect before making its next request. A
custom transport must implement equivalent redirect handling; its final URL is
also checked before response bytes are consumed here.
"""
import hashlib
import json
import os
import re
import struct
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict

MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_ICON_BYTES = 2 * 1024 * 1024
_ICON_CACHE_LIMIT = 16 * 1024 * 1024
_ICON_CACHE = OrderedDict()
_ICON_CACHE_LOCK = threading.Lock()
API = 'https://api.modrinth.com/v2'
_KINDS = {'mods': 'mod', 'resourcepacks': 'resourcepack', 'shaderpacks': 'shader'}
_HASH_LENGTHS = {'sha1': 40, 'sha256': 64, 'sha512': 128}


class ProviderError(ValueError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.details = code, details


def validate_url(url, host='cdn.modrinth.com'):
    try:
        parts = urllib.parse.urlsplit(url)
        allowed = (parts.scheme == 'https' and parts.hostname == host and
                   parts.port in (None, 443) and parts.username is None and
                   parts.password is None and not parts.fragment and
                   not any(ord(char) < 33 or ord(char) == 127 for char in url) and
                   '\\' not in url)
    except (ValueError, TypeError):
        allowed = False
    if not allowed:
        raise ProviderError('unsafe_url', 'Provider URL is outside the supported HTTPS host.', {'url': url})
    return url


def optional_icon_url(value):
    """Optional artwork is allowed only on the supported provider CDN."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    try:
        return validate_url(value)
    except ProviderError:
        return None


def raster_image_type(data):
    """Identify bounded raster artwork without executing or decoding it."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_ICON_BYTES:
        raise ProviderError('icon_limit', 'Project artwork exceeds the supported size.')
    dimensions, content_type = None, None
    if len(data) >= 33 and data.startswith(b'\x89PNG\r\n\x1a\n') and data[12:16] == b'IHDR':
        dimensions, content_type = struct.unpack('>II', data[16:24]), 'image/png'
    elif len(data) >= 13 and data[:6] in (b'GIF87a', b'GIF89a'):
        dimensions, content_type = struct.unpack('<HH', data[6:10]), 'image/gif'
    elif len(data) >= 30 and data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        chunk = data[12:16]
        if chunk == b'VP8X':
            dimensions = (1 + int.from_bytes(data[24:27], 'little'), 1 + int.from_bytes(data[27:30], 'little'))
        elif chunk == b'VP8 ' and data[23:26] == b'\x9d\x01\x2a':
            dimensions = tuple(value & 0x3FFF for value in struct.unpack('<HH', data[26:30]))
        elif chunk == b'VP8L' and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], 'little')
            dimensions = (1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF))
        content_type = 'image/webp'
    elif data.startswith(b'\xff\xd8'):
        position = 2
        while position < len(data):
            if data[position] != 0xFF:
                break
            while position < len(data) and data[position] == 0xFF:
                position += 1
            if position >= len(data):
                break
            marker, position = data[position], position + 1
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                continue
            if marker in (0xD9, 0xDA) or position + 2 > len(data):
                break
            length = int.from_bytes(data[position:position + 2], 'big')
            if length < 2 or position + length > len(data):
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF) and length >= 7:
                height, width = struct.unpack('>HH', data[position + 3:position + 7])
                dimensions, content_type = (width, height), 'image/jpeg'
                break
            position += length
    if dimensions is None or any(not 0 < value <= 4096 for value in dimensions):
        raise ProviderError('icon_type', 'Project artwork is not a supported bounded raster image.')
    return content_type


def validate_file(file):
    """Validate metadata before staging. Returned hashes have normalized keys."""
    if not isinstance(file, dict):
        raise ProviderError('invalid_file', 'Missing provider file metadata.')
    name = file.get('filename')
    if (not isinstance(name, str) or not name or len(name) > 240 or name in ('.', '..') or
            any(char in name for char in '\\/:*?"<>|') or name[-1] in '. ' or
            any(ord(char) < 32 or ord(char) == 127 for char in name) or
            re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', name)):
        raise ProviderError('invalid_file', 'Provider filename is unsafe.')
    validate_url(file.get('url'))
    size = file.get('size')
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_FILE_BYTES:
        raise ProviderError('invalid_file', 'Provider file size is missing or exceeds 512 MiB.')
    hashes = file.get('hashes')
    if not isinstance(hashes, dict) or not hashes:
        raise ProviderError('invalid_file', 'Provider file has no supported verification hash.')
    normalized = {}
    for key, value in hashes.items():
        key = str(key).lower()
        if key not in _HASH_LENGTHS:
            continue
        if not isinstance(value, str) or not re.fullmatch(r'[a-fA-F0-9]{%d}' % _HASH_LENGTHS[key], value):
            raise ProviderError('invalid_file', 'Provider verification hash is malformed.')
        normalized[key] = value.lower()
    if not normalized:
        raise ProviderError('invalid_file', 'Provider file has no supported verification hash.')
    return normalized


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        host = urllib.parse.urlsplit(request.full_url).hostname
        validate_url(newurl, host)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def _default_transport(request, timeout=30):
    return urllib.request.build_opener(_CheckedRedirect()).open(request, timeout=timeout)


def _loaders(loaders, project_type):
    if project_type == 'resourcepack':
        return ['minecraft']
    if project_type == 'shader':
        return []  # Shader renderer requirements are distinct from mod loaders.
    if isinstance(loaders, str):
        loaders = [loaders]
    result = []
    for loader in loaders or []:
        value = str(loader).lower().replace('-', '').replace('_', '')
        if value in ('fabric', 'quilt', 'forge', 'neoforge') and value not in result:
            result.append(value)
    return result


class ModrinthProvider:
    name = 'modrinth'

    def __init__(self, transport=None):
        self.transport = transport or _default_transport
        self._cache_icons = transport is None

    def icon(self, url):
        """Fetch original raster bytes for the launcher's same-origin image route.

        The shared byte/count-bounded cache applies only to the real transport;
        injected transports retain deterministic, independent test boundaries.
        """
        if optional_icon_url(url) is None:
            raise ProviderError('unsafe_url', 'Project artwork is outside the supported HTTPS CDN.')
        if self._cache_icons:
            with _ICON_CACHE_LOCK:
                cached = _ICON_CACHE.get(url)
                if cached is not None and time.monotonic() - cached[0] < 3600:
                    _ICON_CACHE.move_to_end(url)
                    return dict(cached[1])
                _ICON_CACHE.pop(url, None)
        request = urllib.request.Request(url, headers={'User-Agent': 'InstallationWorkspace/1.0 (MinecraftLauncher)',
            'Accept': 'image/png,image/jpeg,image/webp,image/gif', 'Accept-Encoding': 'identity'})
        try:
            with self.transport(request, timeout=15) as response:
                validate_url(response.geturl())
                declared = response.headers.get('Content-Length')
                if declared is not None and not 0 < int(declared) <= MAX_ICON_BYTES:
                    raise ProviderError('icon_limit', 'Project artwork exceeds 2 MiB.')
                body = response.read(MAX_ICON_BYTES + 1)
                if declared is not None and len(body) != int(declared):
                    raise ProviderError('icon_response', 'Project artwork was not received completely.')
                content_type = raster_image_type(body)
                reported = (response.headers.get('Content-Type') or '').split(';', 1)[0].strip().lower()
                if reported and reported not in (content_type, 'application/octet-stream'):
                    raise ProviderError('icon_type', 'Project artwork content type does not match its raster bytes.')
            result = {'body': body, 'contentType': content_type}
            if self._cache_icons:
                with _ICON_CACHE_LOCK:
                    _ICON_CACHE[url] = (time.monotonic(), result)
                    _ICON_CACHE.move_to_end(url)
                    while len(_ICON_CACHE) > 128 or sum(len(entry[1]['body']) for entry in _ICON_CACHE.values()) > _ICON_CACHE_LIMIT:
                        _ICON_CACHE.popitem(last=False)
            return dict(result)
        except ProviderError:
            raise
        except (OSError, ValueError) as exc:
            raise ProviderError('icon_response', 'Project artwork could not be loaded.') from exc

    def _request(self, path, query=None, body=None):
        url = API + path
        if query:
            url += '?' + urllib.parse.urlencode(query)
        validate_url(url, 'api.modrinth.com')
        request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                         headers={'User-Agent': 'InstallationWorkspace/1.0 (MinecraftLauncher)',
                                                  'Accept': 'application/json', 'Content-Type': 'application/json'})
        try:
            with self.transport(request, timeout=30) as response:
                validate_url(response.geturl(), 'api.modrinth.com')
                declared = response.headers.get('Content-Length')
                if declared is not None and int(declared) > MAX_JSON_BYTES:
                    raise ProviderError('metadata_limit', 'Provider metadata exceeds 8 MiB.')
                data = response.read(MAX_JSON_BYTES + 1)
                if len(data) > MAX_JSON_BYTES:
                    raise ProviderError('metadata_limit', 'Provider metadata exceeds 8 MiB.')
            return json.loads(data.decode('utf-8'))
        except ProviderError:
            raise
        except urllib.error.HTTPError as exc:
            raise ProviderError('provider_http', 'Provider rejected this request.', {'status': exc.code}) from exc
        except (OSError, ValueError, UnicodeError, RecursionError) as exc:
            raise ProviderError('provider_response', 'Provider metadata could not be read: ' + str(exc)) from exc

    def search(self, query, kind, minecraft, loaders, offset=0, limit=18):
        project_type = _KINDS.get(kind)
        if not project_type or not minecraft or not isinstance(query, str):
            raise ProviderError('invalid_query', 'Select a supported content kind and pinned Minecraft version.')
        if not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ProviderError('invalid_query', 'Search pagination is invalid.')
        facets = [['project_type:' + project_type], ['versions:' + str(minecraft)]]
        wanted = _loaders(loaders, project_type)
        if wanted:
            facets.append(['categories:' + loader for loader in wanted])
        facets.append(['client_side:required', 'client_side:optional'])
        return self._request('/search', {'query': query[:512], 'facets': json.dumps(facets), 'offset': offset, 'limit': limit})

    def project(self, ident):
        record = self._request('/project/' + urllib.parse.quote(str(ident), safe=''))
        if not isinstance(record, dict):
            raise ProviderError('provider_response', 'Provider returned invalid project metadata.')
        return record

    def versions(self, project_id, minecraft, loaders):
        project = self.project(project_id)
        if project.get('client_side') == 'unsupported':
            return []
        wanted = _loaders(loaders, project.get('project_type', 'mod'))
        if project.get('project_type', 'mod') == 'mod' and not wanted:
            return []
        query = {'game_versions': json.dumps([str(minecraft)])}
        if wanted:
            query['loaders'] = json.dumps(wanted)
        records = self._request('/project/' + urllib.parse.quote(str(project_id), safe='') + '/version', query)
        if not isinstance(records, list):
            raise ProviderError('provider_response', 'Provider returned an invalid version list.')
        return [record for record in records if isinstance(record, dict) and
                str(minecraft) in record.get('game_versions', []) and
                (not wanted or set(wanted).intersection(record.get('loaders', []))) and
                record.get('client_side') != 'unsupported']

    def version(self, ident):
        record = self._request('/version/' + urllib.parse.quote(str(ident), safe=''))
        if not isinstance(record, dict):
            raise ProviderError('provider_response', 'Provider returned invalid version metadata.')
        return record

    def identify(self, hashes):
        for algorithm in ('sha512', 'sha1'):
            digest = hashes.get(algorithm) if isinstance(hashes, dict) else None
            if isinstance(digest, str) and re.fullmatch(r'[a-fA-F0-9]{%d}' % _HASH_LENGTHS[algorithm], digest):
                matches = self._request('/version_files', body={'hashes': [digest.lower()], 'algorithm': algorithm})
                return matches.get(digest.lower()) if isinstance(matches, dict) else None
        return None

    def updates(self, items, minecraft, loaders):
        """Return candidates; the caller must resolve this *whole* proposed set."""
        result = []
        for item in items:
            if item.get('provider') != self.name or not item.get('projectId'):
                continue
            channels = item.get('releaseChannels') or [item.get('releaseChannel') or 'release']
            available = self.versions(item['projectId'], minecraft, loaders)
            versions = [record for record in available
                        if record.get('version_type', 'release') in channels]
            versions.sort(key=lambda record: record.get('date_published', ''), reverse=True)
            if versions and versions[0].get('id') != item.get('versionId'):
                if not item.get('versionId'):
                    continue
                current = next((record for record in available if record.get('id') == item['versionId']), None) or self.version(item['versionId'])
                current_date, candidate_date = current.get('date_published'), versions[0].get('date_published')
                if not isinstance(current_date, str) or not isinstance(candidate_date, str):
                    raise ProviderError('unknown_update_order', 'Publication metadata is missing; choose an explicit content version instead of applying an automatic update.')
                if candidate_date <= current_date:
                    continue
                result.append({'id': item['id'], 'projectId': item['projectId'], 'versionId': versions[0]['id'],
                               'releaseChannels': list(channels)})
        return result

    def download(self, file, destination):
        hashes = validate_file(file)
        destination = os.fspath(destination)
        parent = os.path.dirname(os.path.abspath(destination))
        digests = {key: hashlib.new(key) for key in hashes}
        temporary = None
        request = urllib.request.Request(file['url'], headers={'User-Agent': 'InstallationWorkspace/1.0 (MinecraftLauncher)',
                                                               'Accept-Encoding': 'identity'})
        try:
            with self.transport(request, timeout=30) as response:
                validate_url(response.geturl())
                declared = response.headers.get('Content-Length')
                if declared is not None and int(declared) != file['size']:
                    raise ProviderError('size_mismatch', 'Provider download size disagrees with selected metadata.')
                with tempfile.NamedTemporaryFile(prefix='.acquire-', suffix='.partial', dir=parent, delete=False) as staged:
                    temporary = staged.name
                    total = 0
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_FILE_BYTES or total > file['size']:
                            raise ProviderError('size_mismatch', 'Downloaded file exceeds its selected size or 512 MiB.')
                        staged.write(chunk)
                        for digest in digests.values():
                            digest.update(chunk)
                    staged.flush()
                    os.fsync(staged.fileno())
                if total != file['size']:
                    raise ProviderError('size_mismatch', 'Downloaded file is incomplete.')
                if any(digests[key].hexdigest() != value for key, value in hashes.items()):
                    raise ProviderError('hash_mismatch', 'Downloaded file does not match selected verification hashes.')
            os.replace(temporary, destination)
            temporary = None
            return destination
        except ProviderError:
            raise
        except (OSError, ValueError) as exc:
            raise ProviderError('download_failed', 'Provider download failed: ' + str(exc)) from exc
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
