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
    def setUp(self):
        self.original_fetch_uptime = freshness.fetch_uptime
        freshness.fetch_uptime = lambda model_id: 99.9

    def tearDown(self):
        freshness.fetch_uptime = self.original_fetch_uptime

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

    def test_excludes_models_below_uptime_threshold(self):
        freshness.fetch_uptime = lambda model_id: 90.0
        catalog = {
            "some/flaky-model": {"supported_parameters": ["reasoning"], "context_length": 200_000,
                                  "pricing": {"prompt": "0.00000001"}},
        }
        self.assertEqual(freshness.find_candidates(catalog, set(), 0.09), [])


class GenerateSvgTest(unittest.TestCase):
    def test_none_when_no_plottable_points(self):
        self.assertIsNone(freshness.generate_svg([], []))

    def test_plots_pinned_and_candidate_ids_with_distinct_colors(self):
        pinned = [{"id": "z-ai/glm-5.3-flash", "input_rate_per_million": 0.09, "uptime_pct": 99.9}]
        candidates = [{"id": "some/cheaper-model", "input_rate_per_million": 0.02, "uptime_pct": 99.5}]
        svg = freshness.generate_svg(pinned, candidates)
        self.assertIn("z-ai/glm-5.3-flash", svg)
        self.assertIn("some/cheaper-model", svg)
        self.assertIn('fill="#1f77b4"', svg)  # pinned color
        self.assertIn('fill="#2ca02c"', svg)  # candidate color
        self.assertTrue(svg.startswith("<svg"))
        self.assertTrue(svg.endswith("</svg>"))

    def test_escapes_ids_to_stay_valid_xml(self):
        pinned = [{"id": "vendor/model<x>&y", "input_rate_per_million": 0.09, "uptime_pct": 99.9}]
        svg = freshness.generate_svg(pinned, [])
        self.assertIn("&lt;x&gt;&amp;y", svg)
        self.assertNotIn("<x>", svg)


class WriteSvgTest(unittest.TestCase):
    def test_creates_parent_directories(self):
        path = Path(tempfile.mkdtemp()) / 'nested' / 'dir' / 'frontier.svg'
        freshness.write_svg(str(path), "<svg></svg>")
        self.assertEqual(path.read_text(), "<svg></svg>")


class MainTest(unittest.TestCase):
    def setUp(self):
        self.original_fetch_catalog = freshness.fetch_catalog
        self.original_fetch_uptime = freshness.fetch_uptime
        freshness.fetch_uptime = lambda model_id: 99.9

    def tearDown(self):
        freshness.fetch_catalog = self.original_fetch_catalog
        freshness.fetch_uptime = self.original_fetch_uptime

    def run_main(self, models_path, chart_path=None):
        import io
        import contextlib
        chart_path = chart_path or str(Path(tempfile.mkdtemp()) / 'frontier.svg')
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = freshness.main(['check_model_freshness.py', models_path, chart_path])
        return json.loads(buffer.getvalue()), exit_code

    def test_flags_a_pinned_model_dropped_from_the_catalog(self):
        models_path = write_models([PINNED])
        freshness.fetch_catalog = lambda: ({}, None)

        result, exit_code = self.run_main(models_path)

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["missing"], ["z-ai/glm-5.3-flash"])
        self.assertTrue(result["notable"])

    def test_reports_not_notable_when_catalog_matches_pin_exactly(self):
        models_path = write_models([PINNED])
        live_entry = {"pricing": {"prompt": "0.00000009", "completion": "0.0000003",
                                   "input_cache_read": "0.000000018", "input_cache_write": "0"},
                      "supported_parameters": ["reasoning"], "context_length": 1_000_000}
        freshness.fetch_catalog = lambda: ({"z-ai/glm-5.3-flash": live_entry}, None)

        result, _ = self.run_main(models_path)

        self.assertFalse(result["notable"])

    def test_flags_a_pinned_model_whose_uptime_has_dropped(self):
        models_path = write_models([PINNED])
        live_entry = {"pricing": {"prompt": "0.00000009", "completion": "0.0000003",
                                   "input_cache_read": "0.000000018", "input_cache_write": "0"},
                      "supported_parameters": ["reasoning"], "context_length": 1_000_000}
        freshness.fetch_catalog = lambda: ({"z-ai/glm-5.3-flash": live_entry}, None)
        freshness.fetch_uptime = lambda model_id: 90.0

        result, _ = self.run_main(models_path)

        self.assertEqual(result["unreliable_pinned"], [{"id": "z-ai/glm-5.3-flash", "uptime_pct": 90.0}])
        self.assertTrue(result["notable"])

    def test_writes_a_chart_when_notable(self):
        models_path = write_models([PINNED])
        live_entry = {"pricing": {"prompt": "0.00000009", "completion": "0.0000003",
                                   "input_cache_read": "0.000000018", "input_cache_write": "0"},
                      "supported_parameters": ["reasoning"], "context_length": 1_000_000}
        freshness.fetch_catalog = lambda: ({"z-ai/glm-5.3-flash": live_entry}, None)
        freshness.fetch_uptime = lambda model_id: 90.0  # unreliable -> notable, still plottable
        chart_path = str(Path(tempfile.mkdtemp()) / 'frontier.svg')

        result, _ = self.run_main(models_path, chart_path)

        self.assertEqual(result["chart_path"], chart_path)
        self.assertTrue(Path(chart_path).exists())
        self.assertIn("z-ai/glm-5.3-flash", Path(chart_path).read_text())

    def test_does_not_write_a_chart_when_not_notable(self):
        models_path = write_models([PINNED])
        live_entry = {"pricing": {"prompt": "0.00000009", "completion": "0.0000003",
                                   "input_cache_read": "0.000000018", "input_cache_write": "0"},
                      "supported_parameters": ["reasoning"], "context_length": 1_000_000}
        freshness.fetch_catalog = lambda: ({"z-ai/glm-5.3-flash": live_entry}, None)
        chart_path = str(Path(tempfile.mkdtemp()) / 'frontier.svg')

        result, _ = self.run_main(models_path, chart_path)

        self.assertNotIn("chart_path", result)
        self.assertFalse(Path(chart_path).exists())


if __name__ == '__main__':
    unittest.main()
