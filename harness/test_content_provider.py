import base64
import hashlib
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import content_provider as cp


class Response(io.BytesIO):
    def __init__(self, body, url, headers=None):
        super().__init__(body)
        self.url, self.headers = url, headers or {}

    def geturl(self):
        return self.url


class Transport:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, request, timeout=30):
        self.requests.append(request)
        value = self.responses.pop(0)
        if isinstance(value, Response):
            return value
        return Response(json.dumps(value).encode(), request.full_url)


def file_record(body=b'fixture', **overrides):
    result = {'filename': 'fixture.jar', 'url': 'https://cdn.modrinth.com/data/p/versions/v/fixture.jar',
              'size': len(body), 'hashes': {'sha512': hashlib.sha512(body).hexdigest()}}
    result.update(overrides)
    return result


class ProviderTests(unittest.TestCase):
    def test_original_project_icon_bytes_are_returned_with_verified_raster_type(self):
        body = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1QAAAABJRU5ErkJggg==')
        url = 'https://cdn.modrinth.com/data/sodium/icon.png'
        result = cp.ModrinthProvider(Transport([Response(body, url, {'Content-Type': 'image/png', 'Content-Length': str(len(body))})])).icon(url)
        self.assertEqual(result, {'body': body, 'contentType': 'image/png'})

    def test_project_icon_rejects_unsafe_host_redirect_and_non_raster_content(self):
        url = 'https://cdn.modrinth.com/data/p/icon.png'
        for response in (Response(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', url, {'Content-Type': 'image/svg+xml'}),
                         Response(b'<html>not an image</html>', url, {'Content-Type': 'image/png'}),
                         Response(b'fixture', 'https://evil.test/icon.png', {'Content-Type': 'image/png'})):
            with self.subTest(response=response.url), self.assertRaises(cp.ProviderError):
                cp.ModrinthProvider(Transport([response])).icon(url)
        with self.assertRaises(cp.ProviderError):
            cp.ModrinthProvider(Transport([])).icon('http://127.0.0.1/private.png')

    def test_project_icon_rejects_oversized_and_truncated_payloads(self):
        url = 'https://cdn.modrinth.com/data/p/icon.png'
        body = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1QAAAABJRU5ErkJggg==')
        for response in (Response(body, url, {'Content-Length': str(3 * 1024 * 1024)}),
                         Response(b'x' * (2 * 1024 * 1024 + 1), url),
                         Response(body, url, {'Content-Length': str(len(body) + 1)})):
            with self.subTest(size=len(response.getvalue())), self.assertRaises(cp.ProviderError):
                cp.ModrinthProvider(Transport([response])).icon(url)

    def test_original_project_icon_cache_avoids_repeated_acquisition(self):
        body = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1QAAAABJRU5ErkJggg==')
        url = 'https://cdn.modrinth.com/data/cache-fixture/icon.png'
        transport = Transport([Response(body, url, {'Content-Type': 'image/png'})])
        with mock.patch.object(cp, '_default_transport', transport), mock.patch.object(cp, '_ICON_CACHE', cp.OrderedDict()):
            first = cp.ModrinthProvider().icon(url)
            second = cp.ModrinthProvider().icon(url)
        self.assertEqual(first['body'], body)
        self.assertEqual(second['body'], body)
        self.assertEqual(len(transport.requests), 1)

    def test_project_icon_rejects_unbounded_dimensions_and_declared_type_mismatch(self):
        body = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1QAAAABJRU5ErkJggg==')
        huge = body[:16] + (100000).to_bytes(4, 'big') + body[20:]
        url = 'https://cdn.modrinth.com/data/p/icon.png'
        for response in (Response(huge, url), Response(body, url, {'Content-Type': 'image/jpeg'})):
            with self.subTest(size=len(response.getvalue())), self.assertRaises(cp.ProviderError):
                cp.ModrinthProvider(Transport([response])).icon(url)

    def test_resourcepack_search_uses_minecraft_loader_facets(self):
        transport = Transport([{'hits': [], 'offset': 0, 'total_hits': 0}])
        cp.ModrinthProvider(transport).search('textures', 'resourcepacks', '1.20.1', ['fabric'])
        query = parse_qs(urlsplit(transport.requests[0].full_url).query)
        facets = json.loads(query['facets'][0])
        self.assertIn(['categories:minecraft'], facets)
        self.assertIn(['project_type:resourcepack'], facets)

    def test_versions_filter_client_and_exact_context(self):
        records = [{'id': 'right', 'game_versions': ['1.20.1'], 'loaders': ['fabric'], 'version_type': 'release'},
                   {'id': 'wrong', 'game_versions': ['1.20.2'], 'loaders': ['fabric']},
                   {'id': 'server', 'game_versions': ['1.20.1'], 'loaders': ['fabric'], 'client_side': 'unsupported'}]
        provider = cp.ModrinthProvider(Transport([{'id': 'p', 'project_type': 'mod', 'client_side': 'optional'}, records]))
        self.assertEqual([x['id'] for x in provider.versions('p', '1.20.1', ['fabric'])], ['right'])

    def test_identify_uses_exact_sha512_hash(self):
        digest = hashlib.sha512(b'fixture').hexdigest()
        transport = Transport([{digest: {'id': 'known'}}])
        self.assertEqual(cp.ModrinthProvider(transport).identify({'sha512': digest})['id'], 'known')
        payload = json.loads(transport.requests[0].data)
        self.assertEqual(payload, {'hashes': [digest], 'algorithm': 'sha512'})

    def test_update_preserves_release_channel(self):
        versions = [{'id': 'beta', 'version_type': 'beta', 'game_versions': ['1.20.1'], 'loaders': ['fabric'], 'date_published': '2026-11-10'},
                    {'id': 'release', 'version_type': 'release', 'game_versions': ['1.20.1'], 'loaders': ['fabric'], 'date_published': '2026-11-09'}]
        provider = cp.ModrinthProvider(Transport([{'project_type': 'mod'}, versions, {'id': 'old', 'date_published': '2026-11-01'}]))
        result = provider.updates([{'id': 'local', 'provider': 'modrinth', 'projectId': 'p', 'versionId': 'old', 'releaseChannels': ['release']}], '1.20.1', ['fabric'])
        self.assertEqual(result, [{'id': 'local', 'projectId': 'p', 'versionId': 'release', 'releaseChannels': ['release']}])

    def test_update_never_downgrades_to_older_allowed_release(self):
        candidate = {'id': 'older', 'version_type': 'release', 'game_versions': ['1.20.1'], 'loaders': ['fabric'], 'date_published': '2026-11-01'}
        provider = cp.ModrinthProvider(Transport([{'project_type': 'mod'}, [candidate], {'id': 'newer', 'date_published': '2026-11-09'}]))
        items = [{'id': 'local', 'provider': 'modrinth', 'projectId': 'p', 'versionId': 'newer', 'releaseChannels': ['release']}]
        self.assertEqual(provider.updates(items, '1.20.1', ['fabric']), [])

    def test_download_verified_stream_preserves_existing_until_success(self):
        body = b'fixture' * 30000
        with tempfile.TemporaryDirectory() as root:
            target = pathlib.Path(root) / 'acquired.jar'
            target.write_bytes(b'previous')
            provider = cp.ModrinthProvider(Transport([Response(body, file_record(body)['url'])]))
            self.assertEqual(pathlib.Path(provider.download(file_record(body), target)), target)
            self.assertEqual(target.read_bytes(), body)

    def test_download_rejects_hash_and_size_failure_without_overwrite(self):
        for changes in ({'hashes': {'sha512': '0' * 128}}, {'size': 2}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as root:
                target = pathlib.Path(root) / 'file.jar'
                target.write_bytes(b'previous')
                provider = cp.ModrinthProvider(Transport([Response(b'fixture', file_record()['url'])]))
                with self.assertRaises(cp.ProviderError):
                    provider.download(file_record(**changes), target)
                self.assertEqual(target.read_bytes(), b'previous')
                self.assertEqual(list(pathlib.Path(root).iterdir()), [target])

    def test_download_rejects_missing_hash_unsafe_urls_and_redirect(self):
        for changes in ({'hashes': {}}, {'url': 'http://cdn.modrinth.com/file.jar'},
                        {'url': 'https://cdn.modrinth.com.evil.test/file.jar'},
                        {'url': 'https://user@cdn.modrinth.com/file.jar'}, {'filename': '../bad.jar'},
                        {'size': cp.MAX_FILE_BYTES + 1}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as root:
                with self.assertRaises(cp.ProviderError):
                    cp.ModrinthProvider(Transport([])).download(file_record(**changes), pathlib.Path(root) / 'file.jar')
        with tempfile.TemporaryDirectory() as root:
            response = Response(b'fixture', 'https://evil.test/file.jar')
            with self.assertRaises(cp.ProviderError):
                cp.ModrinthProvider(Transport([response])).download(file_record(), pathlib.Path(root) / 'file.jar')
            self.assertFalse(response.closed is False)

    def test_api_json_and_redirect_limits(self):
        for response in (Response(b' ' * (cp.MAX_JSON_BYTES + 1), 'https://api.modrinth.com/v2/project/p'),
                         Response(b'{}', 'https://evil.test/metadata')):
            with self.subTest(url=response.url), self.assertRaises(cp.ProviderError):
                cp.ModrinthProvider(Transport([response])).project('p')

    def test_default_redirect_handler_rejects_each_cross_host_hop(self):
        request = cp.urllib.request.Request('https://cdn.modrinth.com/file.jar')
        with self.assertRaises(cp.ProviderError):
            cp._CheckedRedirect().redirect_request(request, None, 302, 'Found', {}, 'https://evil.test/file.jar')

    def test_download_checks_all_provided_hashes(self):
        body = b'fixture'
        hashes = file_record()['hashes'] | {'sha1': '0' * 40}
        with tempfile.TemporaryDirectory() as root:
            destination = pathlib.Path(root) / 'fixture.jar'
            with self.assertRaises(cp.ProviderError):
                cp.ModrinthProvider(Transport([Response(body, file_record()['url'])])).download(file_record(hashes=hashes), destination)
            self.assertFalse(destination.exists())

    def test_mod_versions_without_supported_loader_are_not_advertised_compatible(self):
        provider = cp.ModrinthProvider(Transport([{'project_type': 'mod'}]))
        self.assertEqual(provider.versions('mod', '1.20.1', ['vanilla']), [])

    def test_project_metadata_must_be_an_object(self):
        payload = b'[{}]'
        with self.assertRaises(cp.ProviderError):
            cp.ModrinthProvider(Transport([Response(payload, 'https://api.modrinth.com/v2/project/p')])).project('p')


if __name__ == '__main__':
    unittest.main()
