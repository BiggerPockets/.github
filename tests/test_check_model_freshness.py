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


class CandidateCapabilityTest(unittest.TestCase):
    """Selecting candidates on cost AND capability, not cost alone."""

    def setUp(self):
        self.original_fetch_uptime = freshness.fetch_uptime
        freshness.fetch_uptime = lambda model_id: 99.9

    def tearDown(self):
        freshness.fetch_uptime = self.original_fetch_uptime

    CATALOG = {
        # Cheapest of the three, but reviews far worse than what's pinned.
        "cheap/weak": {"supported_parameters": ["reasoning"], "context_length": 200_000,
                       "pricing": {"prompt": "0.000000005"}},
        # Pricier than cheap/weak, still cheaper than pinned, and much stronger.
        "mid/strong": {"supported_parameters": ["reasoning"], "context_length": 200_000,
                       "pricing": {"prompt": "0.00000002"}},
        # Cheap, but the leaderboard doesn't cover it.
        "cheap/unscored": {"supported_parameters": ["reasoning"], "context_length": 200_000,
                           "pricing": {"prompt": "0.000000008"}},
    }
    SCORES = freshness.build_coding_score_index([
        {"model_id": "weak", "organization_id": "cheap", "index_code": 10.0},
        {"model_id": "strong", "organization_id": "mid", "index_code": 44.0},
    ])
    FLOOR = 36.0  # roughly what the pinned model scores

    DEFAULT = object()  # distinct from None, which means "no floor" / "no scores"

    def candidates(self, index=DEFAULT, floor=DEFAULT):
        return [c["id"] for c in freshness.find_candidates(
            self.CATALOG, set(), 0.09, None,
            self.SCORES if index is self.DEFAULT else index,
            self.FLOOR if floor is self.DEFAULT else floor)]

    def test_ranks_the_stronger_model_first_although_it_costs_more(self):
        # The failure this ordering prevents: every candidate is already cheaper than
        # what's pinned, so ranking on price again just re-answers "what is cheapest"
        # and buries the model actually worth switching to.
        self.assertEqual(self.candidates(floor=None), ["mid/strong", "cheap/weak"])

    def test_drops_a_cheaper_model_that_reviews_worse_than_what_is_pinned(self):
        self.assertEqual(self.candidates(), ["mid/strong"])

    def test_drops_models_the_leaderboard_does_not_cover(self):
        # cheap/unscored is cheaper than mid/strong and would otherwise take a slot,
        # but there's no capability number to weigh its price against.
        self.assertNotIn("cheap/unscored", self.candidates(floor=None))

    def test_keeps_unscored_models_when_the_leaderboard_is_unreachable(self):
        # With no scores at all, a degraded price-ranked shortlist beats an empty one.
        self.assertEqual(sorted(self.candidates(index={}, floor=None)),
                         ["cheap/unscored", "cheap/weak", "mid/strong"])

    def test_reports_each_candidate_s_coding_score(self):
        found = freshness.find_candidates(
            self.CATALOG, set(), 0.09, None, self.SCORES, None)
        self.assertEqual({c["id"]: c["coding_score"] for c in found},
                         {"mid/strong": 44.0, "cheap/weak": 10.0})


def fake_leaderboard_html(rows):
    """Build a minimal HTML page mimicking llm-stats.com's leaderboard: the
    data lives inside a self.__next_f.push([1, "...escaped JSON..."]) call,
    not in a plain <script type="application/json"> tag. The chunk text is
    JS-string-escaped (backslashes and quotes) before being embedded between
    the literal quotes in push(...), matching how Next.js actually emits it."""
    # Next.js emits this compact (no spaces), which is what fetch_coding_scores'
    # marker search relies on.
    chunk_text = '33:["$",null,' + json.dumps({"initialData": rows}, separators=(",", ":")) + ']'
    escaped = chunk_text.replace("\\", "\\\\").replace('"', '\\"')
    return f'<html><script>self.__next_f.push([1,"{escaped}"])</script></html>'


class FetchCodingScoresTest(unittest.TestCase):
    def setUp(self):
        self.original_urlopen = freshness.urllib.request.urlopen

    def tearDown(self):
        freshness.urllib.request.urlopen = self.original_urlopen

    def _mock_html(self, html):
        import io

        class FakeResponse:
            def __enter__(self):
                return io.BytesIO(html.encode())

            def __exit__(self, *args):
                return False

        freshness.urllib.request.urlopen = lambda request, timeout=30: FakeResponse()

    def test_parses_the_embedded_next_js_payload(self):
        rows = [{"model_id": "claude-haiku-4-5-20251001", "organization_id": "anthropic", "index_code": 19.65}]
        self._mock_html(fake_leaderboard_html(rows))
        self.assertEqual(freshness.fetch_coding_scores(), rows)

    def test_empty_list_on_request_failure(self):
        def raise_error(request, timeout=30):
            raise OSError("boom")

        freshness.urllib.request.urlopen = raise_error
        self.assertEqual(freshness.fetch_coding_scores(), [])

    def test_empty_list_when_page_has_no_matching_payload(self):
        self._mock_html("<html><script>self.__next_f.push([1,\"no data here\"])</script></html>")
        self.assertEqual(freshness.fetch_coding_scores(), [])


class CodingScoreMatchingTest(unittest.TestCase):
    def test_exact_match(self):
        index = freshness.build_coding_score_index(
            [{"model_id": "deepseek-v4.1-flash", "organization_id": "deepseek", "index_code": 44.22}]
        )
        entry = freshness.coding_score_for("deepseek/deepseek-v4.1-flash", index)
        self.assertEqual(entry["index_code"], 44.22)

    def test_strips_a_release_date_suffix(self):
        index = freshness.build_coding_score_index(
            [{"model_id": "claude-4-5-haiku-20251001", "organization_id": "anthropic", "index_code": 19.65}]
        )
        entry = freshness.coding_score_for("anthropic/claude-4-5-haiku", index)
        self.assertEqual(entry["index_code"], 19.65)

    def test_matches_across_dot_vs_dash_naming(self):
        # OpenRouter's catalog uses "claude-haiku-4.5"; llm-stats.com's
        # leaderboard uses "claude-haiku-4-5-20251001" for the same model.
        index = freshness.build_coding_score_index(
            [{"model_id": "claude-haiku-4-5-20251001", "organization_id": "anthropic", "index_code": 19.65}]
        )
        entry = freshness.coding_score_for("anthropic/claude-haiku-4.5", index)
        self.assertEqual(entry["index_code"], 19.65)

    def test_never_matches_across_org_prefixes(self):
        index = freshness.build_coding_score_index(
            [{"model_id": "some-model", "organization_id": "openai", "index_code": 50.0}]
        )
        self.assertIsNone(freshness.coding_score_for("anthropic/some-model", index))

    def test_no_match_returns_none(self):
        index = freshness.build_coding_score_index([])
        self.assertIsNone(freshness.coding_score_for("anthropic/claude-haiku-4.5", index))

    def test_picks_the_highest_scoring_row_when_normalization_collapses_variants(self):
        index = freshness.build_coding_score_index([
            {"model_id": "gpt-6-astra-20260903", "organization_id": "openai", "index_code": 40.0},
            {"model_id": "gpt-6-astra-20260903", "organization_id": "openai", "index_code": 76.9},
        ])
        entry = freshness.coding_score_for("openai/gpt-6-astra", index)
        self.assertEqual(entry["index_code"], 76.9)


def mix(working_context=None, max_output=None, turns=4, samples=10, **extra):
    """A token_mix as fetch_token_mix builds one. `working_context` is the turn-aware
    threshold the chart gates on; it lives under `context` because it is derived from
    the subset of spans that report a turn count."""
    value = dict(extra)
    if max_output is not None:
        value["max_output_tokens"] = max_output
    if working_context is not None:
        value["context"] = {"max_working_context_tokens": working_context,
                            "max_turns": turns, "sample_count": samples}
    return value


class GenerateSvgTest(unittest.TestCase):
    def test_none_when_no_plottable_points(self):
        self.assertIsNone(freshness.generate_svg([], [], None))

    def test_plots_pinned_and_candidate_ids_with_distinct_colors(self):
        pinned = [{"id": "z-ai/glm-5.3-flash", "effective_rate_per_million": 0.09, "context_length": 1_000_000}]
        candidates = [{"id": "some/cheaper-model", "effective_rate_per_million": 0.02, "context_length": 200_000}]
        svg = freshness.generate_svg(pinned, candidates, None)
        self.assertIn("z-ai/glm-5.3-flash", svg)
        self.assertIn("some/cheaper-model", svg)
        self.assertIn("#1f77b4", svg)  # pinned color
        self.assertIn("#2ca02c", svg)  # candidate color
        self.assertIn("<svg", svg)
        self.assertTrue(svg.rstrip().endswith("</svg>"))

    def test_legend_lists_pinned_and_candidate(self):
        pinned = [{"id": "z-ai/glm-5.3-flash", "effective_rate_per_million": 0.09, "context_length": 1_000_000}]
        svg = freshness.generate_svg(pinned, [], None)
        self.assertIn(">pinned<", svg)
        self.assertIn(">candidate<", svg)

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

    def test_labels_the_context_axis_ticks_in_k_or_m(self):
        pinned = [{"id": "a/model", "effective_rate_per_million": 0.09, "context_length": 1_000_000}]
        candidates = [{"id": "b/model", "effective_rate_per_million": 0.02, "context_length": 100_000}]
        svg = freshness.generate_svg(pinned, candidates, None)
        self.assertIn(">100k<", svg)
        self.assertIn(">1M<", svg)

    def test_draws_a_line_at_the_working_context(self):
        pinned = [{"id": "a/model", "effective_rate_per_million": 0.09, "context_length": 100_000}]
        svg = freshness.generate_svg(pinned, [], mix(working_context=200_000))
        self.assertIn("working context", svg)
        self.assertIn("200k", svg)

    def test_no_context_line_without_token_mix(self):
        pinned = [{"id": "a/model", "effective_rate_per_million": 0.09, "context_length": 100_000}]
        svg = freshness.generate_svg(pinned, [], None)
        self.assertNotIn("working context", svg)

    def test_no_context_line_when_no_span_reported_a_turn_count(self):
        # Spans recorded before turn counts were instrumented can't distinguish a
        # long agentic session from one huge prompt, so no threshold is drawn.
        pinned = [{"id": "a/model", "effective_rate_per_million": 0.09, "context_length": 100_000}]
        svg = freshness.generate_svg(pinned, [], mix(max_input_tokens=1_150_702, max_total_tokens=1_155_634))
        self.assertNotIn("working context", svg)
        self.assertNotIn("#d62728", svg)

    def test_marks_points_smaller_than_the_working_context_as_inadequate(self):
        pinned = [{"id": "small/model", "effective_rate_per_million": 0.09, "context_length": 50_000}]
        candidates = [{"id": "big/model", "effective_rate_per_million": 0.02, "context_length": 500_000}]
        svg = freshness.generate_svg(pinned, candidates, mix(working_context=200_000, max_output=50_000))
        self.assertIn("#d62728", svg)
        self.assertIn("inadequate context", svg)

    def test_gates_on_the_working_context_not_the_cumulative_input(self):
        # The real Stage 1 numbers: 1.15M cumulative input across ~29 turns, but an
        # ~80k working context. Gating on the cumulative figure would mark a 1M-context
        # model inadequate when it has more than ten times the headroom it needs.
        pinned = [{"id": "roomy/model", "effective_rate_per_million": 0.09, "context_length": 1_000_000}]
        svg = freshness.generate_svg(pinned, [], mix(
            working_context=80_375, max_output=4_932, turns=29,
            max_input_tokens=1_150_702, max_total_tokens=1_155_634))
        self.assertNotIn("#d62728", svg)
        self.assertNotIn("inadequate context", svg)

    def test_does_not_mark_points_as_inadequate_without_a_token_mix(self):
        pinned = [{"id": "small/model", "effective_rate_per_million": 0.09, "context_length": 50_000}]
        svg = freshness.generate_svg(pinned, [], None)
        self.assertNotIn("#d62728", svg)

    def test_draws_a_line_at_the_max_review_output_size(self):
        pinned = [{"id": "a/model", "effective_rate_per_million": 0.09, "context_length": 100_000}]
        svg = freshness.generate_svg(pinned, [], mix(max_output=5_000))
        self.assertIn("max review output size", svg)
        self.assertIn("5k", svg)

    def test_widens_the_x_axis_to_include_maxes_smaller_than_any_plotted_point(self):
        pinned = [{"id": "a/model", "effective_rate_per_million": 0.09, "context_length": 1_000_000}]
        svg = freshness.generate_svg(pinned, [], mix(working_context=10_000, max_output=500))
        self.assertIn("working context (10k)", svg)
        self.assertIn("max review output size (500)", svg)

    def test_titles_the_chart_with_the_stage(self):
        pinned = [{"id": "a/model", "effective_rate_per_million": 0.09, "context_length": 100_000}]
        svg = freshness.generate_svg(pinned, [], None, "Stage 1 first pass")
        self.assertIn("Stage 1 first pass", svg)


class GenerateSvgCodingAxisTest(unittest.TestCase):
    def test_uses_coding_score_as_the_x_axis_when_any_point_has_one(self):
        pinned = [{"id": "a/model", "effective_rate_per_million": 0.09, "context_length": 100_000,
                   "coding_score": 19.65}]
        svg = freshness.generate_svg(pinned, [], None)
        self.assertIn("Price vs. coding score", svg)
        self.assertIn("coding score (llm-stats.com index_code", svg)
        self.assertNotIn("context length (log scale)", svg)

    def test_falls_back_to_context_length_when_no_point_has_a_coding_score(self):
        pinned = [{"id": "a/model", "effective_rate_per_million": 0.09, "context_length": 100_000,
                   "coding_score": None}]
        svg = freshness.generate_svg(pinned, [], None)
        self.assertIn("Price vs. context", svg)  # no stage label given
        self.assertIn("context length (log scale)", svg)

    def test_falls_back_when_the_coding_score_key_is_entirely_absent(self):
        # Points built before this feature existed (or a caller that never
        # looked one up) don't carry a "coding_score" key at all.
        pinned = [{"id": "a/model", "effective_rate_per_million": 0.09, "context_length": 100_000}]
        svg = freshness.generate_svg(pinned, [], None)
        self.assertIn("Price vs. context", svg)  # no stage label given

    def test_drops_the_max_review_size_lines_on_the_coding_score_axis(self):
        pinned = [{"id": "a/model", "effective_rate_per_million": 0.09, "context_length": 100_000,
                   "coding_score": 50.0}]
        svg = freshness.generate_svg(pinned, [], mix(working_context=10_000, max_output=500))
        self.assertNotIn("working context", svg)
        self.assertNotIn("max review output size", svg)

    def test_still_marks_inadequate_context_on_the_coding_score_axis(self):
        pinned = [{"id": "small/model", "effective_rate_per_million": 0.09, "context_length": 50_000,
                   "coding_score": 20.0}]
        svg = freshness.generate_svg(pinned, [], mix(working_context=200_000, max_output=50_000))
        self.assertIn("#d62728", svg)
        self.assertIn("inadequate context", svg)

    def test_plots_points_missing_a_coding_score_in_a_separate_no_score_column(self):
        pinned = [
            {"id": "has/score", "effective_rate_per_million": 0.09, "context_length": 100_000, "coding_score": 50.0},
            {"id": "no/score", "effective_rate_per_million": 0.02, "context_length": 200_000, "coding_score": None},
        ]
        svg = freshness.generate_svg(pinned, [], None)
        self.assertIn("has/score", svg)
        self.assertIn("no/score", svg)
        self.assertIn("no coding score available", svg)
        # hollow marker (white fill), neutral stroke matching the legend
        # swatch — pinned-vs-candidate still shows via the text label's color.
        self.assertIn("fill: #ffffff; stroke: #666666", svg)

    def test_no_score_column_omitted_when_every_point_has_a_score(self):
        pinned = [{"id": "has/score", "effective_rate_per_million": 0.09, "context_length": 100_000,
                   "coding_score": 50.0}]
        svg = freshness.generate_svg(pinned, [], None)
        self.assertNotIn("no coding score available", svg)
        self.assertNotIn("no score", svg)

    def test_no_score_ring_matches_the_legend_swatch_color_for_both_series(self):
        # A candidate's ring shouldn't be green while the legend swatch is
        # gray — same neutral color regardless of pinned vs. candidate.
        pinned = [{"id": "pinned/no-score", "effective_rate_per_million": 0.09, "context_length": 100_000,
                   "coding_score": None}]
        candidates = [{"id": "candidate/has-score", "effective_rate_per_million": 0.02, "context_length": 200_000,
                       "coding_score": 50.0},
                      {"id": "candidate/no-score", "effective_rate_per_million": 0.03, "context_length": 300_000,
                       "coding_score": None}]
        svg = freshness.generate_svg(pinned, candidates, None)
        self.assertIn("fill: #ffffff; stroke: #666666", svg)
        self.assertNotIn("fill: #ffffff; stroke: #2ca02c", svg)
        self.assertNotIn("fill: #ffffff; stroke: #1f77b4", svg)

    def test_no_score_points_still_get_marked_inadequate(self):
        pinned = [{"id": "no/score", "effective_rate_per_million": 0.02, "context_length": 50_000,
                   "coding_score": None}]
        candidates = [{"id": "has/score", "effective_rate_per_million": 0.09, "context_length": 500_000,
                       "coding_score": 50.0}]
        svg = freshness.generate_svg(pinned, candidates, mix(working_context=200_000, max_output=50_000))
        self.assertIn("fill: #ffffff; stroke: #d62728", svg)


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
        self.assertIsNone(freshness.fetch_token_mix("stage2"))

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
            mix = freshness.fetch_token_mix("stage2")
        finally:
            freshness.urllib.request.urlopen = original_urlopen

        self.assertEqual(mix["sample_count"], 2)
        total = 15 + 120 + 8
        self.assertAlmostEqual(mix["fresh_input_share"], 15 / total)
        self.assertAlmostEqual(mix["cache_read_share"], 120 / total)
        self.assertAlmostEqual(mix["output_share"], 8 / total)
        self.assertEqual(mix["max_input_tokens"], 90)  # span 1: 10 + 80
        self.assertEqual(mix["max_output_tokens"], 5)  # span 1
        self.assertEqual(mix["max_total_tokens"], 95)  # span 1: 90 + 5

    def test_none_on_request_failure(self):
        freshness.os.environ["DD_API_KEY"] = "key"
        freshness.os.environ["DD_APP_KEY"] = "app-key"
        original_urlopen = freshness.urllib.request.urlopen

        def raise_error(request, timeout=30):
            raise OSError("boom")

        freshness.urllib.request.urlopen = raise_error
        try:
            self.assertIsNone(freshness.fetch_token_mix("stage2"))
        finally:
            freshness.urllib.request.urlopen = original_urlopen


class SpanTokensTest(unittest.TestCase):
    # The real numbers off a Stage 1 codex.review span. Datadog derives
    # non_cached_input_tokens = 1150702 - 1075259 = 75443 by subtraction, which is
    # what "nested" means: input_tokens already contains the cache read.
    NESTED = {"input_tokens": 1_150_702, "cache_read_input_tokens": 1_075_259,
              "output_tokens": 4_932}
    # And off a Stage 2 pi.synthesize span, where the same subtraction yields
    # -31775. A negative token count is impossible, which is the tell that pi's
    # input_tokens counts only fresh tokens and the two fields are disjoint.
    DISJOINT = {"input_tokens": 225, "cache_read_input_tokens": 32_000,
                "output_tokens": 343}

    def test_nested_input_already_contains_the_cache_read(self):
        fresh, cache_read, output, whole = freshness.span_tokens(self.NESTED, "nested")
        self.assertEqual(fresh, 75_443)
        self.assertEqual(cache_read, 1_075_259)
        self.assertEqual(output, 4_932)
        self.assertEqual(whole, 1_150_702)

    def test_disjoint_input_is_added_to_the_cache_read(self):
        fresh, cache_read, output, whole = freshness.span_tokens(self.DISJOINT, "disjoint")
        self.assertEqual(fresh, 225)
        self.assertEqual(cache_read, 32_000)
        self.assertEqual(whole, 32_225)

    def test_reading_nested_as_disjoint_would_inflate_the_fresh_share(self):
        # The failure this convention exists to prevent: it doesn't error, it just
        # reports a fresh-input share an order of magnitude too high, which makes
        # cache-hostile models look cheap in the blended rate.
        right, _, _, _ = freshness.span_tokens(self.NESTED, "nested")
        wrong, _, _, _ = freshness.span_tokens(self.NESTED, "disjoint")
        self.assertEqual(right, 75_443)
        self.assertEqual(wrong, 1_150_702)
        self.assertGreater(wrong / right, 10)

    def test_never_returns_a_negative_fresh_count(self):
        # A nested span whose cache read exceeds its input would subtract below zero.
        fresh, _, _, _ = freshness.span_tokens(
            {"input_tokens": 100, "cache_read_input_tokens": 500, "output_tokens": 1}, "nested")
        self.assertEqual(fresh, 0)

    def test_missing_fields_count_as_zero(self):
        self.assertEqual(freshness.span_tokens({"input_tokens": 10}, "disjoint"), (10, 0, 0, 10))


class WorkingContextTokensTest(unittest.TestCase):
    def test_multi_turn_context_is_what_accumulated_not_what_was_billed(self):
        # 1.15M cumulative input over ~29 turns is ~80k of actual conversation.
        self.assertEqual(
            freshness.working_context_tokens(75_443, 1_150_702, 4_932, 29), 80_375)

    def test_single_turn_holds_its_whole_input_including_the_cache_read(self):
        # A cache read is a billing discount, not a smaller prompt — it still
        # occupies the context window, so a single turn counts the whole input.
        self.assertEqual(
            freshness.working_context_tokens(225, 32_225, 343, 1), 32_568)

    def test_unknown_turn_count_yields_no_estimate(self):
        self.assertIsNone(
            freshness.working_context_tokens(75_443, 1_150_702, 4_932, None))
        self.assertIsNone(
            freshness.working_context_tokens(75_443, 1_150_702, 4_932, 0))


class LoadPinnedTest(unittest.TestCase):
    def test_returns_only_the_models_listed_for_the_stage(self):
        path = write_models([
            {"id": "a/one", "stages": ["stage1"]},
            {"id": "b/two", "stages": ["stage2"]},
        ])
        self.assertEqual([m["id"] for m in freshness.load_pinned(path, "stage1")], ["a/one"])
        self.assertEqual([m["id"] for m in freshness.load_pinned(path, "stage2")], ["b/two"])

    def test_a_model_listed_for_both_stages_appears_in_both(self):
        path = write_models([{"id": "a/both", "stages": ["stage1", "stage2"]}])
        self.assertEqual(len(freshness.load_pinned(path, "stage1")), 1)
        self.assertEqual(len(freshness.load_pinned(path, "stage2")), 1)

    def test_a_model_with_no_stages_belongs_to_every_stage(self):
        # Surfacing an unlabelled model in both charts beats silently dropping it
        # from both, which is what filtering it out would do.
        path = write_models([{"id": "a/unlabelled"}])
        self.assertEqual(len(freshness.load_pinned(path, "stage1")), 1)
        self.assertEqual(len(freshness.load_pinned(path, "stage2")), 1)


class FetchTokenMixStageTest(unittest.TestCase):
    """fetch_token_mix over a stage whose window straddles a harness rename."""

    def setUp(self):
        self.original_environ = dict(freshness.os.environ)
        freshness.os.environ["DD_API_KEY"] = "key"
        freshness.os.environ["DD_APP_KEY"] = "app-key"
        self.original_urlopen = freshness.urllib.request.urlopen

    def tearDown(self):
        freshness.urllib.request.urlopen = self.original_urlopen
        freshness.os.environ.clear()
        freshness.os.environ.update(self.original_environ)

    def serve(self, by_span_name):
        """Answer each search with the page registered for the span name it queries."""
        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                import io
                return io.BytesIO(json.dumps(self.payload).encode())

            def __exit__(self, *args):
                return False

        def fake_urlopen(request, timeout=30):
            body = json.loads(request.data.decode())
            query = body["data"]["attributes"]["filter"]["query"]
            name = query.split(":", 1)[1]
            return FakeResponse({"data": by_span_name.get(name, []), "meta": {"page": {}}})

        freshness.urllib.request.urlopen = fake_urlopen

    def test_reads_each_span_name_under_its_own_convention(self):
        # Stage 1 queries pi.first_pass (disjoint) and codex.review (nested). Read
        # correctly both spans carry 100 fresh / 900 cached / 10 output.
        self.serve({
            "pi.first_pass": [{"attributes": {"metrics": {
                "input_tokens": 100, "cache_read_input_tokens": 900, "output_tokens": 10}}}],
            "codex.review": [{"attributes": {"metrics": {
                "input_tokens": 1000, "cache_read_input_tokens": 900, "output_tokens": 10}}}],
        })
        mix = freshness.fetch_token_mix("stage1")

        self.assertEqual(mix["sample_count"], 2)
        self.assertEqual(mix["spans"], {"pi.first_pass": 1, "codex.review": 1})
        total = 200 + 1800 + 20
        self.assertAlmostEqual(mix["fresh_input_share"], 200 / total)
        self.assertAlmostEqual(mix["cache_read_share"], 1800 / total)
        # Both spans describe the same 1010-token request despite reporting it
        # differently, so neither is double-counted.
        self.assertEqual(mix["max_input_tokens"], 1000)

    def test_context_estimate_uses_only_spans_reporting_a_turn_count(self):
        self.serve({"pi.synthesize": [
            {"attributes": {"metrics": {"input_tokens": 100, "cache_read_input_tokens": 900,
                                        "output_tokens": 10, "turn_count": 5}}},
            {"attributes": {"metrics": {"input_tokens": 100, "cache_read_input_tokens": 900,
                                        "output_tokens": 10}}},  # pre-instrumentation
        ]})
        mix = freshness.fetch_token_mix("stage2")

        self.assertEqual(mix["sample_count"], 2)  # both count toward the price mix
        self.assertEqual(mix["context"]["sample_count"], 1)  # only one toward context
        self.assertEqual(mix["context"]["max_working_context_tokens"], 110)  # 100 fresh + 10 out
        self.assertEqual(mix["context"]["max_turns"], 5)

    def test_context_is_absent_when_no_span_reports_a_turn_count(self):
        self.serve({"pi.synthesize": [{"attributes": {"metrics": {
            "input_tokens": 100, "cache_read_input_tokens": 900, "output_tokens": 10}}}]})
        mix = freshness.fetch_token_mix("stage2")

        self.assertIsNone(mix["context"])
        self.assertEqual(mix["sample_count"], 1)

    def test_a_stage_whose_spans_return_nothing_yields_no_mix(self):
        self.serve({})
        self.assertIsNone(freshness.fetch_token_mix("stage1"))


class MainStageTest(unittest.TestCase):
    def setUp(self):
        self.originals = (freshness.fetch_catalog, freshness.fetch_uptime,
                          freshness.fetch_token_mix, freshness.fetch_coding_scores)
        freshness.fetch_uptime = lambda model_id: 99.9
        freshness.fetch_token_mix = lambda stage: None
        freshness.fetch_coding_scores = lambda: []
        freshness.fetch_catalog = lambda: ({}, None)

    def tearDown(self):
        (freshness.fetch_catalog, freshness.fetch_uptime,
         freshness.fetch_token_mix, freshness.fetch_coding_scores) = self.originals

    def run_main(self, *args):
        import io
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = freshness.main(['check_model_freshness.py', *args])
        return json.loads(buffer.getvalue()), exit_code

    def test_checks_only_the_requested_stage_s_models(self):
        path = write_models([
            {"id": "a/stage-one", "stages": ["stage1"]},
            {"id": "b/stage-two", "stages": ["stage2"]},
        ])
        result, _ = self.run_main("--stage=stage1", path)
        self.assertEqual(result["stage"], "stage1")
        self.assertEqual(result["missing"], ["a/stage-one"])

    def test_defaults_to_stage_two(self):
        path = write_models([
            {"id": "a/stage-one", "stages": ["stage1"]},
            {"id": "b/stage-two", "stages": ["stage2"]},
        ])
        result, _ = self.run_main(path)
        self.assertEqual(result["stage"], "stage2")
        self.assertEqual(result["missing"], ["b/stage-two"])

    def test_each_stage_defaults_to_its_own_chart_path(self):
        path = write_models([{"id": "a/one", "stages": ["stage1"],
                              "cost": {"input": 0.1, "output": 0.4, "cacheRead": 0.01}}])
        freshness.fetch_catalog = lambda: ({"a/one": {
            "pricing": {"prompt": "0.0000001"}, "supported_parameters": ["reasoning"],
            "context_length": 1_000_000}}, None)
        freshness.fetch_uptime = lambda model_id: 90.0  # unreliable -> notable
        chart = str(Path(tempfile.mkdtemp()) / 'stage1.svg')
        result, _ = self.run_main("--stage=stage1", path, chart)
        self.assertEqual(result["chart_path"], chart)
        # and without an explicit path it falls back to the stage's own default
        self.assertEqual(freshness.STAGES["stage1"]["chart_path"],
                         "docs/model-freshness/stage1.svg")
        self.assertNotEqual(freshness.STAGES["stage1"]["chart_path"],
                            freshness.STAGES["stage2"]["chart_path"])

    def test_an_unknown_stage_is_reported_not_raised(self):
        path = write_models([{"id": "a/one"}])
        result, exit_code = self.run_main("--stage=stage9", path)
        self.assertEqual(exit_code, 0)
        self.assertIn("unknown stage", result["error"])
        self.assertFalse(result["notable"])


class MainTest(unittest.TestCase):
    def setUp(self):
        self.original_fetch_catalog = freshness.fetch_catalog
        self.original_fetch_uptime = freshness.fetch_uptime
        self.original_fetch_token_mix = freshness.fetch_token_mix
        self.original_fetch_coding_scores = freshness.fetch_coding_scores
        freshness.fetch_uptime = lambda model_id: 99.9
        freshness.fetch_token_mix = lambda stage: None
        freshness.fetch_coding_scores = lambda: []

    def tearDown(self):
        freshness.fetch_catalog = self.original_fetch_catalog
        freshness.fetch_uptime = self.original_fetch_uptime
        freshness.fetch_token_mix = self.original_fetch_token_mix
        freshness.fetch_coding_scores = self.original_fetch_coding_scores

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
        freshness.fetch_token_mix = lambda stage: HEAVY_CACHE_MIX

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

    def test_charts_by_coding_score_when_the_pinned_model_has_one(self):
        models_path = write_models([PINNED])
        live_entry = {"pricing": {"prompt": "0.00000009", "completion": "0.0000003",
                                   "input_cache_read": "0.000000018", "input_cache_write": "0"},
                      "supported_parameters": ["reasoning"], "context_length": 1_000_000}
        freshness.fetch_catalog = lambda: ({"z-ai/glm-5.3-flash": live_entry}, None)
        freshness.fetch_uptime = lambda model_id: 90.0  # unreliable -> notable
        freshness.fetch_coding_scores = lambda: [
            {"model_id": "glm-5.3-flash", "organization_id": "z-ai", "index_code": 61.2}
        ]
        chart_path = str(Path(tempfile.mkdtemp()) / 'frontier.svg')

        _, _ = self.run_main(models_path, chart_path)

        self.assertIn("price vs. coding score", Path(chart_path).read_text())

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
