#!/usr/bin/env python3
"""Emit the usage and cost fields for one review pass's Datadog LLM Obs span.

Datadog prices an LLM span two ways. When `model_name`/`model_provider` name a model
in its pricing catalog, token counts on the span are enough — it costs the span
itself. When they don't, it needs the money: a `total_cost` metric it takes at face
value. This script produces whichever of the two the pass can support, from numbers
the tools themselves recorded. It holds no rate table. A list price copied into this
file would go stale silently and be reported with the same confidence as a real one.

Where the numbers come from:

- Both passes reach models through OpenRouter, which reports what it charged in
  the `cost` field of every response's usage object. Where that figure survives into
  what the tool wrote to disk, it is the authoritative cost for the pass and is
  emitted as `total_cost` — a real charge, not an estimate.
- The Codex pass writes token counters to its session rollout. Its model is in the
  catalog, so those counts are enough for Datadog to price it.
- pi records a usage object on each assistant turn, covering that turn's request
  alone, so the pass's tokens are the sum over its turns. pi does not record what
  OpenRouter charged: its `cost.total` is tokens times the rates pinned in
  scripts/pi/models.json, which go stale whenever OpenRouter's prices move. What it
  does record is each turn's `responseId`, which on OpenRouter is the generation id,
  and GET /api/v1/generation returns the amount charged for it. The sum of those is
  emitted as `total_cost` when every charged turn resolves (scripts/pi/openrouter.py,
  `billed_cost`). When any does not, pi's own figure is emitted as `estimated_cost`
  instead, a metric Datadog does not price from, and `total_cost` is left out.
- Claude Code's own `total_cost_usd` is deliberately ignored. It is computed against
  Anthropic's list prices, and these passes are billed by OpenRouter for a non-
  Anthropic model, so it describes a bill nobody was sent. (The Claude Code harness
  itself is no longer used for Stage 2, but the parser stays for historical files.)

The workflow names models the way OpenRouter routes them ("openai/gpt-5.6-sol").
Datadog's catalog keys on the bare model and its originating provider, so
`split_model` splits that into ("gpt-5.6-sol", "openai"); the fact that the call was
routed through OpenRouter is preserved separately as a `gateway` tag.

Every pass also reports a `turn_count`: how many model turns it took. Every pass's
token counts are a total over the session's turns, so the token counts alone
cannot distinguish a long cheap agentic session from one enormous prompt, and a
cumulative input figure is not a context-window requirement. The turn count is
what separates the two, and it is the reason the freshness check sizes context
off fresh (non-cached) input rather than the cumulative total.

Only counts, costs, and model identifiers pass through here. pi's generation ids are
read too, and sent only to OpenRouter's generation endpoint. Message content,
transcripts, prompts, diffs, and responses are never read or emitted.

Usage: llm-usage.py claude <model-slug> [execution-file]
       llm-usage.py codex  <model-slug> [rollout-dir]
       llm-usage.py pi     <model-slug> [event-stream-file]
The pi pass reads OPENROUTER_API_KEY from the environment for the billing lookups.
Prints a JSON object on stdout for the workflow's jq to splice into a span:
  {"model_name": ..., "model_provider": ..., "gateway": ..., "metrics": {...}}
`metrics` carries only what is actually known; it is `{}` when nothing is.
Never raises and always exits 0 — cost telemetry must not fail a code review.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "pi"))
import openrouter  # noqa: E402

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

# pi's normalized usage object, as written to its JSON event stream (--mode json),
# mapped to Datadog's metric names. `cost.total` (pi's estimate from the pinned
# rates) is handled separately below.
PI_USAGE_FIELDS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cacheRead": "cache_read_input_tokens",
    "cacheWrite": "cache_write_input_tokens",
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
    total for the session, and the number of them is the number of turns.

    The turn count is what makes the token counts interpretable. Codex's
    `input_tokens` is a running total across the whole session, so on a cached
    agentic pass it far exceeds any single request's context — 1.15M cumulative
    against an ~80k working context is normal. Without the turn count there is no
    way to tell a long cheap session from one enormous prompt, and sizing a model's
    context window off the cumulative figure would demand roughly an order of
    magnitude more than the pass actually needs."""
    path = find_rollout(directory)
    if not path:
        return ({}, None)
    totals = None
    turns = 0
    for event in read_messages(path):
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        turns += 1
        info = payload.get("info")
        if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
            totals = info["total_token_usage"]
    if totals is None:
        return ({}, None)
    counts = collect(totals, CODEX_USAGE_FIELDS)
    if turns:
        counts["turn_count"] = turns
    return (counts, openrouter_cost(totals))


def pi_estimated_cost(messages):
    """pi's own price for the pass: the sum of each turn's `usage.cost.total`, which
    pi computes from the rates pinned in scripts/pi/models.json. None when no turn
    carries a positive figure."""
    total = 0.0
    for message in messages:
        cost = message["usage"].get("cost")
        value = read_number(cost.get("total")) if isinstance(cost, dict) else None
        if value is not None and value > 0:
            total += value
    return total or None


def pi_charged(message):
    """Whether a turn used any tokens, and so could have been charged for."""
    return any((read_number(message["usage"].get(field)) or 0) > 0
               for field in PI_USAGE_FIELDS)


def pi_billed_cost(messages, api_key):
    """What OpenRouter charged for the pass, from each charged turn's generation id,
    or None unless every one of those turns has an id and resolves."""
    charged = [m for m in messages if pi_charged(m)]
    ids = [m.get("responseId") for m in charged]
    if not ids or not all(isinstance(i, str) and i for i in ids):
        return None
    try:
        return openrouter.billed_cost(ids, api_key)
    except Exception:  # noqa: BLE001 — cost telemetry must not fail a review
        return None


def pi_assistant_messages(events):
    """The pass's assistant messages that carry usage, in order.

    pi reports the same message in two places: a per-message `message_end` event
    and, at the end, an `agent_end` event carrying the whole transcript. Counting
    both would double-count every turn, so when `agent_end` carries a transcript
    that is taken as authoritative and the per-message events are ignored. A
    stream that ends without one (a killed or timed-out run) falls back to the
    `message_end` events, which is all such a run has. `message_start` and
    `turn_end` carry the same message again and are not read."""
    from_agent_end = []
    from_events = []

    def usable(message):
        return (
            isinstance(message, dict)
            and message.get("role") == "assistant"
            and isinstance(message.get("usage"), dict)
        )

    for event in events:
        if event.get("type") == "agent_end":
            messages = event.get("messages")
            if isinstance(messages, list):
                from_agent_end = [m for m in messages if usable(m)]
            continue
        message = event.get("message")
        if event.get("type") == "message_end" and usable(message):
            from_events.append(message)
    return from_agent_end or from_events


def pi_usage(path, api_key=None):
    """pi's JSON event stream (--mode json) -> (token counts, costs).

    Each assistant message carries the usage of its own request, so the pass's
    counts are the sum over its messages, failed retries included, since those were
    sent too. The number of messages is the turn count, reported alongside the
    tokens so a long cheap session is distinguishable from one enormous prompt.

    Costs is a dict holding `total_cost`, the amount OpenRouter charged, when every
    charged turn resolves, and otherwise `estimated_cost`, pi's figure from the
    pinned rates. It is empty when neither is known."""
    messages = pi_assistant_messages(read_messages(path))
    if not messages:
        return ({}, {})
    counts = {}
    for message in messages:
        for metric, value in collect(message["usage"], PI_USAGE_FIELDS).items():
            counts[metric] = counts.get(metric, 0) + value
    counts["turn_count"] = len(messages)
    billed = pi_billed_cost(messages, api_key) if api_key else None
    if billed is not None:
        return (counts, {"total_cost": billed})
    estimated = pi_estimated_cost(messages)
    return (counts, {"estimated_cost": estimated} if estimated is not None else {})


def build_span_fields(slug, counts, cost, estimated_cost=None):
    """Pure assembly of the span fields the workflow splices in: a catalog-matching
    model identity, whichever token counts are known, and a reported cost when there
    is one. `total_tokens` is Datadog's own metric name, so it is spelled out rather
    than left for Datadog to infer. An estimate goes under `estimated_cost`, never
    `total_cost`, so Datadog's spend views count only amounts actually charged."""
    model_name, provider = split_model(slug)
    metrics = dict(counts)
    if "input_tokens" in metrics and "output_tokens" in metrics:
        metrics["total_tokens"] = metrics["input_tokens"] + metrics["output_tokens"]
    if cost is not None:
        metrics["total_cost"] = cost
    elif estimated_cost is not None:
        metrics["estimated_cost"] = estimated_cost
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
    estimated = None
    if pass_name == "codex":
        counts, cost = codex_usage(source or DEFAULT_ROLLOUT_DIR)
    elif pass_name == "claude":
        counts, cost = claude_usage(source)
    elif pass_name == "pi":
        counts, costs = pi_usage(source, os.environ.get("OPENROUTER_API_KEY"))
        cost, estimated = costs.get("total_cost"), costs.get("estimated_cost")
    else:
        counts, cost = ({}, None)
    print(json.dumps(build_span_fields(slug, counts, cost, estimated)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
