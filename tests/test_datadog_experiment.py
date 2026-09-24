import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/gym'))
spec = importlib.util.spec_from_file_location(
    'datadog_experiment', ROOT / 'scripts/gym/datadog_experiment.py')
dx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dx)

TAGS = dx.experiment_tags('exp1', 'proj1', 'ds1', 'openai/gpt-5.6-luna')
VERDICT = {'severity': 'blocker', 'judge_model': 'judge/x',
           'verdict': {'baseline_findings': [{'matched': True}]},
           'score': {'recall': 0.5, 'weighted_recall': 0.25, 'matched_count': 1,
                     'missed_count': 1, 'baseline_count': 2, 'extra_count': 0}}


def attributes(body):
    return body['data']['attributes']


def metrics_by_label(body):
    return {m['label']: m for m in attributes(body)['metrics']}


class RecordBody(unittest.TestCase):
    def body(self, **overrides):
        kwargs = dict(experiment_id='exp1', tags=TAGS, record='repo-pr1',
                      findings='review', expected='recorded', verdict=VERDICT,
                      attribution={'timed_out': False}, started_ns=1_000, ended_ns=5_000)
        kwargs.update(overrides)
        return dx.record_body(**kwargs)

    def test_links_the_span_to_its_dataset_record(self):
        span = attributes(self.body())['spans'][0]
        self.assertIn('dataset_record_id:repo-pr1', span['tags'])
        self.assertIn('experiment_id:exp1', span['tags'])

    def test_carries_the_review_and_the_recorded_findings(self):
        meta = attributes(self.body())['spans'][0]['meta']
        self.assertEqual((meta['output'], meta['expected_output']), ('review', 'recorded'))
        self.assertEqual(meta['metadata']['judge_verdict'], VERDICT['verdict'])

    def test_posts_scores_as_score_metrics_on_the_same_span(self):
        body = self.body()
        span = attributes(body)['spans'][0]
        recall = metrics_by_label(body)['recall']
        self.assertEqual((recall['metric_type'], recall['score_value']), ('score', 0.5))
        self.assertEqual((recall['span_id'], recall['trace_id']),
                         (span['span_id'], span['trace_id']))

    def test_posts_flags_as_boolean_metrics(self):
        timed_out = metrics_by_label(self.body())['timed_out']
        self.assertEqual((timed_out['metric_type'], timed_out['boolean_value']),
                         ('boolean', False))

    def test_a_failed_replay_is_an_errored_span_without_recall(self):
        # A failed replay is unmeasured, not a zero: posting recall 0 would read as the
        # model missing everything.
        body = self.body(verdict=None, failure='pi exit 124')
        span = attributes(body)['spans'][0]
        self.assertEqual(span['status'], 'error')
        self.assertEqual(span['meta']['metadata']['failure'], 'pi exit 124')
        self.assertNotIn('recall', metrics_by_label(body))

    def test_duration_is_never_zero(self):
        span = attributes(self.body(started_ns=5_000, ended_ns=5_000))['spans'][0]
        self.assertEqual(span['duration'], 1)


class CreateBody(unittest.TestCase):
    def test_names_the_one_model_and_the_dataset_version(self):
        body = dx.create_body('ds1', 'proj1', 2, 'openai/gpt-5.6-luna', 'judge/x',
                              '44f066e07f6b', 'https://run')
        attrs = attributes(body)
        self.assertEqual((attrs['dataset_id'], attrs['dataset_version']), ('ds1', 2))
        self.assertEqual(attrs['config']['model'], 'openai/gpt-5.6-luna')
        self.assertIn('model:openai/gpt-5.6-luna', attrs['metadata']['tags'])


class DatasetRecordIds(unittest.TestCase):
    def test_follows_the_cursor_across_pages(self):
        pages = [{'data': [{'id': 'a'}], 'meta': {'after': 'next'}},
                 {'data': [{'id': 'b'}], 'meta': {}}]
        with mock.patch.object(dx, 'request_json', side_effect=pages) as request:
            ids = dx.dataset_record_ids('site', 'k', 'a', 'proj1', 'ds1')
        self.assertEqual(ids, {'a', 'b'})
        self.assertIn('page%5Bcursor%5D=next', request.call_args_list[1].args[4])


if __name__ == '__main__':
    unittest.main()
