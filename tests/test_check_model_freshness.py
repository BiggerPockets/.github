import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/pi/check_model_freshness.py'
spec = importlib.util.spec_from_file_location('check_model_freshness', SCRIPT)
freshness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(freshness)


def write_models(models):
    path = Path(tempfile.mkdtemp()) / 'models.json'
    path.write_text(json.dumps({"providers": {"openrouter": {"models": models}}}))
    return str(path)


PINNED = {
    "id": "z-ai/glm-5.3-flash",
    "name": "GLM 5.3 Flash",
    "reasoning": True,
    "cost": {"input": 0.09, "output": 0.3, "cacheRead": 0.018, "cacheWrite": 0},
}


class RateDriftTest(unittest.TestCase):
    def test_no_drift_within_threshold(self):
        live_entry = {"pricing": {"prompt": "0.00000009", "completion": "0.0000003",
                                   "input_cache_read": "0.000000018", "input_cache_write": "0"}}
        self.assertEqual(freshness.rate_drift(PINNED, live_entry), [])

    def test_flags_a_rate_that_moved_past_threshold(self):
        live_entry = {"pricing": {"prompt": "0.00000018", "completion": "0.0000003",
                                   "input_cache_read": "0.000000018", "input_cache_write": "0"}}
        drifts = freshness.rate_drift(PINNED, live_entry)
        self.assertEqual(len(drifts), 1)
        self.assertEqual(drifts[0]["field"], "input")


class FindCandidatesTest(unittest.TestCase):
    def test_excludes_pinned_models_and_short_context(self):
        catalog = {
            "z-ai/glm-5.3-flash": {"supported_parameters": ["reasoning"],
                                    "context_length": 1_000_000,
                                    "pricing": {"prompt": "0.00000001"}},
            "some/short-context": {"supported_parameters": ["reasoning"],
                                    "context_length": 8_000,
                                    "pricing": {"prompt": "0.00000001"}},
            "some/cheaper-model": {"supported_parameters": ["reasoning"],
                                    "context_length": 200_000,
                                    "pricing": {"prompt": "0.00000005"}},
        }
        candidates = freshness.find_candidates(catalog, {"z-ai/glm-5.3-flash"}, 0.09)
        self.assertEqual([c["id"] for c in candidates], ["some/cheaper-model"])

    def test_excludes_non_reasoning_models(self):
        catalog = {
            "some/non-reasoning": {"supported_parameters": [], "context_length": 200_000,
                                    "pricing": {"prompt": "0.00000001"}},
        }
        self.assertEqual(freshness.find_candidates(catalog, set(), 0.09), [])


class MainTest(unittest.TestCase):
    def test_flags_a_pinned_model_dropped_from_the_catalog(self):
        models_path = write_models([PINNED])
        original_fetch = freshness.fetch_catalog
        freshness.fetch_catalog = lambda: ({}, None)
        try:
            import io
            import contextlib
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = freshness.main(['check_model_freshness.py', models_path])
            result = json.loads(buffer.getvalue())
        finally:
            freshness.fetch_catalog = original_fetch
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["missing"], ["z-ai/glm-5.3-flash"])
        self.assertTrue(result["notable"])

    def test_reports_not_notable_when_catalog_matches_pin_exactly(self):
        models_path = write_models([PINNED])
        live_entry = {"pricing": {"prompt": "0.00000009", "completion": "0.0000003",
                                   "input_cache_read": "0.000000018", "input_cache_write": "0"},
                      "supported_parameters": ["reasoning"], "context_length": 1_000_000}
        original_fetch = freshness.fetch_catalog
        freshness.fetch_catalog = lambda: ({"z-ai/glm-5.3-flash": live_entry}, None)
        try:
            import io
            import contextlib
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                freshness.main(['check_model_freshness.py', models_path])
            result = json.loads(buffer.getvalue())
        finally:
            freshness.fetch_catalog = original_fetch
        self.assertFalse(result["notable"])


if __name__ == '__main__':
    unittest.main()
