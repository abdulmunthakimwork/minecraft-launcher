"""Synthetic Minecraft log shapes; no selected-user logs or external services."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from crash_diagnostics import analyze
except ImportError:
    def analyze(*args, **kwargs):
        raise AssertionError('Local crash explanations have not been implemented')

try:
    from instance_tools import read_logs
except ImportError:
    def read_logs(*args, **kwargs):
        raise AssertionError('Selected-instance log snapshots have not been implemented')


FABRIC_DEPENDENCY = """[12:01:02] [main/ERROR]: Incompatible mods found!
net.fabricmc.loader.impl.FormattedException: Some of your mods are incompatible with the game or each other!
A potential solution has been determined, this may resolve your problem:
 - Install fabric-api, version 0.92.0 or later.
More details:
 - Mod 'Example' (example) 1.0 requires version 0.92.0 or later of fabric-api, which is missing!
"""
FORGE_DEPENDENCY = """[12:01:02] [main/ERROR] [net.minecraftforge.fml.loading.ModSorter/LOADING]: Missing or unsupported mandatory dependencies:
	Mod ID: 'architectury', Requested by: 'example', Expected range: '[9.0,)', Actual version: '[MISSING]'
"""
JAVA_MISMATCH = """Exception in thread "main" java.lang.UnsupportedClassVersionError: net/example/Main has been compiled by a more recent version of the Java Runtime (class file version 65.0), this version of the Java Runtime only recognizes class file versions up to 61.0
"""
HEAP_FAILURE = 'Caused by: java.lang.OutOfMemoryError: Java heap space\n'
GRAPHICS_FAILURE = '[12:01:02] [Render thread/ERROR]: GLFW error 65542: WGL: The driver does not appear to support OpenGL\n'
MIXIN_FAILURE = """[12:01:02] [main/FATAL]: Mixin apply failed example.mixins.json:ExampleMixin -> net.minecraft.client.Minecraft:
org.spongepowered.asm.mixin.transformer.throwables.MixinApplyError: Mixin [example.mixins.json:ExampleMixin] FAILED during APPLY
Caused by: org.spongepowered.asm.mixin.injection.throwables.InjectionError: Critical injection failure: Callback method could not find any targets
"""


class CrashDiagnosticsTests(unittest.TestCase):
    def finding(self, text, ident, context=None):
        result = analyze(text, context)
        match = next((f for f in result['findings'] if f['id'] == ident), None)
        self.assertIsNotNone(match, result)
        self.assertTrue(match['explanation'])
        self.assertTrue(match['suggestions'])
        self.assertTrue(match['evidence'])
        for evidence in match['evidence']:
            line = text.splitlines()[evidence['line'] - 1]
            self.assertTrue(line.startswith(evidence['text']))
        json.dumps(result, allow_nan=False)
        return result, match

    def test_fabric_missing_dependency_keeps_requirement_evidence(self):
        # Dropping structured loader-detail lines must lose this actionable evidence.
        result, finding = self.finding(FABRIC_DEPENDENCY, 'dependency')
        self.assertEqual(finding['confidence'], 'reported')
        self.assertTrue(any('fabric-api, which is missing' in e['text'] for e in finding['evidence']))
        self.assertEqual(result['severity'], 'error')

    def test_forge_missing_dependency_keeps_actual_version_evidence(self):
        _, finding = self.finding(FORGE_DEPENDENCY, 'dependency')
        self.assertTrue(any("Actual version: '[MISSING]'" in e['text'] for e in finding['evidence']))

    def test_incompatible_dependency_distinguishes_installed_version(self):
        text = "[main/ERROR]: Incompatible mods found!\n - Mod 'Example' (example) 1.0 requires version 2.0 or later of mod 'Helper' (helper), but only the wrong version is present: 1.0!\n"
        _, finding = self.finding(text, 'dependency')
        self.assertTrue(any('wrong version is present: 1.0' in e['text'] for e in finding['evidence']))

    def test_duplicate_loader_failure_reports_duplicate_mods(self):
        self.finding("[main/ERROR]: Duplicate versions for mod ID 'example': [example-1.jar, example-2.jar]\n", 'duplicate-mods')
        self.finding('net.minecraftforge.fml.loading.moddiscovery.ModFileLoadingException: Found duplicate mods: example\n', 'duplicate-mods')

    def test_class_version_mismatch_explains_required_java_from_real_numbers(self):
        _, finding = self.finding(JAVA_MISMATCH, 'java-version')
        self.assertIn('Java 21', finding['explanation'])
        self.assertIn('Java 17', finding['explanation'])

    def test_heap_failure_does_not_promise_increasing_ram_always_fixes_it(self):
        _, finding = self.finding(HEAP_FAILURE, 'memory')
        self.assertEqual(finding['confidence'], 'reported')
        self.assertTrue(any('available' in suggestion.lower() for suggestion in finding['suggestions']))

    def test_native_allocation_failure_is_memory_evidence(self):
        self.finding('# There is insufficient memory for the Java Runtime Environment to continue.\n# Native memory allocation (malloc) failed to allocate 100000 bytes.\n', 'memory')
        self.finding('Error occurred during initialization of VM\nCould not reserve enough space for 2097152KB object heap\n', 'memory')

    def test_graphics_failure_has_conservative_driver_advice(self):
        _, finding = self.finding(GRAPHICS_FAILURE, 'graphics')
        self.assertTrue(any('driver' in suggestion.lower() for suggestion in finding['suggestions']))

    def test_mixin_error_marks_underlying_mod_conflict_as_inference(self):
        _, finding = self.finding(MIXIN_FAILURE, 'mixin')
        self.assertEqual(finding['confidence'], 'likely')
        self.assertIn('may', finding['explanation'].lower())

    def test_incidental_class_names_and_normal_mixin_loading_do_not_diagnose(self):
        text = '[main/INFO]: Registering diagnostic example java.lang.OutOfMemoryError\n[main/INFO]: Loaded mixins for example\n[main/INFO]: Guide: GLFW error 65542\n[main/INFO]: Mod description: requires helper, which is missing!\n'
        result = analyze(text)
        self.assertEqual(result['findings'], [])
        self.assertEqual(result['severity'], 'info')

    def test_info_message_quoting_error_marker_is_not_treated_as_error_prefix(self):
        # Treating the message body as severity would fabricate a graphics failure.
        result = analyze('[main/INFO]: Guide [ERROR]: Failed to create window examples are explained here\n')
        self.assertEqual(result['findings'], [])

    def test_incidental_exception_class_documentation_is_not_an_exception(self):
        result = analyze('[main/INFO]: java.lang.OutOfMemoryError handling support registered\n')
        self.assertEqual(result['findings'], [])

    def test_graphics_error_inside_exception_is_recognized(self):
        text = 'java.lang.IllegalStateException: GLFW error 65542: WGL: The driver does not appear to support OpenGL\n'
        self.finding(text, 'graphics')

    def test_lwjgl_context_exception_is_recognized(self):
        self.finding('org.lwjgl.LWJGLException: Could not create context\n', 'graphics')

    def test_unknown_failure_has_useful_inspection_guidance_without_inventing_cause(self):
        result = analyze('[main/ERROR]: An unusual failure happened\n', {'exitCode': 1})
        self.assertEqual(result['findings'], [])
        self.assertEqual(result['state'], 'failed')
        self.assertIn('first', result['summary'].lower())

    def test_no_logs_has_no_diagnosis(self):
        result = analyze('')
        self.assertEqual(result['findings'], [])
        self.assertEqual(result['state'], 'no-logs')
        self.assertTrue(result['summary'])

    def test_multiple_failures_are_retained_without_duplicate_findings(self):
        result = analyze(FABRIC_DEPENDENCY + JAVA_MISMATCH + HEAP_FAILURE * 5)
        self.assertEqual({f['id'] for f in result['findings']}, {'dependency', 'java-version', 'memory'})
        self.assertEqual(len(result['findings']), 3)

    def test_running_session_never_claims_a_crash_result(self):
        result = analyze(HEAP_FAILURE, {'running': True, 'exitCode': None})
        self.assertEqual(result['state'], 'running')
        self.assertIn('running', result['summary'].lower())
        self.assertIn('not a completed crash', result['summary'].lower())
        self.assertTrue(result['findings'])

    def test_successful_new_launch_labels_old_crash_as_previous_evidence(self):
        result = analyze(HEAP_FAILURE, {'exitCode': 0, 'startedAt': 200, 'logModifiedAt': 100})
        self.assertEqual(result['state'], 'successful')
        self.assertEqual(result['scope'], 'previous')
        self.assertIn('previous', result['summary'].lower())
        self.assertIn('success', result['summary'].lower())

    def test_new_launch_marker_excludes_failure_from_earlier_session(self):
        text = HEAP_FAILURE + '[harness] launched Minecraft 1.21.1 (pid 10)\n[Render thread/INFO]: Stopping!\n[harness] game exited with code 0\n'
        result = analyze(text)
        self.assertEqual(result['findings'], [])
        self.assertEqual(result['state'], 'successful')

    def test_new_fabric_session_marker_excludes_previous_failure(self):
        text = HEAP_FAILURE + '[13:00:00] [main/INFO]: Loading Minecraft 1.21.1 with Fabric Loader 0.16.1\n[13:00:10] [Render thread/INFO]: Stopping!\n'
        result = analyze(text)
        self.assertEqual(result['findings'], [])

    def test_truncated_source_makes_unknown_guidance_mention_missing_earlier_lines(self):
        result = analyze('[main/ERROR]: Unknown failure\n', {'sourceTruncated': True, 'exitCode': 1})
        self.assertTrue(result['inputTruncated'])
        self.assertIn('earlier', result['summary'].lower())

    def test_successful_exit_labels_error_as_logged_issue_without_claiming_crash(self):
        result = analyze(HEAP_FAILURE, {'exitCode': 0})
        self.assertEqual(result['state'], 'successful')
        self.assertIn('success', result['summary'].lower())
        self.assertIn('logged', result['summary'].lower())

    def test_bounded_tail_truncation_reports_limits_and_keeps_late_failure(self):
        text = 'early harmless line\n' * 90000 + HEAP_FAILURE
        result = analyze(text)
        self.assertTrue(result['inputTruncated'])
        self.assertTrue(result['truncated'])
        self.assertEqual([f['id'] for f in result['findings']], ['memory'])
        self.assertEqual(result['findings'][0]['evidence'][0]['line'], 90001)
        self.assertLess(len(json.dumps(result)), 12000)

    def test_many_tiny_lines_are_bounded_and_preserve_original_evidence_line(self):
        # A byte-only bound still allocates a tuple per tiny line during analysis.
        result = analyze('\n' * 30000 + HEAP_FAILURE)
        self.assertTrue(result['inputTruncated'])
        self.assertEqual(result['findings'][0]['evidence'][0]['line'], 30001)

    def test_oversized_and_repeated_evidence_is_bounded_and_marked(self):
        text = ''.join('Caused by: java.lang.OutOfMemoryError: Java heap space ' + 'x' * 4000 + '\n' for _ in range(40))
        result, finding = self.finding(text, 'memory')
        self.assertLessEqual(len(finding['evidence']), 4)
        self.assertTrue(all(len(e['text']) <= 360 for e in finding['evidence']))
        self.assertTrue(result['evidenceTruncated'])
        self.assertLess(len(json.dumps(result)), 12000)


class SelectedLogSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'instance'
        self.root.mkdir()

    def log(self, text):
        path = self.root / 'logs' / 'latest.log'
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def test_real_selected_log_is_redacted_before_evidence_and_remains_unchanged(self):
        path = self.log(FABRIC_DEPENDENCY + 'accessToken="fixture-secret"\nAuthorization: Bearer fixture-bearer\n' + JAVA_MISMATCH.replace('net/example/Main', 'fixture-secret/Main'))
        before = path.read_bytes()
        result = read_logs(self.root, instance_id='selected', redact=lambda value: value.replace('fixture-secret', '[redacted]'))
        serialized = json.dumps(result)
        self.assertNotIn('fixture-secret', serialized)
        self.assertNotIn('fixture-bearer', serialized)
        self.assertEqual({f['id'] for f in result['diagnosis']['findings']}, {'dependency', 'java-version'})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(result['capabilities'], ['export'])

    def test_foreign_instance_launch_path_and_exit_are_ignored(self):
        self.log(HEAP_FAILURE)
        foreign = self.base / 'foreign.log'
        foreign.write_text('foreign session', encoding='utf-8')
        result = read_logs(self.root, instance_id='selected', launch_status={'configurationId': 'foreign', 'logPath': str(foreign), 'exitCode': 0}, running=True)
        self.assertNotIn('foreign session', result['text'])
        self.assertFalse(result['running'])
        self.assertNotEqual(result['diagnosis']['state'], 'successful')

    def test_matching_host_launch_log_is_used_and_success_overrides_old_failure(self):
        self.log(HEAP_FAILURE)
        host_log = self.base / 'host-game.log'
        host_log.write_text('[harness] launched Minecraft 1.21.1 (pid 10)\nAll done\n', encoding='utf-8')
        result = read_logs(self.root, instance_id='selected', launch_status={'configurationId': 'selected', 'logPath': str(host_log), 'exitCode': 0})
        self.assertIn('All done', result['text'])
        self.assertEqual(result['diagnosis']['state'], 'successful')
        self.assertEqual(result['diagnosis']['findings'], [])

    def test_successful_launch_without_new_log_labels_stale_selected_log(self):
        path = self.log(HEAP_FAILURE)
        os.utime(path, (100, 100))
        result = read_logs(self.root, instance_id='selected', launch_status={'configurationId': 'selected', 'startedAt': 200, 'exitCode': 0})
        self.assertEqual(result['diagnosis']['scope'], 'previous')

    def test_missing_instance_snapshot_does_not_create_root(self):
        self.root.rmdir()
        result = read_logs(self.root, instance_id='selected')
        self.assertEqual(result['text'], '')
        self.assertEqual(result['diagnosis']['state'], 'no-logs')
        self.assertFalse(self.root.exists())

    def test_file_tail_is_bounded_and_drops_partial_first_line(self):
        self.log('x' * (2 * 1024 * 1024) + '\n' + GRAPHICS_FAILURE)
        result = read_logs(self.root)
        self.assertLessEqual(len(result['text'].encode('utf-8')), 1024 * 1024)
        self.assertTrue(result['sourceTruncated'])
        self.assertTrue(result['diagnosis']['inputTruncated'])
        self.assertTrue(result['text'].startswith('[12:01:02]'))
        self.assertEqual([f['id'] for f in result['diagnosis']['findings']], ['graphics'])

    def test_tail_starting_on_line_boundary_does_not_discard_first_complete_error(self):
        retained = HEAP_FAILURE + 'a' * (1024 * 1024 - len(HEAP_FAILURE))
        path = self.log('')
        # Byte sizing is deliberate: Windows write_text translates newlines.
        path.write_bytes(b'outside tail\n' + retained.encode('utf-8'))
        result = read_logs(self.root)
        self.assertTrue(result['text'].startswith('Caused by:'))
        self.assertEqual([f['id'] for f in result['diagnosis']['findings']], ['memory'])

    def test_linked_instance_log_folder_is_refused(self):
        import instance_content
        from instance_content import ContentError
        self.log(HEAP_FAILURE)
        with patch.object(instance_content, '_reparse', side_effect=lambda path: Path(path).name == 'logs'):
            with self.assertRaises(ContentError) as caught:
                read_logs(self.root)
        self.assertEqual(caught.exception.code, 'unsafe_path')


if __name__ == '__main__':
    unittest.main()
