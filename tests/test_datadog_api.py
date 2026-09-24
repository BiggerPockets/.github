import io
from pathlib import Path
import sys
import unittest
import unittest.mock
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/gym'))
import datadog_api  # noqa: E402


class FindProject(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.original = datadog_api.request_json
        self.addCleanup(setattr, datadog_api, 'request_json', self.original)

    def stub(self, payload):
        def request_json(site, api_key, app_key, method, path, body=None):
            self.calls.append((method, path, body))
            return payload
        datadog_api.request_json = request_json

    def test_returns_the_id_of_an_exact_name_match(self):
        self.stub({'data': [{'id': 'abc', 'attributes': {'name': 'gym'}}]})
        self.assertEqual(datadog_api.find_project('datadoghq.com', 'k', 'a', 'gym'), 'abc')

    def test_ignores_a_near_miss_from_a_contains_filter(self):
        self.stub({'data': [{'id': 'abc', 'attributes': {'name': 'gym-staging'}}]})
        self.assertIsNone(datadog_api.find_project('datadoghq.com', 'k', 'a', 'gym'))

    def test_returns_none_when_nothing_matches(self):
        self.stub({'data': []})
        self.assertIsNone(datadog_api.find_project('datadoghq.com', 'k', 'a', 'gym'))

    def test_url_encodes_the_name(self):
        self.stub({'data': []})
        datadog_api.find_project('datadoghq.com', 'k', 'a', 'a b')
        self.assertIn('a%20b', self.calls[0][1])


class Credentials(unittest.TestCase):
    def test_requires_both_keys(self):
        with unittest.mock.patch.dict('os.environ', {'DD_API_KEY': 'k'}, clear=True):
            with self.assertRaises(datadog_api.DatadogError):
                datadog_api.credentials()

    def test_defaults_the_site(self):
        env = {'DD_API_KEY': 'k', 'DD_APP_KEY': 'a'}
        with unittest.mock.patch.dict('os.environ', env, clear=True):
            self.assertEqual(datadog_api.credentials(), ('datadoghq.com', 'k', 'a'))


class RequestJson(unittest.TestCase):
    def test_wraps_an_http_error_with_the_response_body(self):
        def urlopen(request, timeout=0):
            raise urllib.error.HTTPError(
                'u', 404, 'Not Found', {}, io.BytesIO(b'{"errors":["no such path"]}'))

        original = datadog_api.urllib.request.urlopen
        datadog_api.urllib.request.urlopen = urlopen
        try:
            with self.assertRaises(datadog_api.DatadogError) as caught:
                datadog_api.request_json('datadoghq.com', 'k', 'a', 'GET', '/x')
        finally:
            datadog_api.urllib.request.urlopen = original
        self.assertIn('404', str(caught.exception))
        self.assertIn('no such path', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
