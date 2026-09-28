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
