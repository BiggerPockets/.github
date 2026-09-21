#!/usr/bin/env python3
"""Aggregate per-record judge verdicts into a per-arm comparison.

This is where the run stops being a pile of JSON and becomes the answer to the question
that started it: does the candidate first-pass model still catch what the previous one
caught?

**Read the gap between arms, not a single arm's number.** A review is not deterministic.
Re-running the baseline model against its own recorded findings does not score 100%, and
how far short it falls is the measurement's noise floor. A candidate at 65% means nothing
until you know the baseline reproduces itself at 70% (a small real gap) or at 95% (a large
one). That is the entire reason the control arm is worth paying for, and why this report
refuses to print a verdict when only one arm is present.

Two numbers per arm. Plain recall is findings matched over findings sought. Weighted recall
scores a missed blocker above a missed nitpick, using the severity recorded in the dataset
rather than anything the judge decides, so the weighting cannot drift between runs. Where
they disagree, look: a candidate whose plain recall holds but whose weighted recall drops
is failing selectively on the findings that matter, which is worse than failing uniformly
and is invisible in the plain number.

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


def load_results(directory):
    """Every verdict JSON under `directory`, keyed by (arm, record)."""
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
            record, arm = payload.get("record"), payload.get("arm")
            if record and arm:
                results[(arm, record)] = payload
    return results


def aggregate(results):
    """Per-arm totals. Recall is summed over findings, not averaged over records: a record
    with eight findings should weigh more than one with a single finding, and averaging
    per-record ratios would silently equalise them."""
    arms = collections.defaultdict(lambda: {
        "records": 0, "baseline": 0, "matched": 0, "missed": 0, "extra": 0,
        "weighted_total": 0.0, "weighted_matched": 0.0, "empty_candidates": 0,
        "by_severity": collections.defaultdict(lambda: {"baseline": 0, "matched": 0}),
    })
    for (arm, _record), payload in results.items():
        score = payload.get("score") or {}
        bucket = arms[arm]
        bucket["records"] += 1
        bucket["baseline"] += score.get("baseline_count", 0)
        bucket["matched"] += score.get("matched_count", 0)
        bucket["missed"] += score.get("missed_count", 0)
        bucket["extra"] += score.get("extra_count", 0)
        bucket["weighted_total"] += score.get("weighted_total", 0.0)
        bucket["weighted_matched"] += score.get("weighted_matched", 0.0)
        bucket["empty_candidates"] += 1 if score.get("empty_candidate") else 0
        severity = payload.get("severity", "blocking")
        bucket["by_severity"][severity]["baseline"] += score.get("baseline_count", 0)
        bucket["by_severity"][severity]["matched"] += score.get("matched_count", 0)

    for bucket in arms.values():
        bucket["recall"] = (bucket["matched"] / bucket["baseline"]
                            if bucket["baseline"] else None)
        bucket["weighted_recall"] = (bucket["weighted_matched"] / bucket["weighted_total"]
                                     if bucket["weighted_total"] else None)
        bucket["by_severity"] = {k: dict(v) for k, v in bucket["by_severity"].items()}
    return dict(arms)


def pct(value):
    return "n/a" if value is None else f"{value * 100:.1f}%"


def render(summary, baseline_arm=None, failures=None):
    arms = sorted(summary)
    lines = ["# Gym run: first-pass findings recall", ""]
    lines += ["| arm | records | findings sought | matched | recall | weighted recall | extra |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for arm in arms:
        a = summary[arm]
        lines.append(f"| `{arm}` | {a['records']} | {a['baseline']} | {a['matched']} | "
                     f"{pct(a['recall'])} | {pct(a['weighted_recall'])} | {a['extra']} |")
    lines.append("")

    if len(arms) < 2:
        lines += [
            "> **Only one arm ran, so this number cannot be interpreted.** Review output is "
            "non-deterministic: the baseline model does not reproduce its own recorded "
            "findings at 100% either. Without a control arm there is nothing to compare "
            "this against, and a low score here is as likely to be normal variance as a "
            "regression. Re-run with both arms.", ""]
        return "\n".join(lines)

    control = baseline_arm if baseline_arm in summary else None
    if control:
        ceiling = summary[control]["recall"]
        lines += [f"Control arm `{control}` reproduces its own recorded findings at "
                  f"**{pct(ceiling)}**. That is the ceiling, not 100% — treat it as the "
                  f"noise floor for every other arm.", ""]
        for arm in arms:
            if arm == control:
                continue
            gap = ((summary[arm]["recall"] or 0) - (ceiling or 0))
            lines.append(f"- `{arm}` vs control: **{gap * 100:+.1f} points** "
                         f"({pct(summary[arm]['recall'])} vs {pct(ceiling)})")
        lines.append("")

    lines += ["## Recall by severity", "",
              "| arm | " + " | ".join(f"{s}" for s in ("blocker", "blocking", "non-blocking"))
              + " |", "|---|---|---|---|"]
    for arm in arms:
        cells = []
        for severity in ("blocker", "blocking", "non-blocking"):
            stats = summary[arm]["by_severity"].get(severity)
            cells.append("—" if not stats or not stats["baseline"]
                         else f"{stats['matched']}/{stats['baseline']}")
        lines.append(f"| `{arm}` | " + " | ".join(cells) + " |")
    lines.append("")

    empty = {a: summary[a]["empty_candidates"] for a in arms if summary[a]["empty_candidates"]}
    if empty:
        lines.append("Replays that produced no findings at all: "
                     + ", ".join(f"`{a}` ×{n}" for a, n in empty.items()))
        lines.append("")
    if failures:
        lines += [f"**{len(failures)} replay(s) failed and are excluded from recall** "
                  f"(infrastructure, not model quality): "
                  + ", ".join(sorted(failures)[:10]), ""]
    lines += ["---", "",
              "Recall is against the recorded baseline's findings, which are a previous "
              "model's output and not ground truth. Findings a candidate reports that the "
              "baseline missed are counted under *extra* and never penalised."]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--out", default="summary.md")
    parser.add_argument("--json", dest="json_out")
    parser.add_argument("--control-arm", help="arm label to treat as the control")
    parser.add_argument("--failures", help="file with one failed record id per line")
    args = parser.parse_args(argv)

    results = load_results(args.results_dir)
    if not results:
        print(f"no verdicts found under {args.results_dir}", file=sys.stderr)
        return 1
    summary = aggregate(results)

    failures = []
    if args.failures and os.path.exists(args.failures):
        failures = [line.strip() for line in open(args.failures) if line.strip()]

    markdown = render(summary, args.control_arm, failures)
    with open(args.out, "w") as stream:
        stream.write(markdown + "\n")
    if args.json_out:
        with open(args.json_out, "w") as stream:
            json.dump({"arms": summary, "failures": failures}, stream, indent=2)
    print(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
