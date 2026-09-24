import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/gym'))
import datadog_experiment as dx  # noqa: E402

TAGS = dx.experiment_tags('exp1', 'proj1', 'ds1', 'openai/gpt-5.6-luna')
VERDICT = {'severity': 'blocker', 'judge_model': 'judge/x',
           'verdict': {'baseline_findings': [{'matched': True}]},
           'score': {'recall': 0.5, 'weighted_recall': 0.25, 'matched_count': 1,
                     'missed_count': 1, 'baseline_count': 2, 'extra_count': 0}}
RUN = {'started_ns': 1_000, 'ended_ns': 5_000, 'pi_exit': 0}


def replay(**overrides):
    base = {'run': RUN, 'findings': 'review', 'expected': 'recorded', 'verdict': VERDICT,
            'failure': '', 'attribution': {'primary': 'provider'}}
    return {**base, **overrides}


def attributes(body):
    return body['data']['attributes']


def span_of(body):
    return attributes(body)['spans'][0]


def metrics_by_label(body):
    return {m['label']: m for m in attributes(body)['metrics']}


class RecordBody(unittest.TestCase):
    def body(self, **overrides):
        return dx.record_body('exp1', TAGS, 'repo-pr1', replay(**overrides))

    def test_links_the_span_to_its_dataset_record(self):
        tags = span_of(self.body())['tags']
        self.assertIn('dataset_record_id:repo-pr1', tags)
        self.assertIn('experiment_id:exp1', tags)

    def test_carries_the_review_the_recorded_findings_and_the_verdict(self):
        meta = span_of(self.body())['meta']
        self.assertEqual((meta['output'], meta['expected_output']), ('review', 'recorded'))
        self.assertEqual(meta['metadata']['judge_verdict'], VERDICT['verdict'])

    def test_posts_scores_as_score_metrics_on_the_same_span(self):
        body = self.body()
        span, recall = span_of(body), metrics_by_label(body)['recall']
        self.assertEqual((recall['metric_type'], recall['score_value']), ('score', 0.5))
        self.assertEqual((recall['span_id'], recall['trace_id']),
                         (span['span_id'], span['trace_id']))

    def test_a_timeout_exit_is_a_true_timed_out_metric(self):
        body = self.body(run={**RUN, 'pi_exit': dx.TIMEOUT_EXIT})
        timed_out = metrics_by_label(body)['timed_out']
        self.assertEqual((timed_out['metric_type'], timed_out['boolean_value']),
                         ('boolean', True))

    def test_no_timed_out_metric_without_an_exit_status(self):
        body = self.body(run={'started_ns': 1_000, 'ended_ns': 5_000})
        self.assertNotIn('timed_out', metrics_by_label(body))

    def test_a_replay_without_a_verdict_is_an_errored_span_without_recall(self):
        # Unmeasured, not zero: posting recall 0 would read as the model missing everything.
        body = self.body(verdict=None, findings='', failure='pi exit 1')
        self.assertEqual(span_of(body)['status'], 'error')
        self.assertEqual(span_of(body)['meta']['metadata']['failure'], 'pi exit 1')
        self.assertNotIn('recall', metrics_by_label(body))

    def test_takes_its_timing_from_the_run(self):
        span = span_of(self.body())
        self.assertEqual((span['start_ns'], span['duration']), (1_000, 4_000))

    def test_timestamps_metrics_when_posted_not_when_the_replay_ran(self):
        with mock.patch.object(dx.time, 'time_ns', return_value=9_000_000_000):
            recall = metrics_by_label(self.body())['recall']
        self.assertEqual(recall['timestamp_ms'], 9_000)

    def test_duration_is_never_zero(self):
        span = span_of(self.body(run={**RUN, 'started_ns': 5_000}))
        self.assertEqual(span['duration'], 1)


class FailureReason(unittest.TestCase):
    def test_none_when_the_replay_was_scored(self):
        self.assertIsNone(dx.failure_reason(replay()))

    def test_the_replays_own_account_of_its_failure(self):
        self.assertEqual(dx.failure_reason(replay(verdict=None, failure='pi exit 1')),
                         'pi exit 1')

    def test_blames_the_judge_when_the_replay_produced_a_review(self):
        self.assertIn('judge', dx.failure_reason(replay(verdict=None)))

    def test_says_the_replay_did_not_complete_when_nothing_was_left_behind(self):
        self.assertIn('did not complete',
                      dx.failure_reason(replay(verdict=None, findings='')))


class LoadReplay(unittest.TestCase):
    def test_reads_what_the_replay_wrote_and_treats_missing_files_as_empty(self):
        directory = Path(tempfile.mkdtemp())
        (directory / 'run.json').write_text(json.dumps(RUN))
        (directory / 'findings.md').write_text('review')
        loaded = dx.load_replay(str(directory), str(directory / 'absent.json'))
        self.assertEqual(loaded['run'], RUN)
        self.assertEqual(loaded['findings'], 'review')
        self.assertIsNone(loaded['verdict'])
        self.assertEqual((loaded['failure'], loaded['attribution']), ('', {}))


class CreateBody(unittest.TestCase):
    def test_names_the_one_model_and_the_dataset_version(self):
        attrs = attributes(dx.create_body('ds1', 'proj1', 2, 'openai/gpt-5.6-luna',
                                          'judge/x', '44f066e07f6b', 'https://run'))
        self.assertEqual((attrs['dataset_id'], attrs['dataset_version']), ('ds1', 2))
        self.assertEqual(attrs['config']['model'], 'openai/gpt-5.6-luna')
        self.assertIn('model:openai/gpt-5.6-luna', attrs['metadata']['tags'])


class DatasetName(unittest.TestCase):
    def test_reads_the_name_the_file_gives_its_dataset(self):
        path = Path(tempfile.mkdtemp()) / 'd.yaml'
        path.write_text('dataset:\n  name: sol-first-pass-findings\nrecords: []\n')
        self.assertEqual(dx.dataset_name(str(path)), 'sol-first-pass-findings')

    def test_raises_when_the_file_names_no_dataset(self):
        path = Path(tempfile.mkdtemp()) / 'd.yaml'
        path.write_text('records: []\n')
        with self.assertRaises(dx.DatadogError):
            dx.dataset_name(str(path))


class DatasetRecordIds(unittest.TestCase):
    def test_follows_the_cursor_across_pages(self):
        pages = [{'data': [{'id': 'a'}], 'meta': {'after': 'next'}},
                 {'data': [{'id': 'b'}], 'meta': {}}]
        with mock.patch.object(dx, 'request_json', side_effect=pages) as request:
            ids = dx.dataset_record_ids('site', 'k', 'a', 'proj1', 'ds1')
        self.assertEqual(ids, {'a', 'b'})
        self.assertIn('page%5Bcursor%5D=next', request.call_args_list[1].args[4])


class Create(unittest.TestCase):
    """The run stops before creating anything when the Datadog copy of the dataset is
    missing a record the run is about to replay."""

    def setUp(self):
        directory = Path(tempfile.mkdtemp())
        self.dataset = directory / 'd.yaml'
        self.dataset.write_text('dataset:\n  name: gym\nrecords: []\n')
        self.matrix = directory / 'matrix.json'
        self.matrix.write_text(json.dumps({'include': [{'record': 'a'}, {'record': 'b'}]}))
        self.env = mock.patch.dict('os.environ', {'DD_API_KEY': 'k', 'DD_APP_KEY': 'a'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_create(self, stored_ids):
        calls = []

        def request_json(site, api_key, app_key, method, path, body=None):
            calls.append((method, path))
            if '/records' in path:
                return {'data': [{'id': i} for i in stored_ids]}
            if '/datasets' in path:
                return {'data': [{'id': 'ds1', 'attributes': {
                    'name': 'gym', 'current_version': 2}}]}
            return {'data': {'id': 'exp1'}}

        with mock.patch.object(dx, 'request_json', side_effect=request_json), \
                mock.patch.object(dx, 'find_project', return_value='proj1'):
            rc = dx.main(['create', '--project', 'p', '--dataset-file', str(self.dataset),
                          '--matrix', str(self.matrix), '--model', 'm/one',
                          '--judge-model', 'j/judge'])
        return rc, calls

    def test_creates_the_experiment_when_every_record_is_present(self):
        rc, calls = self.run_create(['a', 'b', 'c'])
        self.assertEqual(rc, 0)
        self.assertIn(('POST', '/api/unstable/llm-obs/v1/experiments'), calls)

    def test_creates_nothing_when_a_planned_record_is_missing(self):
        rc, calls = self.run_create(['a'])
        self.assertEqual(rc, 1)
        self.assertNotIn('POST', [method for method, _ in calls])


if __name__ == '__main__':
    unittest.main()
