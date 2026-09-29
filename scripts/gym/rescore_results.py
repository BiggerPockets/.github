#!/usr/bin/env python3
"""Re-score saved gym results against the confirmed findings, without re-running them.

Every saved result (see `gym_results.py`) holds the judge's verdict on each recorded
finding, in the order the findings appear in the record. `confirm_findings.py` gives each
of those findings a status in the same order. So a result scored against every recorded
finding can be re-scored against the confirmed ones by keeping the judge's verdicts on the
fixed findings and recounting: no replay and no judge call.

The judge let each candidate finding match at most one recorded finding, and it chose
among all of them. A candidate finding it paired with an unconfirmed recorded finding could
have matched a confirmed one instead, so a re-scored recall can read slightly lower than a
fresh judgement against the confirmed findings alone would.

The re-score is written to each result as `confirmed_score`, beside the original `score`.
A result is left as it is when it has no verdict (the replay or the judge failed), when its
record has no confirmed finding, or when the verdict does not hold one entry per recorded
finding, since the verdicts then cannot be paired with the statuses.

Prints one line per run: counts only, since results quote private source.

Usage:
  scripts/gym/rescore_results.py --dataset ../pi-gym-data/gym/sol-first-pass-findings.yaml \\
      --results ../pi-gym-data/results/sol-first-pass-findings
"""
import argparse
import collections
import glob
import json
import os
import sys

import yaml

from judge_findings import WEIGHTS


def confirmed_score(result, record):
    """The result's score over the record's fixed findings, or None when it can't be
    re-scored."""
    confirmation = (record.get("metadata") or {}).get("confirmation") or {}
    statuses = confirmation.get("statuses") or []
    verdicts = (result.get("judge_verdict") or {}).get("baseline_findings")
    if "fixed" not in statuses or verdicts is None or len(verdicts) != len(statuses):
        return None
    kept = [v for v, status in zip(verdicts, statuses) if status == "fixed"]
    matched = sum(1 for v in kept if v.get("matched"))
    weight = WEIGHTS.get(result.get("severity"), 1.0)
    return {
        "baseline_count": len(kept),
        "matched_count": matched,
        "missed_count": len(kept) - matched,
        "recall": matched / len(kept),
        "weight": weight,
        "weighted_total": len(kept) * weight,
        "weighted_matched": matched * weight,
        "confirmation_method": confirmation.get("method"),
        "labeled_on": confirmation.get("labeled_on"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", required=True, help="the labeled gym dataset YAML")
    parser.add_argument("--results", required=True,
                        help="the dataset's results directory, one subdirectory per run")
    args = parser.parse_args(argv)

    with open(args.dataset) as stream:
        records = {r["id"]: r for r in yaml.safe_load(stream).get("records") or []}

    runs = collections.defaultdict(collections.Counter)
    for path in sorted(glob.glob(os.path.join(args.results, "*", "*.json"))):
        run = os.path.basename(os.path.dirname(path))
        with open(path) as stream:
            result = json.load(stream)
        record = records.get(result.get("record"))
        score = confirmed_score(result, record) if record else None
        tally = runs[run]
        if score is None:
            tally["left"] += 1
            continue
        result["confirmed_score"] = score
        with open(path, "w") as stream:
            stream.write(json.dumps(result, indent=2) + "\n")
        tally["rescored"] += 1
        tally["sought"] += score["baseline_count"]
        tally["matched"] += score["matched_count"]

    for run, tally in sorted(runs.items()):
        recall = f"{tally['matched'] / tally['sought']:.1%}" if tally["sought"] else "-"
        print(f"{run}: rescored {tally['rescored']}, left {tally['left']}, "
              f"confirmed recall {tally['matched']}/{tally['sought']} ({recall})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
