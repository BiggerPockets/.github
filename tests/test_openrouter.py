import importlib.util
from pathlib import Path
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/pi/openrouter.py'
spec = importlib.util.spec_from_file_location('openrouter', SCRIPT)
openrouter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(openrouter)


class BilledCostTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(openrouter.time, 'sleep')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sums_what_each_generation_was_charged(self):
        records = {'gen-1': {'total_cost': 0.25}, 'gen-2': {'total_cost': 0.5}}
        with mock.patch.object(openrouter, 'lookup',
                               side_effect=lambda i, key, timeout: records[i]):
            self.assertEqual(openrouter.billed_cost(['gen-1', 'gen-2'], 'key'), 0.75)

    def test_retries_a_generation_whose_record_is_not_ready_yet(self):
        with mock.patch.object(openrouter, 'lookup',
                               side_effect=[None, {'total_cost': 0.1}]) as lookup:
            self.assertEqual(openrouter.billed_cost(['gen-1'], 'key'), 0.1)
        self.assertEqual(lookup.call_count, 2)

    def test_is_none_when_any_generation_never_resolves(self):
        records = {'gen-1': {'total_cost': 0.25}, 'gen-2': None}
        with mock.patch.object(openrouter, 'lookup',
                               side_effect=lambda i, key, timeout: records[i]):
            self.assertIsNone(openrouter.billed_cost(['gen-1', 'gen-2'], 'key'))

    def test_is_none_when_a_record_has_no_charge(self):
        with mock.patch.object(openrouter, 'lookup', return_value={'provider_name': 'x'}):
            self.assertIsNone(openrouter.billed_cost(['gen-1'], 'key'))

    def test_stops_retrying_at_the_deadline(self):
        with mock.patch.object(openrouter, 'BILLING_DEADLINE_S', 0), \
                mock.patch.object(openrouter, 'lookup') as lookup:
            self.assertIsNone(openrouter.billed_cost(['gen-1'], 'key'))
        lookup.assert_not_called()

    def test_is_none_without_ids_or_a_key(self):
        self.assertIsNone(openrouter.billed_cost([], 'key'))
        self.assertIsNone(openrouter.billed_cost(['gen-1'], ''))


if __name__ == '__main__':
    unittest.main()


def _endpoint(name, prompt, completion, quantization=None):
    endpoint = {'provider_name': name, 'status': 0, 'supported_parameters': ['tools'],
                'pricing': {'prompt': str(prompt / 1e6), 'completion': str(completion / 1e6)}}
    if quantization:
        endpoint['quantization'] = quantization
    return endpoint


class RoutingTest(unittest.TestCase):
    def test_leaves_fp4_endpoints_out_of_the_field(self):
        payload = {'data': {'endpoints': [_endpoint('a', 0.1, 0.4, 'fp8'),
                                          _endpoint('b', 0.03, 0.5, 'fp4'),
                                          _endpoint('c', 0.2, 0.6)]}}
        names = [e['provider_name'] for e in openrouter.eligible_endpoints(payload)]
        self.assertEqual(names, ['a', 'c'])

    def test_an_fp4_endpoint_does_not_lower_the_ceiling(self):
        field = [_endpoint(str(i), 0.1 + i / 100, 0.4, 'fp8') for i in range(7)]
        field.append(_endpoint('cheap-fp4', 0.01, 0.1, 'fp4'))
        with mock.patch.object(openrouter, '_get', return_value={'data': {'endpoints': field}}):
            routing = openrouter.routing_for('some/model')
        self.assertEqual(routing['max_price'], {'prompt': 0.15, 'completion': 0.4})

    def test_denies_data_collection(self):
        field = [_endpoint('a', 0.1, 0.4, 'fp8')]
        with mock.patch.object(openrouter, '_get', return_value={'data': {'endpoints': field}}):
            routing = openrouter.routing_for('some/model')
        self.assertEqual(routing['data_collection'], 'deny')

    def test_denies_data_collection_when_the_rest_of_the_routing_fails(self):
        with mock.patch.object(openrouter, '_get', side_effect=OSError):
            self.assertEqual(openrouter.routing_for('some/model'), {'data_collection': 'deny'})
        with mock.patch.object(openrouter, '_get', return_value={'data': {'endpoints': []}}):
            self.assertEqual(openrouter.routing_for('some/model'), {'data_collection': 'deny'})

    def test_tells_openrouter_never_to_route_to_fp4(self):
        field = [_endpoint('a', 0.1, 0.4, 'fp8')]
        with mock.patch.object(openrouter, '_get', return_value={'data': {'endpoints': field}}):
            routing = openrouter.routing_for('some/model')
        self.assertNotIn('fp4', routing['quantizations'])
        self.assertIn('unknown', routing['quantizations'])
