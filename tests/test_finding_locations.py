import json
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/gym'))
import finding_locations as fl  # noqa: E402


def report(findings_json):
    return f'## Findings\n\n- **Blocking** prose\n\n```findings\n{findings_json}\n```\n'


def link(path, line):
    return f'[{path}](/home/runner/work/repo/repo/{path}:{line})'


EXPECTED = (f'- **Blocking** {link("app/a.rb", 20)} races\n\n'
            f'- **Blocking** {link("app/b.rb", 5)} leaks\n')


class Structured(unittest.TestCase):
    def test_reads_the_findings_block(self):
        self.assertEqual(fl.structured(report('[{"summary": "s"}]')), [{'summary': 's'}])

    def test_reads_an_indented_block(self):
        text = '  ```findings\n  []\n  ```\n'
        self.assertEqual(fl.structured(text), [])

    def test_a_report_without_a_block_or_with_broken_json_has_none(self):
        self.assertIsNone(fl.structured('- **Blocking** prose only'))
        self.assertIsNone(fl.structured(report('[{"summary": ')))
        self.assertIsNone(fl.structured(report('{"summary": "s"}')))


class LocationMatches(unittest.TestCase):
    def matches(self, locations):
        finding = {'category': 'correctness', 'locations': locations}
        return fl.location_matches(EXPECTED, report(json.dumps([finding])))

    def test_an_overlapping_range_in_the_same_file_matches(self):
        self.assertEqual(self.matches([{'path': 'app/a.rb', 'start_line': 15,
                                        'end_line': 18}]), [True, False])

    def test_a_range_beyond_slop_or_in_another_file_does_not(self):
        self.assertEqual(self.matches([{'path': 'app/a.rb', 'start_line': 30}]),
                         [False, False])
        self.assertEqual(self.matches([{'path': 'app/c.rb', 'start_line': 20}]),
                         [False, False])

    def test_a_location_without_lines_matches_the_whole_file(self):
        self.assertEqual(self.matches([{'path': 'app/b.rb'}]), [False, True])

    def test_any_of_several_locations_can_match(self):
        self.assertEqual(self.matches([{'path': 'app/c.rb', 'start_line': 1},
                                       {'path': 'app/b.rb', 'start_line': 6}]),
                         [False, True])

    def test_an_unstructured_candidate_has_no_location_match(self):
        self.assertIsNone(fl.location_matches(EXPECTED, '- prose only'))


class LocationScore(unittest.TestCase):
    CANDIDATE = report('[{"locations": [{"path": "app/a.rb", "start_line": 20}]}]')

    def test_counts_matches_and_agreement_with_the_judge(self):
        verdict = {'baseline_findings': [{'matched': True}, {'matched': True}]}
        score = fl.location_score(EXPECTED, self.CANDIDATE, verdict)
        self.assertEqual((score['matched_count'], score['baseline_count']), (1, 2))
        self.assertEqual(score['agrees_with_judge'], 1)

    def test_agreement_is_none_when_the_judge_split_the_findings_differently(self):
        verdict = {'baseline_findings': [{'matched': True}]}
        self.assertIsNone(fl.location_score(EXPECTED, self.CANDIDATE,
                                            verdict)['agrees_with_judge'])

    def test_an_unstructured_candidate_is_marked(self):
        self.assertEqual(fl.location_score(EXPECTED, 'prose', {}), {'structured': False})


class PromptCategories(unittest.TestCase):
    def test_the_prompt_offers_exactly_the_known_categories(self):
        prompt = (ROOT / 'prompts/first-pass.md').read_text()
        section = prompt[prompt.index('`category`'):prompt.index('`locations`:')]
        self.assertEqual(set(re.findall(r'`([a-z-]+)`', section)) - {'category'},
                         fl.CATEGORIES)


if __name__ == '__main__':
    unittest.main()
