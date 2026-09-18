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
     cheapest pinned model, so there's something concrete to look at when
     deciding whether to roll the default forward. A cheaper model that's down
     5%+ of the time isn't actually a saving.
  3. Unreliable pinned models: any currently-pinned model whose uptime has
     dropped below MIN_UPTIME_PCT, since that's worth knowing even with no
     cheaper alternative in sight.

When anything above is notable, also writes a price-vs-uptime SVG chart (pinned
models vs. candidates) to the given chart path, dependency-free (plain XML, no
matplotlib) so it needs nothing beyond the stdlib in CI.

Prints one JSON object to stdout: {"drift": [...], "missing": [...],
"candidates": [...], "unreliable_pinned": [...], "notable": bool}. Never raises
and always exits 0 — this is a weekly nudge-to-look, not a check that should ever
fail CI.

Usage: check_model_freshness.py <path-to-models.json> [chart-output-path]
"""
import json
import math
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
CHART_WIDTH = 640
CHART_HEIGHT = 420
CHART_MARGIN = {"left": 60, "right": 20, "top": 30, "bottom": 50}


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


def find_candidates(catalog, pinned_ids, cheapest_pinned_input_rate):
    pool = []
    for model_id, entry in catalog.items():
        if model_id in pinned_ids:
            continue
        if not is_reasoning_capable(entry):
            continue
        if (entry.get("context_length") or 0) < CANDIDATE_MIN_CONTEXT:
            continue
        input_rate = per_token_to_per_million(entry.get("pricing", {}).get("prompt"))
        if input_rate is None or input_rate <= 0:
            continue
        if cheapest_pinned_input_rate is not None and input_rate >= cheapest_pinned_input_rate:
            continue
        pool.append({
            "id": model_id,
            "name": entry.get("name"),
            "context_length": entry.get("context_length"),
            "input_rate_per_million": round(input_rate, 4),
        })
    pool.sort(key=lambda c: c["input_rate_per_million"])

    candidates = []
    for candidate in pool[:CANDIDATE_POOL_SIZE]:
        uptime = fetch_uptime(candidate["id"])
        if uptime is None or uptime < MIN_UPTIME_PCT:
            continue
        candidates.append({**candidate, "uptime_pct": uptime})
        if len(candidates) == CANDIDATE_LIMIT:
            break
    return candidates


def generate_svg(pinned_points, candidate_points):
    """Plain-XML price-vs-uptime scatter, pinned models vs. candidates. Returns
    None when there's nothing plottable (no positive rate to build a log-scale
    axis from)."""
    all_points = pinned_points + candidate_points
    rates = [p["input_rate_per_million"] for p in all_points if (p.get("input_rate_per_million") or 0) > 0]
    if not rates:
        return None

    plot_w = CHART_WIDTH - CHART_MARGIN["left"] - CHART_MARGIN["right"]
    plot_h = CHART_HEIGHT - CHART_MARGIN["top"] - CHART_MARGIN["bottom"]
    log_min, log_max = math.log10(min(rates)), math.log10(max(rates))
    if log_min == log_max:
        log_min, log_max = log_min - 0.5, log_max + 0.5

    def x_pos(rate):
        rate = max(rate, min(rates))
        frac = (math.log10(rate) - log_min) / (log_max - log_min)
        return CHART_MARGIN["left"] + frac * plot_w

    def y_pos(uptime_pct):
        frac = max(0.0, min(100.0, uptime_pct)) / 100
        return CHART_MARGIN["top"] + (1 - frac) * plot_h

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{CHART_WIDTH}" height="{CHART_HEIGHT}" '
        f'font-family="sans-serif" font-size="11">',
        f'<rect width="{CHART_WIDTH}" height="{CHART_HEIGHT}" fill="white"/>',
        f'<text x="{CHART_WIDTH / 2}" y="18" text-anchor="middle" font-size="13" font-weight="bold">'
        f'Price vs. uptime</text>',
        f'<line x1="{CHART_MARGIN["left"]}" y1="{CHART_MARGIN["top"]}" '
        f'x2="{CHART_MARGIN["left"]}" y2="{CHART_HEIGHT - CHART_MARGIN["bottom"]}" stroke="black"/>',
        f'<line x1="{CHART_MARGIN["left"]}" y1="{CHART_HEIGHT - CHART_MARGIN["bottom"]}" '
        f'x2="{CHART_WIDTH - CHART_MARGIN["right"]}" y2="{CHART_HEIGHT - CHART_MARGIN["bottom"]}" stroke="black"/>',
        f'<text x="{CHART_WIDTH / 2}" y="{CHART_HEIGHT - 10}" text-anchor="middle">$/1M input (log scale)</text>',
        f'<text x="16" y="{CHART_HEIGHT / 2}" text-anchor="middle" '
        f'transform="rotate(-90 16 {CHART_HEIGHT / 2})">uptime %</text>',
    ]

    for pct in (100, 95, 90, 75, 50, 0):
        y = y_pos(pct)
        parts.append(f'<text x="{CHART_MARGIN["left"] - 6}" y="{y + 3}" text-anchor="end">{pct}</text>')
        parts.append(
            f'<line x1="{CHART_MARGIN["left"]}" y1="{y}" x2="{CHART_WIDTH - CHART_MARGIN["right"]}" '
            f'y2="{y}" stroke="#eee"/>'
        )

    def plot(points, color):
        for point in points:
            rate = point.get("input_rate_per_million")
            uptime = point.get("uptime_pct")
            if not rate or rate <= 0 or uptime is None:
                continue
            x, y = x_pos(rate), y_pos(uptime)
            title = xml_escape(f'{point["id"]}: ${rate}/1M, {uptime}% uptime')
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{color}" fill-opacity="0.85">'
                f'<title>{title}</title></circle>'
            )
            near_right_edge = x > CHART_WIDTH - CHART_MARGIN["right"] - 100
            label_x = x - 7 if near_right_edge else x + 7
            anchor = 'end' if near_right_edge else 'start'
            parts.append(
                f'<text x="{label_x:.1f}" y="{y + 3:.1f}" text-anchor="{anchor}" '
                f'fill="{color}">{xml_escape(point["id"])}</text>'
            )

    plot(pinned_points, "#1f77b4")
    plot(candidate_points, "#2ca02c")

    legend_y = CHART_MARGIN["top"]
    parts.append(f'<circle cx="{CHART_WIDTH - 110}" cy="{legend_y}" r="5" fill="#1f77b4"/>')
    parts.append(f'<text x="{CHART_WIDTH - 100}" y="{legend_y + 4}">pinned</text>')
    parts.append(f'<circle cx="{CHART_WIDTH - 110}" cy="{legend_y + 16}" r="5" fill="#2ca02c"/>')
    parts.append(f'<text x="{CHART_WIDTH - 100}" y="{legend_y + 20}">candidate</text>')

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

    pinned_ids = {model["id"] for model in pinned}
    cheapest_pinned_rate = None
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
        rate = model.get("cost", {}).get("input")
        if rate is not None and (cheapest_pinned_rate is None or rate < cheapest_pinned_rate):
            cheapest_pinned_rate = rate
        if rate is not None and uptime is not None:
            pinned_points.append({"id": model["id"], "input_rate_per_million": rate, "uptime_pct": uptime})

    result["candidates"] = find_candidates(catalog, pinned_ids, cheapest_pinned_rate)
    result["notable"] = bool(
        result["drift"] or result["missing"] or result["candidates"] or result["unreliable_pinned"]
    )

    if result["notable"]:
        svg = generate_svg(pinned_points, result["candidates"])
        if svg:
            write_svg(chart_path, svg)
            result["chart_path"] = chart_path

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
