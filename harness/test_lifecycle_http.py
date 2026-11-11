"""Host shutdown admission uses bounded, disposable HTTP/process fixtures."""
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
with tempfile.TemporaryDirectory() as bootstrap:
    with mock.patch.dict(os.environ, {"LAUNCHER_DATA_DIR": bootstrap}):
        import server


class LifecycleHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.running = self.installing = self.content = self.pack = False
        self.actual_launcher_status = server.launcher.status
        for target, name, callback in (
            (server.launcher, 'status', lambda: {'running': self.running, 'pid': 42 if self.running else None}),
            (server.installer, 'state', lambda: {'active': self.installing}),
            (server.installation_service, 'busy', lambda: self.content),
            (server.modpacks, 'busy', lambda: self.pack),
            (server, '_read_json', lambda *args: {'keepLauncherOpen': False}),
            (server, '_load_overlay', lambda: {}),
        ):
            patch = mock.patch.object(target, name, side_effect=callback)
            patch.start()
            self.addCleanup(patch.stop)
        server._HOST_SHUTDOWN = False
        server._HOST_WORK = 0
        self.host = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.host.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.host.shutdown()
        self.host.server_close()
        self.thread.join(2)
        server._HOST_SHUTDOWN = False
        server._HOST_WORK = 0

    def request(self, path, body=None):
        conn = http.client.HTTPConnection(*self.host.server_address, timeout=2)
        try:
            conn.request('POST' if body is not None else 'GET', path,
                         json.dumps(body) if body is not None else None)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_status_uses_effective_setting_and_tracks_paused_content(self):
        self.assertEqual(self.request('/__lifecycle'), (200, {
            'gameRunning': False, 'activeWork': False, 'keepLauncherOpen': False}))
        self.content = True
        self.assertTrue(self.request('/__lifecycle')[1]['activeWork'])
        self.assertFalse(self.request('/__lifecycle/prepare-shutdown', {})[1]['safeToStop'])

    def test_native_configuration_settings_take_precedence(self):
        with mock.patch.object(server, '_load_overlay', return_value={
                '__launcher_settings': {'keepLauncherOpen': True},
                '__configuration_settings': {'keepLauncherOpen': False, 'crashAssistance': False}}):
            self.assertFalse(self.request('/__lifecycle')[1]['keepLauncherOpen'])
            self.assertFalse(server._launcher_preferences()['crashAssistance'])

    def preparation_failure_snapshots(self):
        """Exercise real launch admission/preparation without any game spawn."""
        entered, release = threading.Event(), threading.Event()
        response = []
        state = dict(server.launcher.STATE, running=False, pid=None, error=None)

        def fail_preparation(*args, **kwargs):
            entered.set()
            release.wait(3)
            raise RuntimeError('Synthetic Java preparation failure')

        with mock.patch.multiple(server.launcher, STATE=state, _PROCESS=None, _SECRET_VALUES=()), \
                mock.patch.object(server.launcher, 'status', self.actual_launcher_status), \
                mock.patch.object(server.launcher, '_resolve_version', side_effect=fail_preparation), \
                mock.patch.multiple(server, STATE=self.tmp.name, GAME_DIR=self.tmp.name, GAME_LOG=str(Path(self.tmp.name) / 'game.log')), \
                mock.patch.object(server, '_configuration', return_value=None), \
                mock.patch.object(server, '_resolve_alias', return_value='synthetic-version'), \
                mock.patch.object(server, '_quickplay_context', return_value={}), \
                mock.patch.object(server, '_selected_game_identity', return_value=None):
            idle = self.request('/__lifecycle')[1]
            worker = threading.Thread(target=lambda: response.append(self.request('/__launch', {})))
            worker.start()
            try:
                self.assertTrue(entered.wait(1), 'Launch never entered held preparation')
                preparing = self.request('/__lifecycle')[1]
                pid = server.launcher.status()['pid']
                self.assertFalse(self.request('/__lifecycle/prepare-shutdown', {})[1]['safeToStop'])
            finally:
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive(), 'Synthetic launch did not finish')
            failed = self.request('/__lifecycle')[1]
        return {'idle': idle, 'preparing': preparing, 'failed': failed, 'pid': pid, 'response': response[0]}

    def test_held_preparation_is_active_work_then_failed_launch_returns_idle(self):
        snapshots = self.preparation_failure_snapshots()
        self.assertIsNone(snapshots['pid'])
        self.assertEqual(snapshots['preparing'], {
            'gameRunning': False, 'activeWork': True, 'keepLauncherOpen': False})
        self.assertEqual(snapshots['failed'], {
            'gameRunning': False, 'activeWork': False, 'keepLauncherOpen': False})
        self.assertEqual(snapshots['response'], (409, {'error': 'Synthetic Java preparation failure'}))

    def test_running_without_a_positive_integer_pid_remains_busy_work(self):
        for pid in (None, 0, -1, True, '42', 1.0):
            with self.subTest(pid=pid), mock.patch.object(server.launcher, 'status', return_value={'running': True, 'pid': pid}):
                self.assertEqual(self.request('/__lifecycle')[1], {
                    'gameRunning': False, 'activeWork': True, 'keepLauncherOpen': False})
                self.assertFalse(self.request('/__lifecycle/prepare-shutdown', {})[1]['safeToStop'])
        self.running = True
        self.assertEqual(self.request('/__lifecycle')[1], {
            'gameRunning': True, 'activeWork': False, 'keepLauncherOpen': False})
        self.assertFalse(self.request('/__lifecycle/prepare-shutdown', {})[1]['safeToStop'])
        with mock.patch.object(server.launcher, 'status', return_value={'running': False, 'pid': 42}):
            self.assertEqual(self.request('/__lifecycle')[1], {
                'gameRunning': False, 'activeWork': False, 'keepLauncherOpen': False})

    def test_idle_admission_latches_and_resume_reopens_work(self):
        self.assertTrue(self.request('/__lifecycle/prepare-shutdown', {})[1]['safeToStop'])
        with mock.patch.object(server.Handler, '_do_POST', return_value=None) as mutation:
            code, data = self.request('/__install', {})
            self.assertEqual((code, data['code']), (409, 'launcher_shutting_down'))
            mutation.assert_not_called()
        self.assertEqual(self.request('/__lifecycle/resume', {}), (200, {'ok': True}))
        with mock.patch.object(server.installer, 'control', return_value={'jobId': 'x', 'paused': True}) as control:
            self.assertTrue(self.request('/__install_control', {'jobId': 'x', 'action': 'pause'})[1]['ok'])
            control.assert_called_once_with('x', 'pause')

    def test_active_admission_prevents_shutdown_race(self):
        entered, release = threading.Event(), threading.Event()
        original = server.Handler._do_POST
        def delayed(handler):
            if handler.path == '/__install':
                entered.set()
                release.wait(2)
                return handler._send_json({'started': True})
            return original(handler)
        with mock.patch.object(server.Handler, '_do_POST', delayed):
            request = threading.Thread(target=lambda: self.request('/__install', {}))
            request.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertFalse(self.request('/__lifecycle/prepare-shutdown', {})[1]['safeToStop'])
                self.assertTrue(self.request('/__lifecycle')[1]['activeWork'])
            finally:
                release.set()
                request.join(2)
        self.assertTrue(self.request('/__lifecycle/prepare-shutdown', {})[1]['safeToStop'])

    def test_malformed_or_failed_worker_status_is_conservatively_busy(self):
        for value in ({}, {'running': None}):
            with mock.patch.object(server.launcher, 'status', return_value=value):
                self.assertFalse(self.request('/__lifecycle/prepare-shutdown', {})[1]['safeToStop'])
        with mock.patch.object(server.installer, 'state', side_effect=OSError('unavailable')):
            self.assertTrue(self.request('/__lifecycle')[1]['activeWork'])

    def test_controls_bypass_transaction_coordinator(self):
        held, release = threading.Event(), threading.Event()
        def transaction():
            with server.installation_service.coordinator():
                held.set()
                release.wait(3)
        worker = threading.Thread(target=transaction)
        worker.start()
        self.assertTrue(held.wait(1))
        try:
            with mock.patch.object(server.modpacks, 'control', return_value={'jobId': 'p', 'paused': False}) as control:
                self.assertTrue(self.request('/__modpacks/control', {'jobId': 'p', 'action': 'resume'})[1]['ok'])
                control.assert_called_once_with('p', 'resume')
            with mock.patch.object(server, '_installation_service') as factory:
                factory.return_value.post.return_value = {'ok': True}
                self.assertEqual(self.request('/__instances/one/job/control', {'jobId': 'j', 'action': 'resume'}), (200, {'ok': True}))
                factory.return_value.post.assert_called_once_with('one', 'job/control', {'jobId': 'j', 'action': 'resume'})
        finally:
            release.set()
            worker.join(3)


if __name__ == '__main__':
    unittest.main()
