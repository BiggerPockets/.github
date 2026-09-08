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


if __name__ == '__main__':
    unittest.main()
