"""Engine regressions using temporary files, mock HTTP and harmless Java doubles."""
import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import installer
import launcher


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hx-engine-")
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        self.state_dir = os.path.join(self.root, "state")
        self.game_dir = os.path.join(self.root, "game with spaces")
        old_install = copy.deepcopy(installer._STATE)
        old_hints = dict(installer.DIR_HINTS)
        old_launch = dict(launcher.STATE)
        old_lines = list(launcher.LINES)
        old_offset = getattr(launcher, "_LINE_OFFSET", 0)
        old_process, old_log = launcher._PROCESS, launcher._LOG_PATH
        old_secrets = getattr(launcher, "_SECRET_VALUES", ())
        self.addCleanup(installer._STATE.update, old_install)
        self.addCleanup(installer.DIR_HINTS.update, old_hints)
        self.addCleanup(launcher.STATE.update, old_launch)
        self.addCleanup(lambda: launcher.LINES.__setitem__(slice(None), old_lines))
        self.addCleanup(setattr, launcher, "_LINE_OFFSET", old_offset)
        self.addCleanup(setattr, launcher, "_PROCESS", old_process)
        self.addCleanup(setattr, launcher, "_LOG_PATH", old_log)
        self.addCleanup(setattr, launcher, "_SECRET_VALUES", old_secrets)
        installer._STATE.update(active=False, phase="idle", todo=[], result=None,
                                _detail={}, error=None, java_error=None)
        installer.DIR_HINTS.update(game_dir=self.game_dir, state=self.state_dir, version_id=None)
        launcher.STATE.update(running=False)
        launcher.LINES.clear()
        launcher._LINE_OFFSET = 0
        launcher._SECRET_VALUES = ()

    def write(self, path, data=b"fixture"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def metadata(self, version):
        ident = version["id"]
        return self.write(os.path.join(self.state_dir, "versions", ident, ident + ".json"),
                          json.dumps(version).encode())

    def managed_runtime(self, component):
        root = os.path.join(self.game_dir, "runtime", component)
        java = self.write(os.path.join(root, "bin", "java.exe"))
        self.write(os.path.join(root, "lib", "jvm.dll"))
        self.write(os.path.join(root, "manifest.json"), json.dumps({"files": {
            "bin/java.exe": {"type": "file", "downloads": {"raw": {"size": 7}}},
            "lib/jvm.dll": {"type": "file", "downloads": {"raw": {"size": 7}}},
        }}).encode())
        return java

    def launch(self, version, **kwargs):
        self.metadata(version)
        self.write(os.path.join(self.game_dir, "versions", version["id"], version["id"] + ".jar"))
        java = self.write(os.path.join(self.root, "fake-java.exe"))
        launches = []

        def spawn(args, **options):
            launches.append((args, options))
            return types.SimpleNamespace(pid=42, stdout=io.BytesIO(), wait=lambda: 0, poll=lambda: None)

        class NoPumpThread:
            def __init__(self, *args, **options):
                pass

            def start(self):
                pass

        with mock.patch.object(launcher.subprocess, "Popen", side_effect=spawn), \
                mock.patch.object(launcher, "threading", types.SimpleNamespace(Thread=NoPumpThread)):
            launcher.start(self.state_dir, self.game_dir, version["id"], java_dir=java,
                           log_path=os.path.join(self.root, "game.log"), **kwargs)
        return launches[0]


class ProcessLifecycleTests(EngineTest):
    def live_process(self, pid=42):
        return types.SimpleNamespace(pid=pid, poll=lambda: None,
                                     wait=lambda timeout=None: (_ for _ in ()).throw(subprocess.TimeoutExpired('fixture', timeout)),
                                     terminate=lambda: None)

    def test_failed_windows_stop_keeps_the_game_running_and_reports_the_error(self):
        process = self.live_process()
        launcher._PROCESS = process
        launcher.STATE.update(running=True, pid=process.pid, error=None)
        for failure in (subprocess.CompletedProcess([], 1), OSError('fixture unavailable'),
                        subprocess.TimeoutExpired('taskkill', 5)):
            with self.subTest(failure=type(failure).__name__):
                options = {'side_effect': failure} if isinstance(failure, BaseException) else {'return_value': failure}
                with mock.patch.object(launcher.sys, 'platform', 'win32'), \
                        mock.patch.object(launcher.subprocess, 'run', **options):
                    data = launcher.stop()
                self.assertTrue(data['running'])
                self.assertEqual(data['pid'], 42)
                self.assertIsInstance(data.get('error'), str)
                self.assertNotIn('[harness] game terminated by the launcher', launcher.LINES)

    def test_stop_waits_for_confirmed_exit_before_releasing_the_game_lock(self):
        process = self.live_process()
        launcher._PROCESS = process
        launcher.STATE.update(running=True, pid=42, error=None)
        with mock.patch.object(launcher.sys, 'platform', 'win32'), \
                mock.patch.object(launcher.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
            data = launcher.stop()
        self.assertTrue(data['running'])
        self.assertIsInstance(data.get('error'), str)

    def test_failed_unix_termination_keeps_the_game_lock(self):
        process = self.live_process()
        process.terminate = mock.Mock(side_effect=PermissionError('fixture denied'))
        launcher._PROCESS = process
        launcher.STATE.update(running=True, pid=42, error=None)
        with mock.patch.object(launcher.sys, 'platform', 'linux'):
            data = launcher.stop()
        self.assertTrue(data['running'])
        self.assertIsInstance(data.get('error'), str)

    def test_stop_during_launch_preparation_cannot_claim_to_cancel_the_future_spawn(self):
        launcher._PROCESS = None
        launcher.STATE.update(running=True, pid=None, error=None)
        data = launcher.stop()
        self.assertTrue(data['running'])
        self.assertIsInstance(data.get('error'), str)

    def test_successful_spawn_clears_the_temporary_stop_during_startup_error(self):
        stop_results = []
        def prepare(*args, **kwargs):
            stop_results.append(launcher.stop())
            return self.root
        with mock.patch.object(launcher, '_extract_natives', side_effect=prepare):
            self.launch({'id': 'stop-during-prepare'})
        self.assertEqual(len(stop_results), 1)
        self.assertTrue(stop_results[0]['running'])
        self.assertIsInstance(stop_results[0].get('error'), str)
        result = launcher.status()
        self.assertTrue(result['running'])
        self.assertEqual(result['pid'], 42)
        self.assertIsNone(result['error'])

    def test_successful_stop_releases_only_the_confirmed_process(self):
        exited = [False]
        process = types.SimpleNamespace(pid=42, poll=lambda: 0 if exited[0] else None,
                                        wait=lambda timeout=None: 0)
        launcher._PROCESS = process
        launcher.STATE.update(running=True, pid=42, error=None)
        def terminate():
            exited[0] = True
        process.terminate = terminate
        with mock.patch.object(launcher.sys, 'platform', 'linux'):
            data = launcher.stop()
        self.assertFalse(data['running'])
        self.assertIsNone(data['error'])
        self.assertEqual(data['exitCode'], 0)

    def test_old_log_pump_cannot_mutate_a_newer_process_or_its_log(self):
        current = self.live_process(pid=222)
        old = types.SimpleNamespace(pid=111, stdout=io.BytesIO(b'Old process output\n'), wait=lambda: 1)
        path = self.write(os.path.join(self.root, 'current.log'), b'Current process log\n')
        launcher._PROCESS = current
        launcher.STATE.update(running=True, pid=222, exitCode=None, error=None)
        launcher._pump(old, log_path=path, secrets=())
        self.assertTrue(launcher.STATE['running'])
        self.assertEqual(launcher.STATE['pid'], 222)
        self.assertIsNone(launcher.STATE['exitCode'])
        self.assertEqual(launcher.LINES, [])
        with open(path, 'rb') as output:
            self.assertEqual(output.read(), b'Current process log\n')

    def test_owned_log_pump_reports_exit_and_keeps_redacted_output(self):
        process = types.SimpleNamespace(pid=42, stdout=io.BytesIO(b'token=synthetic-secret\n'), wait=lambda: 2)
        launcher._PROCESS = process
        launcher.STATE.update(running=True, pid=42, exitCode=None, error=None)
        launcher._pump(process, log_path=None, secrets=('synthetic-secret',))
        self.assertFalse(launcher.STATE['running'])
        self.assertEqual(launcher.STATE['exitCode'], 2)
        self.assertIn('token=[REDACTED]', launcher.LINES)

    def test_detached_pump_cannot_append_to_a_replaced_log_after_output_capture(self):
        current = self.live_process(pid=222)
        old = types.SimpleNamespace(pid=111, stdout=io.BytesIO(b'Old process output\n'), wait=lambda: 1)
        path = self.write(os.path.join(self.root, 'current.log'), b'Current process log\n')
        launcher._PROCESS = old
        launcher.STATE.update(running=True, pid=111, exitCode=None, error=None)
        real_append = launcher._append
        def replace_after_capture(line, secrets=None, owner=None):
            text = real_append(line, secrets, owner=owner)
            launcher._PROCESS = current
            launcher.STATE.update(running=True, pid=222, exitCode=None)
            launcher.LINES.clear()
            return text
        with mock.patch.object(launcher, '_append', side_effect=replace_after_capture):
            launcher._pump(old, log_path=path, secrets=())
        self.assertTrue(launcher.STATE['running'])
        self.assertEqual(launcher.STATE['pid'], 222)
        self.assertEqual(launcher.LINES, [])
        with open(path, 'rb') as output:
            self.assertEqual(output.read(), b'Current process log\n')


class RepeatedRequestTests(unittest.TestCase):
    def test_busy_install_and_launch_return_without_locking_themselves(self):
        # A child confines the old deadlocked daemon and proves a second request returns.
        for module, busy, call in (
            ("installer", "installer._STATE['active'] = True", "installer.start('.', '.', 'unused.json')"),
            ("launcher", "launcher.STATE['running'] = True", "launcher.start('.', '.')"),
        ):
            with self.subTest(module=module):
                code = textwrap.dedent("""
                    import sys, threading
                    sys.path.insert(0, sys.argv[1])
                    import {module}
                    {busy}
                    returned = threading.Event()
                    def second():
                        {call}
                        returned.set()
                    threading.Thread(target=second, daemon=True).start()
                    print(returned.wait(0.5))
                """).format(module=module, busy=busy, call=call)
                result = subprocess.run([sys.executable, "-c", code, os.path.dirname(__file__)],
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "True", module + " repeat request deadlocked")


class LoaderMavenTests(EngineTest):
    def test_official_fabric_and_quilt_entries_download_and_reach_classpath(self):
        for name, repo, rel in (
            ("net.fabricmc:fabric-loader:0.16.10", "https://maven.fabricmc.net/",
             "net/fabricmc/fabric-loader/0.16.10/fabric-loader-0.16.10.jar"),
            ("org.quiltmc:quilt-loader:0.28.1", "https://maven.quiltmc.org/repository/release/",
             "org/quiltmc/quilt-loader/0.28.1/quilt-loader-0.28.1.jar"),
        ):
            with self.subTest(name=name):
                version = {"id": "loader-test", "libraries": [{"name": name, "url": repo}]}
                plan = installer._framework(self.game_dir, version)
                self.assertEqual(len(plan), 1, "Maven loader library was skipped")
                target = os.path.join(self.game_dir, "libraries", *rel.split("/"))
                self.assertEqual(plan[0]["url"], repo + rel)
                self.assertEqual(plan[0]["dest"], target)
                self.assertIn(target, launcher._classpath(version, self.game_dir))

    def test_classifier_only_mojang_library_does_not_invent_main_artifact(self):
        version = {"id": "legacy-native-test", "libraries": [{
            "name": "org.lwjgl.lwjgl:lwjgl-platform:2.9.4-nightly-20150209",
            "natives": {"windows": "natives-windows"},
            "downloads": {"classifiers": {"natives-windows": {
                "path": "org/lwjgl/lwjgl/lwjgl-platform/2.9.4/lwjgl-platform-2.9.4-natives-windows.jar",
                "url": "https://example.invalid/natives.jar", "size": 100}}}}]}
        plan = installer._framework(self.game_dir, version)
        self.assertEqual([entry["url"] for entry in plan], ["https://example.invalid/natives.jar"])
        self.assertEqual(launcher._classpath(version, self.game_dir), [])


class LaunchArgumentTests(EngineTest):
    def test_login_token_reaches_java_but_diagnostics_and_saved_console_redact_it(self):
        token = "synthetic-engine-token-12345"
        version = {"id": "token-diagnostics", "arguments": {
            "jvm": ["-Dfixture.token=${auth_access_token}", "-Dfixture.session=${auth_session}"],
            "game": ["--accessToken", "${auth_access_token}"]}}
        args, _ = self.launch(version, player={"name": "SyntheticOnline", "uuid": "d" * 32,
                                               "access_token": token, "user_type": "msa"})
        self.assertIn("-Dfixture.token=" + token, args)
        self.assertIn("-Dfixture.session=" + token, args)
        self.assertEqual(args[args.index("--accessToken") + 1], token)
        self.assertNotIn(token, launcher.status()["command"])
        self.assertIn("[REDACTED]", launcher.status()["command"])
        launcher._append("Bearer " + token + "; auth_session=" + token)
        self.assertEqual(launcher.log_since(0)["lines"][-1],
                         "Bearer [REDACTED]; auth_session=[REDACTED]")
        proc = types.SimpleNamespace(stdout=io.BytesIO(("client token=" + token + "\n").encode()), wait=lambda: 0)
        launcher._PROCESS = proc
        launcher._pump(proc)
        self.assertNotIn(token, json.dumps(launcher.status()))
        with open(os.path.join(self.root, "game.log"), encoding="utf-8") as fh:
            saved = fh.read()
        self.assertNotIn(token, saved)
        self.assertIn("client token=[REDACTED]", saved)

    def test_failed_spawn_does_not_return_login_token_in_error(self):
        token = "synthetic-spawn-token-67890"
        version = {"id": "token-error", "arguments": {"jvm": ["-Dsession=${auth_session}"]}}
        self.metadata(version)
        self.write(os.path.join(self.game_dir, "versions", "token-error", "token-error.jar"))
        java = self.write(os.path.join(self.root, "fake-java.exe"))
        with mock.patch.object(launcher.subprocess, "Popen", side_effect=OSError("failed for " + token)):
            with self.assertRaisesRegex(RuntimeError, r"failed for \[REDACTED\]"):
                launcher.start(self.state_dir, self.game_dir, "token-error", java_dir=java,
                               log_path=os.path.join(self.root, "game.log"),
                               player={"name": "SyntheticOnline", "uuid": "e" * 32, "access_token": token})
        self.assertNotIn(token, launcher.status()["error"])

    def test_legacy_placeholder_paths_and_quoted_values_are_single_arguments(self):
        version = {"id": "legacy-test", "minecraftArguments":
                   '--username ${auth_player_name} --gameDir ${game_directory} --label "two words"'}
        args, _ = self.launch(version, player={"name": "Offline Player", "uuid": "a" * 32,
                                               "user_type": "legacy"})
        self.assertEqual(args[args.index("--gameDir") + 1], self.game_dir)
        self.assertEqual(args[args.index("--username") + 1], "Offline Player")
        self.assertEqual(args[args.index("--label") + 1], "two words")

    def test_modern_classpath_uses_environment_without_duplicate_argv(self):
        version = {"id": "modern-test", "arguments": {
            "jvm": ["-cp", "${classpath}", "-Dfixture=yes"],
            "game": ["--gameDir", "${game_directory}"]},
            "libraries": [{"downloads": {"artifact": {
                "path": "deep/" + "x" * 180 + "/library.jar", "url": "https://example.invalid/lib"}}}]}
        args, options = self.launch(version)
        self.assertNotIn("-cp", args)
        self.assertNotIn(options["env"]["CLASSPATH"], args)
        self.assertIn("library.jar", options["env"]["CLASSPATH"])
        self.assertIn("-Dfixture=yes", args)

    def test_explicit_offline_identity_keeps_legacy_session_type(self):
        version = {"id": "identity-test", "arguments": {"game": [
            "--username", "${auth_player_name}", "--uuid", "${auth_uuid}",
            "--userType", "${user_type}", "--accessToken", "${auth_access_token}"]}}
        args, _ = self.launch(version, player={"name": "ChosenOffline", "uuid": "b" * 32,
                                               "user_type": "legacy"})
        self.assertEqual(args[args.index("--userType") + 1], "legacy")
        self.assertEqual(args[args.index("--username") + 1], "ChosenOffline")
        self.assertEqual(args[args.index("--accessToken") + 1], "0")

    def test_standalone_offline_lookup_uses_configured_data_directory(self):
        account_path = os.path.join(self.root, "data", "offline_accounts.json")
        self.write(account_path, json.dumps({"active": "c" * 32, "accounts": [
            {"id": "c" * 32, "name": "DataDirectoryPlayer"}]}).encode())
        with mock.patch.dict(os.environ, {"LAUNCHER_DATA_DIR": os.path.dirname(account_path)}):
            self.assertEqual(launcher.identity(self.state_dir), ("DataDirectoryPlayer", "c" * 32))


class DownloadProgressTests(EngineTest):
    def test_completed_download_is_counted_once_in_its_phase(self):
        data = b"x" * 100
        item = {"phase": "client", "url": "https://example.invalid/client", "size": 100,
                "sha1": hashlib.sha1(data).hexdigest(), "dest": os.path.join(self.game_dir, "client.jar")}
        installer._STATE["todo"] = [item]
        with mock.patch.object(installer.urllib.request, "urlopen", return_value=io.BytesIO(data)):
            installer._download_one(item)
        installer._STATE["result"] = {"client": 100}
        snapshot = installer.state()
        self.assertEqual(snapshot["bytesDone"], 100)
        self.assertEqual(snapshot["bytesTotal"], 100)
        self.assertEqual(snapshot["phases"], {"client": {"done": 100, "total": 100}})

    def test_inflight_download_bytes_are_assigned_to_the_declared_phase(self):
        data = b"x" * 100
        item = {"phase": "libraries", "url": "https://example.invalid/lib", "size": 100,
                "dest": os.path.join(self.game_dir, "lib.jar")}
        installer._STATE["todo"] = [item]
        snapshots = []

        class PartialResponse(io.BytesIO):
            def read(self, amount=-1):
                if self.tell():
                    snapshots.append(installer.state())
                return super().read(40 if not self.tell() else amount)

        with mock.patch.object(installer.urllib.request, "urlopen", return_value=PartialResponse(data)):
            installer._fetch(item["url"], item["dest"], 100, None)
        self.assertEqual(snapshots[0]["phases"], {"libraries": {"done": 40, "total": 100}})

    def test_new_install_resets_previous_progress_before_worker_runs(self):
        installer._STATE.update(result={"assets": 100}, _detail={"old": 100},
                                todo=[{"phase": "assets", "size": 100}], java_error="old error")
        with mock.patch.object(installer.threading.Thread, "start"):
            snapshot = installer.start(self.game_dir, self.state_dir,
                                       os.path.join(self.state_dir, "versions", "new", "new.json"))
        self.assertEqual(snapshot["bytesDone"], 0)
        self.assertEqual(snapshot["bytesTotal"], 0)
        self.assertIsNone(snapshot["java_error"])

    def test_size_mismatch_is_rejected_when_maven_has_no_checksum(self):
        dest = os.path.join(self.game_dir, "truncated.jar")
        with mock.patch.object(installer.urllib.request, "urlopen", return_value=io.BytesIO(b"short")):
            with self.assertRaisesRegex(RuntimeError, "size"):
                installer._fetch("https://example.invalid/lib", dest, 100, None)
        self.assertFalse(os.path.exists(dest))


class ReadinessTests(EngineTest):
    def prepare_version(self, ident="older-test"):
        version = {"id": ident, "javaVersion": {"component": "runtime-test", "majorVersion": 21},
                   "libraries": [{"downloads": {"artifact": {"path": "fixture/library.jar",
                                    "url": "https://example.invalid/lib", "size": 7}}}]}
        self.metadata(version)
        self.write(os.path.join(self.state_dir, "versions", "version_manifest_v2.json"),
                   json.dumps({"latest": {"release": "latest-test"}}).encode())
        self.write(os.path.join(self.game_dir, "versions", ident, ident + ".jar"))
        return version

    def test_jar_alone_is_not_ready_without_libraries_and_declared_runtime(self):
        self.prepare_version("latest-test")
        with mock.patch.object(installer.shutil, "which", return_value=None):
            self.assertFalse(installer.state()["isInstalled"])

    def test_complete_older_installation_is_ready_even_when_latest_is_absent(self):
        self.prepare_version()
        self.write(os.path.join(self.game_dir, "libraries", "fixture", "library.jar"))
        self.managed_runtime("runtime-test")
        self.assertTrue(installer.state()["isInstalled"])

    def test_matching_system_java_is_usable_for_bootstrap_install_readiness_and_launch(self):
        version = self.prepare_version()
        self.write(os.path.join(self.game_dir, "libraries", "fixture", "library.jar"))
        java = self.write(os.path.join(self.root, "system-java.exe"))
        with mock.patch.object(installer.shutil, "which", return_value=java), \
                mock.patch.object(installer.subprocess, "run", return_value=types.SimpleNamespace(
                    returncode=0, stdout=b'openjdk version "21.0.1"')), \
                mock.patch.object(installer.urllib.request, "urlopen", side_effect=AssertionError("unexpected download")):
            self.assertEqual(installer.ensure_runtime(self.game_dir, self.state_dir, version), java)
            path = os.path.join(self.state_dir, "versions", "older-test", "older-test.json")
            installer._run(self.game_dir, path, None)
            self.assertEqual(installer.state()["phase"], "done", installer.state()["error"])
            self.assertTrue(installer.is_installed(self.game_dir, self.state_dir, "older-test"))
            self.assertEqual(launcher._find_java(self.game_dir, version, []), java)

    def test_pre_java_version_metadata_launches_with_managed_legacy_runtime(self):
        version = {"id": "legacy-eight", "libraries": []}
        self.metadata(version)
        self.write(os.path.join(self.game_dir, "versions", "legacy-eight", "legacy-eight.jar"))
        java = self.managed_runtime("jre-legacy")
        with mock.patch.object(installer.shutil, "which", return_value=None):
            self.assertTrue(installer.is_installed(self.game_dir, self.state_dir, "legacy-eight"))
            self.assertEqual(launcher._find_java(self.game_dir, version, []), java)


class RuntimeBootstrapTests(EngineTest):
    def test_managed_java_alone_cannot_hide_missing_runtime_manifest_and_support(self):
        self.write(os.path.join(self.game_dir, "runtime", "runtime-test", "bin", "java.exe"))
        self.assertIsNone(installer._runtime_java(self.game_dir, "runtime-test"))

    def test_fresh_bootstrap_installs_declared_java_and_support_files(self):
        version = {"id": "runtime-version", "javaVersion": {"component": "runtime-test", "majorVersion": 21}}
        java_data, support_data = b"harmless java stand-in", b"runtime support"
        runtime = {"files": {
            "bin/java.exe": {"type": "file", "downloads": {"raw": {
                "url": "https://example.invalid/java", "size": len(java_data),
                "sha1": hashlib.sha1(java_data).hexdigest()}}},
            "lib/jvm.dll": {"type": "file", "downloads": {"raw": {
                "url": "https://example.invalid/jvm", "size": len(support_data),
                "sha1": hashlib.sha1(support_data).hexdigest()}}},
        }}
        runtime_data = json.dumps(runtime).encode()
        manifest = {"manifest": {"windows-x64": {"runtime-test": [{"manifest": {
            "url": "https://example.invalid/runtime.json", "size": len(runtime_data),
            "sha1": hashlib.sha1(runtime_data).hexdigest()}}]}}}
        self.write(os.path.join(self.state_dir, "versions", "jre_manifest.json"), json.dumps(manifest).encode())
        responses = {"https://example.invalid/runtime.json": runtime_data,
                     "https://example.invalid/java": java_data, "https://example.invalid/jvm": support_data}
        bootstrap = getattr(installer, "ensure_runtime", None)
        self.assertTrue(callable(bootstrap), "fresh Forge setup needs a runtime bootstrap API")
        with mock.patch.object(installer.urllib.request, "urlopen",
                               side_effect=lambda request, **kwargs: io.BytesIO(responses[request.full_url])):
            java = bootstrap(self.game_dir, self.state_dir, version)
        self.assertEqual(java, os.path.join(self.game_dir, "runtime", "runtime-test", "bin", "java.exe"))
        with open(java, "rb") as fh:
            self.assertEqual(fh.read(), java_data)
        with open(os.path.join(self.game_dir, "runtime", "runtime-test", "lib", "jvm.dll"), "rb") as fh:
            self.assertEqual(fh.read(), support_data)
        self.assertFalse(os.path.isdir(os.path.join(self.game_dir, "versions")), "bootstrap downloads Java only")

    def test_missing_required_runtime_metadata_fails_the_install(self):
        with self.assertRaisesRegex(RuntimeError, "runtime"):
            installer._java_plan(self.game_dir, {}, "runtime-test")

    def test_runtime_manifest_cannot_write_outside_its_component(self):
        runtime_data = json.dumps({"files": {"bin/../../escaped.exe": {
            "type": "file", "downloads": {"raw": {"url": "https://example.invalid/escape"}}}}}).encode()
        manifest = {"manifest": {"windows-x64": {"runtime-test": [
            {"manifest": {"url": "https://example.invalid/runtime.json"}}]}}}
        with mock.patch.object(installer.urllib.request, "urlopen", return_value=io.BytesIO(runtime_data)):
            with self.assertRaisesRegex(RuntimeError, "path"):
                installer._java_plan(self.game_dir, manifest, "runtime-test")

    def test_empty_declared_support_file_does_not_make_runtime_incomplete(self):
        root = os.path.join(self.game_dir, "runtime", "runtime-test")
        java = self.write(os.path.join(root, "bin", "java.exe"))
        self.write(os.path.join(root, "legal", "empty.txt"), b"")
        self.write(os.path.join(root, "manifest.json"), json.dumps({"files": {
            "bin/java.exe": {"type": "file", "downloads": {"raw": {"size": 7}}},
            "legal/empty.txt": {"type": "file", "downloads": {"raw": {"size": 0}}},
        }}).encode())
        self.assertEqual(installer._runtime_java(self.game_dir, "runtime-test"), java)

    def test_unsafe_runtime_manifest_error_cannot_fall_back_to_system_java(self):
        version = {"id": "runtime-version", "javaVersion": {"component": "runtime-test", "majorVersion": 21}}
        runtime_data = json.dumps({"files": {"../escape": {"type": "file"}}}).encode()
        manifest = {"manifest": {"windows-x64": {"runtime-test": [
            {"manifest": {"url": "https://example.invalid/runtime.json"}}]}}}
        self.write(os.path.join(self.state_dir, "versions", "jre_manifest.json"), json.dumps(manifest).encode())
        java = self.write(os.path.join(self.root, "system-java.exe"))
        with mock.patch.object(installer.urllib.request, "urlopen", return_value=io.BytesIO(runtime_data)), \
                mock.patch.object(installer.shutil, "which", return_value=java), \
                mock.patch.object(installer.subprocess, "run", return_value=types.SimpleNamespace(
                    returncode=0, stdout=b'openjdk version "21.0.1"')):
            with self.assertRaisesRegex(RuntimeError, "path"):
                installer.ensure_runtime(self.game_dir, self.state_dir, version)


class ConsoleCursorTests(EngineTest):
    def test_absolute_console_cursor_advances_after_ring_buffer_eviction(self):
        for i in range(4000):
            launcher._append("line " + str(i))
        first = launcher.log_since(0)
        for i in range(4000, 4010):
            launcher._append("line " + str(i))
        second = launcher.log_since(first["next"])
        self.assertEqual(second["lines"], ["line " + str(i) for i in range(4000, 4010)])
        self.assertEqual(second["next"], 4010)
        self.assertEqual(second["total"], 4010)
        stale = launcher.log_since(1)
        self.assertEqual(stale["lines"][0], "line 10")
        self.assertEqual(stale["next"], 4010)


if __name__ == "__main__":
    unittest.main()
