#!/usr/bin/env python3
"""Weekly check for one review stage's pinned models drifting off OpenRouter's
price/quality frontier.

Both review stages pin their models in scripts/pi/models.json and are checked by
this same script, once per stage (--stage=stage1|stage2, see STAGES). A stage has
its own pinned set, its own Datadog spans, its own real token mix, and its own
chart; everything else — the catalog, drift, uptime and leaderboard machinery —
is shared.

This never decides anything on its own — review quality isn't something an API can
score, so picking a replacement model stays a human call. All this does is surface
the two things that ARE checkable automatically:

  1. Rate drift: the $/1M rates pinned in models.json are what pi uses to compute
     cost.total for Datadog (see llm-usage.py's docstring). If OpenRouter's list
     price for a pinned model has moved, the billing we report is wrong until
     someone updates the pin.
  2. Cheaper same-tier candidates: reasoning-capable models with >=100k context
     and at least MIN_UPTIME_PCT uptime that currently cost less than the
     cheapest pinned model on an *effective* $/1M basis (see below) AND score no
     worse on coding than the weakest pinned model, so there's something concrete
     to look at when deciding whether to roll the default forward. A cheaper
     model that's down 5%+ of the time isn't actually a saving, and one that
     reviews worse is a downgrade rather than a candidate.
  3. Unreliable pinned models: any currently-pinned model whose uptime has
     dropped below MIN_UPTIME_PCT, since that's worth knowing even with no
     cheaper alternative in sight.

Why "effective" $/1M instead of the raw input rate: review prompts are almost
entirely re-sent context, so OpenRouter's prompt-cache discount — not the list
input rate — dominates real cost. Sampling the stage's own spans from Datadog LLM
Observability (see fetch_token_mix) over a trailing window consistently shows
something like 90% cache-read tokens, ~9% fresh input, ~1% output — ranking or
charting models by raw input price alone would be comparing list prices nobody
actually pays. effective_rate_per_million blends a model's input/cacheRead/output
rates by that observed share, falling back to the raw input rate when Datadog
credentials (DD_API_KEY/DD_APP_KEY) aren't set or the lookup fails, so the check
still degrades gracefully without them.

Reading that mix correctly depends on which harness recorded the span, because
Codex and pi report cached tokens in opposite conventions — see STAGES and
span_tokens. Sizing a context window off it depends on the turn count, because
both harnesses report tokens as a running session total — see
working_context_tokens.

When anything above is notable, also writes an SVG chart (pinned models vs.
candidates, price using the effective rate) to the given chart path, via
matplotlib (see requirements.txt in this directory). The x-axis is a
coding-specific score scraped from llm-stats.com's public leaderboard (see
fetch_coding_scores) when at least one plotted model has one, since that's
more relevant to a code-review bot than raw context length; it falls back
to context length otherwise. Uptime isn't charted: OpenRouter's public API
doesn't expose throughput/latency (always null), and after the >=95% filter
the remaining uptime spread is too small to be a useful axis, so it stays a
table-only reliability gate instead.

Prints one JSON object to stdout: {"stage": str, "drift": [...], "missing": [...],
"candidates": [...], "unreliable_pinned": [...], "token_mix": {...} | null,
"notable": bool}. Never raises and always exits 0 — this is a weekly
nudge-to-look, not a check that should ever fail CI.

Usage: check_model_freshness.py [--stage=stage1|stage2] <path-to-models.json>
                               [chart-output-path]
The stage selects which pinned models, which Datadog spans, and which chart path
are used; it defaults to stage2. See STAGES.
"""
import io
import json
import os
import re
import sys
import urllib.request

import matplotlib

matplotlib.use("Agg")
matplotlib.rcParams["svg.fonttype"] = "none"  # keep chart text as real <text>, not glyph paths
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{model_id}/endpoints"
DRIFT_THRESHOLD = 0.05  # flag a pinned rate more than 5% off the live list price
CANDIDATE_MIN_CONTEXT = 100_000
CANDIDATE_LIMIT = 5
CANDIDATE_POOL_SIZE = 40  # cheaper-and-no-weaker pool checked for uptime before limiting
MIN_UPTIME_PCT = 95.0  # a model down more than 5% of the time isn't a real saving
DEFAULT_STAGE = "stage2"

# Each review stage pins its own models, bills its own token mix, and needs its own
# chart, but the catalog/drift/uptime/leaderboard machinery is identical for both, so
# the check runs once per stage rather than existing twice.
#
# `spans` lists the Datadog LLM Obs span names that carry the stage's real usage,
# newest naming first, each with how that harness reports cached tokens:
#
#   "nested"   — `input_tokens` is the WHOLE input and already contains
#                `cache_read_input_tokens`. Codex reports this way.
#   "disjoint" — `input_tokens` counts only fresh tokens and does NOT overlap
#                `cache_read_input_tokens`; the two are added. pi reports this way.
#
# Getting this backwards silently corrupts the price blend rather than failing: on a
# nested span, adding the two fields double-counts the cache read, which on a pass
# that is ~93% cache inflates the apparent fresh-input share by roughly an order of
# magnitude and makes cache-hostile models look cheap. Datadog itself assumes the
# nested convention — it derives `non_cached_input_tokens` by subtraction, which comes
# out NEGATIVE on pi's spans, and that negative is the tell that the two conventions
# are genuinely different rather than a reporting quirk.
#
# Stage 1 lists both names because it is mid-migration from the Codex harness to pi:
# a trailing window straddles the rename, so both are queried and each is read with
# its own convention. The Codex entry ages out of the window on its own.
STAGES = {
    "stage1": {
        "label": "Stage 1 first pass",
        "chart_path": "docs/model-freshness/stage1.svg",
        "spans": [("pi.first_pass", "disjoint"), ("codex.review", "nested")],
    },
    "stage2": {
        "label": "Stage 2 synthesis",
        "chart_path": "docs/model-freshness/stage2.svg",
        "spans": [("pi.synthesize", "disjoint")],
    },
}
PLOT_PADDING_FRACTION = 0.08  # headroom inside the axes so extreme points aren't flush against them
NO_SCORE_GAP_FRACTION = 0.22  # how far left of the real coding-score domain the "no score" column sits
PINNED_COLOR = "#1f77b4"
CANDIDATE_COLOR = "#2ca02c"
INADEQUATE_COLOR = "#d62728"
NO_SCORE_COLOR = "#666666"

# Real usage mix for the pi review pass, sampled from Datadog LLM Observability
# spans. A longer window smooths out any single noisy week; refreshed on every
# run rather than hardcoded so the blend tracks actual usage as it drifts.
DATADOG_SPANS_SEARCH_URL = "https://api.datadoghq.com/api/v2/llm-obs/v1/spans/events/search"
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


def span_tokens(metrics, convention):
    """One span's (fresh input, cache read, output, whole input) token counts, read
    under its harness's cached-token convention. See STAGES for why the two differ
    and what reading one as the other would do to the price blend."""
    cache_read = metrics.get("cache_read_input_tokens", 0) or 0
    reported_input = metrics.get("input_tokens", 0) or 0
    output = metrics.get("output_tokens", 0) or 0
    if convention == "nested":
        whole_input = reported_input
        fresh = max(reported_input - cache_read, 0)
    else:
        whole_input = reported_input + cache_read
        fresh = reported_input
    return fresh, cache_read, output, whole_input


def working_context_tokens(fresh, whole_input, output, turns):
    """What a model actually has to HOLD for this pass, which is not what it gets
    billed for.

    Both harnesses report tokens as a running session total, so on a multi-turn
    agentic pass the input figure is the sum of every turn's context and counts the
    conversation prefix once per turn. A real Stage 1 span shows 1,150,702 input
    tokens against a working context around 80k — sizing a context window off the
    cumulative number would demand ~14x what the pass needs and rule out almost
    every model for no reason.

    With more than one turn, the context is what ACCUMULATED in the conversation:
    each turn appends its fresh (non-cached) input and its output, and the cached
    remainder is the prefix already counted. With a single turn there is nothing to
    accumulate and the request simply holds its whole input — cached or not, a
    cache read still occupies the context window; it is a billing discount, not a
    smaller prompt.

    Returns None when the turn count is unknown, which is every span recorded before
    turn counts were instrumented. Guessing between the two formulas would be wrong
    by an order of magnitude in whichever direction the guess missed, so those spans
    are left out of the context estimate rather than assumed into it."""
    if not turns:
        return None
    if turns > 1:
        return fresh + output
    return whole_input + output


def fetch_token_mix(stage):
    """Sum input/cache-read/output tokens across the stage's own review spans over
    TOKEN_MIX_WINDOW, via Datadog's LLM Observability spans search API. Returns
    {"fresh_input_share", "cache_read_share", "output_share", "max_input_tokens",
    "max_output_tokens", "max_total_tokens", "context", "sample_count", "spans",
    "window"} or None when DD_API_KEY/DD_APP_KEY aren't set, the requests fail, or
    no spans have usable metrics — callers fall back to the raw input rate then.

    A stage may list several span names (a harness migration renames the span while
    the trailing window still holds the old one); each is queried and read under its
    own cached-token convention, so a straddled window blends correctly instead of
    forcing a choice between a wrong convention and a short window.

    max_* are the single largest per-span values seen, not averages: a model only
    needs to handle the worst review it might see, not the typical one.

    `context` is the separate, turn-aware estimate of what a model must actually
    hold (see working_context_tokens), carrying its own sample_count because it is
    computed from the subset of spans that report a turn count. It is None until
    enough spans carry one."""
    api_key = os.environ.get("DD_API_KEY")
    app_key = os.environ.get("DD_APP_KEY")
    if not api_key or not app_key:
        return None

    fresh_input = cache_read_total = output_total = sample_count = 0
    max_input_tokens = max_output_tokens = max_total_tokens = 0
    max_working_context = max_turns = context_samples = 0
    spans_seen = {}

    for span_name, convention in STAGES[stage]["spans"]:
        cursor = None
        seen_for_name = 0
        while sample_count < TOKEN_MIX_MAX_SPANS:
            filter_ = {"from": TOKEN_MIX_WINDOW, "to": "now", "query": f"@name:{span_name}"}
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
                fresh, cache_read, output, whole_input = span_tokens(metrics, convention)
                fresh_input += fresh
                cache_read_total += cache_read
                output_total += output
                sample_count += 1
                seen_for_name += 1
                max_input_tokens = max(max_input_tokens, whole_input)
                max_output_tokens = max(max_output_tokens, output)
                max_total_tokens = max(max_total_tokens, whole_input + output)

                turns = metrics.get("turn_count")
                context = working_context_tokens(fresh, whole_input, output, turns)
                if context is not None:
                    max_working_context = max(max_working_context, context)
                    max_turns = max(max_turns, int(turns))
                    context_samples += 1

            cursor = ((payload.get("meta") or {}).get("page") or {}).get("after")
            if not cursor or not spans:
                break
        if seen_for_name:
            spans_seen[span_name] = seen_for_name

    total = fresh_input + cache_read_total + output_total
    if total <= 0 or sample_count <= 0:
        return None
    return {
        "fresh_input_share": fresh_input / total,
        "cache_read_share": cache_read_total / total,
        "output_share": output_total / total,
        "max_input_tokens": max_input_tokens,
        "max_output_tokens": max_output_tokens,
        "max_total_tokens": max_total_tokens,
        "context": {
            "max_working_context_tokens": max_working_context,
            "max_turns": max_turns,
            "sample_count": context_samples,
        } if context_samples else None,
        "sample_count": sample_count,
        "spans": spans_seen,
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


def load_pinned(path, stage):
    """The models.json entries this stage is allowed to run on. `stages` is our key,
    not pi's — the workflow strips it before seeding pi's config dir. An entry with
    no `stages` list belongs to every stage, so a model added without one is
    surfaced rather than silently dropped from both charts."""
    with open(path) as handle:
        config = json.load(handle)
    models = config["providers"]["openrouter"]["models"]
    return [m for m in models if stage in (m.get("stages") or [stage])]


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


def find_candidates(catalog, pinned_ids, cheapest_pinned_effective_rate, token_mix,
                    coding_score_index=None, min_pinned_coding_score=None):
    """Models worth looking at instead of what is pinned: reasoning-capable, enough
    context, healthy uptime, cheaper than the cheapest pinned model on the effective
    rate, and — when both scores are known — no weaker at coding than the weakest
    pinned model.

    The capability floor is what makes this a shortlist rather than a price list.
    Ranking on cost alone fills the five slots with whatever is cheapest, which is
    reliably a set of models that review worse than what they would replace, while
    burying one that is both cheaper AND stronger further down the list. A cheaper
    model that reviews worse is a downgrade, not a candidate.

    A model llm-stats.com does not cover is left out for the same reason: the whole
    point of the shortlist is cost weighed against capability, and a model with no
    capability number cannot be weighed. Listing it anyway spends a slot on something
    nobody can act on. (Pinned models are still charted without a score — what is
    already running has to be shown whether or not the leaderboard covers it.)

    If the leaderboard fetch fails entirely there are no scores to judge by at all,
    so the floor and the coverage requirement are both skipped and this falls back to
    ranking on price — a degraded shortlist beats an empty one."""
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
        score_entry = coding_score_for(model_id, coding_score_index) if coding_score_index else None
        coding_score = score_entry.get("index_code") if score_entry else None
        if coding_score_index:
            if coding_score is None:
                continue
            if min_pinned_coding_score is not None and coding_score < min_pinned_coding_score:
                continue
        pool.append({
            "id": model_id,
            "name": entry.get("name"),
            "context_length": entry.get("context_length"),
            "input_rate_per_million": round(input_rate, 4),
            "effective_rate_per_million": round(effective_rate, 4),
            "coding_score": coding_score,
        })
    # Strongest first, not cheapest first. Every model in the pool is already cheaper
    # than what is pinned, so price has done its job as a filter and ranking on it
    # again just answers "what is cheapest" a second time — which fills the shortlist
    # with the bottom of the catalog and buries a model that is both cheaper and
    # markedly stronger below five that are merely cheaper. (The score is None only
    # when the leaderboard was unreachable, in which case every candidate is unscored
    # and this degrades to the price ordering.)
    pool.sort(key=lambda c: (
        -(c["coding_score"] or 0),
        c["effective_rate_per_million"],
    ))

    candidates = []
    for candidate in pool[:CANDIDATE_POOL_SIZE]:
        uptime = fetch_uptime(candidate["id"])
        if uptime is None or uptime < MIN_UPTIME_PCT:
            continue
        candidates.append({**candidate, "uptime_pct": uptime})
        if len(candidates) == CANDIDATE_LIMIT:
            break
    return candidates


def format_context(n):
    n = round(n)
    return f'{n / 1_000_000:.3g}M' if n >= 1_000_000 else f'{n / 1_000:.0f}k' if n >= 1_000 else str(n)


def generate_svg(pinned_points, candidate_points, token_mix, stage_label=""):
    """Price-vs-x scatter (pinned models vs. candidates), rendered with
    matplotlib and returned as an SVG string.

    The x-axis is the model's coding score from llm-stats.com's leaderboard
    (see fetch_coding_scores) when at least one plottable point has one,
    since that's a more relevant axis for a code-review bot than raw context
    length. It falls back to context length (log scale, marked with the
    working context the stage actually needs) when no point has a coding
    score — a llm-stats.com fetch/parse failure, or no matches for any
    plotted model, degrades to that rather than losing the chart. The y-axis
    (effective $/1M) is always log scale.

    A model whose context window is smaller than the stage's measured working
    context is colored as inadequate. That threshold is the turn-aware
    estimate from working_context_tokens, never the cumulative input total:
    the cumulative figure counts a multi-turn pass's conversation prefix once
    per turn, and gating on it would mark nearly every model inadequate. When
    no span carries a turn count the threshold is unknown and nothing is
    colored inadequate, since a guessed gate is worse than none.

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

    context_info = (token_mix or {}).get("context") or {}
    required_context = context_info.get("max_working_context_tokens") or 0
    max_output_tokens = (token_mix or {}).get("max_output_tokens") or 0

    coding_scores = [p["coding_score"] for p in all_points if p.get("coding_score") is not None]
    use_coding_axis = bool(coding_scores)

    plottable = [(p, PINNED_COLOR) for p in pinned_points] + [(p, CANDIDATE_COLOR) for p in candidate_points]
    plottable = [
        (point, color, point.get("coding_score") if use_coding_axis else point.get("context_length"))
        for point, color in plottable
        if (point.get("effective_rate_per_million") or 0) > 0 and (point.get("context_length") or 0) > 0
    ]
    if not use_coding_axis:
        plottable = [(p, c, x) for p, c, x in plottable if x is not None and x > 0]
    if not plottable:
        return None

    scored_x = [x for _, _, x in plottable if x is not None]
    if not scored_x:
        return None  # coding axis chosen, but every plottable point is missing a score

    no_score_gutter = use_coding_axis and any(x is None for _, _, x in plottable)
    score_min, score_max = min(scored_x), max(scored_x)
    if no_score_gutter:
        span = (score_max - score_min) or max(abs(score_min), 1)
        no_score_x = score_min - span * NO_SCORE_GAP_FRACTION
    else:
        no_score_x = None

    fig, ax = plt.subplots(figsize=(7.6, 4.8), dpi=100)
    ax.set_yscale("log")

    has_inadequate = False
    for point, color, x_value in plottable:
        rate = point["effective_rate_per_million"]
        context_length = point["context_length"]
        no_score = x_value is None
        plotted_x = no_score_x if no_score else x_value
        inadequate = bool(required_context) and context_length < required_context
        has_inadequate = has_inadequate or inadequate
        if no_score:
            ring_color = INADEQUATE_COLOR if inadequate else NO_SCORE_COLOR
            ax.scatter([plotted_x], [rate], s=64, facecolors="white", edgecolors=ring_color, linewidths=2, zorder=3)
        else:
            marker_color = INADEQUATE_COLOR if inadequate else color
            ax.scatter([plotted_x], [rate], s=64, color=marker_color, alpha=0.85, zorder=3)
        # The pinned-vs-candidate color still shows on the label even when the
        # marker itself is neutral (a no-score ring), so no identity is lost.
        label_color = INADEQUATE_COLOR if inadequate else color
        ax.annotate(
            point["id"], (plotted_x, rate), textcoords="offset points", xytext=(7, 0),
            va="center", ha="left", fontsize=8, color=label_color, clip_on=False,
        )

    if use_coding_axis:
        ax.set_xlabel("coding score (llm-stats.com index_code, higher is better)")
        ax.set_title(f"{stage_label}: price vs. coding score" if stage_label else "Price vs. coding score")
        if no_score_gutter:
            ax.axvline(score_min, color="#999999", linestyle=(0, (2, 3)), linewidth=1)
            ax.set_xlim(no_score_x - span * PLOT_PADDING_FRACTION, score_max + span * PLOT_PADDING_FRACTION)
            y_min, _ = ax.get_ylim()
            ax.text(no_score_x, y_min, "no score", ha="center", va="top", color="#999999", fontsize=8)
        else:
            pad = (score_max - score_min) * PLOT_PADDING_FRACTION or 1
            ax.set_xlim(score_min - pad, score_max + pad)
    else:
        ax.set_xscale("log")
        ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _pos: format_context(value)))
        ax.set_xlabel("context length (log scale)")
        ax.set_title(f"{stage_label}: price vs. context" if stage_label else "Price vs. context")
        # These are context-length values, so they only make sense positioned on a
        # context-length x-axis. The input line is the working context the stage
        # needs, not its cumulative billed input — see working_context_tokens.
        if required_context > 0:
            ax.axvline(required_context, color="#999999", linestyle=(0, (4, 3)), linewidth=1.5)
            ax.text(
                required_context, 1, f"working context ({format_context(required_context)})",
                transform=ax.get_xaxis_transform(), ha="center", va="bottom", color="#999999", fontsize=8,
            )
        if max_output_tokens > 0:
            ax.axvline(max_output_tokens, color="#c49a00", linestyle=(0, (4, 3)), linewidth=1.5)
            ax.text(
                max_output_tokens, 0.92, f"max review output size ({format_context(max_output_tokens)})",
                transform=ax.get_xaxis_transform(), ha="center", va="bottom", color="#c49a00", fontsize=8,
            )

    ax.set_ylabel("effective $/1M (log scale)")
    ax.margins(y=PLOT_PADDING_FRACTION)
    ax.grid(True, which="both", color="#eeeeee", linewidth=0.8, zorder=0)

    legend_handles = [
        Line2D([0], [0], marker="o", linestyle="none", color=PINNED_COLOR, label="pinned"),
        Line2D([0], [0], marker="o", linestyle="none", color=CANDIDATE_COLOR, label="candidate"),
    ]
    if has_inadequate:
        legend_handles.append(
            Line2D([0], [0], marker="o", linestyle="none", color=INADEQUATE_COLOR, label="inadequate context")
        )
    if no_score_gutter:
        legend_handles.append(
            Line2D(
                [0], [0], marker="o", linestyle="none", markerfacecolor="white",
                markeredgecolor=NO_SCORE_COLOR, markeredgewidth=2, label="no coding score available",
            )
        )
    ax.legend(handles=legend_handles, loc="upper left", fontsize=8, frameon=False)

    fig.tight_layout()
    buffer = io.StringIO()
    fig.savefig(buffer, format="svg")
    plt.close(fig)
    return buffer.getvalue()


def write_svg(path, svg):
    import os
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as handle:
        handle.write(svg)


def main(argv):
    stage = DEFAULT_STAGE
    args = []
    for arg in argv[1:]:
        if arg.startswith("--stage="):
            stage = arg.split("=", 1)[1]
        else:
            args.append(arg)

    result = {"stage": stage, "drift": [], "missing": [], "candidates": [],
              "unreliable_pinned": [], "notable": False}
    if stage not in STAGES:
        result["error"] = f"unknown stage {stage!r} (have: {', '.join(sorted(STAGES))})"
        print(json.dumps(result))
        return 0

    models_path = args[0] if args else "scripts/pi/models.json"
    chart_path = args[1] if len(args) > 1 else STAGES[stage]["chart_path"]
    try:
        pinned = load_pinned(models_path, stage)
    except Exception as error:
        result["error"] = f"could not read {models_path}: {error}"
        print(json.dumps(result))
        return 0

    catalog, fetch_error = fetch_catalog()
    if fetch_error:
        result["error"] = f"could not fetch OpenRouter catalog: {fetch_error}"
        print(json.dumps(result))
        return 0

    token_mix = fetch_token_mix(stage)
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

    # Fetched before candidate selection, not after: the coding score is now a filter
    # on the shortlist (see find_candidates), not just a chart axis.
    coding_score_index = build_coding_score_index(fetch_coding_scores())
    for point in pinned_points:
        entry = coding_score_for(point["id"], coding_score_index)
        point["coding_score"] = entry.get("index_code") if entry else None

    pinned_scores = [p["coding_score"] for p in pinned_points if p.get("coding_score") is not None]
    min_pinned_coding_score = min(pinned_scores) if pinned_scores else None

    result["candidates"] = find_candidates(
        catalog, pinned_ids, cheapest_pinned_effective_rate, token_mix,
        coding_score_index, min_pinned_coding_score)
    result["notable"] = bool(
        result["drift"] or result["missing"] or result["candidates"] or result["unreliable_pinned"]
    )

    if result["notable"]:
        svg = generate_svg(pinned_points, result["candidates"], token_mix, STAGES[stage]["label"])
        if svg:
            write_svg(chart_path, svg)
            result["chart_path"] = chart_path

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
