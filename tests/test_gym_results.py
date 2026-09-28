import base64
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/gym'))
import gym_results as gr  # noqa: E402

RECORD = {
    'id': 'repo-pr7',
    'input': {'repo': 'org/repo', 'pr': 7, 'head_sha': 'a' * 40, 'base_sha': 'b' * 40},
    'expected_output': {'findings': 'recorded\n'},
    'metadata': {'severity': 'blocking', 'codex_prompt_version': '44f066e07f6b'},
}
PROMPT = {'name': 'first-pass', 'version': '3f4a8e3d3003', 'registry_sha': 'c' * 40}
SCORE = {'recall': 0.5, 'weighted_recall': 0.25, 'matched_count': 1, 'missed_count': 1,
         'baseline_count': 2, 'extra_count': 0}
VERDICT = {'judge_model': 'judge/x', 'verdict': {'baseline_findings': []}, 'score': SCORE}
RUN = {'id': '123', 'attempt': '2', 'url': 'https://example/runs/123'}
DIFF = """diff --git a/x.rb b/x.rb
--- a/x.rb
+++ b/x.rb
@@ -1,2 +1,3 @@
-old
+new
+added
 same
diff --git a/y.rb b/y.rb
--- a/y.rb
+++ b/y.rb
@@ -1 +0,0 @@
-gone
"""


def replay(**overrides):
    base = {'run': {'started_ns': 1_000_000_000, 'ended_ns': 4_000_000_000, 'pi_exit': 0},
            'findings': 'review', 'expected': 'recorded', 'verdict': VERDICT,
            'failure': '', 'attribution': {'primary': 'provider'}}
    return {**base, **overrides}


def row(**overrides):
    return gr.result_row('sol-set', RECORD, 'openai/gpt-5.6-luna', 'judge/x', RUN, PROMPT,
                         replay(**overrides), {'files': 2, 'additions': 2, 'deletions': 2})


class DiffStats(unittest.TestCase):
    def test_counts_files_and_changed_lines_but_not_file_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'pr.diff')
            Path(path).write_text(DIFF)
            self.assertEqual(gr.diff_stats(path),
                             {'files': 2, 'additions': 2, 'deletions': 2})

    def test_is_none_when_the_replay_never_built_a_diff(self):
        self.assertIsNone(gr.diff_stats('/nonexistent/pr.diff'))


class ResultRow(unittest.TestCase):
    def test_carries_the_pull_request_commits_and_diff_size(self):
        result = row()
        self.assertEqual((result['repo'], result['pr'], result['head_sha'], result['base_sha']),
                         ('org/repo', 7, 'a' * 40, 'b' * 40))
        self.assertEqual(result['diff']['additions'], 2)
        self.assertEqual(result['severity'], 'blocking')

    def test_carries_the_replay_prompt_and_the_recorded_one(self):
        result = row()
        self.assertEqual((result['replay_prompt_name'], result['replay_prompt_version'],
                          result['registry_sha'], result['recorded_prompt_version']),
                         ('first-pass', '3f4a8e3d3003', 'c' * 40, '44f066e07f6b'))

    def test_carries_the_scores_verdict_and_both_reviews(self):
        result = row()
        self.assertEqual(result['score'], SCORE)
        self.assertEqual(result['judge_verdict'], VERDICT['verdict'])
        self.assertEqual((result['findings'], result['expected']), ('review', 'recorded'))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['duration_s'], 3.0)

    def test_a_failed_replay_is_an_error_with_no_score(self):
        result = row(verdict=None, failure='replay produced no findings')
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['failure'], 'replay produced no findings')
        self.assertIsNone(result['score'])

    def test_a_timeout_exit_is_timed_out(self):
        result = row(run={'started_ns': 1, 'ended_ns': 2, 'pi_exit': gr.TIMEOUT_EXIT})
        self.assertTrue(result['timed_out'])

    def test_timed_out_is_unknown_without_an_exit_status(self):
        self.assertIsNone(row(run={})['timed_out'])


class ResultPath(unittest.TestCase):
    def test_groups_by_dataset_run_attempt_and_model(self):
        self.assertEqual(gr.result_path('sol-set', RUN, 'openai/gpt-5.6-luna', 'repo-pr7'),
                         'results/sol-set/123-2-gpt-5-6-luna/repo-pr7.json')


def http_error(code):
    return urllib.error.HTTPError('url', code, 'msg', {}, io.BytesIO(b'detail'))


class PutFile(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(gr.time, 'sleep')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sends_the_content_base64_encoded(self):
        with mock.patch.object(gr.urllib.request, 'urlopen') as urlopen:
            gr.put_file('org/data', 'results/a.json', '{"x": 1}', 'msg', 'tok')
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url,
                         'https://api.github.com/repos/org/data/contents/results/a.json')
        self.assertEqual(request.get_method(), 'PUT')
        body = json.loads(request.data)
        self.assertEqual(base64.b64decode(body['content']).decode(), '{"x": 1}')

    def test_retries_a_conflict_with_another_job(self):
        with mock.patch.object(gr.urllib.request, 'urlopen',
                               side_effect=[http_error(409), mock.MagicMock()]) as urlopen:
            gr.put_file('org/data', 'a.json', '{}', 'msg', 'tok')
        self.assertEqual(urlopen.call_count, 2)

    def test_does_not_retry_other_errors(self):
        with mock.patch.object(gr.urllib.request, 'urlopen',
                               side_effect=http_error(403)) as urlopen:
            with self.assertRaises(gr.ResultsError):
                gr.put_file('org/data', 'a.json', '{}', 'msg', 'tok')
        self.assertEqual(urlopen.call_count, 1)


class Main(unittest.TestCase):
    def test_saves_the_replay_under_its_run_and_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset = os.path.join(tmp, 'set.yaml')
            Path(dataset).write_text(yaml.safe_dump(
                {'dataset': {'name': 'sol-set'}, 'records': [RECORD]}))
            replay_dir = os.path.join(tmp, 'replay')
            os.makedirs(os.path.join(replay_dir, 'repo'))
            Path(replay_dir, 'repo', 'pr.diff').write_text(DIFF)
            Path(replay_dir, 'findings.md').write_text('review')
            Path(replay_dir, 'expected.md').write_text('recorded')
            Path(replay_dir, 'verdict.json').write_text(json.dumps(VERDICT))
            Path(replay_dir, 'run.json').write_text(json.dumps({'pi_exit': 0}))

            with mock.patch.dict(os.environ, {'GH_TOKEN': 'tok'}), \
                    mock.patch.object(gr, 'put_file') as put:
                status = gr.main(['--dataset', dataset, '--record', 'repo-pr7',
                                  '--model', 'openai/gpt-5.6-luna', '--judge-model', 'j/x',
                                  '--replay-dir', replay_dir, '--run-id', '123',
                                  '--run-attempt', '1', '--prompt-version', '',
                                  '--registry-sha', 'c' * 40])

        self.assertEqual(status, 0)
        repo, path, content = put.call_args.args[:3]
        self.assertEqual(repo, 'BiggerPockets/pi-gym-data')
        self.assertEqual(path, 'results/sol-set/123-1-gpt-5-6-luna/repo-pr7.json')
        saved = json.loads(content)
        self.assertEqual(saved['diff'], {'files': 2, 'additions': 2, 'deletions': 2})
        self.assertEqual(saved['score'], SCORE)
        self.assertIsNone(saved['replay_prompt_version'])
        self.assertEqual(saved['registry_sha'], 'c' * 40)

    def test_fails_without_a_token(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gr.main(['--dataset', 'x', '--record', 'r', '--model', 'm',
                                      '--judge-model', 'j', '--replay-dir', 'd',
                                      '--run-id', '1', '--run-attempt', '1']), 1)


if __name__ == '__main__':
    unittest.main()
