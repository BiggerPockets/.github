#!/usr/bin/env python3
"""Choose an OpenRouter endpoint for a review, and report which one served it.

OpenRouter fronts one model slug with many serving endpoints. They differ by more
than a factor of three in price and by far more than that in speed, and OpenRouter
picks one per call. That single fact is behind three separate problems: reviews time
out because a slow endpoint was picked, spend drifts because an expensive one was,
and neither can be investigated because nothing downstream records which it was.

Two subcommands, run at the two moments where those problems are addressable.

`routing` runs before pi starts and produces the `provider` object pi sends on every
request (pi's `compat.openRouterRouting`). It asks OpenRouter which endpoints are
currently serving the model and builds a price ceiling from that live list rather
than from a number pinned in this repository. A pinned ceiling is wrong in both
directions and silently: set above the field it never excludes anything, set below it
turns `max_price` -- a hard filter, not a preference -- into a failed review. A
ceiling at the median of what is actually on offer today cannot do either. Roughly
half the field always clears it, so routing always has somewhere to go, and the
expensive tail is always excluded no matter how prices move.

`attribute` runs after pi exits and answers which endpoint actually served the run.
pi records `responseId` on every completed assistant turn, and on OpenRouter that is
the generation id, so the ids are already in pi-output.jsonl with no change to pi.
Each one resolves through GET /api/v1/generation to the serving provider and its real
latency, generation time and billed cost. This is measured rather than asserted,
which is what makes it valid while fallbacks are on: routing is free to move off a
degrading endpoint mid-run, and the report still names where the calls truly landed.
It reads only ids and per-call statistics, never prompts or completions.
"""

import argparse
import json
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request

ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{slug}/endpoints"
GENERATION_URL = "https://openrouter.ai/api/v1/generation"

# Sorting key for endpoint selection. A review is a long multi-turn agent run whose
# wall clock is dominated by streaming rather than by time-to-first-token, so
# throughput is the term that decides whether it fits inside the 900s cap.
#
# Sorting on speed is only safe because of the ceiling below. An endpoint sets both
# its own price and its own serving rate, so "fastest" is a position it can buy;
# max_price is what stops it being worth buying. The two belong together, and neither
# is sound alone.
SORT = "throughput"

# How many endpoints the ceiling must leave standing. This is the one number that
# sets where the ceiling lands, and it is expressed as room to reroute rather than as
# a price, because room is what the ceiling actually trades away. The ceiling is then
# the cheapest one that still leaves this many candidates: as tight as the live field
# allows, and never tight enough to leave fallbacks with nowhere to go.
#
# A quantile was the obvious alternative and is worse. It is loose exactly where the
# field is wide -- the median of deepseek-v4.1-flash's 22 eligible endpoints admits
# almost the entire expensive tail it exists to exclude -- and tight exactly where the
# field is narrow and there is nothing to spare.
MIN_CANDIDATES = 6

# A run makes far more calls than we need to identify its endpoints, and each lookup
# is a round trip on the critical path of reporting. The most recent calls are the
# ones that describe a slow or killed run, so the tail is what gets resolved.
MAX_LOOKUPS = 40


def _get(url, api_key=None, timeout=30):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def eligible_endpoints(payload):
    """Endpoints that could actually serve a review, from an /endpoints response.

    A negative `status` is OpenRouter's own derank. Tool support is not optional
    here: pi is an agent and every turn carries tools, so an endpoint without them
    is not a cheaper option, it is a broken one. Excluding both before taking the
    median keeps the ceiling anchored to endpoints routing would really consider.
    """
    endpoints = (payload.get("data") or {}).get("endpoints") or []
    return [
        endpoint
        for endpoint in endpoints
        if (endpoint.get("status") or 0) >= 0
        and "tools" in (endpoint.get("supported_parameters") or [])
    ]


def _rates(endpoint):
    """($/M prompt, $/M completion) for an endpoint, or None if either is unusable."""
    pricing = endpoint.get("pricing") or {}
    try:
        return float(pricing["prompt"]) * 1_000_000, float(pricing["completion"]) * 1_000_000
    except (KeyError, TypeError, ValueError):
        return None


def price_ceiling(endpoints):
    """A per-million-token ceiling admitting the MIN_CANDIDATES cheapest endpoints.

    Both fields are set from that same set, so every one of those endpoints clears the
    ceiling on prompt *and* completion. Deriving the two independently would not
    guarantee that: endpoints do not rank the same way on both, and the cheapest
    prompt price in a field is regularly not the cheapest completion price, so
    independent cutoffs can admit a count neither field alone predicts.

    Returns None when the field is already at or below MIN_CANDIDATES. A ceiling there
    could only exclude endpoints that fallbacks may need, and capping a field that
    small buys nothing: there is no expensive tail to cut off.
    """
    priced = [rates for rates in map(_rates, endpoints) if rates]
    if len(priced) <= MIN_CANDIDATES:
        return None

    # Ranked on both rates together. Ranking on prompt alone would let an endpoint
    # that is cheapest on prompt and absurd on completion into the cheap set and drag
    # the completion ceiling up with it -- the exact gouge the ceiling exists to stop,
    # and reviews are output-heavy, so completion is the rate that costs real money.
    cheapest = sorted(priced, key=sum)[:MIN_CANDIDATES]
    return {
        "prompt": round(max(prompt for prompt, _ in cheapest), 6),
        "completion": round(max(completion for _, completion in cheapest), 6),
    }


def routing_for(slug, timeout=30):
    """The `provider` object to send for this model, or {} if it can't be built.

    An empty result is a working review under OpenRouter's default routing, which is
    the behaviour this replaces. Routing preferences are an improvement on the
    default, not a precondition for running, so nothing here is worth failing over.
    """
    url = ENDPOINTS_URL.format(slug=urllib.parse.quote(slug, safe="/"))
    try:
        payload = _get(url, timeout=timeout)
    except (urllib.error.URLError, ValueError, TimeoutError, OSError):
        return {}

    endpoints = eligible_endpoints(payload)
    if not endpoints:
        return {}

    routing = {
        "sort": SORT,
        # Drop endpoints that do not accept what pi sends, rather than discovering
        # mid-review that one of them silently ignored the tool definitions.
        "require_parameters": True,
        # Fallbacks stay on. A degrading endpoint is the common case behind a slow
        # review, and rerouting off it is the whole point of ranking them; turning
        # this off would convert that reroute into a failed review.
        "allow_fallbacks": True,
    }
    ceiling = price_ceiling(endpoints)
    if ceiling:
        routing["max_price"] = ceiling
    return routing


def generation_ids(path):
    """Ordered, de-duplicated OpenRouter generation ids from a pi JSONL stream.

    pi writes `responseId` on the terminal event of each assistant turn. The events
    are searched structurally rather than by path: pi's event envelope is not part of
    its public contract, and an id that moves is worse than one that is absent,
    because the absence is visible and the move is not.
    """
    seen = {}
    with open(path, encoding="utf-8", errors="replace") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            for value in _walk_response_ids(event):
                seen[value] = None
    return list(seen)


def _walk_response_ids(node):
    if isinstance(node, dict):
        value = node.get("responseId")
        if isinstance(value, str) and value.startswith("gen-"):
            yield value
        for child in node.values():
            yield from _walk_response_ids(child)
    elif isinstance(node, list):
        for child in node:
            yield from _walk_response_ids(child)


def lookup(generation_id, api_key, timeout=15):
    url = f"{GENERATION_URL}?{urllib.parse.urlencode({'id': generation_id})}"
    try:
        payload = _get(url, api_key=api_key, timeout=timeout)
    except (urllib.error.URLError, ValueError, TimeoutError, OSError):
        return None
    record = payload.get("data") if isinstance(payload, dict) else None
    return record if isinstance(record, dict) else None


def summarize(records):
    """Per-endpoint attribution and timing for one pass.

    `primary` is the endpoint that served the most calls. A run with fallbacks on can
    legitimately span several, so the full set is reported alongside it and the count
    is what says whether the run was stable or was bounced around.
    """
    providers = {}
    latencies = []
    generation_times = []
    cost = 0.0
    cancelled = 0

    for record in records:
        name = record.get("provider_name")
        if name:
            providers[name] = providers.get(name, 0) + 1
        for field, sink in (("latency", latencies), ("generation_time", generation_times)):
            try:
                sink.append(float(record[field]))
            except (KeyError, TypeError, ValueError):
                pass
        try:
            cost += float(record.get("total_cost") or 0)
        except (TypeError, ValueError):
            pass
        if record.get("cancelled"):
            cancelled += 1

    ranked = sorted(providers.items(), key=lambda item: (-item[1], item[0]))
    metrics = {
        "openrouter.calls_attributed": len(records),
        "openrouter.provider_count": len(ranked),
        "openrouter.cancelled_calls": cancelled,
    }
    if latencies:
        metrics["openrouter.latency_ms_max"] = round(max(latencies), 3)
        metrics["openrouter.latency_ms_mean"] = round(statistics.fmean(latencies), 3)
    if generation_times:
        metrics["openrouter.generation_ms_max"] = round(max(generation_times), 3)
        metrics["openrouter.generation_ms_total"] = round(sum(generation_times), 3)
    if cost:
        metrics["openrouter.billed_cost"] = round(cost, 8)

    return {
        "primary": ranked[0][0] if ranked else "",
        "providers": [name for name, _ in ranked],
        "calls_by_provider": dict(ranked),
        "metrics": metrics,
    }


def attribute(path, api_key, max_lookups=MAX_LOOKUPS):
    ids = generation_ids(path)[-max_lookups:]
    records = [record for record in (lookup(i, api_key) for i in ids) if record]
    return summarize(records)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    routing = sub.add_parser("routing", help="emit the provider routing object for a model")
    routing.add_argument("slug")

    attributing = sub.add_parser("attribute", help="report which endpoints served a pi run")
    attributing.add_argument("jsonl")
    attributing.add_argument("--api-key", required=True)

    args = parser.parse_args(argv)
    if args.command == "routing":
        print(json.dumps(routing_for(args.slug), separators=(",", ":")))
    else:
        try:
            result = attribute(args.jsonl, args.api_key)
        except OSError:
            result = summarize([])
        print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
