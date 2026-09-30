import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/gym'))
import structure_findings as sf  # noqa: E402

PROSE = '- **Blocking** [app/a.rb](/home/runner/work/r/r/app/a.rb:20) races'


def entry(**overrides):
    return {'severity': 'blocking', 'categories': ['correctness'],
            'locations': [{'path': 'app/a.rb', 'start_line': 20, 'end_line': 22}],
            'summary': 's', **overrides}


class Parse(unittest.TestCase):
    def test_keeps_a_location_the_prose_cites(self):
        self.assertEqual(sf.parse(json.dumps([entry()]), [PROSE])[0]['locations'],
                         [{'start_line': 20, 'end_line': 22, 'path': 'app/a.rb'}])

    def test_drops_a_location_the_prose_never_mentions(self):
        restated = sf.parse(json.dumps([entry(locations=[{'path': 'app/made_up.rb'}])]),
                            [PROSE])
        self.assertEqual(restated[0]['locations'], [])

    def test_strips_the_runner_checkout_from_a_path(self):
        location = {'path': '/home/runner/work/r/r/app/a.rb', 'start_line': 20}
        restated = sf.parse(json.dumps([entry(locations=[location])]), [PROSE])
        self.assertEqual(restated[0]['locations'][0]['path'], 'app/a.rb')

    def test_keeps_at_most_two_known_categories(self):
        restated = sf.parse(json.dumps([entry(categories=[
            'privacy', 'style', 'privacy', 'security', 'correctness'])]), [PROSE])
        self.assertEqual(restated[0]['categories'], ['privacy', 'security'])

    def test_blanks_an_unknown_severity(self):
        restated = sf.parse(json.dumps([entry(severity='high')]), [PROSE])
        self.assertIsNone(restated[0]['severity'])

    def test_rejects_a_response_that_does_not_pair_one_to_one(self):
        with self.assertRaises(ValueError):
            sf.parse(json.dumps([entry(), entry()]), [PROSE])

    def test_reads_the_categories_from_the_first_pass_prompt(self):
        self.assertIn('`maintainability`', sf.block_instructions())


class StructureDataset(unittest.TestCase):
    def test_restates_each_record_once(self):
        path = Path(tempfile.mkdtemp()) / 'd.yaml'
        record = {'id': 'r1', 'expected_output': {'findings': PROSE + '\n'}, 'metadata': {}}
        path.write_text(yaml.safe_dump({'records': [record]}))
        with mock.patch.object(sf, 'call_model', return_value=json.dumps([entry()])) as call:
            first = sf.structure_dataset(str(path), 'm/x', 'key', sf.datetime.date(2026, 9, 30))
            second = sf.structure_dataset(str(path), 'm/x', 'key', sf.datetime.date(2026, 9, 30))
        self.assertEqual((first['restated'], second['skipped'], call.call_count), (1, 1, 1))
        saved = yaml.safe_load(path.read_text())['records'][0]
        self.assertEqual(saved['expected_output']['structured_findings'][0]['categories'],
                         ['correctness'])
        self.assertEqual(saved['metadata']['structuring']['model'], 'm/x')


class StructureResults(unittest.TestCase):
    def test_skips_a_review_that_already_has_a_findings_block(self):
        run = Path(tempfile.mkdtemp()) / '1-1-m'
        run.mkdir()
        (run / 'old.json').write_text(json.dumps({'findings': PROSE}))
        (run / 'new.json').write_text(json.dumps({'findings': '```findings\n[]\n```\n'}))
        with mock.patch.object(sf, 'call_model', return_value=json.dumps([entry()])):
            tally = sf.structure_results(str(run.parent), 'm/x', 'key')
        self.assertEqual((tally['restated'], tally['skipped']), (1, 1))
        self.assertIn('structured_findings', json.loads((run / 'old.json').read_text()))


if __name__ == '__main__':
    unittest.main()
