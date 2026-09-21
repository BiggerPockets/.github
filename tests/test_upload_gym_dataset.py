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

    def test_carries_the_yaml_row_id_into_metadata(self):
        self.assertEqual(self.records[0]['metadata']['record_id'], 'repo-pr1')

    def test_sends_no_tags_field(self):
        # This API version drops it; a read-back shows tags: []. Sending it would make
        # the record look tagged in the file and untagged in Datadog.
        self.assertNotIn('tags', self.records[0])

    def test_folds_unstructured_tag_dimensions_into_metadata(self):
        document = upload.load(write())
        document['records'][0]['tags'] = ['source_model:openai/gpt-5.6-sol',
                                          'stage:first_pass']
        record = upload.to_api_records(document)[0]
        self.assertEqual(record['metadata']['source_model'], 'openai/gpt-5.6-sol')
        self.assertEqual(record['metadata']['stage'], 'first_pass')

    def test_skips_tag_dimensions_already_structured_elsewhere(self):
        document = upload.load(write())
        document['records'][0]['tags'] = ['repo:org/repo', 'pr:1', 'severity:blocking']
        record = upload.to_api_records(document)[0]
        for key in ('repo', 'pr'):
            self.assertNotIn(key, record['metadata'])
        # severity is already a real metadata field; folding must not overwrite it
        self.assertEqual(record['metadata']['severity'], 'blocking')

    def test_never_overwrites_an_existing_metadata_field(self):
        document = upload.load(write())
        document['records'][0]['tags'] = ['stage2_verdict:approve']
        record = upload.to_api_records(document)[0]
        self.assertEqual(record['metadata']['stage2_verdict'], 'request_changes')

    def test_every_record_has_the_same_keys(self):
        document = upload.load(write())
        document['records'].append(dict(document['records'][0], id='repo-pr2'))
        records = upload.to_api_records(document)
        self.assertEqual({frozenset(r) for r in records},
                         {frozenset(records[0])})


class FindByName(unittest.TestCase):
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
        self.assertEqual(
            upload.find_by_name('datadoghq.com', 'k', 'a', 'projects', 'gym'), 'abc')

    def test_ignores_a_near_miss_from_a_contains_filter(self):
        self.stub({'data': [{'id': 'abc', 'attributes': {'name': 'gym-staging'}}]})
        self.assertIsNone(
            upload.find_by_name('datadoghq.com', 'k', 'a', 'projects', 'gym'))

    def test_returns_none_when_nothing_matches(self):
        self.stub({'data': []})
        self.assertIsNone(
            upload.find_by_name('datadoghq.com', 'k', 'a', 'datasets', 'gym'))

    def test_url_encodes_the_name(self):
        self.stub({'data': []})
        upload.find_by_name('datadoghq.com', 'k', 'a', 'projects', 'a b')
        self.assertIn('a%20b', self.calls[0][1])


class Batching(unittest.TestCase):
    def test_uploads_every_record_across_batches(self):
        sent = []

        def append_records(site, api_key, app_key, dataset_id, records):
            sent.append(len(records))
            return {}

        original = upload.append_records
        upload.append_records = append_records
        try:
            records = [{'input': {}, 'expected_output': {}, 'metadata': {}, 'tags': []}
                       for _ in range(upload.BATCH_SIZE + 3)]
            for start in range(0, len(records), upload.BATCH_SIZE):
                upload.append_records('s', 'k', 'a', 'd',
                                      records[start:start + upload.BATCH_SIZE])
            self.assertEqual(sum(sent), upload.BATCH_SIZE + 3)
            self.assertEqual(sent, [upload.BATCH_SIZE, 3])
        finally:
            upload.append_records = original


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
