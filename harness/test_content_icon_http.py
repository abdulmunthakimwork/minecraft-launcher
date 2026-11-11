"""Original public project logos stay bounded and same-origin."""
import unittest
from unittest import mock
import urllib.parse
import test_installation_http as fixtures


class ContentIconHttpTests(unittest.TestCase):
    setUp = fixtures.InstallationHttpTests.setUp
    close_host = fixtures.InstallationHttpTests.close_host
    request = fixtures.InstallationHttpTests.request

    def test_original_raster_bytes_are_served_without_sniffing(self):
        image = b'fixture original PNG'
        with mock.patch.object(fixtures.server.content_provider.ModrinthProvider, 'icon', create=True,
                               return_value={'body': image, 'contentType': 'image/png'}) as retrieve:
            url = 'https://cdn.modrinth.com/data/main/icon.png'
            status, body, headers = self.request('/__content_icon?url=' + urllib.parse.quote(url, safe=''))
        self.assertEqual(status, 200)
        self.assertEqual(body, image)
        self.assertEqual(headers['Content-Type'], 'image/png')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        retrieve.assert_called_once_with(url)

    def test_unsafe_logo_is_rejected_before_network(self):
        with mock.patch.object(fixtures.server.content_provider.ModrinthProvider, 'icon', create=True) as retrieve:
            status, _, _ = self.request('/__content_icon?url=https%3A%2F%2F127.0.0.1%2Fsecret')
        self.assertEqual(status, 400)
        retrieve.assert_not_called()
