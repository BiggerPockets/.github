#!/usr/bin/env python3
"""Export Stage-2 synthesis outcomes from Datadog LLM Observability into a gym dataset.

sol-first-pass-findings.yaml answers "does a candidate Stage-1 model still catch what a
previous one caught". This script answers the other half of the pipeline: "does a
candidate Stage-2 model still reach the same review decision, given the same Stage-1
findings". The two questions are kept apart deliberately — replaying Stage 2 against the
*recorded* first-pass findings (rather than a freshly generated one) isolates the
synthesis model from whatever Stage 1 is doing, so a Stage-2 regression can't hide behind
a Stage-1 improvement or vice versa. Compose the two results, don't chain the replays.

Stage 2 writes its decision into the `pi.synthesize` span. The review workflow's own
Datadog step (see .github/workflows/biggiepockets-review.yml) puts the exact first-pass
findings text it handed to Stage 2 on that same span's `meta.input.messages[0].content`
— which means this export needs no join against the `pi.first_pass` span at all; the
synthesis span already carries its own input. The workflow truncates that field (and the
output) to 4000 characters before reporting it, so a very long findings report or summary
is exported truncated. That is a known ceiling of this dataset, not a bug in this script.

What it selects, and why:

- `@status:ok` — the pass produced a verdict. An errored pass has no decision to check a
  candidate against.
- `synthesis_model:<model>` — the model whose decisions become the baseline. Defaults to
  deepseek/deepseek-v4.1-flash, the org's current Stage 2 default.
- `arm_role:control` by default — the A/B prompt test in registry.json means two
  different prompts can produce a verdict on the same model; mixing them into one
  baseline would blame the model for a prompt variant. Pass `--arm-role any` to include
  the experiment arm too.
- One record per (repo, PR), keeping the most recent qualifying pass — same reasoning as
  export_sol_findings.py: a PR is reviewed again on every push.

Every record pins `head_sha`/`base_ref`/`base_sha`, the same way and for the same reason
as the first-pass export: the diff and the tree a replay checks out must be the ones
Stage 2 actually read, or a drifted PR silently reviews the wrong code and scores as a
disagreement that was never real. See export_sol_findings.py for the full rationale and
the two things pinning cannot fix (deleted branches, ticket drift).

Usage:
  DD_API_KEY=... DD_APP_KEY=... scripts/gym/export_synthesis_findings.py \
      --model deepseek/deepseek-v4.1-flash --out gym/deepseek-synthesis-findings.yaml
"""
import argparse
import datetime
import os
import re
import sys
import urllib.error

import yaml

# Shared with export_sol_findings.py: same search/paging/commit-resolution machinery,
# just aimed at a different span. Importing rather than duplicating keeps both scripts
# behind one Datadog paging implementation and one gh-based commit resolver.
sys.path.insert(0, os.path.dirname(__file__))
from export_sol_findings import (  # noqa: E402
    Block, redact, resolve_commit, search_spans, tag_map,
)


class LiteralBlock(Block):
    """Alias kept local so a reader of this file doesn't have to open the sibling
    module to see that findings/summary text is emitted as a YAML literal block."""


def findings_text(attributes):
    """The first-pass findings Stage 2 was handed, as recorded on its own input."""
    messages = ((attributes.get("meta") or {}).get("input") or {}).get("messages") or []
    for message in messages:
        content = (message or {}).get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def summary_text(attributes):
    """The decision's summary, as recorded on the span's output."""
    messages = ((attributes.get("meta") or {}).get("output") or {}).get("messages") or []
    for message in reversed(messages):
        content = (message or {}).get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def to_record(span, model, commit=None):
    """One dataset row, or None when the span carries no usable decision."""
    attributes = span.get("attributes") or {}
    tags = tag_map(attributes.get("tags"))
    repo, pr = tags.get("repo"), tags.get("pr")
    verdict = tags.get("verdict")
    if not repo or not pr or verdict not in ("approve", "request_changes"):
        return None

    findings = redact(findings_text(attributes))
    summary = redact(summary_text(attributes))
    if not summary:
        return None
    commit = commit or {}

    start_ns = span.get("start_ns") or attributes.get("start_ns") or 0
    reviewed_at = datetime.datetime.fromtimestamp(
        start_ns / 1e9, datetime.UTC).isoformat(timespec="seconds") if start_ns else None

    return {
        "id": f"{repo.split('/')[-1]}-pr{pr}",
        "_sort_key": (repo, int(pr) if pr.isdigit() else 0),
        "_start_ns": start_ns,
        "_run_id": tags.get("run_id"),
        "input": {
            "repo": repo,
            "pr": int(pr) if pr.isdigit() else pr,
            "head_sha": commit.get("head_sha"),
            "base_ref": commit.get("base_ref"),
            "base_sha": commit.get("base_sha"),
            # What Stage 1 handed this Stage-2 pass, pinned alongside the commit so a
            # replay reviews the same findings against the same code — not today's
            # first-pass output against today's code, which would measure the wrong
            # model twice over.
            "first_pass_findings": LiteralBlock(
                (findings or "(no first-pass findings recorded)") + "\n"),
            "instruction": f"Synthesize a review decision for pr:{pr}",
        },
        "expected_output": {
            "verdict": verdict,
            "summary": LiteralBlock(summary + "\n"),
        },
        "metadata": {
            "synthesis_model": tags.get("synthesis_model"),
            "first_pass_model": tags.get("first_pass_model"),
            "arm": tags.get("arm"),
            "arm_role": tags.get("arm_role"),
            "prompt_name": tags.get("prompt_name"),
            "prompt_version": tags.get("prompt_version"),
            "reviewed_at": reviewed_at,
            "trace_id": span.get("trace_id") or attributes.get("trace_id"),
            "span_id": span.get("span_id") or attributes.get("span_id"),
        },
        "tags": [
            f"repo:{repo}",
            f"pr:{pr}",
            f"source_model:{model}",
            "stage:synthesis",
            f"verdict:{verdict}",
        ],
    }


def dedupe(records):
    best = {}
    for record in records:
        key = record["_sort_key"]
        if key not in best or record["_start_ns"] > best[key]["_start_ns"]:
            best[key] = record
    ordered = sorted(best.values(), key=lambda r: r["_sort_key"])
    for record in ordered:
        record.pop("_sort_key")
        record.pop("_start_ns")
    return ordered


def build_query(model, ml_app, arm_role):
    query = f"@ml_app:{ml_app} @name:pi.synthesize synthesis_model:{model} @status:ok"
    if arm_role != "any":
        query += f" arm_role:{arm_role}"
    return query


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="deepseek/deepseek-v4.1-flash",
                        help="Stage-2 model slug to export decisions for")
    parser.add_argument("--ml-app", default="biggiepockets-review")
    parser.add_argument("--arm-role", default="control",
                        help="restrict to this prompt arm's decisions, or 'any'")
    parser.add_argument("--since", default="now-30d")
    parser.add_argument("--until", default="now")
    parser.add_argument("--name", default="deepseek-synthesis-findings",
                        help="dataset name recorded in the file")
    parser.add_argument("--out", default="gym/deepseek-synthesis-findings.yaml")
    parser.add_argument("--no-resolve-commits", action="store_true")
    args = parser.parse_args(argv)

    api_key = os.environ.get("DD_API_KEY")
    app_key = os.environ.get("DD_APP_KEY")
    if not api_key or not app_key:
        print("DD_API_KEY and DD_APP_KEY must be set", file=sys.stderr)
        return 2
    site = os.environ.get("DD_SITE", "datadoghq.com")

    query = build_query(args.model, args.ml_app, args.arm_role)
    try:
        spans = list(search_spans(site, api_key, app_key, query, args.since, args.until))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as error:
        print(f"Datadog spans search failed: {error}", file=sys.stderr)
        return 1

    staged = dedupe([r for r in (to_record(s, args.model) for s in spans) if r])
    if not staged:
        print("No qualifying spans found — check the window, model slug and arm role.",
              file=sys.stderr)
        return 1

    records = []
    cache = {}
    for record in staged:
        run_id_tag = record.pop("_run_id", None)
        if not args.no_resolve_commits:
            commit = resolve_commit(record["input"]["repo"], record["input"]["pr"],
                                    run_id_tag, cache)
            record["input"].update({
                "head_sha": commit.get("head_sha"),
                "base_ref": commit.get("base_ref"),
                "base_sha": commit.get("base_sha"),
            })
        records.append(record)

    unpinned = [r["id"] for r in records if not r["input"].get("head_sha")]
    if unpinned:
        print(f"Warning: {len(unpinned)} record(s) could not be pinned to a commit: "
              f"{', '.join(unpinned[:5])}{' …' if len(unpinned) > 5 else ''}",
              file=sys.stderr)

    document = {
        "version": 1,
        "dataset": {
            "name": args.name,
            "description": (
                f"Stage-2 review decisions ({{verdict, summary}}) produced by "
                f"{args.model} from the exact Stage-1 findings it was handed. Used to "
                f"check whether a replacement Stage-2 model reaches the same decision "
                f"given the same input, independent of any Stage-1 change."),
        },
        "source": {
            "ml_app": args.ml_app,
            "span_name": "pi.synthesize",
            "model": args.model,
            "query": query,
            "window": {"from": args.since, "to": args.until},
            "exported_at": datetime.datetime.now(datetime.UTC).date().isoformat(),
        },
        "records": records,
    }

    yaml.add_representer(
        LiteralBlock,
        lambda dumper, data: dumper.represent_scalar(
            "tag:yaml.org,2002:str", str(data), style="|"))
    with open(args.out, "w") as stream:
        yaml.dump(document, stream, sort_keys=False, allow_unicode=True,
                  width=100, default_flow_style=False)
    print(f"Wrote {len(records)} records from {len(spans)} spans to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
