import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/gym'))
import import_run  # noqa: E402

SCORE = {'baseline_count': 2, 'matched_count': 1, 'missed_count': 1, 'recall': 0.5,
         'weight': 2.0, 'weighted_total': 4.0, 'weighted_matched': 1.0, 'extra_count': 0}
VERDICT = {'arm': 'm-one', 'record': 'repo-pr1', 'severity': 'blocking',
           'judge_model': 'judge/x', 'score': SCORE, 'verdict': {}}
MATRIX = {'include': [
    {'record': 'repo-pr1', 'arm': 'm-one', 'model': 'm/one'},
    {'record': 'repo-pr2', 'arm': 'm-one', 'model': 'm/one'},
    {'record': 'repo-pr1', 'arm': 'm-two', 'model': 'm/two'},
]}
RUN = {'id': 42, 'head_sha': 'abcdef1234', 'html_url': 'https://run/42',
       'conclusion': 'success', 'path': '.github/workflows/gym-experiment.yml'}
JOBS = [
    {'name': 'plan', 'started_at': '2026-09-22T10:00:00Z',
     'completed_at': '2026-09-22T10:00:10Z', 'steps': []},
    {'name': 'm-one · repo-pr1', 'started_at': '2026-09-22T10:01:00Z',
     'completed_at': '2026-09-22T10:09:00Z', 'steps': [
         {'name': 'Install pi', 'started_at': '2026-09-22T10:01:30Z',
          'completed_at': '2026-09-22T10:02:00Z'},
         {'name': 'Replay first-pass review', 'started_at': '2026-09-22T10:02:00Z',
          'completed_at': '2026-09-22T10:07:00Z'}]},
]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def make_run(scored=('m-one--repo-pr1',), failed=('m-one--repo-pr2', 'm-two--repo-pr1')):
    """A run directory as fetch-run.sh writes it, plus the dataset file."""
    root = Path(tempfile.mkdtemp())
    run_dir = root / 'run'
    write_json(run_dir / 'run.json', RUN)
    write_json(run_dir / 'jobs.json', JOBS)
    (run_dir / 'plan.log').write_text(
        '2026-09-22T10:00:05Z 3 jobs\n2026-09-22T10:00:05Z ' + json.dumps(MATRIX) + '\n')
    for name in scored + failed:
        artifact = run_dir / 'artifacts' / f'gym-{name}'
        write_json(artifact / 'out' / f'{name}-attribution.json',
                   {'primary': 'provider', 'pi_exit': '0', 'arm': 'x', 'record': 'y'})
        if name in scored:
            write_json(artifact / 'out' / f'{name}.json', VERDICT)
            (artifact / 'replay').mkdir()
            (artifact / 'replay' / 'findings.md').write_text('review')
        else:
            (artifact / 'out' / 'FAILED').write_text(name)
    dataset = root / 'd.yaml'
    dataset.write_text(
        'dataset:\n  name: gym\nrecords:\n'
        '  - id: repo-pr1\n    expected_output:\n      findings: recorded one\n'
        '  - id: repo-pr2\n    expected_output:\n      findings: recorded two\n')
    return run_dir, dataset


class PlannedJobs(unittest.TestCase):
    def test_reads_the_matrix_the_plan_job_printed(self):
        log = 'noise\n2026-09-22T10:00:05.1Z ' + json.dumps(MATRIX) + '\nmore noise\n'
        self.assertEqual(import_run.planned_jobs(log), MATRIX['include'])

    def test_raises_when_the_log_holds_no_matrix(self):
        with self.assertRaises(import_run.DatadogError):
            import_run.planned_jobs('no matrix here\n')


class ReplayTimings(unittest.TestCase):
    def test_times_a_replay_by_its_replay_step(self):
        timing = import_run.replay_timings(JOBS)['m-one · repo-pr1']
        self.assertEqual(timing['ended_ns'] - timing['started_ns'], 300 * 10**9)

    def test_falls_back_to_the_job_when_the_replay_step_never_ran(self):
        timing = import_run.replay_timings(JOBS)['plan']
        self.assertEqual(timing['ended_ns'] - timing['started_ns'], 10 * 10**9)


class NormalizeVerdict(unittest.TestCase):
    def test_derives_weighted_recall_from_the_weighted_totals(self):
        score = import_run.normalize_verdict(VERDICT, 'review')['score']
        self.assertEqual(score['weighted_recall'], 0.25)

    def test_leaves_weighted_recall_unset_when_nothing_was_sought(self):
        verdict = {**VERDICT, 'score': {**SCORE, 'weighted_total': 0.0}}
        self.assertNotIn('weighted_recall', import_run.normalize_verdict(verdict, 'review')['score'])


    def test_an_empty_review_is_an_empty_candidate(self):
        self.assertTrue(import_run.normalize_verdict(VERDICT, ' \n')['score']['empty_candidate'])
        self.assertFalse(import_run.normalize_verdict(VERDICT, 'review')['score']['empty_candidate'])


class ReplaysByModel(unittest.TestCase):
    def setUp(self):
        run_dir, dataset = make_run()
        self.models = import_run.replays_by_model(str(run_dir), str(dataset))

    def test_groups_the_replays_by_the_model_each_arm_ran(self):
        self.assertEqual({model: sorted(planned['replays'])
                          for model, planned in self.models.items()},
                         {'m/one': ['repo-pr1', 'repo-pr2'], 'm/two': ['repo-pr1']})

    def test_loads_a_scored_replay_in_the_shape_a_live_replay_has(self):
        replay = self.models['m/one']['replays']['repo-pr1']
        self.assertEqual((replay['findings'], replay['expected']), ('review', 'recorded one'))
        self.assertEqual(replay['verdict']['score']['recall'], 0.5)
        self.assertEqual(replay['run']['pi_exit'], 0)
        self.assertEqual(replay['attribution'], {'primary': 'provider'})

    def test_a_failed_replay_has_no_verdict(self):
        self.assertIsNone(self.models['m/one']['replays']['repo-pr2']['verdict'])


class Main(unittest.TestCase):
    def setUp(self):
        self.run_dir, self.dataset = make_run()
        env = mock.patch.dict('os.environ', {'DD_API_KEY': 'k', 'DD_APP_KEY': 'a'})
        env.start()
        self.addCleanup(env.stop)
        self.dx = mock.patch.object(import_run, 'dx', wraps=import_run.dx).start()
        self.addCleanup(mock.patch.stopall)
        self.dx.resolve_project = mock.Mock(return_value='proj1')
        self.dx.find_experiment = mock.Mock(return_value=None)
        self.dx.start_experiment = mock.Mock(return_value={
            'experiment_id': 'exp1', 'project_id': 'proj1', 'dataset_id': 'ds1'})
        self.dx.post_replay = mock.Mock()
        self.dx.finish_experiment = mock.Mock()

    def run_import(self):
        return import_run.main(['--run-dir', str(self.run_dir), '--dataset-file',
                                str(self.dataset), '--project', 'p'])

    def test_imports_each_scored_model_as_its_own_experiment(self):
        self.assertEqual(self.run_import(), 0)
        (call,) = self.dx.start_experiment.call_args_list
        self.assertEqual((call.kwargs['model'], call.kwargs['judge_model']),
                         ('m/one', 'judge/x'))
        self.assertEqual(call.kwargs['extra_tags'], ['github_run_id:42', 'workflow_sha:abcdef1'])
        self.assertEqual(self.dx.post_replay.call_count, 2)
        self.dx.finish_experiment.assert_called_once_with(
            mock.ANY, mock.ANY, mock.ANY, 'exp1', 'completed')

    def test_a_run_that_did_not_succeed_is_a_failed_experiment(self):
        write_json(self.run_dir / 'run.json', {**RUN, 'conclusion': 'cancelled'})
        self.run_import()
        self.assertEqual(self.dx.finish_experiment.call_args.args[4], 'failed')

    def test_skips_a_model_already_imported_for_the_run(self):
        self.dx.find_experiment.return_value = {'id': 'old', 'status': 'completed'}
        self.assertEqual(self.run_import(), 0)
        self.dx.start_experiment.assert_not_called()

    def test_stops_on_an_import_that_did_not_finish(self):
        self.dx.find_experiment.return_value = {'id': 'old', 'status': 'running'}
        self.assertEqual(self.run_import(), 1)
        self.dx.start_experiment.assert_not_called()

    def test_refuses_a_run_of_another_workflow(self):
        write_json(self.run_dir / 'run.json', {**RUN, 'path': '.github/workflows/tests.yml'})
        self.assertEqual(self.run_import(), 1)
        self.dx.start_experiment.assert_not_called()


if __name__ == '__main__':
    unittest.main()
