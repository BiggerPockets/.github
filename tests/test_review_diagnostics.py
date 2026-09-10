import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/review-diagnostics.py'


class ReviewDiagnosticsTest(unittest.TestCase):
    def run_diagnostics(self, content):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'execution.json'
            if content is not None:
                source.write_text(content)
            destination = root / 'diagnostics.json'
            summary = root / 'summary.md'
            result = subprocess.run(
                ['python3', str(SCRIPT), str(source), str(destination)],
                env={**os.environ, 'GITHUB_STEP_SUMMARY': str(summary),
                     'DIAGNOSTIC_SECRET_VALUES': json.dumps(['private-test-secret'])},
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(destination.read_text()), summary.read_text()

    def test_retains_error_without_tool_transcript_or_secrets(self):
        report, summary = self.run_diagnostics(json.dumps([
            {'type': 'assistant', 'message': {'content': 'private tool transcript'}},
            {'type': 'result', 'subtype': 'success', 'is_error': True,
             'result': 'API Error: private-test-secret', 'num_turns': 36,
             'errors': ['Provider failed'], 'extra': 'private tool transcript'},
        ]))
        self.assertEqual(report['result']['result'], 'API Error: [REDACTED]')
        self.assertEqual(report['result']['errors'], ['Provider failed'])
        self.assertNotIn('private tool transcript', json.dumps(report) + summary)
        self.assertNotIn('private-test-secret', json.dumps(report) + summary)
        self.assertIn('Provider failed', summary)

    def test_reads_json_lines(self):
        report, _ = self.run_diagnostics('\n'.join([
            json.dumps({'type': 'system'}),
            json.dumps({'type': 'result', 'is_error': True, 'result': 'API error'}),
        ]))
        self.assertEqual(report['result']['result'], 'API error')

    def test_missing_and_malformed_output_remain_diagnostic(self):
        for content in [None, 'invalid private-test-secret']:
            with self.subTest(content=content):
                report, summary = self.run_diagnostics(content)
                self.assertIn('unavailable', report['status'])
                self.assertNotIn('private-test-secret', summary)

    def test_no_result_does_not_publish_transcript(self):
        report, summary = self.run_diagnostics(json.dumps([
            {'type': 'assistant', 'message': 'private tool transcript'},
        ]))
        self.assertEqual(report['status'], 'result unavailable')
        self.assertNotIn('private tool transcript', summary)

    def test_pi_stream_captures_terminal_message_without_tool_transcripts(self):
        stream = [
            {'type': 'session'},
            {'type': 'turn_start'},
            {'type': 'tool_execution_end', 'toolName': 'write',
             'args': {'file_path': '/tmp/x', 'content': 'private tool transcript'},
             'result': 'written: private tool transcript'},
            {'type': 'message_end', 'message': {'role': 'assistant',
             'stopReason': 'error',
             'errorMessage': '429 provider error private-test-secret',
             'model': 'deepseek/deepseek-v4.1-flash',
             'provider': 'openrouter',
             'usage': {'input': 5, 'output': 1, 'cacheRead': 0, 'cacheWrite': 0,
                       'totalTokens': 6, 'cost': {'total': 0.0012}}}},
            {'type': 'turn_end'},
            {'type': 'agent_end', 'messages': [{'role': 'assistant',
             'stopReason': 'error',
             'errorMessage': '429 provider error private-test-secret',
             'model': 'deepseek/deepseek-v4.1-flash',
             'provider': 'openrouter',
             'usage': {'input': 5, 'output': 1, 'cacheRead': 0, 'cacheWrite': 0,
                       'totalTokens': 6, 'cost': {'total': 0.0012}}}]},
        ]
        report, summary = self.run_diagnostics(json.dumps(stream))
        self.assertEqual(report['status'], 'captured')
        self.assertTrue(report['result']['is_error'])
        self.assertEqual(report['result']['errors'],
                         ['429 provider error [REDACTED]'])
        self.assertEqual(report['result']['num_turns'], 1)
        self.assertEqual(report['result']['total_cost'], 0.0012)
        self.assertNotIn('private tool transcript', json.dumps(report) + summary)
        self.assertNotIn('private-test-secret', json.dumps(report) + summary)

    def test_pi_stream_success_reports_the_terminal_text(self):
        stream = [
            {'type': 'agent_end', 'messages': [{'role': 'assistant',
             'stopReason': 'stop',
             'content': [{'type': 'text', 'text': '## Review summary private-test-secret'}]}]},
        ]
        report, summary = self.run_diagnostics(json.dumps(stream))
        self.assertEqual(report['result']['is_error'], False)
        self.assertIn('Review summary [REDACTED]', report['result']['result'])

    def test_pi_stderr_tail_is_captured_and_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'pi-output.jsonl'
            source.write_text(json.dumps({'type': 'agent_end', 'messages': [
                {'role': 'assistant', 'stopReason': 'error',
                 'errorMessage': 'boom'}]}))
            destination = root / 'diagnostics.json'
            stderr = root / 'pi-error.log'
            stderr.write_text('line1\nline2 private-test-secret\n')
            summary = root / 'summary.md'
            result = subprocess.run(
                ['python3', str(SCRIPT), str(source), str(destination), str(stderr)],
                env={**os.environ, 'GITHUB_STEP_SUMMARY': str(summary),
                     'DIAGNOSTIC_SECRET_VALUES': json.dumps(['private-test-secret'])},
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(destination.read_text())
            self.assertEqual(report['stderr_tail'], ['line1', 'line2 [REDACTED]'])
            self.assertNotIn('private-test-secret', stderr.read_text() and '')


if __name__ == '__main__':
    unittest.main()
