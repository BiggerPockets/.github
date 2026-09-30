import json
from pathlib import Path
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/gym'))
import rescore_results as rescore  # noqa: E402


def record(statuses):
    return {'id': 'repo-pr1', 'metadata': {'confirmation': {
        'method': 'm', 'labeled_on': '2026-09-29', 'statuses': statuses}}}


def result(matched, severity='blocking'):
    return {'record': 'repo-pr1', 'severity': severity,
            'judge_verdict': {'baseline_findings': [{'matched': m} for m in matched]},
            'score': {'recall': sum(matched) / len(matched)}}


class ConfirmedScore(unittest.TestCase):
    def test_counts_only_the_fixed_findings(self):
        score = rescore.confirmed_score(result([True, False, False]),
                                        record(['fixed', 'untouched', 'fixed']))
        self.assertEqual((score['matched_count'], score['baseline_count']), (1, 2))
        self.assertEqual(score['recall'], 0.5)
        self.assertEqual(score['weighted_total'], 4.0)

    def test_a_record_with_no_fixed_finding_is_not_rescored(self):
        self.assertIsNone(rescore.confirmed_score(result([True]), record(['untouched'])))

    def test_a_result_without_a_verdict_is_not_rescored(self):
        self.assertIsNone(rescore.confirmed_score({'record': 'repo-pr1'}, record(['fixed'])))

    def test_a_verdict_that_does_not_pair_with_the_statuses_is_not_rescored(self):
        self.assertIsNone(rescore.confirmed_score(result([True, True]), record(['fixed'])))


class Main(unittest.TestCase):
    def test_writes_confirmed_score_beside_the_original(self):
        root = Path(tempfile.mkdtemp())
        dataset = root / 'd.yaml'
        dataset.write_text(yaml.safe_dump({'records': [record(['fixed', 'untouched'])]}))
        run = root / 'results' / '1-1-model'
        run.mkdir(parents=True)
        (run / 'repo-pr1.json').write_text(json.dumps(result([False, True])))
        self.assertEqual(rescore.main(['--dataset', str(dataset),
                                       '--results', str(root / 'results')]), 0)
        saved = json.loads((run / 'repo-pr1.json').read_text())
        self.assertEqual(saved['confirmed_score']['recall'], 0.0)
        self.assertEqual(saved['score']['recall'], 0.5)


if __name__ == '__main__':
    unittest.main()
