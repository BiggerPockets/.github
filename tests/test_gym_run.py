import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan_matrix = load('plan_matrix', 'scripts/gym/plan_matrix.py')
judge = load('judge_findings', 'scripts/gym/judge_findings.py')
summarize = load('summarize_run', 'scripts/gym/summarize_run.py')
replay = load('replay_context', 'scripts/gym/replay_context.py')


def record(rid='repo-pr1', pr=1, head='a' * 40, base='b' * 40, severity='blocking',
           prompt_version='44f066e07f6b'):
    return {
        'id': rid,
        'input': {'repo': 'org/repo', 'pr': pr, 'head_sha': head, 'base_sha': base,
                  'instruction': f'Review pr:{pr} against ticket.json/pr.diff'},
        'expected_output': {'findings': '- **Blocking** thing\n'},
        'metadata': {'severity': severity, 'reviewed_at': '2026-09-17T13:16:34+00:00',
                     'codex_prompt_version': prompt_version},
    }


class ArmLabel(unittest.TestCase):
    def test_strips_the_provider_and_punctuation(self):
        self.assertEqual(plan_matrix.arm_label('openai/gpt-5.6-luna'), 'gpt-5-6-luna')

    def test_is_stable_for_a_bare_slug(self):
        self.assertEqual(plan_matrix.arm_label('claude-haiku-4.5'), 'claude-haiku-4-5')


class PlanMatrix(unittest.TestCase):
    def doc(self, n=3):
        return {'records': [record(f'repo-pr{i}', pr=i) for i in range(1, n + 1)]}

    def test_emits_one_job_per_record_per_arm(self):
        include, _ = plan_matrix.plan(self.doc(3), ['a/one', 'b/two'])
        self.assertEqual(len(include), 6)

    def test_limit_takes_the_first_n_so_runs_are_comparable(self):
        include, _ = plan_matrix.plan(self.doc(5), ['a/one'], limit=2)
        self.assertEqual([j['record'] for j in include], ['repo-pr1', 'repo-pr2'])

    def test_carries_the_pinned_commit_into_each_job(self):
        include, _ = plan_matrix.plan(self.doc(1), ['a/one'])
        self.assertEqual(include[0]['head_sha'], 'a' * 40)
        self.assertEqual(include[0]['base_sha'], 'b' * 40)

    def test_skips_and_reports_unpinned_records(self):
        doc = self.doc(2)
        doc['records'][0]['input']['head_sha'] = None
        include, skipped = plan_matrix.plan(doc, ['a/one'])
        self.assertEqual(skipped, ['repo-pr1'])
        self.assertEqual([j['record'] for j in include], ['repo-pr2'])

    def test_filters_by_severity(self):
        doc = {'records': [record('a', severity='blocker'),
                           record('b', severity='non-blocking')]}
        include, _ = plan_matrix.plan(doc, ['a/one'], severities={'blocker'})
        self.assertEqual([j['record'] for j in include], ['a'])

    def test_prompt_version_filter_excludes_records_from_an_older_prompt(self):
        # Replaying a record whose prompt has changed measures the prompt edit and the
        # model swap together and reports the sum as a model difference.
        doc = {'records': [record('current', prompt_version='44f066e07f6b'),
                           record('stale', prompt_version='301569d42943')]}
        include, skipped = plan_matrix.plan(doc, ['a/one'],
                                            prompt_version='44f066e07f6b')
        self.assertEqual([j['record'] for j in include], ['current'])
        self.assertIn('stale', skipped)

    def test_no_prompt_version_filter_keeps_every_record(self):
        doc = {'records': [record('current', prompt_version='44f066e07f6b'),
                           record('stale', prompt_version='301569d42943')]}
        include, skipped = plan_matrix.plan(doc, ['a/one'])
        self.assertEqual(len(include), 2)
        self.assertEqual(skipped, [])

    def test_a_record_with_no_recorded_prompt_version_is_excluded_when_filtering(self):
        doc = {'records': [record('unknown', prompt_version=None)]}
        include, skipped = plan_matrix.plan(doc, ['a/one'],
                                            prompt_version='44f066e07f6b')
        self.assertEqual(include, [])
        self.assertIn('unknown', skipped)

    def test_severity_rides_along_for_weighting(self):
        doc = {'records': [record('a', severity='blocker')]}
        include, _ = plan_matrix.plan(doc, ['a/one'])
        self.assertEqual(include[0]['severity'], 'blocker')


class PlanMatrixRecordIdsFilter(unittest.TestCase):
    """--record-ids re-runs specific records (e.g. ones that timed out) without paying
    for the whole dataset — exercised via main() since the filtering happens there,
    before plan() ever sees the document."""

    def write_dataset(self, ids):
        path = Path(tempfile.mkdtemp()) / 'd.yaml'
        import yaml
        yaml.safe_dump({'records': [record(i) for i in ids]}, path.open('w'))
        return str(path)

    def test_keeps_only_the_named_records(self):
        dataset = self.write_dataset(['a', 'b', 'c'])
        out = Path(tempfile.mkdtemp()) / 'matrix.json'
        rc = plan_matrix.main(['--dataset', dataset, '--arms', 'x/one',
                               '--record-ids', 'a,c', '--out', str(out)])
        self.assertEqual(rc, 0)
        matrix = json.loads(out.read_text())
        self.assertEqual([j['record'] for j in matrix['include']], ['a', 'c'])

    def test_warns_but_does_not_fail_on_an_unknown_id(self):
        dataset = self.write_dataset(['a'])
        out = Path(tempfile.mkdtemp()) / 'matrix.json'
        rc = plan_matrix.main(['--dataset', dataset, '--arms', 'x/one',
                               '--record-ids', 'a,nonexistent', '--out', str(out)])
        self.assertEqual(rc, 0)
        matrix = json.loads(out.read_text())
        self.assertEqual([j['record'] for j in matrix['include']], ['a'])


class ParseVerdict(unittest.TestCase):
    PAYLOAD = ('{"baseline_findings": [{"summary": "s", "matched": true, '
               '"candidate_text": "t", "reason": "r"}], "extra_findings": []}')

    def test_reads_bare_json(self):
        self.assertTrue(judge.parse_verdict(self.PAYLOAD)['baseline_findings'][0]['matched'])

    def test_reads_json_inside_a_fenced_block(self):
        wrapped = f"here you go\n```json\n{self.PAYLOAD}\n```\nthanks"
        self.assertEqual(len(judge.parse_verdict(wrapped)['baseline_findings']), 1)

    def test_raises_rather_than_scoring_zero_on_unparseable_output(self):
        # A judging failure must not look like a candidate that found nothing.
        with self.assertRaises(ValueError):
            judge.parse_verdict('I could not complete that request.')

    def test_raises_when_the_shape_is_wrong(self):
        with self.assertRaises(ValueError):
            judge.parse_verdict('{"baseline_findings": "all of them"}')


class Score(unittest.TestCase):
    def verdict(self, matched):
        return {'baseline_findings': [{'matched': m} for m in matched],
                'extra_findings': [{'summary': 'x'}]}

    def test_recall_counts_matched_over_sought(self):
        s = judge.score(self.verdict([True, False, True, True]), 'blocking')
        self.assertEqual((s['baseline_count'], s['matched_count'], s['missed_count']),
                         (4, 3, 1))
        self.assertAlmostEqual(s['recall'], 0.75)

    def test_weighting_favours_blockers(self):
        blocker = judge.score(self.verdict([True]), 'blocker')
        nit = judge.score(self.verdict([True]), 'non-blocking')
        self.assertGreater(blocker['weighted_total'], nit['weighted_total'])

    def test_extra_findings_are_counted_but_not_penalised(self):
        s = judge.score(self.verdict([True]), 'blocking')
        self.assertEqual(s['extra_count'], 1)
        self.assertAlmostEqual(s['recall'], 1.0)

    def test_recall_is_none_when_the_baseline_had_no_findings(self):
        self.assertIsNone(judge.score({'baseline_findings': []}, 'blocking')['recall'])


class Aggregate(unittest.TestCase):
    def verdicts(self, arm, rows):
        return {(arm, f'r{i}'): {
            'record': f'r{i}', 'arm': arm, 'severity': sev,
            'score': {'baseline_count': b, 'matched_count': m, 'missed_count': b - m,
                      'extra_count': 0, 'weight': judge.WEIGHTS[sev],
                      'weighted_total': b * judge.WEIGHTS[sev],
                      'weighted_matched': m * judge.WEIGHTS[sev]}}
            for i, (b, m, sev) in enumerate(rows)}

    def test_recall_sums_findings_rather_than_averaging_records(self):
        # 1/1 and 1/9 is 2/10, not the 55% an average of ratios would give.
        summary = summarize.aggregate(self.verdicts('luna', [(1, 1, 'blocking'),
                                                             (9, 1, 'blocking')]))
        self.assertAlmostEqual(summary['luna']['recall'], 0.2)

    def test_splits_by_severity(self):
        summary = summarize.aggregate(self.verdicts('luna', [(2, 2, 'blocker'),
                                                             (4, 1, 'non-blocking')]))
        by = summary['luna']['by_severity']
        self.assertEqual(by['blocker'], {'baseline': 2, 'matched': 2})
        self.assertEqual(by['non-blocking'], {'baseline': 4, 'matched': 1})

    def test_weighted_recall_differs_when_blockers_are_missed(self):
        summary = summarize.aggregate(self.verdicts('luna', [(2, 0, 'blocker'),
                                                             (2, 2, 'non-blocking')]))
        arm = summary['luna']
        self.assertAlmostEqual(arm['recall'], 0.5)
        self.assertLess(arm['weighted_recall'], arm['recall'])


class Render(unittest.TestCase):
    def summary(self, arms):
        return summarize.aggregate({
            (arm, 'r1'): {'record': 'r1', 'arm': arm, 'severity': 'blocking',
                          'score': {'baseline_count': 10, 'matched_count': m,
                                    'missed_count': 10 - m, 'extra_count': 0,
                                    'weighted_total': 20.0, 'weighted_matched': m * 2.0}}
            for arm, m in arms.items()})

    def test_refuses_to_conclude_from_a_single_arm(self):
        out = summarize.render(self.summary({'luna': 6}))
        self.assertIn('cannot be interpreted', out)

    def test_reports_the_gap_against_the_control(self):
        out = summarize.render(self.summary({'luna': 6, 'sol': 8}), baseline_arm='sol')
        self.assertIn('vs control', out)
        self.assertIn('-20.0 points', out)

    def test_names_the_control_ceiling_rather_than_implying_100(self):
        out = summarize.render(self.summary({'luna': 6, 'sol': 8}), baseline_arm='sol')
        self.assertIn('80.0%', out)
        self.assertIn('ceiling', out)

    def test_lists_failed_replays_as_excluded(self):
        out = summarize.render(self.summary({'luna': 6, 'sol': 8}), 'sol', ['repo-pr9 luna'])
        self.assertIn('excluded from recall', out)


class ConversationCutoff(unittest.TestCase):
    """The replay must not see comments written after the review — BiggiePockets posts its
    findings onto the PR, so a later comment can contain the answer."""

    CUTOFF = '2026-09-17T13:16:34Z'

    def test_keeps_a_comment_written_before_the_review(self):
        self.assertTrue(replay.before({'created_at': '2026-09-16T10:00:00Z'}, self.CUTOFF))

    def test_drops_a_comment_written_after_the_review(self):
        self.assertFalse(replay.before({'created_at': '2026-09-18T10:00:00Z'}, self.CUTOFF))

    def test_drops_a_comment_with_no_timestamp(self):
        self.assertFalse(replay.before({'body': 'x'}, self.CUTOFF))

    def test_reads_submitted_at_for_reviews(self):
        self.assertTrue(replay.before({'submitted_at': '2026-09-01T00:00:00Z'}, self.CUTOFF))


class LoadRecord(unittest.TestCase):
    def test_finds_a_record_by_id(self):
        path = Path(tempfile.mkdtemp()) / 'd.yaml'
        import yaml
        path.write_text(yaml.safe_dump({'records': [record('wanted')]}))
        self.assertEqual(replay.load_record(str(path), 'wanted')['id'], 'wanted')

    def test_exits_when_the_record_is_missing(self):
        path = Path(tempfile.mkdtemp()) / 'd.yaml'
        import yaml
        path.write_text(yaml.safe_dump({'records': [record('other')]}))
        with self.assertRaises(SystemExit):
            replay.load_record(str(path), 'wanted')


class LoadResults(unittest.TestCase):
    def test_keys_verdicts_by_arm_and_record(self):
        directory = Path(tempfile.mkdtemp())
        (directory / 'a.json').write_text(json.dumps(
            {'record': 'r1', 'arm': 'luna', 'score': {}}))
        (directory / 'skip.txt').write_text('not json')
        results = summarize.load_results(str(directory))
        self.assertEqual(list(results), [('luna', 'r1')])

    def test_ignores_unreadable_files(self):
        directory = Path(tempfile.mkdtemp())
        (directory / 'bad.json').write_text('{ not json')
        self.assertEqual(summarize.load_results(str(directory)), {})


if __name__ == '__main__':
    unittest.main()
