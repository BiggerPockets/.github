#!/usr/bin/env python3
"""Turn the gym dataset into a GitHub Actions job matrix: one model, one job per record.

Each replay is an independent review: its own checkout of a different repository at a
different commit, its own model call, its own failure modes. Running them as separate
matrix jobs buys parallelism for free, keeps one wedged repository from taking the run
down, and — the reason that matters most — lets each job invoke `openai/codex-action`
exactly the way the production review job does. A loop inside one job could not: a GitHub
Action cannot be called in a loop, so a loop would have to shell out to the CLI and would
be measuring a slightly different harness than the one under test.

A run evaluates exactly one model. `--model` takes a single OpenRouter slug and rejects a
list: evaluating another model is a separate run, started on purpose, never a side effect
of this one.

The cost is runner minutes, and the matrix cap is real: GitHub allows 256 jobs. Start with
`--limit 3`, confirm the judge is calibrated on findings you can read yourself, then spend
the full run.

Records are emitted in dataset order and `--limit` takes the first N rather than a random
sample, so two runs at the same limit are comparable. Unpinned records — no `head_sha` —
are skipped with a warning: replaying one would review whatever the pull request looks like
today, which is the failure the pinning exists to prevent.

Usage:
  plan_matrix.py --model openai/gpt-5.6-luna --judge-model anthropic/claude-haiku-4.5 --limit 3
Prints {"include": [...]} for `fromJSON` in a matrix strategy.
"""
import argparse
import json
import re
import sys

import yaml

# GitHub's documented ceiling for a single matrix.
MAX_MATRIX_JOBS = 256
TICKET_KEY = re.compile(r"BIG-\d+", re.I)


def model_label(model):
    """A short, filesystem- and artifact-safe name for a model. `openai/gpt-5.6-luna`
    becomes `gpt-5-6-luna`, which is what appears in job names and artifact names."""
    tail = model.split("/")[-1]
    return re.sub(r"[^a-z0-9]+", "-", tail.lower()).strip("-")


def plan(document, model, limit=None, severities=None, prompt_version=None):
    """Jobs to run, plus the records deliberately left out.

    `prompt_version` is the fidelity filter that matters most after the commit pin. The
    findings in a record were produced by a specific first-pass prompt; replaying that
    record under a different prompt measures the prompt change and the model change
    together, and reports the sum as if it were the model. Passing the currently resolved
    version keeps the comparison to records the current prompt actually produced."""
    records = document.get("records") or []
    if severities:
        records = [r for r in records
                   if (r.get("metadata") or {}).get("severity") in severities]
    mismatched = []
    if prompt_version:
        kept = []
        for r in records:
            recorded = (r.get("metadata") or {}).get("codex_prompt_version")
            (kept if recorded == prompt_version else mismatched).append(r)
        records = kept
    pinned, skipped = [], []
    for record in records:
        if (record.get("input") or {}).get("head_sha"):
            pinned.append(record)
        else:
            skipped.append(record.get("id"))
    if limit is not None:
        pinned = pinned[:limit]

    include = []
    for record in pinned:
        source = record["input"]
        include.append({
            "record": record["id"],
            "label": model_label(model),
            "model": model,
            "repo": source["repo"],
            "pr": source["pr"],
            "head_sha": source["head_sha"],
            "base_sha": source.get("base_sha"),
            "severity": (record.get("metadata") or {}).get("severity", "blocking"),
        })
    return include, skipped + [r.get("id") for r in mismatched]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", default="gym/sol-first-pass-findings.yaml")
    parser.add_argument("--model", required=True,
                        help="the one OpenRouter model slug to evaluate")
    parser.add_argument("--judge-model", required=True,
                        help="the model that scores the replays; must not be --model, "
                             "since a model grading its own review is not a measurement")
    parser.add_argument("--limit", type=int, help="first N records (smoke tests)")
    parser.add_argument("--record-ids",
                        help="comma-separated record ids to replay, instead of the whole "
                             "dataset — for re-running specific records (e.g. ones that "
                             "failed or timed out last time) without paying for the rest")
    parser.add_argument("--severity", help="comma-separated severities to include")
    parser.add_argument("--prompt-version",
                        help="only replay records recorded under this first-pass prompt "
                             "version; omit to replay every record regardless")
    parser.add_argument("--out", help="also write the matrix JSON here")
    args = parser.parse_args(argv)

    with open(args.dataset) as stream:
        document = yaml.safe_load(stream)

    if args.record_ids:
        wanted = {r.strip() for r in args.record_ids.split(",") if r.strip()}
        document = dict(document, records=[
            r for r in (document.get("records") or []) if r.get("id") in wanted])
        missing = wanted - {r.get("id") for r in document["records"]}
        if missing:
            print(f"--record-ids named {len(missing)} id(s) not found in the dataset: "
                  f"{', '.join(sorted(missing))}", file=sys.stderr)

    model = args.model.strip()
    if not model or "," in model:
        print(f"--model takes exactly one model slug, got {args.model!r}. "
              f"Evaluate another model in its own run.", file=sys.stderr)
        return 1
    if args.judge_model.strip() == model:
        print(f"--judge-model must differ from --model ({model})", file=sys.stderr)
        return 1
    severities = ([s.strip() for s in args.severity.split(",")]
                  if args.severity else None)
    include, skipped = plan(document, model, args.limit, severities, args.prompt_version)

    if not include:
        print("matrix is empty — check --limit, --severity and the dataset",
              file=sys.stderr)
        return 1
    if len(include) > MAX_MATRIX_JOBS:
        print(f"matrix has {len(include)} jobs, over GitHub's {MAX_MATRIX_JOBS} cap; "
              f"use --limit or --record-ids", file=sys.stderr)
        return 1
    if skipped:
        print(f"skipping {len(skipped)} record(s) (unpinned, or recorded under a "
              f"different prompt version): "
              f"{', '.join(skipped[:5])}{' …' if len(skipped) > 5 else ''}",
              file=sys.stderr)

    payload = json.dumps({"include": include})
    if args.out:
        with open(args.out, "w") as stream:
            stream.write(payload + "\n")
    print(payload)
    print(f"{len(include)} jobs: {len(include)} records on {model}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
