import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
import urllib.error

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/gym/upload_gym_dataset.py'
spec = importlib.util.spec_from_file_location('upload_gym_dataset', SCRIPT)
upload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upload)

DOCUMENT = """
version: 1
dataset:
  name: sol-first-pass-findings
  description: what sol caught
records:
- id: repo-pr1
  input:
    repo: org/repo
    pr: 1
    instruction: Review pr:1 against ticket.json/pr.diff
  expected_output:
    findings: |
      - **Blocking** thing is broken
  metadata:
    severity: blocking
    stage2_verdict: request_changes
  tags:
  - repo:org/repo
  - pr:1
"""


def write(text=DOCUMENT, name='dataset.yaml'):
    path = Path(tempfile.mkdtemp()) / name
    path.write_text(text)
    return str(path)


class Load(unittest.TestCase):
    def test_reads_records(self):
        document = upload.load(write())
        self.assertEqual(len(document['records']), 1)

    def test_rejects_a_file_with_no_records(self):
        with self.assertRaises(ValueError):
            upload.load(write('version: 1\ndataset:\n  name: empty\n'))


class ToApiRecords(unittest.TestCase):
    def setUp(self):
        self.records = upload.to_api_records(upload.load(write()))

    def test_keeps_input_and_expected_output_flat(self):
        record = self.records[0]
        self.assertEqual(record['input']['pr'], 1)
        self.assertIn('broken', record['expected_output']['findings'])

    def test_keeps_the_yaml_row_id_as_the_record_id(self):
        # Deduplication keys on this, so a re-upload reconciles instead of doubling
        # the dataset, and a failing result names the pull request it came from.
        self.assertEqual(self.records[0]['id'], 'repo-pr1')

    def test_sends_the_tags_from_the_file(self):
        self.assertEqual(self.records[0]['tags'], ['repo:org/repo', 'pr:1'])

    def test_leaves_metadata_as_the_file_wrote_it(self):
        self.assertEqual(self.records[0]['metadata'],
                         {'severity': 'blocking', 'stage2_verdict': 'request_changes'})

    def test_copies_metadata_rather_than_aliasing_the_document(self):
        document = upload.load(write())
        record = upload.to_api_records(document)[0]
        record['metadata']['severity'] = 'changed'
        self.assertEqual(document['records'][0]['metadata']['severity'], 'blocking')

    def test_every_record_has_the_same_keys(self):
        document = upload.load(write())
        document['records'].append(dict(document['records'][0], id='repo-pr2'))
        records = upload.to_api_records(document)
        self.assertEqual({frozenset(r) for r in records},
                         {frozenset(records[0])})

    def test_a_row_without_tags_still_carries_the_field(self):
        document = upload.load(write())
        del document['records'][0]['tags']
        self.assertEqual(upload.to_api_records(document)[0]['tags'], [])


class FindProject(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.original = upload.request_json
        self.addCleanup(setattr, upload, 'request_json', self.original)

    def stub(self, payload):
        def request_json(site, api_key, app_key, method, path, body=None):
            self.calls.append((method, path, body))
            return payload
        upload.request_json = request_json

    def test_returns_the_id_of_an_exact_name_match(self):
        self.stub({'data': [{'id': 'abc', 'attributes': {'name': 'gym'}}]})
        self.assertEqual(upload.find_project('datadoghq.com', 'k', 'a', 'gym'), 'abc')

    def test_ignores_a_near_miss_from_a_contains_filter(self):
        self.stub({'data': [{'id': 'abc', 'attributes': {'name': 'gym-staging'}}]})
        self.assertIsNone(upload.find_project('datadoghq.com', 'k', 'a', 'gym'))

    def test_returns_none_when_nothing_matches(self):
        self.stub({'data': []})
        self.assertIsNone(upload.find_project('datadoghq.com', 'k', 'a', 'gym'))

    def test_url_encodes_the_name(self):
        self.stub({'data': []})
        upload.find_project('datadoghq.com', 'k', 'a', 'a b')
        self.assertIn('a%20b', self.calls[0][1])


class ProjectDatasets(unittest.TestCase):
    """The org-wide datasets listing ignores a project filter, so membership can only be
    read from the route that carries the project in its path."""

    def setUp(self):
        self.calls = []
        self.original = upload.request_json
        self.addCleanup(setattr, upload, 'request_json', self.original)

    def stub(self, payload):
        def request_json(site, api_key, app_key, method, path, body=None):
            self.calls.append((method, path, body))
            return payload
        upload.request_json = request_json

    def test_maps_every_dataset_name_to_its_id(self):
        self.stub({'data': [{'id': 'd1', 'attributes': {'name': 'gym'}},
                            {'id': 'd2', 'attributes': {'name': 'other'}}]})
        self.assertEqual(upload.project_datasets('datadoghq.com', 'k', 'a', 'ours'),
                         {'gym': 'd1', 'other': 'd2'})

    def test_asks_the_project_scoped_route(self):
        self.stub({'data': []})
        upload.project_datasets('datadoghq.com', 'k', 'a', 'ours')
        self.assertEqual(self.calls[0][1], '/api/v2/llm-obs/v1/ours/datasets')

    def test_is_empty_when_the_project_holds_nothing(self):
        self.stub({'data': []})
        self.assertEqual(upload.project_datasets('datadoghq.com', 'k', 'a', 'ours'), {})

    def test_skips_an_entry_missing_a_name_or_an_id(self):
        self.stub({'data': [{'id': 'd1', 'attributes': {}},
                            {'attributes': {'name': 'gym'}}]})
        self.assertEqual(upload.project_datasets('datadoghq.com', 'k', 'a', 'ours'), {})


class EntityId(unittest.TestCase):
    def test_reads_an_id_from_a_single_object(self):
        self.assertEqual(upload.entity_id({'data': {'id': 'abc'}}), 'abc')

    def test_reads_an_id_from_a_list_of_one(self):
        self.assertEqual(upload.entity_id({'data': [{'id': 'abc'}]}), 'abc')

    def test_falls_back_to_a_top_level_id(self):
        self.assertEqual(upload.entity_id({'id': 'abc'}), 'abc')

    def test_is_none_when_the_response_carries_no_id(self):
        self.assertIsNone(upload.entity_id({'data': []}))


class CreateGuards(unittest.TestCase):
    """A create that yields no id, or a dataset that is not in the project afterwards,
    must stop the run. Sending the project in the body instead of the URL returns 200
    and files the dataset under the org's default project, which once put 97 rows where
    no experiment could see them."""

    def setUp(self):
        self.calls = []
        self.original = upload.request_json
        self.addCleanup(setattr, upload, 'request_json', self.original)
        self.original_sleep = upload.time.sleep
        upload.time.sleep = lambda seconds: None
        self.addCleanup(setattr, upload.time, 'sleep', self.original_sleep)

    def stub(self, responses):
        def request_json(site, api_key, app_key, method, path, body=None):
            self.calls.append(f'{method} {path}')
            for fragment, payload in responses.items():
                if fragment in f'{method} {path}':
                    return payload
            return {}
        upload.request_json = request_json

    def test_project_create_without_an_id_raises(self):
        self.stub({'POST': {'data': {}}})
        with self.assertRaises(upload.DatadogError):
            upload.create_project('datadoghq.com', 'k', 'a', 'gym')

    def test_dataset_create_without_an_id_raises(self):
        self.stub({'POST': {'data': {}}})
        with self.assertRaises(upload.DatadogError):
            upload.create_dataset('datadoghq.com', 'k', 'a', 'ours', 'gym', '')

    def test_creates_under_the_project_scoped_route(self):
        self.stub({'POST': {'data': {'id': 'ds1'}},
                   'GET': {'data': [{'id': 'ds1', 'attributes': {'name': 'gym'}}]}})
        upload.create_dataset('datadoghq.com', 'k', 'a', 'ours', 'gym', '')
        self.assertIn('POST /api/unstable/llm-obs/v1/ours/datasets', self.calls)

    def test_a_dataset_absent_from_the_project_afterwards_raises(self):
        self.stub({'POST': {'data': {'id': 'ds1'}},
                   'GET': {'data': [{'id': 'elsewhere',
                                     'attributes': {'name': 'gym'}}]}})
        with self.assertRaises(upload.DatadogError) as caught:
            upload.create_dataset('datadoghq.com', 'k', 'a', 'ours', 'gym', '')
        self.assertIn('ds1', str(caught.exception))

    def test_an_unreadable_create_response_falls_back_to_a_lookup_by_name(self):
        self.stub({'POST': {'data': {}},
                   'GET': {'data': [{'id': 'ds1', 'attributes': {'name': 'gym'}}]}})
        self.assertEqual(
            upload.create_dataset('datadoghq.com', 'k', 'a', 'ours', 'gym', ''), 'ds1')

    def test_raises_when_neither_the_response_nor_a_lookup_yields_an_id(self):
        self.stub({'POST': {'data': {}}, 'GET': {'data': []}})
        with self.assertRaises(upload.DatadogError):
            upload.create_dataset('datadoghq.com', 'k', 'a', 'ours', 'gym', '')

    def test_a_listing_that_lags_the_create_is_retried_not_failed(self):
        # The project listing catches up a moment after the create; a dataset missing
        # from the first read has not gone anywhere.
        listings = iter([{'data': []},
                         {'data': [{'id': 'ds1', 'attributes': {'name': 'gym'}}]}])

        def request_json(site, api_key, app_key, method, path, body=None):
            if method == 'POST':
                return {'data': {'id': 'ds1'}}
            return next(listings)
        upload.request_json = request_json
        self.assertEqual(
            upload.create_dataset('datadoghq.com', 'k', 'a', 'ours', 'gym', ''), 'ds1')

    def test_a_dataset_present_in_the_project_returns_its_id(self):
        self.stub({'POST': {'data': {'id': 'ds1'}},
                   'GET': {'data': [{'id': 'ds1', 'attributes': {'name': 'gym'}}]}})
        self.assertEqual(
            upload.create_dataset('datadoghq.com', 'k', 'a', 'ours', 'gym', ''), 'ds1')


class AppendRecords(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.original = upload.request_json
        self.addCleanup(setattr, upload, 'request_json', self.original)

        def request_json(site, api_key, app_key, method, path, body=None):
            self.calls.append((method, path, body))
            return {}
        upload.request_json = request_json

    def test_writes_to_the_project_scoped_batch_update_route(self):
        upload.append_records('datadoghq.com', 'k', 'a', 'ours', 'ds1', [{'id': 'r1'}])
        self.assertEqual(self.calls[0][1],
                         '/api/v2/llm-obs/v1/ours/datasets/ds1/batch_update')

    def test_sends_the_rows_as_inserts(self):
        upload.append_records('datadoghq.com', 'k', 'a', 'ours', 'ds1', [{'id': 'r1'}])
        attributes = self.calls[0][2]['data']['attributes']
        self.assertEqual(attributes['insert_records'], [{'id': 'r1'}])
        self.assertEqual(attributes['update_records'], [])
        self.assertEqual(attributes['delete_records'], [])

    def test_deduplicates_so_a_re_upload_reconciles(self):
        upload.append_records('datadoghq.com', 'k', 'a', 'ours', 'ds1', [{'id': 'r1'}])
        self.assertIs(self.calls[0][2]['data']['attributes']['deduplicate'], True)

    def test_cuts_a_new_version_so_a_result_can_name_what_it_ran_against(self):
        upload.append_records('datadoghq.com', 'k', 'a', 'ours', 'ds1', [{'id': 'r1'}])
        self.assertIs(self.calls[0][2]['data']['attributes']['create_new_version'],
                      True)


class Batching(unittest.TestCase):
    def test_uploads_every_record_across_batches(self):
        sent = []

        def append_records(site, api_key, app_key, project_id, dataset_id, records):
            sent.append(len(records))
            return {}

        original = upload.append_records
        upload.append_records = append_records
        try:
            records = [{'id': str(n), 'input': {}, 'expected_output': {},
                        'metadata': {}, 'tags': []}
                       for n in range(upload.BATCH_SIZE + 3)]
            for start in range(0, len(records), upload.BATCH_SIZE):
                upload.append_records('s', 'k', 'a', 'p', 'd',
                                      records[start:start + upload.BATCH_SIZE])
            self.assertEqual(sum(sent), upload.BATCH_SIZE + 3)
            self.assertEqual(sent, [upload.BATCH_SIZE, 3])
        finally:
            upload.append_records = original


class ProjectIdFlag(unittest.TestCase):
    """A project UUID given on the command line must skip name resolution entirely.
    Looking a name up and missing it creates a second project with that same name."""

    def setUp(self):
        self.calls = []
        self.original = upload.request_json
        self.addCleanup(setattr, upload, 'request_json', self.original)

        def request_json(site, api_key, app_key, method, path, body=None):
            self.calls.append(f'{method} {path}')
            if method == 'POST' and path.endswith('/datasets'):
                return {'data': {'id': 'ds1'}}
            if method == 'GET' and path.endswith('/datasets'):
                return {'data': [{'id': 'ds1', 'attributes': {
                    'name': 'sol-first-pass-findings'}}]}
            return {'data': []}
        upload.request_json = request_json

    def run_upload(self, *extra):
        import os
        os.environ['DD_API_KEY'] = 'k'
        os.environ['DD_APP_KEY'] = 'a'
        self.addCleanup(os.environ.pop, 'DD_API_KEY', None)
        self.addCleanup(os.environ.pop, 'DD_APP_KEY', None)
        return upload.main(['--file', write(), '--project', 'gym', *extra])

    def test_skips_the_project_lookup_and_uploads_into_the_given_project(self):
        self.assertEqual(self.run_upload('--project-id', 'given-uuid'), 0)
        self.assertFalse(any('/projects' in call for call in self.calls))

    def test_resolves_the_project_by_name_when_no_uuid_is_given(self):
        self.run_upload()
        self.assertTrue(any('/projects' in call for call in self.calls))


class DryRun(unittest.TestCase):
    def test_calls_datadog_for_nothing(self):
        def explode(*args, **kwargs):
            raise AssertionError('dry run must not call Datadog')

        original = upload.request_json
        upload.request_json = explode
        try:
            code = upload.main(['--file', write(), '--project', 'gym', '--dry-run'])
        finally:
            upload.request_json = original
        self.assertEqual(code, 0)

    def test_reports_a_missing_file(self):
        self.assertEqual(
            upload.main(['--file', '/nonexistent.yaml', '--project', 'g', '--dry-run']),
            2)


class RequestJson(unittest.TestCase):
    def test_wraps_an_http_error_with_the_response_body(self):
        def urlopen(request, timeout=0):
            raise urllib.error.HTTPError(
                'u', 404, 'Not Found', {}, io.BytesIO(b'{"errors":["no such path"]}'))

        original = upload.urllib.request.urlopen
        upload.urllib.request.urlopen = urlopen
        try:
            with self.assertRaises(upload.DatadogError) as caught:
                upload.request_json('datadoghq.com', 'k', 'a', 'GET', '/x')
        finally:
            upload.urllib.request.urlopen = original
        self.assertIn('404', str(caught.exception))
        self.assertIn('no such path', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
