import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan_matrix = load('plan_synthesis_matrix', 'scripts/gym/plan_synthesis_matrix.py')
judge = load('judge_synthesis', 'scripts/gym/judge_synthesis.py')
summarize = load('summarize_synthesis_run', 'scripts/gym/summarize_synthesis_run.py')


def record(rid='repo-pr1', pr=1, verdict='request_changes', head='a' * 40, base='b' * 40):
    return {
        'id': rid,
        'input': {'repo': 'org/repo', 'pr': pr, 'head_sha': head, 'base_sha': base,
                  'first_pass_findings': '- **Blocking** thing\n',
                  'instruction': f'Synthesize a review decision for pr:{pr}'},
        'expected_output': {'verdict': verdict, 'summary': 'Blocks on the thing.\n'},
        'metadata': {'reviewed_at': '2026-09-17T13:16:34+00:00'},
    }


class ArmLabel(unittest.TestCase):
    def test_strips_the_provider_and_punctuation(self):
        self.assertEqual(plan_matrix.arm_label('openai/gpt-5.6-luna'), 'gpt-5-6-luna')


class PlanSynthesisMatrix(unittest.TestCase):
    def doc(self, n=3, verdict='request_changes'):
        return {'records': [record(f'repo-pr{i}', pr=i, verdict=verdict)
                            for i in range(1, n + 1)]}

    def test_emits_one_job_per_record_per_arm(self):
        include, _ = plan_matrix.plan(self.doc(3), ['a/one', 'b/two'])
        self.assertEqual(len(include), 6)

    def test_limit_takes_the_first_n(self):
        include, _ = plan_matrix.plan(self.doc(5), ['a/one'], limit=2)
        self.assertEqual([j['record'] for j in include], ['repo-pr1', 'repo-pr2'])

    def test_carries_the_pinned_commit_and_expected_verdict(self):
        include, _ = plan_matrix.plan(self.doc(1), ['a/one'])
        self.assertEqual(include[0]['head_sha'], 'a' * 40)
        self.assertEqual(include[0]['expected_verdict'], 'request_changes')

    def test_skips_and_reports_unpinned_records(self):
        doc = self.doc(2)
        doc['records'][0]['input']['head_sha'] = None
        include, skipped = plan_matrix.plan(doc, ['a/one'])
        self.assertEqual(skipped, ['repo-pr1'])
        self.assertEqual([j['record'] for j in include], ['repo-pr2'])


class ParseVerdict(unittest.TestCase):
    PAYLOAD = ('{"baseline_concerns": [{"summary": "s", "matched": true, '
               '"candidate_text": "t", "reason": "r"}], "extra_concerns": []}')

    def test_reads_bare_json(self):
        self.assertTrue(judge.parse_verdict(self.PAYLOAD)['baseline_concerns'][0]['matched'])

    def test_reads_json_inside_a_fenced_block(self):
        wrapped = f"here you go\n```json\n{self.PAYLOAD}\n```\nthanks"
        self.assertEqual(len(judge.parse_verdict(wrapped)['baseline_concerns']), 1)

    def test_raises_rather_than_scoring_zero_on_unparseable_output(self):
        with self.assertRaises(ValueError):
            judge.parse_verdict('I could not complete that request.')

    def test_raises_when_the_shape_is_wrong(self):
        with self.assertRaises(ValueError):
            judge.parse_verdict('{"baseline_concerns": "all of them"}')


class CoverageScore(unittest.TestCase):
    def verdict(self, matched):
        return {'baseline_concerns': [{'matched': m} for m in matched],
                'extra_concerns': [{'summary': 'x'}]}

    def test_recall_counts_matched_over_sought(self):
        s = judge.coverage_score(self.verdict([True, False, True, True]))
        self.assertEqual((s['baseline_count'], s['matched_count'], s['missed_count']),
                         (4, 3, 1))
        self.assertAlmostEqual(s['recall'], 0.75)

    def test_extra_concerns_are_counted_but_not_penalised(self):
        s = judge.coverage_score(self.verdict([True]))
        self.assertEqual(s['extra_count'], 1)
        self.assertAlmostEqual(s['recall'], 1.0)

    def test_recall_is_none_when_the_baseline_had_no_concerns(self):
        self.assertIsNone(judge.coverage_score({'baseline_concerns': []})['recall'])


class Aggregate(unittest.TestCase):
    def result(self, arm, record_id, expected, actual, coverage=None):
        return {'record': record_id, 'arm': arm, 'expected_verdict': expected,
                'actual_verdict': actual, 'verdict_agreement': expected == actual,
                'coverage': coverage}

    def test_agreement_rate_over_records(self):
        results = {
            ('luna', 'r1'): self.result('luna', 'r1', 'approve', 'approve'),
            ('luna', 'r2'): self.result('luna', 'r2', 'request_changes', 'approve'),
        }
        summary = summarize.aggregate(results)
        self.assertAlmostEqual(summary['luna']['agreement_rate'], 0.5)

    def test_splits_agreement_by_the_baselines_own_verdict(self):
        results = {
            ('luna', 'r1'): self.result('luna', 'r1', 'approve', 'approve'),
            ('luna', 'r2'): self.result('luna', 'r2', 'request_changes', 'approve'),
        }
        by = summarize.aggregate(results)['luna']['by_baseline_verdict']
        self.assertEqual(by['approve'], {'total': 1, 'agreed': 1})
        self.assertEqual(by['request_changes'], {'total': 1, 'agreed': 0})

    def test_concern_recall_only_counts_records_with_coverage(self):
        results = {
            ('luna', 'r1'): self.result('luna', 'r1', 'approve', 'approve'),
            ('luna', 'r2'): self.result(
                'luna', 'r2', 'request_changes', 'request_changes',
                coverage={'baseline_count': 4, 'matched_count': 3}),
        }
        summary = summarize.aggregate(results)['luna']
        self.assertAlmostEqual(summary['concern_recall'], 0.75)


class Render(unittest.TestCase):
    def summary(self, arms):
        results = {}
        for arm, agreed_of_10 in arms.items():
            for i in range(10):
                agreed = i < agreed_of_10
                results[(arm, f'r{i}')] = {
                    'record': f'r{i}', 'arm': arm, 'expected_verdict': 'request_changes',
                    'actual_verdict': 'request_changes' if agreed else 'approve',
                    'verdict_agreement': agreed, 'coverage': None,
                }
        return summarize.aggregate(results)

    def test_refuses_to_conclude_from_a_single_arm(self):
        out = summarize.render(self.summary({'luna': 6}))
        self.assertIn('cannot be interpreted', out)

    def test_reports_the_gap_against_the_control(self):
        out = summarize.render(self.summary({'luna': 6, 'deepseek': 8}), baseline_arm='deepseek')
        self.assertIn('vs control', out)
        self.assertIn('-20.0 points', out)


if __name__ == '__main__':
    unittest.main()
