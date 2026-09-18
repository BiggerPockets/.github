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
     that currently cost less than the cheapest pinned model, so there's something
     concrete to look at when deciding whether to roll the default forward.

Prints one JSON object to stdout: {"drift": [...], "missing": [...],
"candidates": [...], "notable": bool}. Never raises and always exits 0 — this is a
weekly nudge-to-look, not a check that should ever fail CI.

Usage: check_model_freshness.py <path-to-models.json>
"""
import json
import sys
import urllib.request

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
DRIFT_THRESHOLD = 0.05  # flag a pinned rate more than 5% off the live list price
CANDIDATE_MIN_CONTEXT = 100_000
CANDIDATE_LIMIT = 5


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
    candidates = []
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
        candidates.append({
            "id": model_id,
            "name": entry.get("name"),
            "context_length": entry.get("context_length"),
            "input_rate_per_million": round(input_rate, 4),
        })
    candidates.sort(key=lambda c: c["input_rate_per_million"])
    return candidates[:CANDIDATE_LIMIT]


def main(argv):
    models_path = argv[1] if len(argv) > 1 else "scripts/pi/models.json"
    result = {"drift": [], "missing": [], "candidates": [], "notable": False}
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
    for model in pinned:
        live_entry = catalog.get(model["id"])
        if live_entry is None:
            result["missing"].append(model["id"])
            continue
        drifts = rate_drift(model, live_entry)
        if drifts:
            result["drift"].append({"id": model["id"], "fields": drifts})
        rate = model.get("cost", {}).get("input")
        if rate is not None and (cheapest_pinned_rate is None or rate < cheapest_pinned_rate):
            cheapest_pinned_rate = rate

    result["candidates"] = find_candidates(catalog, pinned_ids, cheapest_pinned_rate)
    result["notable"] = bool(result["drift"] or result["missing"] or result["candidates"])
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
