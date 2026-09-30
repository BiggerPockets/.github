#!/usr/bin/env python3
"""Write findings reports that predate the findings block into its structured form.

The first-pass prompt now ends every report with a fenced `findings` block (see
`finding_locations.py`). The recorded findings in the gym dataset, and the reviews saved
by earlier gym runs, are prose only. This asks a model, once per report (by default the gym judge), to restate each
prose finding in the block's form: severity, category, locations, summary. It is
extraction, not judgement: the model is told to take locations only from the finding's
own text, and any location whose path the finding does not mention is dropped.

The category definitions are read from `prompts/first-pass.md`, so the restated findings
use exactly the categories a live first pass is given.

- With `--dataset`, each record's findings are restated into
  `expected_output.structured_findings`, one entry per top-level finding in
  `expected_output.findings`, in order, so they pair with `metadata.confirmation.statuses`.
- With `--results`, each saved result whose review has no findings block gets
  `structured_findings`, one entry per top-level finding in the review.

A report is skipped when it already has its structured form, so an interrupted run can
be restarted. Needs OPENROUTER_API_KEY. Prints counts only: the findings quote private
source.

Usage:
  scripts/gym/structure_findings.py --dataset ../pi-gym-data/gym/sol-first-pass-findings.yaml \\
      --results ../pi-gym-data/results/sol-first-pass-findings
"""
import argparse
import datetime
import glob
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from confirm_findings import Block, citations, split_findings
from finding_locations import CATEGORIES, structured
from judge_findings import DEFAULT_JUDGE

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# The gym judge: this is the same reading of review prose, and a cheap model does it.
DEFAULT_MODEL = DEFAULT_JUDGE
PROMPT = Path(__file__).resolve().parents[2] / "prompts/first-pass.md"
SEVERITIES = {"blocker", "blocking", "non-blocking"}
# Findings link into the runner's checkout; a restated path is repository-relative.
RUNNER_PREFIX = re.compile(r"^/home/runner/work/[^/]+/[^/]+/")


def block_instructions():
    """The first-pass prompt's description of the findings block's fields."""
    text = PROMPT.read_text()
    return text[text.index("   - `severity`"):text.index("With no findings")].strip()


SYSTEM = """You restate code-review findings in a structured form. You do not judge them.

You are given numbered findings from one review. For each, in order, return one JSON
object with these fields:

{fields}

Rules:
- Return exactly one object per numbered finding, in the same order.
- Take locations only from the finding's own text. Never invent a path or a line number.
  A path in a link or backticks counts; so does a line number stated in prose.
- When the finding names no file, return an empty locations list.

Return ONLY a JSON array."""


def call_model(model, findings, api_key, timeout=180):
    numbered = "\n\n".join(f"FINDING {i}:\n{text}" for i, text in enumerate(findings, 1))
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM.format(fields=block_instructions())},
            {"role": "user", "content": numbered},
        ],
    }).encode()
    request = urllib.request.Request(
        OPENROUTER_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    return payload["choices"][0]["message"]["content"]


def parse(text, findings):
    """The model's array, checked against the prose it restated. Raises ValueError when it
    cannot be paired with the findings one to one."""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < 0:
        raise ValueError("no JSON array in the response")
    restated = json.loads(text[start:end + 1])
    if not isinstance(restated, list) or len(restated) != len(findings):
        raise ValueError(f"expected {len(findings)} findings, got "
                         f"{len(restated) if isinstance(restated, list) else 'no list'}")
    return [clean(entry, prose) for entry, prose in zip(restated, findings)]


def clean(entry, prose):
    """One restated finding with its fields normalised, and every location whose path the
    prose does not mention dropped."""
    entry = entry if isinstance(entry, dict) else {}
    cited = {path for path, _ in citations(prose)}
    kept = []
    for location in entry.get("locations") or []:
        if not isinstance(location, dict) or not isinstance(location.get("path"), str):
            continue
        path = RUNNER_PREFIX.sub("", location["path"].strip())
        path = path[2:] if path.startswith("./") else path
        if path in cited or path in prose:
            kept.append({k: location[k] for k in ("start_line", "end_line")
                         if isinstance(location.get(k), int)} | {"path": path})
    return {
        "severity": entry.get("severity") if entry.get("severity") in SEVERITIES else None,
        "category": entry.get("category") if entry.get("category") in CATEGORIES else None,
        "locations": kept,
        "summary": entry.get("summary") if isinstance(entry.get("summary"), str) else "",
    }


def restate(text, model, api_key):
    """The structured form of every top-level finding in `text`, or [] when it has none."""
    _, findings = split_findings(text or "")
    if not findings:
        return []
    return parse(call_model(model, findings, api_key), findings)


def save_dataset(path, document):
    for record in document.get("records") or []:
        for key in ("findings", "confirmed_findings"):
            text = record["expected_output"].get(key)
            if text:
                record["expected_output"][key] = Block(text)
    with open(path, "w") as stream:
        yaml.dump(document, stream, sort_keys=False, allow_unicode=True, width=100)


def structure_dataset(path, model, api_key, today):
    """Restate each record, saving after every one so an interrupted run keeps its work."""
    with open(path) as stream:
        document = yaml.safe_load(stream)
    tally = {"restated": 0, "skipped": 0, "failed": 0, "findings": 0, "unlocated": 0}
    for record in document.get("records") or []:
        expected = record["expected_output"]
        if "structured_findings" in expected:
            tally["skipped"] += 1
            continue
        try:
            restated = restate(expected.get("findings"), model, api_key)
        except (urllib.error.URLError, ValueError, KeyError) as error:
            print(f"{record['id']}: {error}", file=sys.stderr)
            tally["failed"] += 1
            continue
        expected["structured_findings"] = restated
        record.setdefault("metadata", {})["structuring"] = {
            "model": model, "labeled_on": today.isoformat()}
        save_dataset(path, document)
        tally["restated"] += 1
        tally["findings"] += len(restated)
        tally["unlocated"] += sum(1 for f in restated if not f["locations"])
    return tally


def structure_results(directory, model, api_key):
    tally = {"restated": 0, "skipped": 0, "failed": 0, "findings": 0, "unlocated": 0}
    for path in sorted(glob.glob(os.path.join(directory, "*", "*.json"))):
        with open(path) as stream:
            result = json.load(stream)
        review = result.get("findings")
        if not review or "structured_findings" in result or structured(review) is not None:
            tally["skipped"] += 1
            continue
        try:
            restated = restate(review, model, api_key)
        except (urllib.error.URLError, ValueError, KeyError) as error:
            print(f"{os.path.basename(path)}: {error}", file=sys.stderr)
            tally["failed"] += 1
            continue
        result["structured_findings"] = restated
        result["structuring_model"] = model
        with open(path, "w") as stream:
            stream.write(json.dumps(result, indent=2) + "\n")
        tally["restated"] += 1
        tally["findings"] += len(restated)
        tally["unlocated"] += sum(1 for f in restated if not f["locations"])
    return tally


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", help="the gym dataset YAML, restated in place")
    parser.add_argument("--results", help="a results directory, one subdirectory per run")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args(argv)
    if not (args.dataset or args.results):
        parser.error("give --dataset, --results or both")

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("OPENROUTER_API_KEY must be set", file=sys.stderr)
        return 2

    tallies = []
    if args.dataset:
        tallies.append(("dataset", structure_dataset(args.dataset, args.model, api_key,
                                                      datetime.date.today())))
    if args.results:
        tallies.append(("results", structure_results(args.results, args.model, api_key)))
    for name, t in tallies:
        print(f"{name}: restated {t['restated']} reports ({t['findings']} findings, "
              f"{t['unlocated']} with no location), skipped {t['skipped']}, "
              f"failed {t['failed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
