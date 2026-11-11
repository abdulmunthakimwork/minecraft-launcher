"""Sanitizer checks use only a synthetic PNG in a disposable directory."""
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

SCRIPT = Path(__file__).with_name('sanitize-png-metadata.py')
SIGNATURE = b'\x89PNG\r\n\x1a\n'


def chunk(kind, payload):
    return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff)


class SanitizePngTests(unittest.TestCase):
    def test_metadata_removed_with_retained_rendering_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / 'fixture.png'
            retained = [chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0)),
                        chunk(b'sRGB', b'\x00'), chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00\xff')),
                        chunk(b'IEND', b'')]
            metadata = [chunk(b'tEXt', b'Source\x00C:\\Users\\fixture\\private.png'),
                        chunk(b'eXIf', b'Synthetic private metadata'), chunk(b'tIME', b'\x07\xe8\x01\x01\x00\x00\x00')]
            image.write_bytes(SIGNATURE + retained[0] + b''.join(metadata) + b''.join(retained[1:]))
            report_path = root / 'report.json'
            result = subprocess.run([sys.executable, str(SCRIPT), str(root), str(report_path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(image.read_bytes(), SIGNATURE + b''.join(retained))
            report = json.loads(report_path.read_text())
            self.assertEqual(report['filesChanged'], 1)
            self.assertTrue(report['renderingChunksUnchanged'])
            self.assertEqual(report['removedChunks'], {'tEXt':1, 'eXIf':1, 'tIME':1})
            result = subprocess.run([sys.executable, str(SCRIPT), str(root), str(report_path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(report_path.read_text())['filesChanged'], 0)

    def test_invalid_crc_refuses_to_rewrite_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / 'fixture.png'
            original = SIGNATURE + chunk(b'tEXt', b'Name\x00Synthetic')[:-1] + b'\x00'
            image.write_bytes(original)
            result = subprocess.run([sys.executable, str(SCRIPT), str(root), str(root / 'report.json')], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Invalid PNG CRC', result.stderr)
            self.assertEqual(image.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
