"""The gym's Datadog steps must never stop a run.

pi-gym-data holds the primary copy of a run's results, so a Datadog outage or a rejected
key should cost the Datadog copy and nothing else.
"""
import os
import pathlib
import subprocess
import tempfile
import unittest

STEPS = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'gym' / 'steps'


def run_step(name, env, cwd):
    base = {'PATH': os.environ['PATH'], 'GITHUB_SERVER_URL': 'https://github.com',
            'GITHUB_REPOSITORY': 'org/repo', 'GITHUB_RUN_ID': '1'}
    return subprocess.run(['bash', str(STEPS / name)], env={**base, **env}, cwd=cwd,
                          capture_output=True, text=True)


class DatadogStepsDoNotStopTheRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = pathlib.Path(self.tmp.name, 'github_output')
        self.output.touch()

    def test_a_failed_experiment_create_warns_and_publishes_no_ids(self):
        result = run_step('create-experiment.sh', {
            'GITHUB_OUTPUT': str(self.output), 'DATADOG_PROJECT': 'p',
            'DATASET_FILE': 'missing.yaml', 'MODEL': 'm', 'JUDGE_MODEL': 'j',
            'PROMPT_VERSION': 'v'}, self.tmp.name)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('::warning::', result.stdout)
        self.assertEqual(self.output.read_text(), '')

    def test_recording_is_skipped_without_an_experiment(self):
        result = run_step('record-replay.sh', {'EXPERIMENT_ID': ''}, self.tmp.name)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('No Datadog experiment', result.stdout)

    def test_finishing_is_skipped_without_an_experiment(self):
        result = run_step('finish-experiment.sh',
                          {'EXPERIMENT_ID': '', 'REPLAY_RESULT': 'success'}, self.tmp.name)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_rejected_record_post_warns_instead_of_failing(self):
        result = run_step('record-replay.sh', {
            'EXPERIMENT_ID': 'exp', 'PROJECT_ID': 'p', 'DATASET_ID': 'd', 'MODEL': 'm',
            'RECORD': 'r', 'REPLAY_DIR': self.tmp.name, 'OUT_DIR': self.tmp.name},
            self.tmp.name)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('::warning::', result.stdout)


if __name__ == '__main__':
    unittest.main()
