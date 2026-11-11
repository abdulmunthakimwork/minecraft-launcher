"""Owned downloads exercised with local HTTP and disposable destinations only."""
import hashlib
import http.server
import importlib
import copy
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

import installer
import modpacks
import installation_service
import test_installation_service as service_fixtures


def until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Timed out waiting for fixture condition')


class LocalTransfer:
    def __init__(self, body, ranges=True, truncate=False, delayed_headers=False):
        self.body, self.ranges, self.truncate = body, ranges, truncate
        self.requests = []
        self.headers_gate = threading.Event()
        if not delayed_headers:
            self.headers_gate.set()
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                start = int(self.headers.get('Range', 'bytes=0-').split('=')[1].split('-')[0])
                fixture.requests.append(start)
                fixture.headers_gate.wait(4)
                offset = start if fixture.ranges else 0
                self.send_response(206 if offset else 200)
                if fixture.truncate:
                    self.send_header('Transfer-Encoding', 'chunked')
                else:
                    self.send_header('Content-Length', str(len(fixture.body) - offset))
                self.send_header('ETag', '"fixture-v1"')
                if offset:
                    self.send_header('Content-Range', f'bytes {offset}-{len(fixture.body)-1}/{len(fixture.body)}')
                self.end_headers()
                body = fixture.body[offset:]
                if fixture.truncate:
                    body = body[:65536]
                try:
                    for index in range(0, len(body), 8192):
                        block = body[index:index+8192]
                        if fixture.truncate:
                            self.wfile.write(('%x\r\n' % len(block)).encode() + block + b'\r\n')
                        else:
                            self.wfile.write(block)
                        self.wfile.flush()
                        time.sleep(.005)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                if fixture.truncate:
                    self.connection.shutdown(socket.SHUT_RDWR)

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:%d/file' % self.server.server_port

    def close(self):
        self.headers_gate.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class TransferControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dest = Path(self.temp.name) / 'artifact.jar'
        self.raw = b'fixture payload\0' * 65536

    def fixture(self, **options):
        fixture = LocalTransfer(self.raw, **options)
        self.addCleanup(fixture.close)
        return fixture

    def controls(self):
        try:
            module = importlib.import_module('download_control')
        except ModuleNotFoundError:
            module = None
        self.assertIsNotNone(module, 'owned resumable transfers need a shared download controller')
        return module

    def transfer(self, fixture, control):
        module = self.controls()
        progress, errors = [], []

        def run():
            try:
                module.download(fixture.url, self.dest, size=len(self.raw),
                                hashes={'sha256': hashlib.sha256(self.raw).hexdigest()},
                                control=control, progress=progress.append)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(lambda: (control.request('cancel') if thread.is_alive() else None, thread.join(3)))
        return thread, progress, errors

    def test_failed_transfer_removes_unverified_partial_file(self):
        fixture = self.fixture(truncate=True)
        with self.assertRaises(Exception):
            installer._fetch(fixture.url, str(self.dest), len(self.raw), hashlib.sha1(self.raw).hexdigest())
        self.assertFalse(self.dest.exists())
        self.assertFalse(Path(str(self.dest) + '.part').exists(), 'broken connection must not leave unsafe bytes')

    def test_transfer_size_failure_preserves_integrity_error_code(self):
        module = self.controls()
        fixture = self.fixture()
        with self.assertRaises(RuntimeError) as caught:
            module.download(fixture.url, self.dest, size=len(self.raw) + 1,
                            hashes={'sha1': hashlib.sha1(self.raw).hexdigest()})
        self.assertEqual(getattr(caught.exception, 'code', None), 'size_mismatch')
        self.assertFalse(self.dest.exists())

    def test_transfer_hash_failure_preserves_integrity_error_code(self):
        module = self.controls()
        fixture = self.fixture()
        with self.assertRaises(RuntimeError) as caught:
            module.download(fixture.url, self.dest, size=len(self.raw), hashes={'sha1': '0' * 40})
        self.assertEqual(getattr(caught.exception, 'code', None), 'hash_mismatch')
        self.assertFalse(self.dest.exists())

    def test_pause_closes_connection_and_resume_preserves_verified_bytes(self):
        control = self.controls().JobControl()
        fixture = self.fixture()
        thread, progress, errors = self.transfer(fixture, control)
        until(lambda: progress and progress[-1] >= 65536)
        control.request('pause')
        time.sleep(.08)
        paused = progress[-1]
        time.sleep(.08)
        self.assertEqual(progress[-1], paused, 'acquisition must stop while paused')
        self.assertTrue(thread.is_alive())
        self.assertFalse(self.dest.exists())
        control.request('resume')
        thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.dest.read_bytes(), self.raw)
        self.assertTrue(any(offset > 0 for offset in fixture.requests), 'resume must reopen the HTTP transfer')

    def test_range_refusal_restarts_without_corrupting_destination(self):
        control = self.controls().JobControl()
        fixture = self.fixture(ranges=False)
        thread, progress, errors = self.transfer(fixture, control)
        until(lambda: progress and progress[-1] >= 65536)
        control.request('pause')
        time.sleep(.06)
        control.request('resume')
        thread.join(4)
        self.assertEqual(errors, [])
        self.assertEqual(self.dest.read_bytes(), self.raw)
        self.assertGreater(len(fixture.requests), 1)

    def test_cancel_paused_transfer_wakes_worker_and_cleans_partial(self):
        module = self.controls()
        control = module.JobControl()
        fixture = self.fixture()
        thread, progress, errors = self.transfer(fixture, control)
        until(lambda: progress and progress[-1] >= 65536)
        control.request('pause')
        time.sleep(.06)
        control.request('cancel')
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], module.DownloadCancelled)
        self.assertFalse(self.dest.exists())
        self.assertFalse(Path(str(self.dest) + '.part').exists())

    def test_completed_verified_file_is_reused_without_a_request(self):
        module = self.controls()
        fixture = self.fixture()
        self.dest.write_bytes(self.raw)
        module.download(fixture.url, self.dest, size=len(self.raw),
                        hashes={'sha1': hashlib.sha1(self.raw).hexdigest()}, control=module.JobControl())
        self.assertEqual(fixture.requests, [])
        self.assertEqual(self.dest.read_bytes(), self.raw)

    def test_commit_rejects_controls_and_cancel_cannot_enter_commit(self):
        module = self.controls()
        control = module.JobControl()
        with control.noninterruptible('Applying verified content'):
            self.assertFalse(control.snapshot()['canCancel'])
            with self.assertRaises(module.ControlError):
                control.request('cancel')
        control.request('cancel')
        with self.assertRaises(module.DownloadCancelled):
            with control.noninterruptible('Applying verified content'):
                self.fail('cancelled transaction reached commit')

    def test_concurrent_jobs_reuse_one_verified_destination_without_partial_races(self):
        module = self.controls()
        fixture = self.fixture()
        barrier, errors = threading.Barrier(2), []
        def worker():
            try:
                barrier.wait()
                module.download(fixture.url, self.dest, size=len(self.raw),
                    hashes={'sha256': hashlib.sha256(self.raw).hexdigest()}, control=module.JobControl())
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(4)
        self.assertEqual(errors, [], 'two owned jobs raced their shared verified cache')
        self.assertEqual(self.dest.read_bytes(), self.raw)
        self.assertEqual(len(fixture.requests), 1)

    def test_cancel_during_pending_http_headers_returns_before_network_timeout(self):
        module = self.controls()
        fixture = self.fixture(delayed_headers=True)
        control = module.JobControl()
        thread, progress, errors = self.transfer(fixture, control)
        until(lambda: fixture.requests)
        control.request('pause')
        control.request('cancel')
        thread.join(.4)
        was_blocked = thread.is_alive()
        fixture.headers_gate.set()
        thread.join(3)
        self.assertFalse(was_blocked, 'cancel was held by an HTTP opener waiting for headers')
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], module.DownloadCancelled)
        self.assertFalse(self.dest.exists())


class InstallerJobTests(unittest.TestCase):
    # Share fixture helpers only; do not rerun the transfer cases in this class.
    def setUp(self):
        TransferControlTests.setUp(self)
        previous = copy.deepcopy(installer._STATE)
        hints = dict(installer.DIR_HINTS)
        self.addCleanup(installer._STATE.update, previous)
        self.addCleanup(installer.DIR_HINTS.update, hints)
        installer._STATE.update(active=False, phase='idle', todo=[], result=None, _detail={})

    fixture = TransferControlTests.fixture
    controls = TransferControlTests.controls

    def prepared(self, fixture):
        game, state = Path(self.temp.name) / 'game', Path(self.temp.name) / 'state'
        component = game / 'runtime' / 'fixture-runtime'
        (component / 'bin').mkdir(parents=True)
        (component / 'bin/java.exe').write_bytes(b'local synthetic executable; never run')
        (component / 'manifest.json').write_text(json.dumps({'files': {'bin/java.exe': {'type': 'file'}}}))
        metadata = state / 'versions' / 'fixture' / 'fixture.json'
        metadata.parent.mkdir(parents=True)
        metadata.write_text(json.dumps({'id': 'fixture', 'javaVersion': {'component': 'fixture-runtime'},
                                       'downloads': {'client': {'url': fixture.url, 'size': len(self.raw),
                                                                'sha1': hashlib.sha1(self.raw).hexdigest()}}}))
        return game, state, metadata

    def test_installer_cancel_is_job_scoped_and_never_reports_completion(self):
        fixture = self.fixture()
        game, state, metadata = self.prepared(fixture)
        initial = installer.start(str(game), str(state), str(metadata))
        self.assertIn('jobId', initial, 'actual installer must expose its owned job')
        until(lambda: installer.state()['bytesDone'] >= 65536)
        with self.assertRaises(ValueError):
            installer.control('unowned-job', 'cancel')
        installer.control(initial['jobId'], 'pause')
        installer.control(initial['jobId'], 'cancel')
        until(lambda: not installer.state()['active'])
        final = installer.state()
        self.assertEqual(final['phase'], 'cancelled')
        self.assertFalse(final['isInstalled'])
        self.assertFalse((game / 'versions/fixture/fixture.jar').exists())
        self.assertEqual(list(game.rglob('*.part')), [])

    def test_late_progress_from_old_owned_worker_cannot_touch_new_job(self):
        module = self.controls()
        old = module.JobControl('old-worker')
        new = module.JobControl('new-worker')
        fixture = self.fixture()
        errors = []
        def run():
            try:
                with module.bind(old):
                    installer._fetch(fixture.url, str(self.dest), len(self.raw), hashlib.sha1(self.raw).hexdigest())
            except Exception as error:
                errors.append(error)
        installer._STATE.update(jobId=old.id)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        until(lambda: fixture.requests)
        installer._STATE.update(jobId=new.id, _detail={})
        thread.join(4)
        self.assertEqual(errors, [])
        self.assertEqual(installer._STATE['_detail'], {}, 'old transfer callback overwrote new progress')


class ModpackJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.raw = b'local mod artifact\0' * 65536
        previous = dict(modpacks._STATE)
        self.addCleanup(modpacks._STATE.update, previous)
        modpacks._STATE.update(job=None, phase='idle')

    def test_cancel_paused_modpack_removes_owned_instance_and_skips_profile_callback(self):
        fixture = LocalTransfer(self.raw)
        self.addCleanup(fixture.close)
        base = Path(self.temp.name)
        archive = base / 'fixture.mrpack'
        with zipfile.ZipFile(archive, 'w') as zipped:
            zipped.writestr('modrinth.index.json', json.dumps({'formatVersion': 1, 'game': 'minecraft',
                'name': 'Disposable pack', 'dependencies': {'minecraft': 'fixture'}, 'files': [{
                'path': 'mods/fixture.jar', 'downloads': [fixture.url], 'fileSize': len(self.raw),
                'hashes': {'sha1': hashlib.sha1(self.raw).hexdigest()}}]}))
        done = []
        with mock.patch.object(modpacks, 'CDN', fixture.url.rsplit('/', 1)[0] + '/'):
            self.assertTrue(modpacks.start_import_file(str(base / 'game'), str(base / 'state'), archive, on_done=done.append))
            initial = modpacks.state()
            self.assertIn('jobId', initial, 'modpack controls must own the selected import')
            until(lambda: fixture.requests)
            modpacks.control(initial['jobId'], 'pause')
            modpacks.control(initial['jobId'], 'cancel')
            until(lambda: not modpacks.busy())
        self.assertEqual(modpacks.state()['phase'], 'cancelled')
        self.assertEqual(done, [], 'cancelled import must never create a profile or launch')
        self.assertEqual(list((base / 'game/instances').iterdir()), [])

    def test_completed_export_has_terminal_owner_and_rejects_controls(self):
        base = Path(self.temp.name)
        game, output = base / 'export-source', base / 'output'
        (game / 'config').mkdir(parents=True)
        output.mkdir()
        (game / 'config/fixture.txt').write_bytes(b'disposable exported configuration')
        completed = threading.Event()
        with mock.patch.object(modpacks, '_resolve_hashes', return_value={}):
            self.assertTrue(modpacks.start_export(str(game), {'name': 'Synthetic export'}, str(output),
                {'minecraft': 'fixture'}, on_done=lambda result: (completed.set(), result)[1]))
            self.assertTrue(completed.wait(3))
            until(lambda: not modpacks.busy())
        snapshot = modpacks.state()
        self.assertEqual(snapshot['phase'], 'done', snapshot)
        self.assertEqual(snapshot['controlStatus'], 'done', 'export worker did not finish its captured controller')
        for capability in ('canPause', 'canResume', 'canCancel'):
            self.assertFalse(snapshot[capability], 'completed export advertised ' + capability)
        with self.assertRaises(ValueError):
            modpacks.control(snapshot['jobId'], 'pause')
        with zipfile.ZipFile(snapshot['result']['path']) as archive:
            self.assertEqual(archive.read('overrides/config/fixture.txt'), b'disposable exported configuration')

    def test_failed_export_has_terminal_owner_and_rejects_controls(self):
        base = Path(self.temp.name)
        game, output = base / 'empty-source', base / 'output'
        game.mkdir()
        output.mkdir()
        self.assertTrue(modpacks.start_export(str(game), {'name': 'Empty synthetic export'}, str(output)))
        until(lambda: not modpacks.busy())
        snapshot = modpacks.state()
        self.assertEqual(snapshot['phase'], 'error', snapshot)
        self.assertEqual(snapshot['controlStatus'], 'error')
        self.assertFalse(snapshot['canCancel'])
        with self.assertRaises(ValueError):
            modpacks.control(snapshot['jobId'], 'cancel')

    def test_pending_modpack_resolution_reports_unavailable_controls(self):
        entered, release = threading.Event(), threading.Event()
        paths = []
        class Metadata(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                paths.append(self.path)
                if self.path == '/v2/project/fixture':
                    entered.set()
                    release.wait(3)
                    body = b'{"title":"Synthetic metadata fixture"}'
                else:
                    body = b'[]'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Metadata)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(release.set)
        base = Path(self.temp.name)
        with mock.patch.object(modpacks, 'API', 'http://127.0.0.1:%d/v2' % server.server_port):
            self.assertTrue(modpacks.start_import(str(base / 'game'), str(base / 'state'), 'fixture'))
            self.assertTrue(entered.wait(2))
            try:
                snapshot = modpacks.state()
                self.assertEqual(snapshot['phase'], 'resolving')
                self.assertFalse(snapshot['canPause'], 'pending synchronous metadata cannot honestly pause')
                self.assertFalse(snapshot['canCancel'], 'pending synchronous metadata cannot honestly cancel')
                self.assertTrue(snapshot['controlReason'])
                for action in ('pause', 'cancel'):
                    with self.assertRaises(ValueError):
                        modpacks.control(snapshot['jobId'], action)
            finally:
                release.set()
                until(lambda: not modpacks.busy())
        self.assertEqual(paths, ['/v2/project/fixture', '/v2/project/fixture/version'])


class ManagedContentControlTests(unittest.TestCase):
    setUp = service_fixtures.InstallationServiceTests.setUp
    save = service_fixtures.InstallationServiceTests.save
    planned = service_fixtures.InstallationServiceTests.planned

    def job(self, ident):
        return self.service.get('private', 'job', {'jobId': ident})['job']

    def test_cancel_after_acquisition_never_commits_the_content_transaction(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('fabric.mod.json', '{"id":"main","version":"1.0.0"}')
            archive.writestr('assets/fixture', b'fixture' * 150000)
        raw = data.getvalue()
        fixture = LocalTransfer(raw)
        self.addCleanup(fixture.close)
        provider = service_fixtures.DownloadProvider({'main': raw})
        def transport(request, timeout):
            response = urllib.request.urlopen(urllib.request.Request(fixture.url, headers=dict(request.header_items())), timeout=timeout)
            response.geturl = lambda: request.full_url
            return response
        provider.transport = transport
        self.service.provider = provider
        plan = self.planned()
        ack = self.service.post('private', 'apply', {'planId': plan['planId']})
        until(lambda: self.job(ack['jobId']).get('bytesDone', 0) >= 65536)
        initial = self.job(ack['jobId'])
        self.assertIn('canCancel', initial, 'content job must expose honest controls')
        self.service.post('private', 'job/control', {'jobId': ack['jobId'], 'action': 'cancel'})
        until(lambda: self.job(ack['jobId'])['status'] == 'cancelled')
        self.assertEqual(self.service.get('private', 'content')['items'], [])
        self.assertFalse((self.private / '.launcher-content/journal.json').exists())

    def test_job_controls_and_status_do_not_wait_for_transaction_coordinator(self):
        entered, release = threading.Event(), threading.Event()
        def worker(job):
            with installation_service.coordinator():
                entered.set()
                release.wait(3)
            return {'fixture': True}
        with installation_service.coordinator():
            ident = self.service._start_job('private', 'Disposable operation', worker)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(2))
        responses, errors = [], []
        def request():
            try:
                responses.append(self.service.post('private', 'job/control', {'jobId': ident, 'action': 'cancel'}))
                responses.append(self.job(ident))
                responses.append(installation_service.busy())
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=request, daemon=True)
        thread.start()
        thread.join(.4)
        was_blocked = thread.is_alive()
        release.set()
        thread.join(3)
        self.assertFalse(was_blocked, 'control route waited behind coordinator held by active job')
        self.assertEqual(errors, [])
        self.assertEqual(len(responses), 3)
        self.assertTrue(responses[-1])
        until(lambda: self.job(ident)['status'] == 'cancelled')


if __name__ == '__main__':
    unittest.main()
