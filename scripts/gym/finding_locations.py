#!/usr/bin/env python3
"""Match a replayed review's findings to the recorded ones by where they point.

The first-pass prompt ends every report with a fenced `findings` block of JSON: one entry
per finding, with its severity, category and every file and line range it lives at. That
makes a candidate's findings machine-comparable. A recorded finding counts as found by
location when some candidate finding names the same file at a line range that overlaps
one it cites, allowing `SLOP` lines either way. A citation or location with no line
counts as the whole file.

The recorded findings are prose, so their locations come from the `path:line` citations
`confirm_findings.py` reads. Location is weaker evidence than the judge's reading: two
different defects can sit on one line, and one defect can be cited at its call site by
one review and its definition by another. `judge_findings.py` records the location match
beside the judge's verdict so the two can be compared run by run.
"""
import json
import re

from confirm_findings import SLOP, citations, split_findings

# The categories the first-pass prompt offers, kept in step with it by the tests.
CATEGORIES = {
    "completeness", "privacy", "email", "data", "performance", "parsing", "navigation",
    "compatibility", "configuration", "routing", "tests", "correctness", "security",
    "intent", "maintainability",
}

BLOCK = re.compile(r"^[ \t]*```findings[ \t]*\n(.*?)^[ \t]*```", re.M | re.S)


def structured(text):
    """The report's findings block as a list of findings, or None when the report has no
    block that parses to a list. The last block wins, since the prompt puts it at the end."""
    blocks = BLOCK.findall(text or "")
    if not blocks:
        return None
    try:
        findings = json.loads(blocks[-1])
    except json.JSONDecodeError:
        return None
    return findings if isinstance(findings, list) else None


def locations(finding):
    """[(path, [(start, end)] or None)] for one structured finding, skipping malformed
    entries."""
    found = []
    for location in (finding.get("locations") if isinstance(finding, dict) else None) or []:
        if not isinstance(location, dict) or not isinstance(location.get("path"), str):
            continue
        start, end = location.get("start_line"), location.get("end_line")
        if not isinstance(start, int):
            found.append((location["path"], None))
            continue
        found.append((location["path"], [(start, end if isinstance(end, int) else start)]))
    return found


def overlaps(cited, pointed):
    """Whether a recorded finding's citations and a candidate finding's locations share a
    file at overlapping lines."""
    for path, ranges in cited:
        for other, spans in pointed:
            if path != other:
                continue
            if ranges is None or spans is None:
                return True
            if any(start - SLOP <= span_end and span_start <= end + SLOP
                   for start, end in ranges for span_start, span_end in spans):
                return True
    return False


def found(recorded, candidates, same_category=False):
    """Whether any structured candidate finding points at the structured `recorded` one,
    and, with `same_category`, gives it the same category."""
    cited = locations(recorded)
    return any(overlaps(cited, locations(c))
               and (not same_category or c.get("category") == recorded.get("category"))
               for c in candidates if isinstance(c, dict))


def location_matches(expected, actual):
    """One bool per recorded finding in `expected`, in order: whether some candidate
    finding in `actual` points at it. None when `actual` has no findings block."""
    candidate = structured(actual)
    if candidate is None:
        return None
    pointed = [locations(f) for f in candidate]
    _, recorded = split_findings(expected)
    return [any(overlaps(citations(finding), p) for p in pointed) for finding in recorded]


def location_score(expected, actual, verdict=None):
    """Recall by location, and how often it agrees with the judge's per-finding verdict.
    `structured` is False when the candidate wrote no findings block."""
    matched = location_matches(expected, actual)
    if matched is None:
        return {"structured": False}
    judged = [bool(f.get("matched")) for f in (verdict or {}).get("baseline_findings") or []]
    return {
        "structured": True,
        "matched": matched,
        "baseline_count": len(matched),
        "matched_count": sum(matched),
        "recall": sum(matched) / len(matched) if matched else None,
        # None when the judge split the recorded findings differently from
        # split_findings, since the verdicts then cannot be paired one to one.
        "agrees_with_judge": (sum(a == b for a, b in zip(matched, judged))
                              if verdict is not None and len(judged) == len(matched) else None),
    }
