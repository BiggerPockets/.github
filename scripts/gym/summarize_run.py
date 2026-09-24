#!/usr/bin/env python3
"""Aggregate per-record judge verdicts into one model's recall report.

This is where the run stops being a pile of JSON and becomes the answer to the question
that started it: does this first-pass model still catch what the recorded model caught?

A run evaluates exactly one model, so this report describes exactly one model. Verdicts
from more than one model under the same results directory are an error rather than
something to tabulate side by side: they mean two runs' artifacts were mixed together.

Two numbers. Plain recall is findings matched over findings sought. Weighted recall scores
a missed blocker above a missed nitpick, using the severity recorded in the dataset rather
than anything the judge decides, so the weighting cannot drift between runs. Where they
disagree, look: a model whose plain recall holds but whose weighted recall drops is failing
selectively on the findings that matter, which is worse than failing uniformly and is
invisible in the plain number.

Records where the replay failed are reported separately and excluded from recall. Folding
an infrastructure failure into a model's score would make a flaky checkout look like a
worse reviewer.

Usage:
  summarize_run.py --results-dir results/ --out summary.md [--json summary.json]
"""
import argparse
import collections
import json
import os
import sys

SEVERITIES = ("blocker", "blocking", "non-blocking")


def load_results(directory):
    """Every verdict JSON under `directory`, keyed by (label, record). A replay also
    uploads an endpoint-attribution JSON carrying the same record and label; it has no
    score, and is skipped so it cannot stand in for the verdict."""
    results = {}
    for root, _, files in os.walk(directory):
        for name in files:
            if not name.endswith(".json"):
                continue
            path = os.path.join(root, name)
            try:
                with open(path) as stream:
                    payload = json.load(stream)
            except (OSError, ValueError):
                continue
            record, label = payload.get("record"), payload.get("label")
            if record and label and "score" in payload:
                results[(label, record)] = payload
    return results


def aggregate(results):
    """Totals for the one model in `results`. Recall is summed over findings, not averaged
    over records: a record with eight findings should weigh more than one with a single
    finding, and averaging per-record ratios would silently equalise them.

    Raises ValueError when the verdicts come from more than one model."""
    labels = sorted({label for label, _record in results})
    if len(labels) != 1:
        raise ValueError(f"expected verdicts for exactly one model, found {len(labels)}: "
                         f"{', '.join(labels) or 'none'}")
    totals = {
        "label": labels[0],
        "records": 0, "sought": 0, "matched": 0, "missed": 0, "extra": 0,
        "weighted_total": 0.0, "weighted_matched": 0.0, "empty_candidates": 0,
    }
    by_severity = collections.defaultdict(lambda: {"sought": 0, "matched": 0})
    for payload in results.values():
        score = payload.get("score") or {}
        totals["records"] += 1
        totals["sought"] += score.get("baseline_count", 0)
        totals["matched"] += score.get("matched_count", 0)
        totals["missed"] += score.get("missed_count", 0)
        totals["extra"] += score.get("extra_count", 0)
        totals["weighted_total"] += score.get("weighted_total", 0.0)
        totals["weighted_matched"] += score.get("weighted_matched", 0.0)
        totals["empty_candidates"] += 1 if score.get("empty_candidate") else 0
        severity = payload.get("severity", "blocking")
        by_severity[severity]["sought"] += score.get("baseline_count", 0)
        by_severity[severity]["matched"] += score.get("matched_count", 0)

    totals["recall"] = (totals["matched"] / totals["sought"]
                        if totals["sought"] else None)
    totals["weighted_recall"] = (totals["weighted_matched"] / totals["weighted_total"]
                                 if totals["weighted_total"] else None)
    totals["by_severity"] = dict(by_severity)
    return totals


def pct(value):
    return "n/a" if value is None else f"{value * 100:.1f}%"


def render(summary, failures=None):
    s = summary
    lines = [f"# Gym run: first-pass findings recall for `{s['label']}`", "",
             "| records | findings sought | matched | recall | weighted recall | extra |",
             "|---:|---:|---:|---:|---:|---:|",
             f"| {s['records']} | {s['sought']} | {s['matched']} | {pct(s['recall'])} | "
             f"{pct(s['weighted_recall'])} | {s['extra']} |", ""]

    lines += ["## Recall by severity", "",
              "| " + " | ".join(SEVERITIES) + " |", "|---|---|---|"]
    cells = []
    for severity in SEVERITIES:
        stats = s["by_severity"].get(severity)
        cells.append("—" if not stats or not stats["sought"]
                     else f"{stats['matched']}/{stats['sought']}")
    lines += ["| " + " | ".join(cells) + " |", ""]

    if s["empty_candidates"]:
        lines += [f"Replays that produced no findings at all: {s['empty_candidates']}", ""]
    if failures:
        lines += [f"**{len(failures)} replay(s) failed and are excluded from recall** "
                  f"(infrastructure, not model quality): "
                  + ", ".join(sorted(failures)[:10]), ""]
    lines += ["---", "",
              "Recall is against the recorded model's findings, which are a previous "
              "model's output and not ground truth. Review output is non-deterministic, so "
              "the recorded model would not reproduce its own findings at 100% either. "
              "Findings this model reports that the recorded model missed are counted under "
              "*extra* and never penalised."]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--out", default="summary.md")
    parser.add_argument("--json", dest="json_out")
    parser.add_argument("--failures", help="file with one failed record id per line")
    args = parser.parse_args(argv)

    results = load_results(args.results_dir)
    if not results:
        print(f"no verdicts found under {args.results_dir}", file=sys.stderr)
        return 1
    try:
        summary = aggregate(results)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1

    failures = []
    if args.failures and os.path.exists(args.failures):
        failures = [line.strip() for line in open(args.failures) if line.strip()]

    markdown = render(summary, failures)
    with open(args.out, "w") as stream:
        stream.write(markdown + "\n")
    if args.json_out:
        with open(args.json_out, "w") as stream:
            json.dump({"model": summary, "failures": failures}, stream, indent=2)
    print(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
