#!/usr/bin/env python3
"""Turn a Stage-2 synthesis gym dataset into a GitHub Actions job matrix.

Sibling to plan_matrix.py — same one-job-per-record-per-arm shape, same 256-job matrix
cap, same reasoning for why a loop inside one job can't substitute for separate matrix
jobs (see that file's docstring). The only difference is the dataset shape: a synthesis
record has no `codex_prompt_version` to filter on (Stage 2's prompt versioning is an
independent axis — registry.json's arms — and this planner leaves it to the workflow to
pin `arm_role:control` at export time instead).

Usage:
  plan_synthesis_matrix.py --arms openai/gpt-5.6-luna,deepseek/deepseek-v4.1-flash --limit 3
Prints {"include": [...]} for `fromJSON` in a matrix strategy.
"""
import argparse
import json
import sys

import yaml

MAX_MATRIX_JOBS = 256


def arm_label(model):
    tail = model.split("/")[-1]
    import re
    return re.sub(r"[^a-z0-9]+", "-", tail.lower()).strip("-")


def plan(document, arms, limit=None):
    records = document.get("records") or []
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
        expected = record["expected_output"]
        for model in arms:
            include.append({
                "record": record["id"],
                "arm": arm_label(model),
                "model": model,
                "repo": source["repo"],
                "pr": source["pr"],
                "head_sha": source["head_sha"],
                "base_sha": source.get("base_sha"),
                "expected_verdict": expected["verdict"],
            })
    return include, skipped


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", default="gym/deepseek-synthesis-findings.yaml")
    parser.add_argument("--arms", required=True, help="comma-separated OpenRouter model slugs")
    parser.add_argument("--limit", type=int, help="first N records (smoke tests)")
    parser.add_argument("--out", help="also write the matrix JSON here")
    args = parser.parse_args(argv)

    with open(args.dataset) as stream:
        document = yaml.safe_load(stream)

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    include, skipped = plan(document, arms, args.limit)

    if not include:
        print("matrix is empty — check --limit and the dataset", file=sys.stderr)
        return 1
    if len(include) > MAX_MATRIX_JOBS:
        print(f"matrix has {len(include)} jobs, over GitHub's {MAX_MATRIX_JOBS} cap; "
              f"use --limit or fewer arms", file=sys.stderr)
        return 1
    if skipped:
        print(f"skipping {len(skipped)} unpinned record(s): "
              f"{', '.join(skipped[:5])}{' …' if len(skipped) > 5 else ''}", file=sys.stderr)

    payload = json.dumps({"include": include})
    if args.out:
        with open(args.out, "w") as stream:
            stream.write(payload + "\n")
    print(payload)
    print(f"{len(include)} jobs: {len(include) // max(len(arms), 1)} records x "
          f"{len(arms)} arm(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
