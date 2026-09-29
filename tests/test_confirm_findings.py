import datetime
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import yaml

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/gym/confirm_findings.py'
spec = importlib.util.spec_from_file_location('confirm_findings', SCRIPT)
confirm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(confirm)

REVIEWED, FINAL = 'a' * 40, 'f' * 40
TODAY = datetime.date(2026, 9, 29)


def lines(n, changed=None):
    """n numbered lines, with the 1-based line numbers in `changed` rewritten."""
    return ''.join(f'line {i}{" fixed" if i in (changed or ()) else ""}\n'
                   for i in range(1, n + 1))


class FakeGitHub:
    def __init__(self, pull, files):
        self._pull, self._files = pull, files

    def pull(self, repo, pr):
        return self._pull

    def file(self, repo, path, sha):
        return self._files.get((path, sha))


def merged(head=FINAL):
    return {'state': 'closed', 'merged': True, 'head_sha': head}


def record(findings):
    return {'id': 'repo-pr1',
            'input': {'repo': 'org/repo', 'pr': 1, 'head_sha': REVIEWED},
            'expected_output': {'findings': findings},
            'metadata': {'severity': 'blocking'}}


def link(path, line=None):
    return f'[{path}](/home/runner/work/repo/repo/{path}{f":{line}" if line else ""})'


class SplitFindings(unittest.TestCase):
    def test_splits_top_level_items_and_keeps_their_continuation_lines(self):
        preamble, findings = confirm.split_findings(
            '## Findings\n\n- **Blocking** one\n  more of one\n\n- **Blocking** two\n'
            '\n**Verdict:** request changes.\n')
        self.assertEqual(preamble, '## Findings')
        self.assertEqual(findings, ['- **Blocking** one\n  more of one',
                                    '- **Blocking** two'])


class Citations(unittest.TestCase):
    def test_reads_a_runner_link_whose_path_holds_parentheses(self):
        self.assertEqual(confirm.citations(link('src/app/(tabs)/index.tsx', 598) + ' breaks'),
                         [('src/app/(tabs)/index.tsx', [(598, 598)])])

    def test_reads_a_runner_link_with_no_line(self):
        self.assertEqual(confirm.citations(link('app/me_controller.rb')),
                         [('app/me_controller.rb', None)])

    def test_reads_backticked_ranges(self):
        self.assertEqual(confirm.citations('**`.claude/SKILL.md:15-19,32-36`** stale'),
                         [('.claude/SKILL.md', [(15, 19), (32, 36)])])

    def test_reads_a_backticked_path_without_an_extension(self):
        self.assertEqual(confirm.citations('`.agents/setup:209`'),
                         [('.agents/setup', [(209, 209)])])


class Touches(unittest.TestCase):
    def test_a_change_within_slop_of_the_cited_line_counts(self):
        changes = confirm.changed_ranges(lines(40), lines(40, changed={23}))
        self.assertTrue(confirm.touches([(20, 20)], changes))

    def test_a_change_far_from_the_cited_line_does_not(self):
        changes = confirm.changed_ranges(lines(40), lines(40, changed={35}))
        self.assertFalse(confirm.touches([(20, 20)], changes))

    def test_an_insertion_beside_the_cited_line_counts(self):
        before = lines(40)
        after = before.replace('line 20\n', 'line 20\nguard\n')
        self.assertTrue(confirm.touches([(20, 20)], confirm.changed_ranges(before, after)))

    def test_a_citation_without_a_line_counts_any_change_to_the_file(self):
        self.assertTrue(confirm.touches(None, confirm.changed_ranges(lines(5), lines(5, {1}))))
        self.assertFalse(confirm.touches(None, confirm.changed_ranges(lines(5), lines(5))))


class LabelRecord(unittest.TestCase):
    def label(self, findings, pull, files):
        return confirm.label_record(record(findings), FakeGitHub(pull, files), TODAY)

    def test_keeps_only_the_findings_whose_cited_lines_changed(self):
        findings = (f'## Findings\n\n- **Blocking** {link("a.rb", 20)} fixed later\n\n'
                    f'- **Blocking** {link("b.rb", 5)} left alone\n')
        files = {('a.rb', REVIEWED): lines(40), ('a.rb', FINAL): lines(40, {20}),
                 ('b.rb', REVIEWED): lines(40), ('b.rb', FINAL): lines(40, {35})}
        labeled = self.label(findings, merged(), files)
        confirmed = labeled['expected_output']['confirmed_findings']
        self.assertIn('fixed later', confirmed)
        self.assertNotIn('left alone', confirmed)
        self.assertTrue(confirmed.startswith('## Findings'))
        self.assertEqual(labeled['metadata']['confirmation']['statuses'],
                         ['fixed', 'untouched'])
        self.assertEqual(labeled['expected_output']['findings'], findings)

    def test_a_deleted_cited_file_counts_as_fixed(self):
        files = {('a.rb', REVIEWED): lines(10)}
        labeled = self.label(f'- {link("a.rb", 3)}\n', merged(), files)
        self.assertEqual(labeled['metadata']['confirmation']['fixed'], 1)

    def test_merged_at_the_reviewed_commit_fixes_nothing(self):
        files = {('a.rb', REVIEWED): lines(10)}
        labeled = self.label(f'- {link("a.rb", 3)}\n', merged(head=REVIEWED), files)
        self.assertEqual(labeled['metadata']['confirmation']['statuses'], ['untouched'])
        self.assertEqual(labeled['expected_output']['confirmed_findings'], '')

    def test_an_unmerged_pull_request_leaves_every_finding_unknown(self):
        files = {('a.rb', REVIEWED): lines(10), ('a.rb', FINAL): lines(10, {3})}
        for pull in ({'state': 'closed', 'merged': False, 'head_sha': FINAL},
                     {'state': 'open', 'merged': False, 'head_sha': FINAL}):
            labeled = self.label(f'- {link("a.rb", 3)}\n', pull, files)
            self.assertEqual(labeled['metadata']['confirmation']['statuses'], ['unknown'])
            self.assertEqual(labeled['metadata']['confirmation']['pull_request'],
                             pull['state'])

    def test_a_finding_citing_no_readable_file_is_unknown(self):
        labeled = self.label(f'- {link("gone.rb", 3)}\n- no citation at all\n', merged(), {})
        self.assertEqual(labeled['metadata']['confirmation']['statuses'],
                         ['unknown', 'unknown'])


class Main(unittest.TestCase):
    def test_writes_the_labeled_dataset_as_literal_blocks(self):
        directory = Path(tempfile.mkdtemp())
        dataset = directory / 'd.yaml'
        findings = f'- **Blocking** {link("a.rb", 2)}\n'
        yaml.safe_dump({'version': 1, 'records': [record(findings)]}, dataset.open('w'))
        files = {('a.rb', REVIEWED): lines(5), ('a.rb', FINAL): lines(5, {2})}
        with mock.patch.object(confirm, 'GitHub', lambda: FakeGitHub(merged(), files)):
            self.assertEqual(confirm.main(['--dataset', str(dataset)]), 0)
        text = dataset.read_text()
        self.assertIn('confirmed_findings: |', text)
        saved = yaml.safe_load(text)['records'][0]
        self.assertEqual(saved['metadata']['confirmation']['fixed'], 1)


if __name__ == '__main__':
    unittest.main()
