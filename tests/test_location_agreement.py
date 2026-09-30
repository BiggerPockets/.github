from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/gym'))
import location_agreement as la  # noqa: E402


def finding(path, category='correctness'):
    return {'category': category, 'locations': [{'path': path, 'start_line': 10}]}


RECORD = {'id': 'r1',
          'expected_output': {'structured_findings': [finding('a.rb'), finding('b.rb'),
                                                      finding('c.rb')]},
          'metadata': {'confirmation': {'statuses': ['fixed', 'untouched', 'fixed']}}}


def result(verdicts, candidates):
    return {'record': 'r1', 'findings': 'prose',
            'judge_verdict': {'baseline_findings': [{'matched': v} for v in verdicts]},
            'structured_findings': candidates}


class Tally(unittest.TestCase):
    def test_pairs_verdicts_given_for_every_recorded_finding(self):
        counts = la.tally({'r1': RECORD}, [result([True, True, False],
                                                  [finding('a.rb', 'data')])])
        self.assertEqual(counts['location'], {(True, True): 1, (False, False): 1})
        self.assertEqual(counts['location and category'],
                         {(True, False): 1, (False, False): 1})

    def test_pairs_verdicts_given_for_the_confirmed_findings_only(self):
        counts = la.tally({'r1': RECORD}, [result([False, True], [finding('c.rb')])])
        self.assertEqual(counts['location'], {(False, False): 1, (True, True): 1})

    def test_skips_a_result_whose_verdicts_do_not_pair(self):
        counts = la.tally({'r1': RECORD}, [result([True], [finding('a.rb')])])
        self.assertEqual(sum(counts['location'].values()), 0)


if __name__ == '__main__':
    unittest.main()
