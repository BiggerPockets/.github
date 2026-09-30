#!/usr/bin/env python3
"""How far matching findings by location, and by location and a shared category, agrees with the judge.

For every saved result the judge scored, and for every confirmed finding in its record,
this compares the judge's verdict with two machine matches against the review's
structured findings: same file at overlapping lines, and that plus at least one category in common. The
recorded findings' structured form comes from `structure_findings.py`; the review's comes
from its findings block, or from `structure_findings.py` for a review written before the
block existed.

Requiring a shared category was measured and rejected: it drops real matches because the
two sides' labels disagree, and location rarely matches wrongly to begin with. The README's
gym section has the numbers. The rule stays here so a change to the categories can be
measured the same way.

Prints, per rule, how many findings the judge and the rule both call found, only one of
them does, or neither does. Counts only: the findings quote private source.

Usage:
  scripts/gym/location_agreement.py --dataset ../pi-gym-data/gym/sol-first-pass-findings.yaml \\
      --results ../pi-gym-data/results/sol-first-pass-findings
"""
import argparse
import collections
import glob
import json
import os
import sys

import yaml

from finding_locations import found, structured

RULES = {"location": False, "location and category": True}


def candidates(result):
    """The review's structured findings, or None when it has none."""
    block = structured(result.get("findings") or "")
    return block if block is not None else result.get("structured_findings")


def tally(records, results):
    """{rule: Counter of (judge found, rule found)} over every confirmed finding whose
    verdict can be paired with it."""
    counts = {rule: collections.Counter() for rule in RULES}
    for result in results:
        record = records.get(result.get("record"))
        verdicts = (result.get("judge_verdict") or {}).get("baseline_findings")
        pointed = candidates(result)
        if not record or verdicts is None or pointed is None:
            continue
        statuses = record["metadata"]["confirmation"]["statuses"]
        recorded = record["expected_output"].get("structured_findings")
        if not recorded or len(recorded) != len(statuses):
            continue
        keep = [status == "fixed" for status in statuses]
        # Runs judged against every recorded finding pair with all of them; later runs
        # were judged against the confirmed findings only.
        if len(verdicts) == len(recorded):
            pairs = [(f, v) for f, v, k in zip(recorded, verdicts, keep) if k]
        elif len(verdicts) == sum(keep):
            pairs = list(zip([f for f, k in zip(recorded, keep) if k], verdicts))
        else:
            continue
        for finding, verdict in pairs:
            for rule, same_category in RULES.items():
                counts[rule][(bool(verdict.get("matched")),
                              found(finding, pointed, same_category))] += 1
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--results", required=True)
    args = parser.parse_args(argv)

    with open(args.dataset) as stream:
        records = {r["id"]: r for r in yaml.safe_load(stream).get("records") or []}
    results = []
    for path in sorted(glob.glob(os.path.join(args.results, "*", "*.json"))):
        with open(path) as stream:
            results.append(json.load(stream))

    for rule, c in tally(records, results).items():
        total = sum(c.values())
        agreed = c[(True, True)] + c[(False, False)]
        print(f"{rule}: {total} findings; both found {c[(True, True)]}, "
              f"judge only {c[(True, False)]}, rule only {c[(False, True)]}, "
              f"neither {c[(False, False)]}; agree "
              + (f"{agreed}/{total} ({agreed / total:.0%})" if total else "n/a"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
