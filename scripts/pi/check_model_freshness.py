#!/usr/bin/env python3
"""Weekly check for scripts/pi/models.json drifting off OpenRouter's price/quality
frontier.

This never decides anything on its own — review quality isn't something an API can
score, so picking a replacement model stays a human call. All this does is surface
the two things that ARE checkable automatically:

  1. Rate drift: the $/1M rates pinned in models.json are what pi uses to compute
     cost.total for Datadog (see llm-usage.py's docstring). If OpenRouter's list
     price for a pinned model has moved, the billing we report is wrong until
     someone updates the pin.
  2. Cheaper same-tier candidates: reasoning-capable models with >=100k context
     and at least MIN_UPTIME_PCT uptime that currently cost less than the
     cheapest pinned model on an *effective* $/1M basis (see below), so there's
     something concrete to look at when deciding whether to roll the default
     forward. A cheaper model that's down 5%+ of the time isn't actually a
     saving.
  3. Unreliable pinned models: any currently-pinned model whose uptime has
     dropped below MIN_UPTIME_PCT, since that's worth knowing even with no
     cheaper alternative in sight.

Why "effective" $/1M instead of the raw input rate: pi's review prompts are
almost entirely re-sent context, so OpenRouter's prompt-cache discount — not the
list input rate — dominates real cost. Sampling pi's own `pi.synthesize` spans
from Datadog LLM Observability (see fetch_token_mix) over a trailing window
consistently shows something like 90% cache-read tokens, ~9% fresh input, ~1%
output — ranking or charting models by raw input price alone would be comparing
list prices nobody actually pays. effective_rate_per_million blends a model's
input/cacheRead/output rates by that observed share, falling back to the raw
input rate when Datadog credentials (DD_API_KEY/DD_APP_KEY) aren't set or the
lookup fails, so the check still degrades gracefully without them.

When anything above is notable, also writes an SVG chart (pinned models vs.
candidates, price using the effective rate) to the given chart path,
dependency-free (plain XML, no matplotlib) so it needs nothing beyond the
stdlib in CI. The x-axis is a coding-specific score scraped from
llm-stats.com's public leaderboard (see fetch_coding_scores) when at least
one plotted model has one, since that's more relevant to a code-review bot
than raw context length; it falls back to context length otherwise. Uptime
isn't charted: OpenRouter's public API doesn't expose throughput/latency
(always null), and after the >=95% filter the remaining uptime spread is too
small to be a useful axis, so it stays a table-only reliability gate instead.

Prints one JSON object to stdout: {"drift": [...], "missing": [...],
"candidates": [...], "unreliable_pinned": [...], "token_mix": {...} | null,
"notable": bool}. Never raises and always exits 0 — this is a weekly
nudge-to-look, not a check that should ever fail CI.

Usage: check_model_freshness.py <path-to-models.json> [chart-output-path]
"""
import json
import math
import os
import re
import sys
import urllib.request
from xml.sax.saxutils import escape as xml_escape

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{model_id}/endpoints"
DRIFT_THRESHOLD = 0.05  # flag a pinned rate more than 5% off the live list price
CANDIDATE_MIN_CONTEXT = 100_000
CANDIDATE_LIMIT = 5
CANDIDATE_POOL_SIZE = 15  # cheaper-than-pinned pool checked for uptime before limiting
MIN_UPTIME_PCT = 95.0  # a model down more than 5% of the time isn't a real saving
DEFAULT_CHART_PATH = "docs/model-freshness/frontier.svg"
LEGEND_WIDTH = 140  # dedicated gutter left of the y-axis so the legend never sits over plotted points
CHART_WIDTH = 640 + LEGEND_WIDTH
CHART_HEIGHT = 420
CHART_MARGIN = {"left": 60 + LEGEND_WIDTH, "right": 20, "top": 30, "bottom": 50}
CHART_PADDING = 16  # blank border around the whole chart, outside CHART_WIDTH/CHART_HEIGHT
PLOT_PADDING_FRACTION = 0.08  # headroom inside the axes so extreme points aren't flush against them
NO_SCORE_GAP_FRACTION = 0.22  # how far left of the real coding-score domain the "no score" column sits

# Real usage mix for the pi review pass, sampled from Datadog LLM Observability
# spans. A longer window smooths out any single noisy week; refreshed on every
# run rather than hardcoded so the blend tracks actual usage as it drifts.
DATADOG_SPANS_SEARCH_URL = "https://api.datadoghq.com/api/v2/llm-obs/v1/spans/events/search"
TOKEN_MIX_QUERY = "@name:pi.synthesize"
TOKEN_MIX_WINDOW = "now-90d"
TOKEN_MIX_PAGE_LIMIT = 100
TOKEN_MIX_MAX_SPANS = 1000  # hard cap so a slow query can't hang the workflow

# llm-stats.com has no public API for this (its documented one doesn't expose
# benchmark scores at all), but its leaderboard page server-renders a coding
# score (index_code, a composite of SWE-bench/HumanEval/LiveCodeBench/etc.)
# straight into a Next.js RSC payload embedded in the HTML. No auth needed,
# but also no stability contract, so fetch_coding_scores() degrades to an
# empty list on any parsing failure rather than ever raising.
LLM_STATS_LEADERBOARD_URL = "https://llm-stats.com/leaderboards/llm-leaderboard"
DATE_SUFFIX_RE = re.compile(r"-\d{8}$")


def per_token_to_per_million(value):
    try:
        return float(value) * 1_000_000
    except (TypeError, ValueError):
        return None


def fetch_catalog():
    try:
        with urllib.request.urlopen(OPENROUTER_MODELS_URL, timeout=30) as response:
            payload = json.load(response)
    except Exception as error:  # network/format issues never fail the workflow
        return {}, str(error)
    catalog = {}
    for entry in payload.get("data", []):
        model_id = entry.get("id")
        if model_id:
            catalog[model_id] = entry
    return catalog, None


def fetch_uptime(model_id):
    """Best uptime_last_1d across a model's provider endpoints, or None if the
    lookup fails. A model routed across providers is as reliable as its best
    currently-healthy provider, not its worst."""
    url = OPENROUTER_ENDPOINTS_URL.format(model_id=model_id)
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = json.load(response)
    except Exception:
        return None
    uptimes = [
        endpoint.get("uptime_last_1d")
        for endpoint in payload.get("data", {}).get("endpoints", [])
        if endpoint.get("uptime_last_1d") is not None
    ]
    return round(max(uptimes), 2) if uptimes else None


def fetch_token_mix():
    """Sum input/cache-read/output tokens across the pi review pass's own
    `pi.synthesize` spans over TOKEN_MIX_WINDOW, via Datadog's LLM Observability
    spans search API. Returns {"fresh_input_share", "cache_read_share",
    "output_share", "max_input_tokens", "max_output_tokens", "max_total_tokens",
    "sample_count", "window"} or None when DD_API_KEY/DD_APP_KEY aren't set, the
    request fails, or no spans have usable metrics — callers fall back to the
    raw input rate in that case.

    max_input_tokens/max_output_tokens/max_total_tokens are the single largest
    per-span values seen, not averages: a model only needs to worry about the
    worst review it might see, not the typical one, so an average would
    understate how much context is actually required.

    pi's own instrumentation reports `input_tokens` as fresh (non-cached) tokens
    only, disjoint from `cache_read_input_tokens` — unlike some other passes'
    spans, where input_tokens includes the cached portion. Summing the three
    fields directly (no subtraction) is what's correct for this pass."""
    api_key = os.environ.get("DD_API_KEY")
    app_key = os.environ.get("DD_APP_KEY")
    if not api_key or not app_key:
        return None

    fresh_input = cache_read = output = sample_count = 0
    max_input_tokens = max_output_tokens = max_total_tokens = 0
    cursor = None
    while sample_count < TOKEN_MIX_MAX_SPANS:
        filter_ = {"from": TOKEN_MIX_WINDOW, "to": "now", "query": TOKEN_MIX_QUERY}
        page = {"limit": TOKEN_MIX_PAGE_LIMIT}
        if cursor:
            page["cursor"] = cursor
        body = json.dumps({"data": {"type": "spans", "attributes": {"filter": filter_, "page": page}}}).encode()
        request = urllib.request.Request(
            DATADOG_SPANS_SEARCH_URL,
            data=body,
            method="POST",
            headers={
                "DD-API-KEY": api_key,
                "DD-APPLICATION-KEY": app_key,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except Exception:
            break

        spans = payload.get("data") or []
        for span in spans:
            metrics = (span.get("attributes") or {}).get("metrics")
            if not metrics or "input_tokens" not in metrics:
                continue
            span_input = metrics.get("input_tokens", 0) + metrics.get("cache_read_input_tokens", 0)
            span_output = metrics.get("output_tokens", 0)
            fresh_input += metrics.get("input_tokens", 0)
            cache_read += metrics.get("cache_read_input_tokens", 0)
            output += span_output
            sample_count += 1
            max_input_tokens = max(max_input_tokens, span_input)
            max_output_tokens = max(max_output_tokens, span_output)
            max_total_tokens = max(max_total_tokens, span_input + span_output)

        cursor = ((payload.get("meta") or {}).get("page") or {}).get("after")
        if not cursor or not spans:
            break

    total = fresh_input + cache_read + output
    if total <= 0 or sample_count <= 0:
        return None
    return {
        "fresh_input_share": fresh_input / total,
        "cache_read_share": cache_read / total,
        "output_share": output / total,
        "max_input_tokens": max_input_tokens,
        "max_output_tokens": max_output_tokens,
        "max_total_tokens": max_total_tokens,
        "sample_count": sample_count,
        "window": TOKEN_MIX_WINDOW,
    }


def fetch_coding_scores():
    """Scrape llm-stats.com's public leaderboard page for its per-model coding
    score. The page is a Next.js app; the data isn't in the initial HTML tags,
    it's inside a `self.__next_f.push([1, "..."])` call whose argument is a
    JS-string-escaped JSON blob containing an `"initialData": [...]` array.
    Returns that array (a list of dicts with at least `model_id`,
    `organization_id`, and `index_code`), or [] on any fetch/parse failure."""
    try:
        request = urllib.request.Request(
            LLM_STATS_LEADERBOARD_URL, headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            html = response.read().decode("utf-8", errors="replace")
    except Exception:
        return []

    for match in re.finditer(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S):
        chunk = match.group(1)
        if "initialData" not in chunk:
            continue
        try:
            unescaped = chunk.encode().decode("unicode_escape")
        except Exception:
            continue
        marker = 'initialData":['
        start = unescaped.find(marker)
        if start == -1:
            continue
        start += len(marker) - 1  # keep the leading '['
        depth = 0
        end = None
        for i in range(start, len(unescaped)):
            char = unescaped[i]
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is None:
            continue
        try:
            return json.loads(unescaped[start:end])
        except Exception:
            continue
    return []


def _normalize_model_slug(slug):
    return DATE_SUFFIX_RE.sub("", slug.lower().replace(".", "-"))


def build_coding_score_index(scores):
    """Key llm-stats.com's leaderboard rows by (org, normalized model slug) so
    they can be looked up by OpenRouter catalog id. Normalizing strips a
    trailing release-date suffix llm-stats adds that OpenRouter's ids never
    have, and swaps dots for dashes since the two sites disagree on that too
    (`claude-haiku-4.5` vs `claude-haiku-4-5-20251001`). When normalization
    collapses more than one leaderboard row onto the same key (different
    dated snapshots of the same model), keeps the one with the highest
    index_code rather than an arbitrary one."""
    index = {}
    for entry in scores:
        org = entry.get("organization_id")
        model_id = entry.get("model_id")
        if not org or not model_id:
            continue
        key = (org, _normalize_model_slug(model_id))
        existing = index.get(key)
        if existing is None or (entry.get("index_code") or -1) > (existing.get("index_code") or -1):
            index[key] = entry
    return index


def coding_score_for(catalog_id, index):
    """Look up a catalog id's llm-stats.com entry, or None when there's no
    confident match. Never matches across org prefixes (an `anthropic/`
    catalog id can only match an `anthropic` llm-stats row) even if the
    model-name tokens happen to look alike."""
    if "/" not in catalog_id:
        return None
    org, slug = catalog_id.split("/", 1)
    return index.get((org, _normalize_model_slug(slug)))


def effective_rate_per_million(rates, token_mix):
    """Blend a model's input/cacheRead/output $/1M rates by token_mix's observed
    shares. Falls back to the raw input rate when there's no mix to blend with,
    or the model has no input rate at all. A rate missing cacheRead/output
    pricing (data gaps happen) falls back to the input rate for that share
    rather than dropping the model or crashing."""
    input_rate = rates.get("input")
    if input_rate is None:
        return None
    if not token_mix:
        return input_rate
    cache_rate = rates.get("cacheRead")
    cache_rate = cache_rate if cache_rate is not None else input_rate
    output_rate = rates.get("output")
    output_rate = output_rate if output_rate is not None else input_rate
    return (
        token_mix["fresh_input_share"] * input_rate
        + token_mix["cache_read_share"] * cache_rate
        + token_mix["output_share"] * output_rate
    )


def load_pinned(path):
    with open(path) as handle:
        config = json.load(handle)
    return config["providers"]["openrouter"]["models"]


def rate_drift(pinned, live_entry):
    """Compare pinned $/1M rates to OpenRouter's current list price. Returns a list
    of per-field drift descriptions, empty when everything is within threshold."""
    pricing = live_entry.get("pricing", {})
    field_map = {
        "input": "prompt",
        "output": "completion",
        "cacheRead": "input_cache_read",
        "cacheWrite": "input_cache_write",
    }
    drifts = []
    for pinned_field, live_field in field_map.items():
        pinned_rate = pinned.get("cost", {}).get(pinned_field)
        live_rate = per_token_to_per_million(pricing.get(live_field))
        if pinned_rate is None or live_rate is None:
            continue
        if pinned_rate == 0 and live_rate == 0:
            continue
        baseline = max(pinned_rate, live_rate, 1e-9)
        if abs(pinned_rate - live_rate) / baseline > DRIFT_THRESHOLD:
            drifts.append({
                "field": pinned_field,
                "pinned": pinned_rate,
                "live": round(live_rate, 6),
            })
    return drifts


def is_reasoning_capable(entry):
    params = entry.get("supported_parameters") or []
    return "reasoning" in params or "include_reasoning" in params


def find_candidates(catalog, pinned_ids, cheapest_pinned_effective_rate, token_mix):
    pool = []
    for model_id, entry in catalog.items():
        if model_id in pinned_ids:
            continue
        if not is_reasoning_capable(entry):
            continue
        if (entry.get("context_length") or 0) < CANDIDATE_MIN_CONTEXT:
            continue
        pricing = entry.get("pricing", {})
        input_rate = per_token_to_per_million(pricing.get("prompt"))
        if input_rate is None or input_rate <= 0:
            continue
        rates = {
            "input": input_rate,
            "cacheRead": per_token_to_per_million(pricing.get("input_cache_read")),
            "output": per_token_to_per_million(pricing.get("completion")),
        }
        effective_rate = effective_rate_per_million(rates, token_mix)
        if cheapest_pinned_effective_rate is not None and effective_rate >= cheapest_pinned_effective_rate:
            continue
        pool.append({
            "id": model_id,
            "name": entry.get("name"),
            "context_length": entry.get("context_length"),
            "input_rate_per_million": round(input_rate, 4),
            "effective_rate_per_million": round(effective_rate, 4),
        })
    pool.sort(key=lambda c: c["effective_rate_per_million"])

    candidates = []
    for candidate in pool[:CANDIDATE_POOL_SIZE]:
        uptime = fetch_uptime(candidate["id"])
        if uptime is None or uptime < MIN_UPTIME_PCT:
            continue
        candidates.append({**candidate, "uptime_pct": uptime})
        if len(candidates) == CANDIDATE_LIMIT:
            break
    return candidates


def generate_svg(pinned_points, candidate_points, token_mix):
    """Plain-XML price-vs-x scatter, pinned models vs. candidates.

    The x-axis is the model's coding score from llm-stats.com's leaderboard
    (see fetch_coding_scores) when at least one plottable point has one,
    since that's a more relevant axis for a code-review bot than raw context
    length. It falls back to context length (log scale, with the max
    review input/output size reference lines) when no point has a coding
    score — a llm-stats.com fetch/parse failure, or no matches for any
    plotted model, degrades to the old behavior rather than losing the
    chart. The y-axis (effective $/1M) is always log scale.

    On the coding-score axis, a plottable point (positive rate and context)
    that just has no coding score match isn't dropped — llm-stats.com's
    leaderboard doesn't cover every model, and a model missing from the
    chart looks like it was never considered, not like data was
    unavailable. It's plotted in a separate "no score" column to the left
    of the real axis, visually separated by a dashed line, same as how a
    too-small context gets a color instead of being hidden.

    Returns None when there's nothing plottable (no positive rate, or no
    positive value at all for whichever x-axis gets picked)."""
    all_points = pinned_points + candidate_points
    rates = [p["effective_rate_per_million"] for p in all_points if (p.get("effective_rate_per_million") or 0) > 0]
    if not rates:
        return None

    max_input_tokens = (token_mix or {}).get("max_input_tokens") or 0
    max_output_tokens = (token_mix or {}).get("max_output_tokens") or 0
    max_total_tokens = (token_mix or {}).get("max_total_tokens") or 0

    coding_scores = [p["coding_score"] for p in all_points if p.get("coding_score") is not None]
    use_coding_axis = bool(coding_scores)
    no_score_x = None

    if use_coding_axis:
        x_field = "coding_score"
        x_log_scale = False
        x_values = coding_scores
        x_title = "coding score (llm-stats.com index_code, higher is better)"
        chart_title = "Price vs. coding score"
        has_unscored_point = any(
            (p.get("effective_rate_per_million") or 0) > 0
            and (p.get("context_length") or 0) > 0
            and p.get("coding_score") is None
            for p in all_points
        )
        if has_unscored_point:
            score_min, score_max = min(coding_scores), max(coding_scores)
            span = (score_max - score_min) or max(abs(score_min), 1)
            no_score_x = score_min - span * NO_SCORE_GAP_FRACTION
            x_values = x_values + [no_score_x]
    else:
        contexts = [p["context_length"] for p in all_points if (p.get("context_length") or 0) > 0]
        if not contexts:
            return None
        x_field = "context_length"
        x_log_scale = True
        x_values = contexts + [n for n in (max_input_tokens, max_output_tokens) if n > 0]
        x_title = "context length (log scale)"
        chart_title = "Price vs. context"

    plot_w = CHART_WIDTH - CHART_MARGIN["left"] - CHART_MARGIN["right"]
    plot_h = CHART_HEIGHT - CHART_MARGIN["top"] - CHART_MARGIN["bottom"]

    def to_scale(value):
        return math.log10(value) if x_log_scale else value

    def from_scale(value):
        return 10 ** value if x_log_scale else value

    x_min, x_max = min(x_values), max(x_values)
    x_scale_min, x_scale_max = to_scale(x_min), to_scale(x_max)
    if x_scale_min == x_scale_max:
        x_scale_min, x_scale_max = x_scale_min - 0.5, x_scale_max + 0.5

    y_log_min, y_log_max = math.log10(min(rates)), math.log10(max(rates))
    if y_log_min == y_log_max:
        y_log_min, y_log_max = y_log_min - 0.5, y_log_max + 0.5

    # Pad the plotted domain beyond the actual data range so points and lines
    # near an extreme don't render flush against the axis.
    x_pad = (x_scale_max - x_scale_min) * PLOT_PADDING_FRACTION
    x_scale_min, x_scale_max = x_scale_min - x_pad, x_scale_max + x_pad
    y_pad = (y_log_max - y_log_min) * PLOT_PADDING_FRACTION
    y_log_min, y_log_max = y_log_min - y_pad, y_log_max + y_pad

    def x_pos(value):
        value = min(max(value, x_min), x_max)
        frac = (to_scale(value) - x_scale_min) / (x_scale_max - x_scale_min)
        return CHART_MARGIN["left"] + frac * plot_w

    def y_pos(rate):
        rate = max(rate, min(rates))
        frac = (math.log10(rate) - y_log_min) / (y_log_max - y_log_min)
        return CHART_MARGIN["top"] + (1 - frac) * plot_h  # cheaper (lower rate) plots higher

    plot_center_x = CHART_MARGIN["left"] + plot_w / 2
    y_title_x = CHART_MARGIN["left"] - 44

    padded_width = CHART_WIDTH + 2 * CHART_PADDING
    padded_height = CHART_HEIGHT + 2 * CHART_PADDING

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{padded_width}" height="{padded_height}" '
        f'font-family="sans-serif" font-size="11">',
        f'<rect width="{padded_width}" height="{padded_height}" fill="white"/>',
        f'<g transform="translate({CHART_PADDING} {CHART_PADDING})">',
        f'<text x="{plot_center_x}" y="18" text-anchor="middle" font-size="13" font-weight="bold">'
        f'{chart_title}</text>',
        f'<line x1="{CHART_MARGIN["left"]}" y1="{CHART_MARGIN["top"]}" '
        f'x2="{CHART_MARGIN["left"]}" y2="{CHART_HEIGHT - CHART_MARGIN["bottom"]}" stroke="black"/>',
        f'<line x1="{CHART_MARGIN["left"]}" y1="{CHART_HEIGHT - CHART_MARGIN["bottom"]}" '
        f'x2="{CHART_WIDTH - CHART_MARGIN["right"]}" y2="{CHART_HEIGHT - CHART_MARGIN["bottom"]}" stroke="black"/>',
        f'<text x="{plot_center_x}" y="{CHART_HEIGHT - 10}" text-anchor="middle">{x_title}</text>',
        f'<text x="{y_title_x}" y="{CHART_HEIGHT / 2}" text-anchor="middle" '
        f'transform="rotate(-90 {y_title_x} {CHART_HEIGHT / 2})">effective $/1M (log scale)</text>',
    ]

    def format_context(n):
        n = round(n)
        return f'{n / 1_000_000:.3g}M' if n >= 1_000_000 else f'{n / 1_000:.0f}k' if n >= 1_000 else str(n)

    def format_x_tick(value):
        return f'{value:.3g}' if use_coding_axis else format_context(value)

    tick_count = 5
    for i in range(tick_count):
        rate = 10 ** (y_log_min + (y_log_max - y_log_min) * i / (tick_count - 1))
        y = y_pos(rate)
        label = f'{rate:.3g}'
        parts.append(f'<text x="{CHART_MARGIN["left"] - 6}" y="{y + 3}" text-anchor="end">{label}</text>')
        parts.append(
            f'<line x1="{CHART_MARGIN["left"]}" y1="{y}" x2="{CHART_WIDTH - CHART_MARGIN["right"]}" '
            f'y2="{y}" stroke="#eee"/>'
        )

    # score_min is only defined when there's a "no score" gutter (use_coding_axis
    # and no_score_x is not None); ticks that fall inside that gutter would show
    # a meaningless number on the real coding-score scale, so skip them.
    for i in range(tick_count):
        value = from_scale(x_scale_min + (x_scale_max - x_scale_min) * i / (tick_count - 1))
        if no_score_x is not None and value < score_min:
            continue
        x = x_pos(value)
        parts.append(
            f'<text x="{x:.1f}" y="{CHART_HEIGHT - CHART_MARGIN["bottom"] + 16}" text-anchor="middle">'
            f'{format_x_tick(value)}</text>'
        )
        parts.append(
            f'<line x1="{x:.1f}" y1="{CHART_MARGIN["top"]}" x2="{x:.1f}" '
            f'y2="{CHART_HEIGHT - CHART_MARGIN["bottom"]}" stroke="#eee"/>'
        )

    if no_score_x is not None:
        separator_x = x_pos(score_min)
        parts.append(
            f'<line x1="{separator_x:.1f}" y1="{CHART_MARGIN["top"]}" x2="{separator_x:.1f}" '
            f'y2="{CHART_HEIGHT - CHART_MARGIN["bottom"]}" stroke="#999" stroke-dasharray="2,3"/>'
        )
        parts.append(
            f'<text x="{x_pos(no_score_x):.1f}" y="{CHART_HEIGHT - CHART_MARGIN["bottom"] + 16}" '
            f'text-anchor="middle" fill="#999">no score</text>'
        )

    def draw_max_line(value, label, label_y, dash_color):
        x = x_pos(value)
        parts.append(
            f'<line x1="{x:.1f}" y1="{CHART_MARGIN["top"]}" x2="{x:.1f}" '
            f'y2="{CHART_HEIGHT - CHART_MARGIN["bottom"]}" stroke="{dash_color}" stroke-width="1.5" '
            f'stroke-dasharray="4,3"/>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{label_y}" text-anchor="middle" fill="{dash_color}">'
            f'{label} ({format_context(value)})</text>'
        )

    # The max review input/output size lines are context-length values, so
    # they only make sense positioned on a context-length x-axis.
    if not use_coding_axis:
        if max_input_tokens > 0:
            draw_max_line(max_input_tokens, "max review input size", CHART_MARGIN["top"] + 12, "#999")
        if max_output_tokens > 0:
            draw_max_line(max_output_tokens, "max review output size", CHART_MARGIN["top"] + 24, "#c49a00")

    INADEQUATE_COLOR = "#d62728"
    NO_SCORE_COLOR = "#666"

    def plot(points, color):
        for point in points:
            rate = point.get("effective_rate_per_million")
            context_length = point.get("context_length")
            x_value = point.get(x_field)
            if not rate or rate <= 0 or not context_length or context_length <= 0:
                continue
            no_score = x_value is None and no_score_x is not None
            if x_value is None and not no_score:
                continue
            if x_value is not None and x_log_scale and x_value <= 0:
                continue
            x_value = no_score_x if no_score else x_value
            inadequate = bool(max_total_tokens) and context_length < max_total_tokens
            point_color = INADEQUATE_COLOR if inadequate else color
            x, y = x_pos(x_value), y_pos(rate)
            title = xml_escape(f'{point["id"]}: ${rate}/1M effective, {context_length:,} context')
            if point.get("coding_score") is not None:
                title += xml_escape(f', coding score {point["coding_score"]:.1f}')
            elif no_score:
                title += xml_escape(", no coding score available")
            if inadequate:
                title += xml_escape(" (context too small for the worst-case review)")
            if no_score:
                # The pinned-vs-candidate color still shows on the adjacent text
                # label, so the ring itself can stay neutral (matching the
                # legend swatch) unless it's also flagged inadequate.
                ring_color = INADEQUATE_COLOR if inadequate else NO_SCORE_COLOR
                parts.append(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="white" stroke="{ring_color}" '
                    f'stroke-width="2"><title>{title}</title></circle>'
                )
            else:
                parts.append(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{point_color}" fill-opacity="0.85">'
                    f'<title>{title}</title></circle>'
                )
            near_right_edge = x > CHART_WIDTH - CHART_MARGIN["right"] - 100
            label_x = x - 7 if near_right_edge else x + 7
            anchor = 'end' if near_right_edge else 'start'
            parts.append(
                f'<text x="{label_x:.1f}" y="{y + 3:.1f}" text-anchor="{anchor}" '
                f'fill="{point_color}">{xml_escape(point["id"])}</text>'
            )

    plot(pinned_points, "#1f77b4")
    plot(candidate_points, "#2ca02c")

    legend_x, legend_y = 14, CHART_MARGIN["top"]
    parts.append(f'<circle cx="{legend_x}" cy="{legend_y}" r="5" fill="#1f77b4"/>')
    parts.append(f'<text x="{legend_x + 10}" y="{legend_y + 4}">pinned</text>')
    parts.append(f'<circle cx="{legend_x}" cy="{legend_y + 16}" r="5" fill="#2ca02c"/>')
    parts.append(f'<text x="{legend_x + 10}" y="{legend_y + 20}">candidate</text>')
    legend_row = 2
    if max_total_tokens:
        y = legend_y + 16 * legend_row
        parts.append(f'<circle cx="{legend_x}" cy="{y}" r="5" fill="{INADEQUATE_COLOR}"/>')
        parts.append(f'<text x="{legend_x + 10}" y="{y + 4}">inadequate context</text>')
        legend_row += 1
    if no_score_x is not None:
        y = legend_y + 16 * legend_row
        parts.append(f'<circle cx="{legend_x}" cy="{y}" r="5" fill="white" stroke="{NO_SCORE_COLOR}" stroke-width="2"/>')
        parts.append(f'<text x="{legend_x + 10}" y="{y + 4}">no coding score available</text>')

    parts.append('</g>')
    parts.append('</svg>')
    return "\n".join(parts)


def write_svg(path, svg):
    import os
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as handle:
        handle.write(svg)


def main(argv):
    models_path = argv[1] if len(argv) > 1 else "scripts/pi/models.json"
    chart_path = argv[2] if len(argv) > 2 else DEFAULT_CHART_PATH
    result = {"drift": [], "missing": [], "candidates": [], "unreliable_pinned": [], "notable": False}
    try:
        pinned = load_pinned(models_path)
    except Exception as error:
        result["error"] = f"could not read {models_path}: {error}"
        print(json.dumps(result))
        return 0

    catalog, fetch_error = fetch_catalog()
    if fetch_error:
        result["error"] = f"could not fetch OpenRouter catalog: {fetch_error}"
        print(json.dumps(result))
        return 0

    token_mix = fetch_token_mix()
    result["token_mix"] = token_mix

    pinned_ids = {model["id"] for model in pinned}
    cheapest_pinned_effective_rate = None
    pinned_points = []
    for model in pinned:
        live_entry = catalog.get(model["id"])
        if live_entry is None:
            result["missing"].append(model["id"])
            continue
        drifts = rate_drift(model, live_entry)
        if drifts:
            result["drift"].append({"id": model["id"], "fields": drifts})
        uptime = fetch_uptime(model["id"])
        if uptime is not None and uptime < MIN_UPTIME_PCT:
            result["unreliable_pinned"].append({"id": model["id"], "uptime_pct": uptime})
        cost = model.get("cost", {})
        rate = cost.get("input")
        effective_rate = effective_rate_per_million(
            {"input": rate, "cacheRead": cost.get("cacheRead"), "output": cost.get("output")}, token_mix
        )
        if effective_rate is not None and (
            cheapest_pinned_effective_rate is None or effective_rate < cheapest_pinned_effective_rate
        ):
            cheapest_pinned_effective_rate = effective_rate
        if rate is not None and effective_rate is not None and uptime is not None:
            pinned_points.append({
                "id": model["id"],
                "input_rate_per_million": rate,
                "effective_rate_per_million": round(effective_rate, 4),
                "uptime_pct": uptime,
                "context_length": live_entry.get("context_length"),
            })

    result["candidates"] = find_candidates(catalog, pinned_ids, cheapest_pinned_effective_rate, token_mix)
    result["notable"] = bool(
        result["drift"] or result["missing"] or result["candidates"] or result["unreliable_pinned"]
    )

    if result["notable"]:
        # Only fetched when there's actually a chart to draw: an extra network
        # call to a page with no stability contract isn't worth paying most
        # weeks, when nothing's notable and no chart gets generated anyway.
        coding_score_index = build_coding_score_index(fetch_coding_scores())
        for point in pinned_points:
            entry = coding_score_for(point["id"], coding_score_index)
            point["coding_score"] = entry.get("index_code") if entry else None
        for candidate in result["candidates"]:
            entry = coding_score_for(candidate["id"], coding_score_index)
            candidate["coding_score"] = entry.get("index_code") if entry else None

        svg = generate_svg(pinned_points, result["candidates"], token_mix)
        if svg:
            write_svg(chart_path, svg)
            result["chart_path"] = chart_path

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
