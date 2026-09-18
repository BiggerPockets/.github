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

# Roughly what fetch_token_mix has actually observed for the pi pass: reviews
# are almost all re-sent (cached) context.
HEAVY_CACHE_MIX = {"fresh_input_share": 0.1, "cache_read_share": 0.85, "output_share": 0.05, "sample_count": 50}


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


class EffectiveRatePerMillionTest(unittest.TestCase):
    def test_falls_back_to_input_rate_with_no_token_mix(self):
        rate = freshness.effective_rate_per_million({"input": 0.09, "cacheRead": 0.018, "output": 0.3}, None)
        self.assertEqual(rate, 0.09)

    def test_none_without_an_input_rate(self):
        self.assertIsNone(freshness.effective_rate_per_million({"input": None}, HEAVY_CACHE_MIX))

    def test_blends_by_the_observed_token_mix(self):
        rate = freshness.effective_rate_per_million(
            {"input": 1.0, "cacheRead": 0.1, "output": 2.0},
            {"fresh_input_share": 0.1, "cache_read_share": 0.8, "output_share": 0.1},
        )
        self.assertAlmostEqual(rate, 0.1 * 1.0 + 0.8 * 0.1 + 0.1 * 2.0)

    def test_missing_cache_or_output_rate_falls_back_to_input_rate_for_that_share(self):
        rate = freshness.effective_rate_per_million(
            {"input": 1.0, "cacheRead": None, "output": None},
            {"fresh_input_share": 0.1, "cache_read_share": 0.8, "output_share": 0.1},
        )
        self.assertAlmostEqual(rate, 1.0)


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
        candidates = freshness.find_candidates(catalog, {"z-ai/glm-5.3-flash"}, 0.09, None)
        self.assertEqual([c["id"] for c in candidates], ["some/cheaper-model"])

    def test_excludes_non_reasoning_models(self):
        catalog = {
            "some/non-reasoning": {"supported_parameters": [], "context_length": 200_000,
                                    "pricing": {"prompt": "0.00000001"}},
        }
        self.assertEqual(freshness.find_candidates(catalog, set(), 0.09, None), [])

    def test_excludes_models_below_uptime_threshold(self):
        freshness.fetch_uptime = lambda model_id: 90.0
        catalog = {
            "some/flaky-model": {"supported_parameters": ["reasoning"], "context_length": 200_000,
                                  "pricing": {"prompt": "0.00000001"}},
        }
        self.assertEqual(freshness.find_candidates(catalog, set(), 0.09, None), [])

    def test_ranks_by_effective_rate_not_raw_input_rate_when_a_token_mix_is_given(self):
        # cheap_input has a great input price but no cache discount at all; rich_cache
        # has a pricier input rate but a steep cache discount. Under HEAVY_CACHE_MIX
        # (85% cache reads), rich_cache is actually the cheaper real-world choice.
        catalog = {
            "some/cheap-input": {"supported_parameters": ["reasoning"], "context_length": 200_000,
                                  "pricing": {"prompt": "0.00000002", "input_cache_read": "0.00000002"}},
            "some/rich-cache": {"supported_parameters": ["reasoning"], "context_length": 200_000,
                                 "pricing": {"prompt": "0.00000004", "input_cache_read": "0.000000005"}},
        }
        candidates = freshness.find_candidates(catalog, set(), 0.09, HEAVY_CACHE_MIX)
        self.assertEqual([c["id"] for c in candidates], ["some/rich-cache", "some/cheap-input"])


class GenerateSvgTest(unittest.TestCase):
    def test_none_when_no_plottable_points(self):
        self.assertIsNone(freshness.generate_svg([], [], None))

    def test_plots_pinned_and_candidate_ids_with_distinct_colors(self):
        pinned = [{"id": "z-ai/glm-5.3-flash", "effective_rate_per_million": 0.09, "context_length": 1_000_000}]
        candidates = [{"id": "some/cheaper-model", "effective_rate_per_million": 0.02, "context_length": 200_000}]
        svg = freshness.generate_svg(pinned, candidates, None)
        self.assertIn("z-ai/glm-5.3-flash", svg)
        self.assertIn("some/cheaper-model", svg)
        self.assertIn('fill="#1f77b4"', svg)  # pinned color
        self.assertIn('fill="#2ca02c"', svg)  # candidate color
        self.assertTrue(svg.startswith("<svg"))
        self.assertTrue(svg.endswith("</svg>"))

    def test_does_not_mention_uptime_at_all(self):
        pinned = [{"id": "a/model", "effective_rate_per_million": 0.09, "context_length": 1_000_000,
                   "uptime_pct": 99.9}]
        svg = freshness.generate_svg(pinned, [], None)
        self.assertNotIn("uptime", svg.lower())

    def test_escapes_ids_to_stay_valid_xml(self):
        pinned = [{"id": "vendor/model<x>&y", "effective_rate_per_million": 0.09, "context_length": 1_000_000}]
        svg = freshness.generate_svg(pinned, [], None)
        self.assertIn("&lt;x&gt;&amp;y", svg)
        self.assertNotIn("<x>", svg)

    def test_labels_the_context_axis_ticks(self):
        pinned = [{"id": "a/model", "effective_rate_per_million": 0.09, "context_length": 1_000_000}]
        candidates = [{"id": "b/model", "effective_rate_per_million": 0.02, "context_length": 100_000}]
        svg = freshness.generate_svg(pinned, candidates, None)
        self.assertIn("100k", svg)
        self.assertIn("1M", svg)

    def test_draws_a_line_at_the_average_review_input_size(self):
        pinned = [{"id": "a/model", "effective_rate_per_million": 0.09, "context_length": 100_000}]
        token_mix = {"avg_input_tokens": 200_000}
        svg = freshness.generate_svg(pinned, [], token_mix)
        self.assertIn("avg review input size", svg)
        self.assertIn("200k", svg)

    def test_no_average_line_without_token_mix(self):
        pinned = [{"id": "a/model", "effective_rate_per_million": 0.09, "context_length": 100_000}]
        svg = freshness.generate_svg(pinned, [], None)
        self.assertNotIn("avg review input size", svg)

    def test_no_average_line_when_token_mix_has_no_sampled_spans(self):
        pinned = [{"id": "a/model", "effective_rate_per_million": 0.09, "context_length": 100_000}]
        svg = freshness.generate_svg(pinned, [], {"avg_input_tokens": 0})
        self.assertNotIn("avg review input size", svg)

    def test_marks_points_smaller_than_average_review_input_size_as_inadequate(self):
        pinned = [{"id": "small/model", "effective_rate_per_million": 0.09, "context_length": 50_000}]
        candidates = [{"id": "big/model", "effective_rate_per_million": 0.02, "context_length": 500_000}]
        token_mix = {"avg_input_tokens": 200_000}
        svg = freshness.generate_svg(pinned, candidates, token_mix)
        self.assertIn("too small for the average review", svg)
        self.assertIn('fill="#d62728"', svg)
        self.assertIn("inadequate context", svg)

    def test_does_not_mark_points_as_inadequate_without_a_token_mix(self):
        pinned = [{"id": "small/model", "effective_rate_per_million": 0.09, "context_length": 50_000}]
        svg = freshness.generate_svg(pinned, [], None)
        self.assertNotIn("#d62728", svg)


class WriteSvgTest(unittest.TestCase):
    def test_creates_parent_directories(self):
        path = Path(tempfile.mkdtemp()) / 'nested' / 'dir' / 'frontier.svg'
        freshness.write_svg(str(path), "<svg></svg>")
        self.assertEqual(path.read_text(), "<svg></svg>")


class FetchTokenMixTest(unittest.TestCase):
    def setUp(self):
        self.original_environ = dict(freshness.os.environ)

    def tearDown(self):
        freshness.os.environ.clear()
        freshness.os.environ.update(self.original_environ)

    def test_none_without_credentials(self):
        freshness.os.environ.pop("DD_API_KEY", None)
        freshness.os.environ.pop("DD_APP_KEY", None)
        self.assertIsNone(freshness.fetch_token_mix())

    def test_sums_disjoint_input_cache_and_output_fields_across_pages(self):
        freshness.os.environ["DD_API_KEY"] = "key"
        freshness.os.environ["DD_APP_KEY"] = "app-key"

        pages = [
            {
                "data": [
                    {"attributes": {"metrics": {"input_tokens": 10, "cache_read_input_tokens": 80,
                                                 "output_tokens": 5}}},
                    {"attributes": {"metrics": {}}},  # no usable metrics, skipped
                ],
                "meta": {"page": {"after": "cursor-2"}},
            },
            {
                "data": [
                    {"attributes": {"metrics": {"input_tokens": 5, "cache_read_input_tokens": 40,
                                                 "output_tokens": 3}}},
                ],
                "meta": {"page": {}},
            },
        ]

        call_count = {"n": 0}

        def fake_urlopen(request, timeout=30):
            import io
            page = pages[call_count["n"]]
            call_count["n"] += 1
            return io.BytesIO(json.dumps(page).encode())

        class FakeResponse:
            def __init__(self, buf):
                self.buf = buf

            def __enter__(self):
                return self.buf

            def __exit__(self, *args):
                return False

        original_urlopen = freshness.urllib.request.urlopen
        freshness.urllib.request.urlopen = lambda request, timeout=30: FakeResponse(fake_urlopen(request, timeout))
        try:
            mix = freshness.fetch_token_mix()
        finally:
            freshness.urllib.request.urlopen = original_urlopen

        self.assertEqual(mix["sample_count"], 2)
        total = 15 + 120 + 8
        self.assertAlmostEqual(mix["fresh_input_share"], 15 / total)
        self.assertAlmostEqual(mix["cache_read_share"], 120 / total)
        self.assertAlmostEqual(mix["output_share"], 8 / total)

    def test_none_on_request_failure(self):
        freshness.os.environ["DD_API_KEY"] = "key"
        freshness.os.environ["DD_APP_KEY"] = "app-key"
        original_urlopen = freshness.urllib.request.urlopen

        def raise_error(request, timeout=30):
            raise OSError("boom")

        freshness.urllib.request.urlopen = raise_error
        try:
            self.assertIsNone(freshness.fetch_token_mix())
        finally:
            freshness.urllib.request.urlopen = original_urlopen


class MainTest(unittest.TestCase):
    def setUp(self):
        self.original_fetch_catalog = freshness.fetch_catalog
        self.original_fetch_uptime = freshness.fetch_uptime
        self.original_fetch_token_mix = freshness.fetch_token_mix
        freshness.fetch_uptime = lambda model_id: 99.9
        freshness.fetch_token_mix = lambda: None

    def tearDown(self):
        freshness.fetch_catalog = self.original_fetch_catalog
        freshness.fetch_uptime = self.original_fetch_uptime
        freshness.fetch_token_mix = self.original_fetch_token_mix

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

    def test_result_carries_the_token_mix_used(self):
        models_path = write_models([PINNED])
        live_entry = {"pricing": {"prompt": "0.00000009", "completion": "0.0000003",
                                   "input_cache_read": "0.000000018", "input_cache_write": "0"},
                      "supported_parameters": ["reasoning"], "context_length": 1_000_000}
        freshness.fetch_catalog = lambda: ({"z-ai/glm-5.3-flash": live_entry}, None)
        freshness.fetch_token_mix = lambda: HEAVY_CACHE_MIX

        result, _ = self.run_main(models_path)

        self.assertEqual(result["token_mix"], HEAVY_CACHE_MIX)

    def test_token_mix_absent_is_reported_as_null(self):
        models_path = write_models([PINNED])
        live_entry = {"pricing": {"prompt": "0.00000009", "completion": "0.0000003",
                                   "input_cache_read": "0.000000018", "input_cache_write": "0"},
                      "supported_parameters": ["reasoning"], "context_length": 1_000_000}
        freshness.fetch_catalog = lambda: ({"z-ai/glm-5.3-flash": live_entry}, None)

        result, _ = self.run_main(models_path)

        self.assertIsNone(result["token_mix"])

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
