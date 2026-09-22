#!/usr/bin/env python3
"""Aggregate per-record judge_synthesis.py verdicts into a per-arm comparison.

**Read the gap between arms, not a single arm's number** — same principle as
summarize_run.py. Re-running the baseline model against its own recorded decisions is the
control arm; it will not agree with itself 100% of the time either, and how far short it
falls is the noise floor a candidate is measured against.

Two headline numbers per arm:

- **Verdict agreement**: the share of records where the candidate reached the same
  approve/request_changes decision as the baseline, given the identical Stage-1 findings.
  This is the number that would actually change what gets merged.
- **Concern recall**: of the records where the baseline requested changes AND the
  candidate agreed, the share of the baseline's blocking concerns the candidate's summary
  also raised. Narrower than verdict agreement on purpose — it only asks the question on
  records where both models point at a real defect, so it isn't diluted by approvals.

Also reported: verdict agreement split by the baseline's own verdict (approve vs
request_changes), because a candidate that agrees 90% of the time by always requesting
changes is not the same as one that discriminates correctly — the split makes that
visible where the blended number would hide it.

Usage:
  summarize_synthesis_run.py --results-dir results/ --out summary.md [--json summary.json]
"""
import argparse
import collections
import json
import os
import sys


def load_results(directory):
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
    arms = collections.defaultdict(lambda: {
        "records": 0, "agreed": 0,
        "by_baseline_verdict": collections.defaultdict(lambda: {"total": 0, "agreed": 0}),
        "concern_baseline": 0, "concern_matched": 0, "concern_records": 0,
    })
    for (arm, _record), payload in results.items():
        bucket = arms[arm]
        bucket["records"] += 1
        agreed = bool(payload.get("verdict_agreement"))
        bucket["agreed"] += 1 if agreed else 0
        baseline_verdict = payload.get("expected_verdict", "unknown")
        slot = bucket["by_baseline_verdict"][baseline_verdict]
        slot["total"] += 1
        slot["agreed"] += 1 if agreed else 0

        coverage = payload.get("coverage") or {}
        if coverage.get("baseline_count") is not None:
            bucket["concern_records"] += 1
            bucket["concern_baseline"] += coverage.get("baseline_count", 0)
            bucket["concern_matched"] += coverage.get("matched_count", 0)

    for bucket in arms.values():
        bucket["agreement_rate"] = (bucket["agreed"] / bucket["records"]
                                    if bucket["records"] else None)
        bucket["concern_recall"] = (bucket["concern_matched"] / bucket["concern_baseline"]
                                    if bucket["concern_baseline"] else None)
        bucket["by_baseline_verdict"] = {k: dict(v) for k, v in bucket["by_baseline_verdict"].items()}
    return dict(arms)


def pct(value):
    return "n/a" if value is None else f"{value * 100:.1f}%"


def render(summary, baseline_arm=None, failures=None):
    arms = sorted(summary)
    lines = ["# Gym run: Stage-2 synthesis decision agreement", ""]
    lines += ["| arm | records | verdict agreement | concern recall (on request_changes) |",
              "|---|---:|---:|---:|"]
    for arm in arms:
        a = summary[arm]
        lines.append(f"| `{arm}` | {a['records']} | {pct(a['agreement_rate'])} | "
                     f"{pct(a['concern_recall'])} |")
    lines.append("")

    if len(arms) < 2:
        lines += [
            "> **Only one arm ran, so this number cannot be interpreted.** Re-running "
            "the baseline model does not agree with its own recorded decisions 100% of "
            "the time either. Without a control arm there is no noise floor to compare "
            "this against. Re-run with both arms.", ""]
        return "\n".join(lines)

    control = baseline_arm if baseline_arm in summary else None
    if control:
        ceiling = summary[control]["agreement_rate"]
        lines += [f"Control arm `{control}` agrees with its own recorded decisions at "
                  f"**{pct(ceiling)}**. That is the ceiling, not 100% — treat it as the "
                  f"noise floor for every other arm.", ""]
        for arm in arms:
            if arm == control:
                continue
            gap = ((summary[arm]["agreement_rate"] or 0) - (ceiling or 0))
            lines.append(f"- `{arm}` vs control: **{gap * 100:+.1f} points** "
                         f"({pct(summary[arm]['agreement_rate'])} vs {pct(ceiling)})")
        lines.append("")

    lines += ["## Agreement by the baseline's own verdict", "",
              "| arm | when baseline approved | when baseline requested changes |",
              "|---|---|---|"]
    for arm in arms:
        cells = []
        for verdict in ("approve", "request_changes"):
            stats = summary[arm]["by_baseline_verdict"].get(verdict)
            cells.append("—" if not stats or not stats["total"]
                         else f"{stats['agreed']}/{stats['total']}")
        lines.append(f"| `{arm}` | " + " | ".join(cells) + " |")
    lines.append("")

    if failures:
        lines += [f"**{len(failures)} replay(s) failed and are excluded from these "
                  f"numbers** (infrastructure, not model quality): "
                  + ", ".join(sorted(failures)[:10]), ""]
    lines += ["---", "",
              "Verdict agreement compares the candidate's decision to a previous model's "
              "decision, not to ground truth — the baseline itself missed things "
              "sometimes. A low agreement number and a low concern recall together point "
              "at the candidate under-flagging; a low agreement number with concern "
              "recall near 100% (on the records where it agreed) points at it "
              "over-flagging on records it disagreed on instead."]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--out", default="summary.md")
    parser.add_argument("--json", dest="json_out")
    parser.add_argument("--control-arm")
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
