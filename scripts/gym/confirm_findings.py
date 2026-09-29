#!/usr/bin/env python3
"""Mark which recorded findings the pull request's author went on to fix.

A record's findings are what one model wrote about one commit. Scored as they stand, a
replay answers "does the candidate say what that model said", which ties the dataset to
that model's blind spots and to the prompt it ran under. The question the gym exists to
answer is narrower and sturdier: does the candidate catch the defects that turned out to
be real? The pull request's own history answers that. A finding the author fixed after
the review was a real defect; the lines it cites changed between the reviewed commit and
the commit that finally merged.

For each record this reads the pull request's final head from GitHub, and for every
top-level finding compares the files it cites at the reviewed commit (`head_sha`) and at
that final head. Each finding gets one status:

- `fixed` — the pull request merged, and a later commit changed a cited line (or the
  lines within `SLOP` of it), or deleted the cited file. A citation with no line number
  counts when the file changed at all.
- `untouched` — the pull request merged and no cited line changed. This includes a pull
  request merged at the reviewed commit, which fixed nothing after the review.
- `unknown` — the finding cites no file this can read at the reviewed commit, or the pull
  request is open or was closed without merging. An abandoned pull request says nothing
  about which findings were right.

The fixed findings are written, in their original words, to
`expected_output.confirmed_findings`, which is what a gym replay is scored against.
`expected_output.findings` keeps the full recorded text. Counts and each finding's
status go under `metadata.confirmation`.

This is a proxy, and its errors run in known directions. A cited line can change for a
reason unrelated to the finding, which confirms a finding that was not real. A fix made
in a different file from the one cited is missed, and so is a real defect the author
chose not to fix; both drop a real finding from the set. The set is therefore smaller
than the truth, and what is in it is mostly right.

Needs an authenticated `gh` CLI with read access to the repositories in the dataset.
Prints counts only: the findings quote private source.

Usage:
  scripts/gym/confirm_findings.py --dataset ../pi-gym-data/gym/sol-first-pass-findings.yaml
"""
import argparse
import datetime
import difflib
import json
import re
import subprocess
import sys
import urllib.parse

import yaml

# How far from a cited line a later change still counts as touching it. Findings cite the
# line where a defect shows, and the fix often lands a few lines above or below it.
SLOP = 3

METHOD = "cited-lines-changed-before-merge"

# A top-level list item: the start of one finding.
FINDING_START = re.compile(r"^(?:[-*+]|\d+[.)])\s")
# A markdown link into a runner checkout: `(/home/runner/work/<repo>/<repo>/<path>:<line>)`.
# A path may itself hold parentheses, as in `src/app/(tabs)/index.tsx`, so the link ends
# at a `)` that is not followed by more of the path.
RUNNER_LINK = re.compile(
    r"\(/home/runner/work/[^/\s]+/[^/\s]+/(?P<path>[^\s:]+?)"
    r"(?::(?P<lines>\d+(?:[-–]\d+)?))?\)(?![\w/(])")
# A backticked `path:lines` citation, where lines may list ranges: `a/b.rb:15-19,32-36`.
BACKTICK = re.compile(
    r"`(?P<path>[\w.@\[\]()-]*[/.][\w.@\[\]()/-]*):(?P<lines>\d+(?:[-–]\d+)?(?:,\s*\d+(?:[-–]\d+)?)*)`")


class Block(str):
    """A string that YAML should emit as a literal block, not a quoted one-liner."""


yaml.add_representer(Block, lambda dumper, data: dumper.represent_scalar(
    "tag:yaml.org,2002:str", str(data), style="|"))


def split_findings(text):
    """(preamble, [finding text]) — each finding is a top-level list item with its
    indented continuation lines. Lines before the first item stay in the preamble;
    unindented prose after a finding (a closing verdict line) ends it."""
    preamble, findings, current = [], [], None
    for line in text.splitlines():
        if FINDING_START.match(line):
            current = [line]
            findings.append(current)
        elif current is not None and (line.startswith((" ", "\t")) or not line.strip()):
            current.append(line)
        else:
            current = None
            if not findings:
                preamble.append(line)
    return "\n".join(preamble).strip(), ["\n".join(f).strip() for f in findings]


def parse_lines(spec):
    """`15-19,32-36` -> [(15, 19), (32, 36)]; None for a citation with no line."""
    if not spec:
        return None
    ranges = []
    for part in spec.split(","):
        bounds = re.split(r"[-–]", part.strip())
        start = int(bounds[0])
        ranges.append((start, int(bounds[-1]) if len(bounds) > 1 else start))
    return ranges


def citations(finding):
    """Every (path, ranges) the finding cites, in order, without repeats."""
    found = []
    for pattern in (RUNNER_LINK, BACKTICK):
        for match in pattern.finditer(finding):
            item = (match.group("path"), parse_lines(match.group("lines")))
            if item not in found:
                found.append(item)
    return found


def changed_ranges(before, after):
    """Line ranges of `before` (1-based, inclusive) that differ in `after`. An insertion
    between two lines is the zero-width range after the earlier of them."""
    matcher = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines(),
                                      autojunk=False)
    return [(i1 + 1, max(i1 + 1, i2)) for tag, i1, i2, _, _ in matcher.get_opcodes()
            if tag != "equal"]


def touches(ranges, changes):
    """Whether any cited range, widened by SLOP, meets any changed range."""
    if ranges is None:
        return bool(changes)
    return any(start - SLOP <= changed_end and changed_start <= end + SLOP
               for start, end in ranges for changed_start, changed_end in changes)


def gh_api(path, raw=False):
    """stdout of one `gh api` read, or None when it fails (a 404 included)."""
    command = ["gh", "api", path]
    if raw:
        command += ["-H", "Accept: application/vnd.github.raw"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


class GitHub:
    """The two reads labelling needs. Tests pass a stand-in with the same methods."""

    def pull(self, repo, pr):
        """{'state', 'merged', 'head_sha'}, or None when the pull request can't be read."""
        body = gh_api(f"repos/{repo}/pulls/{pr}")
        if body is None:
            return None
        data = json.loads(body)
        return {"state": data["state"], "merged": bool(data.get("merged")),
                "head_sha": data["head"]["sha"]}

    def file(self, repo, path, sha):
        """The file's text at `sha`, or None when it does not exist there."""
        return gh_api(f"repos/{repo}/contents/{urllib.parse.quote(path)}?ref={sha}",
                      raw=True)


def outcome(pull):
    if pull is None:
        return "unreadable"
    if pull["merged"]:
        return "merged"
    return "open" if pull["state"] == "open" else "closed"


def label_finding(finding, repo, reviewed, final, github, cache):
    """`fixed`, `untouched` or `unknown` for one finding of a merged pull request."""
    readable = []
    for path, ranges in citations(finding):
        if (path, reviewed) not in cache:
            cache[(path, reviewed)] = github.file(repo, path, reviewed)
        if cache[(path, reviewed)] is not None:
            readable.append((path, ranges))
    if not readable:
        return "unknown"
    if final == reviewed:
        return "untouched"
    for path, ranges in readable:
        if (path, final) not in cache:
            cache[(path, final)] = github.file(repo, path, final)
        after = cache[(path, final)]
        if after is None:
            return "fixed"
        if touches(ranges, changed_ranges(cache[(path, reviewed)], after)):
            return "fixed"
    return "untouched"


def label_record(record, github, today=None):
    """The record with `confirmed_findings` and `metadata.confirmation` set."""
    source = record["input"]
    repo, reviewed = source["repo"], source.get("head_sha")
    preamble, findings = split_findings(record["expected_output"]["findings"])
    pull = github.pull(repo, source["pr"]) if reviewed else None
    result = outcome(pull) if reviewed else "unpinned"

    cache = {}
    if result == "merged":
        statuses = [label_finding(f, repo, reviewed, pull["head_sha"], github, cache)
                    for f in findings]
    else:
        statuses = ["unknown"] * len(findings)

    fixed = [f for f, status in zip(findings, statuses) if status == "fixed"]
    record = dict(record)
    record["expected_output"] = dict(record["expected_output"])
    record["expected_output"]["confirmed_findings"] = (
        Block("\n\n".join(([preamble] if preamble else []) + fixed) + "\n") if fixed else "")
    record["metadata"] = dict(record.get("metadata") or {})
    record["metadata"]["confirmation"] = {
        "method": METHOD,
        "labeled_on": (today or datetime.date.today()).isoformat(),
        "pull_request": result,
        "final_head_sha": pull["head_sha"] if pull else None,
        "fixed": statuses.count("fixed"),
        "untouched": statuses.count("untouched"),
        "unknown": statuses.count("unknown"),
        "statuses": statuses,
    }
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", required=True, help="the gym dataset YAML to label")
    parser.add_argument("--out", help="write here instead of over --dataset")
    args = parser.parse_args(argv)

    with open(args.dataset) as stream:
        document = yaml.safe_load(stream)
    records = document.get("records") or []
    github = GitHub()
    labeled = []
    for number, record in enumerate(records, 1):
        labeled.append(label_record(record, github))
        c = labeled[-1]["metadata"]["confirmation"]
        print(f"[{number}/{len(records)}] {record['id']}: {c['pull_request']}, "
              f"fixed {c['fixed']}, untouched {c['untouched']}, unknown {c['unknown']}",
              file=sys.stderr)
    document["records"] = labeled

    for record in labeled:
        for key in ("findings", "confirmed_findings"):
            text = record["expected_output"].get(key)
            if text:
                record["expected_output"][key] = Block(text)
    with open(args.out or args.dataset, "w") as stream:
        yaml.dump(document, stream, sort_keys=False, allow_unicode=True, width=100)

    totals = {s: sum(r["metadata"]["confirmation"][s] for r in labeled)
              for s in ("fixed", "untouched", "unknown")}
    usable = sum(1 for r in labeled if r["metadata"]["confirmation"]["fixed"])
    print(f"{len(labeled)} records, {usable} with a confirmed finding; findings: "
          f"{totals['fixed']} fixed, {totals['untouched']} untouched, "
          f"{totals['unknown']} unknown", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
