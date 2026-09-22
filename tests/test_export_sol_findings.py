import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/gym/export_sol_findings.py'
spec = importlib.util.spec_from_file_location('export_sol_findings', SCRIPT)
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)


def span(findings='## Findings\n\n- **Blocking — thing is broken.**', repo='org/repo',
         pr='123', verdict='request_changes', lines='4', arm='control',
         start_ns=1789707119209000000, messages=None, trace='t1', span_id='s1',
         run_id=None):
    output = {'messages': messages} if messages is not None else {'value': findings}
    return {
        'trace_id': trace,
        'span_id': span_id,
        'start_ns': start_ns,
        'attributes': {
            'meta': {'output': output},
            'tags': [
                f'repo:{repo}', f'pr:{pr}', f'verdict:{verdict}',
                f'codex_findings_lines:{lines}', f'arm:{arm}',
                f'run_id:{run_id or f"{repo}-pr{pr}-35135689540"}',
                'codex_model:openai/gpt-5.6-sol',
            ],
        },
    }


class TagMap(unittest.TestCase):
    def test_keeps_colons_inside_a_tag_value(self):
        tags = export.tag_map(['codex_model:openai/gpt-5.6-sol', 'pr:1'])
        self.assertEqual(tags['codex_model'], 'openai/gpt-5.6-sol')

    def test_first_occurrence_wins_for_a_repeated_key(self):
        self.assertEqual(export.tag_map(['arm:control', 'arm:thesis-first'])['arm'],
                         'control')


class FindingsText(unittest.TestCase):
    def test_reads_a_plain_value_output(self):
        self.assertIn('broken', export.findings_text(span()))

    def test_falls_back_to_the_last_message_with_content(self):
        text = export.findings_text(span(messages=[
            {'role': 'assistant', 'content': ''},
            {'role': 'assistant', 'content': '## Findings\n\n- Blocking thing'},
        ]))
        self.assertIn('Blocking thing', text)

    def test_is_empty_when_the_span_carries_no_output(self):
        blank = span()
        blank['attributes']['meta']['output'] = {}
        self.assertEqual(export.findings_text(blank), '')


class Severity(unittest.TestCase):
    def test_p0_is_a_blocker(self):
        self.assertEqual(export.severity('- **[P0] Seat is a no-op**'), 'blocker')

    def test_blocking_prose_is_blocking(self):
        self.assertEqual(export.severity('- **Blocking — leak**'), 'blocking')

    def test_everything_else_is_non_blocking(self):
        self.assertEqual(export.severity('- Consider renaming this'), 'non-blocking')


class Redact(unittest.TestCase):
    def test_scrubs_an_email_address(self):
        self.assertEqual(export.redact('mail someone@example.com now'),
                         'mail [redacted-email] now')

    def test_leaves_code_references_alone(self):
        text = 'app/models/event.rb:24 omits `ends_at`'
        self.assertEqual(export.redact(text), text)


class RunNumber(unittest.TestCase):
    def test_takes_the_trailing_run_id(self):
        self.assertEqual(
            export.run_number('biggerpockets/biggerpockets-pr31230-35308532984'),
            '35308532984')

    def test_survives_a_hyphen_in_the_repo_name(self):
        self.assertEqual(
            export.run_number('biggerpockets/pockets-app-pr17-34398864466'),
            '34398864466')

    def test_is_none_when_the_tag_is_absent_or_malformed(self):
        self.assertIsNone(export.run_number(None))
        self.assertIsNone(export.run_number('biggerpockets/repo-pr17-nope'))


class ResolveCommit(unittest.TestCase):
    """Pinning is what makes a record replayable; these lock the lookup's contract
    rather than GitHub's responses."""

    def setUp(self):
        self.calls = []
        self.original = export._gh
        self.addCleanup(setattr, export, '_gh', self.original)

    def stub(self, responses):
        def _gh(path, jq):
            self.calls.append(path)
            for fragment, value in responses.items():
                if fragment in path:
                    return value
            return None
        export._gh = _gh

    def test_reads_head_from_the_run_and_base_from_the_pull_request(self):
        self.stub({'/actions/runs/': 'abc123',
                   '/pulls/': '{"ref": "main", "sha": "def456"}'})
        commit = export.resolve_commit('org/repo', 7, 'org/repo-pr7-99', {})
        self.assertEqual(commit, {'head_sha': 'abc123', 'base_ref': 'main',
                                  'base_sha': 'def456'})

    def test_caches_one_lookup_per_run(self):
        self.stub({'/actions/runs/': 'abc123',
                   '/pulls/': '{"ref": "main", "sha": "def456"}'})
        cache = {}
        export.resolve_commit('org/repo', 7, 'org/repo-pr7-99', cache)
        export.resolve_commit('org/repo', 7, 'org/repo-pr7-99', cache)
        self.assertEqual(len(self.calls), 2)

    def test_returns_a_null_head_when_the_run_is_gone(self):
        self.stub({})
        commit = export.resolve_commit('org/repo', 7, 'org/repo-pr7-99', {})
        self.assertIsNone(commit['head_sha'])

    def test_skips_the_pull_request_lookup_without_a_head(self):
        self.stub({})
        export.resolve_commit('org/repo', 7, 'org/repo-pr7-99', {})
        self.assertFalse(any('/pulls/' in call for call in self.calls))

    def test_tolerates_unparseable_base_json(self):
        self.stub({'/actions/runs/': 'abc123', '/pulls/': 'not json'})
        commit = export.resolve_commit('org/repo', 7, 'org/repo-pr7-99', {})
        self.assertEqual(commit['head_sha'], 'abc123')
        self.assertNotIn('base_sha', commit)

    def test_does_nothing_without_a_run_id(self):
        self.stub({'/actions/runs/': 'abc123'})
        self.assertEqual(export.resolve_commit('org/repo', 7, None, {}), {})
        self.assertEqual(self.calls, [])


class ToRecord(unittest.TestCase):
    def test_builds_a_record_with_input_expected_output_and_metadata(self):
        record = export.to_record(span(), 'openai/gpt-5.6-sol')
        self.assertEqual(record['id'], 'repo-pr123')
        self.assertEqual(record['input']['repo'], 'org/repo')
        self.assertEqual(record['input']['pr'], 123)
        self.assertEqual(record['input']['instruction'],
                         'Review pr:123 against ticket.json/pr.diff')
        self.assertIn('broken', record['expected_output']['findings'])
        self.assertEqual(record['metadata']['stage2_verdict'], 'request_changes')
        self.assertEqual(record['metadata']['findings_lines'], 4)

    def test_reads_the_prompt_version_and_line_count_under_either_tag_name(self):
        # A trailing export window spans the tag rename, so both names appear in it.
        renamed = span()
        renamed['attributes']['tags'] = [
            t for t in renamed['attributes']['tags']
            if not t.startswith('codex_findings_lines:')
        ] + ['first_pass_findings_lines:7', 'first_pass_prompt_version:abc123def456']
        record = export.to_record(renamed, 'openai/gpt-5.6-sol')
        self.assertEqual(record['metadata']['findings_lines'], 7)
        self.assertEqual(record['metadata']['codex_prompt_version'], 'abc123def456')

    def test_still_reads_the_older_tag_names(self):
        legacy = span()
        legacy['attributes']['tags'].append('codex_prompt_version:beefbeefbeef')
        record = export.to_record(legacy, 'openai/gpt-5.6-sol')
        self.assertEqual(record['metadata']['findings_lines'], 4)
        self.assertEqual(record['metadata']['codex_prompt_version'], 'beefbeefbeef')

    def test_findings_are_emitted_as_a_yaml_block(self):
        record = export.to_record(span(), 'openai/gpt-5.6-sol')
        self.assertIsInstance(record['expected_output']['findings'], export.Block)

    def test_tags_carry_the_source_model_and_severity(self):
        record = export.to_record(span(findings='- **[P0] boom**'), 'openai/gpt-5.6-sol')
        self.assertIn('source_model:openai/gpt-5.6-sol', record['tags'])
        self.assertIn('severity:blocker', record['tags'])

    def test_drops_a_pass_that_reported_nothing(self):
        text = '## Findings\n\nNo blocking findings. The PR satisfies the ticket.'
        self.assertIsNone(export.to_record(span(findings=text), 'm'))

    def test_drops_a_span_without_a_repo_or_pr(self):
        bare = span()
        bare['attributes']['tags'] = ['codex_model:openai/gpt-5.6-sol']
        self.assertIsNone(export.to_record(bare, 'm'))

    def test_redacts_the_findings_text(self):
        record = export.to_record(
            span(findings='- **Blocking** mail to a@b.com leaks'), 'm')
        self.assertNotIn('a@b.com', record['expected_output']['findings'])

    def test_pins_the_reviewed_commit_into_input(self):
        record = export.to_record(span(), 'm', commit={
            'head_sha': 'abc123', 'base_ref': 'main', 'base_sha': 'def456'})
        self.assertEqual(record['input']['head_sha'], 'abc123')
        self.assertEqual(record['input']['base_ref'], 'main')
        self.assertEqual(record['input']['base_sha'], 'def456')

    def test_carries_null_commit_fields_when_nothing_resolved(self):
        # The keys stay present so every row keeps one shape; an experiment can then
        # test `head_sha` for None instead of probing for a missing key.
        record = export.to_record(span(), 'm')
        self.assertIsNone(record['input']['head_sha'])
        self.assertIn('base_sha', record['input'])

    def test_keeps_the_run_id_for_commit_resolution(self):
        record = export.to_record(span(), 'm')
        self.assertEqual(record['_run_id'], 'org/repo-pr123-35135689540')


class RestResponseShape(unittest.TestCase):
    """The spans search API nests ids and timing under `attributes`, unlike the
    flattened view other Datadog surfaces present. Reading only the flat form would
    silently produce records with no trace link and no `reviewed_at`."""

    def rest_span(self):
        return {
            'id': 'AAA',
            'type': 'span',
            'attributes': {
                'trace_id': 'trace-abc',
                'span_id': 'span-abc',
                'start_ns': 1789707119209000000,
                'meta': {'output': {'messages': [
                    {'role': 'assistant', 'content': '- **Blocking** leak'}]}},
                'tags': ['repo:org/repo', 'pr:7', 'verdict:request_changes',
                         'codex_findings_lines:2', 'arm:control'],
            },
        }

    def test_reads_ids_nested_under_attributes(self):
        record = export.to_record(self.rest_span(), 'm')
        self.assertEqual(record['metadata']['trace_id'], 'trace-abc')
        self.assertEqual(record['metadata']['span_id'], 'span-abc')

    def test_reads_start_ns_nested_under_attributes(self):
        record = export.to_record(self.rest_span(), 'm')
        self.assertEqual(record['metadata']['reviewed_at'], '2026-09-18T04:51:59+00:00')

    def test_reads_findings_from_a_nested_message_list(self):
        record = export.to_record(self.rest_span(), 'm')
        self.assertIn('leak', record['expected_output']['findings'])


class Dedupe(unittest.TestCase):
    def test_keeps_the_most_recent_pass_for_one_pull_request(self):
        old = export.to_record(span(findings='- **Blocking** old', start_ns=1), 'm')
        new = export.to_record(span(findings='- **Blocking** new', start_ns=2), 'm')
        records = export.dedupe([old, new])
        self.assertEqual(len(records), 1)
        self.assertIn('new', records[0]['expected_output']['findings'])

    def test_keeps_the_same_pr_number_in_different_repos(self):
        records = export.dedupe([
            export.to_record(span(repo='org/a'), 'm'),
            export.to_record(span(repo='org/b'), 'm'),
        ])
        self.assertEqual(len(records), 2)

    def test_strips_the_internal_sort_fields(self):
        record = export.dedupe([export.to_record(span(), 'm')])[0]
        self.assertNotIn('_sort_key', record)
        self.assertNotIn('_start_ns', record)

    def test_orders_by_repo_then_numeric_pr(self):
        records = export.dedupe([
            export.to_record(span(pr='30'), 'm'),
            export.to_record(span(pr='9'), 'm'),
        ])
        self.assertEqual([r['input']['pr'] for r in records], [9, 30])


class BuildQuery(unittest.TestCase):
    def test_requires_a_verdict_by_default(self):
        query = export.build_query('openai/gpt-5.6-sol', 'pi.first_pass',
                                   'biggiepockets-review', 'request_changes')
        self.assertIn('verdict:request_changes', query)
        self.assertIn('-first_pass_findings_lines:0', query)
        self.assertIn('@status:ok', query)

    def test_matches_the_model_under_either_tag_name(self):
        query = export.build_query('openai/gpt-5.6-sol', 'pi.first_pass', 'app', 'any')
        self.assertIn('(first_pass_model:openai/gpt-5.6-sol '
                      'OR codex_model:openai/gpt-5.6-sol)', query)
        self.assertIn('-codex_findings_lines:0', query)

    def test_any_verdict_drops_the_verdict_filter(self):
        query = export.build_query('m', 'pi.first_pass', 'app', 'any')
        self.assertNotIn('verdict:', query)


if __name__ == '__main__':
    unittest.main()
