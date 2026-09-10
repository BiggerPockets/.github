#!/usr/bin/env python3
"""Emit the usage and cost fields for one review pass's Datadog LLM Obs span.

Datadog prices an LLM span two ways. When `model_name`/`model_provider` name a model
in its pricing catalog, token counts on the span are enough — it costs the span
itself. When they don't, it needs the money: a `total_cost` metric it takes at face
value. This script produces whichever of the two the pass can support, from numbers
the tools themselves recorded. It holds no rate table. A list price copied into this
file would go stale silently and be reported with the same confidence as a real one.

Where the numbers come from:

- Both passes reach models through OpenRouter, which reports what it charged in the
  `cost` field of every response's usage object. Where that figure survives into what
  the tool wrote to disk, it is the authoritative cost for the pass and is emitted as
  `total_cost` — a real charge, not an estimate.
- The Codex pass writes token counters to its session rollout. Its model is in the
  catalog, so those counts are enough for Datadog to price it.
- Claude Code's own `total_cost_usd` is deliberately ignored. It is computed against
  Anthropic's list prices, and these passes are billed by OpenRouter for a non-
  Anthropic model, so it describes a bill nobody was sent.

The workflow names models the way OpenRouter routes them ("openai/gpt-5.6-sol").
Datadog's catalog keys on the bare model and its originating provider, so
`split_model` splits that into ("gpt-5.6-sol", "openai"); the fact that the call was
routed through OpenRouter is preserved separately as a `gateway` tag.

Only counts, costs, and model identifiers pass through here. Message content,
transcripts, prompts, diffs, and responses are never read or emitted.

Usage: llm-usage.py claude <model-slug> [execution-file]
       llm-usage.py codex  <model-slug> [rollout-dir]
Prints a JSON object on stdout for the workflow's jq to splice into a span:
  {"model_name": ..., "model_provider": ..., "gateway": ..., "metrics": {...}}
`metrics` carries only what is actually known; it is `{}` when nothing is.
Never raises and always exits 0 — cost telemetry must not fail a code review.
"""
import json
import os
import sys

# Datadog's catalog keys on the originating provider, not the gateway a call was
# routed through. Slugs the workflow uses map to that provider by their first segment.
GATEWAY = "openrouter"

DEFAULT_ROLLOUT_DIR = os.path.expanduser("~/.codex/sessions")

# Claude Code's usage object, in Anthropic's naming, mapped to Datadog's metric names.
CLAUDE_USAGE_FIELDS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_input_tokens": "cache_read_input_tokens",
    "cache_creation_input_tokens": "cache_write_input_tokens",
}

# Codex's rollout counters, in its own naming, mapped to Datadog's metric names.
# `cached_input_tokens` is a subset of `input_tokens`, matching Datadog's split of
# input into non-cached and cache-read.
CODEX_USAGE_FIELDS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cached_input_tokens": "cache_read_input_tokens",
}


def split_model(slug):
    """"openai/gpt-5.6-sol" -> ("gpt-5.6-sol", "openai"). A slug with no provider
    prefix keeps its name and reports an unspecified provider."""
    slug = (slug or "").strip()
    if not slug:
        return ("unspecified", "unspecified")
    if "/" in slug:
        provider, _, name = slug.partition("/")
        return (name or "unspecified", provider or "unspecified")
    return (slug, "unspecified")


def read_number(value):
    """A usable numeric value, or None. Booleans are numbers in Python and are not
    usable ones here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def read_messages(path):
    """Parse a file that is a JSON array, a single JSON object, or JSON-lines into a
    list of dicts. Empty on anything unreadable. This mirrors review-diagnostics.py's
    tolerant parsing: both tools' output formats have moved before."""
    if not path:
        return []
    try:
        with open(path) as handle:
            text = handle.read()
    except OSError:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def openrouter_cost(usage):
    """The amount OpenRouter reports charging for a call, from its usage object, or
    None when the field didn't survive into what the tool recorded. `cost` is the
    total billed; `cost_details.upstream_inference_cost` is the provider's share of
    it and is the fallback when only the breakdown came through."""
    if not isinstance(usage, dict):
        return None
    cost = read_number(usage.get("cost"))
    if cost is not None:
        return cost
    details = usage.get("cost_details")
    if isinstance(details, dict):
        return read_number(details.get("upstream_inference_cost"))
    return None


def collect(usage, fields):
    """Whichever of `fields` the usage object actually carries, under Datadog's
    metric names."""
    counts = {}
    if not isinstance(usage, dict):
        return counts
    for source, metric in fields.items():
        value = read_number(usage.get(source))
        if value is not None:
            counts[metric] = int(value)
    return counts


def claude_usage(path):
    """Claude Code's execution file -> (token counts, reported cost or None). The
    terminal "result" message carries `usage`; only that object is read."""
    results = [m for m in read_messages(path) if m.get("type") == "result"]
    if not results:
        return ({}, None)
    usage = results[-1].get("usage")
    return (collect(usage, CLAUDE_USAGE_FIELDS), openrouter_cost(usage))


def find_rollout(directory):
    """The most recently modified rollout file under Codex's session directory, which
    nests them by date. None when the directory is absent or holds none."""
    newest = None
    for root, _, names in os.walk(directory or ""):
        for name in names:
            if not (name.startswith("rollout-") and name.endswith(".jsonl")):
                continue
            path = os.path.join(root, name)
            try:
                stamp = os.path.getmtime(path)
            except OSError:
                continue
            if newest is None or stamp > newest[0]:
                newest = (stamp, path)
    return newest[1] if newest else None


def codex_usage(directory):
    """Codex's session rollout -> (token counts, reported cost or None). Codex logs a
    running total after every turn under a `token_count` event; the last one is the
    total for the session."""
    path = find_rollout(directory)
    if not path:
        return ({}, None)
    totals = None
    for event in read_messages(path):
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
            totals = info["total_token_usage"]
    if totals is None:
        return ({}, None)
    return (collect(totals, CODEX_USAGE_FIELDS), openrouter_cost(totals))


def build_span_fields(slug, counts, cost):
    """Pure assembly of the span fields the workflow splices in: a catalog-matching
    model identity, whichever token counts are known, and a reported cost when there
    is one. `total_tokens` is Datadog's own metric name, so it is spelled out rather
    than left for Datadog to infer."""
    model_name, provider = split_model(slug)
    metrics = dict(counts)
    if "input_tokens" in metrics and "output_tokens" in metrics:
        metrics["total_tokens"] = metrics["input_tokens"] + metrics["output_tokens"]
    if cost is not None:
        metrics["total_cost"] = cost
    return {
        "model_name": model_name,
        "model_provider": provider,
        "gateway": GATEWAY,
        "metrics": metrics,
    }


def main(argv):
    pass_name = argv[1] if len(argv) > 1 else ""
    slug = argv[2] if len(argv) > 2 else ""
    source = argv[3] if len(argv) > 3 else ""
    if pass_name == "codex":
        counts, cost = codex_usage(source or DEFAULT_ROLLOUT_DIR)
    elif pass_name == "claude":
        counts, cost = claude_usage(source)
    else:
        counts, cost = ({}, None)
    print(json.dumps(build_span_fields(slug, counts, cost)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
