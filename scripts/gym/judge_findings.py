#!/usr/bin/env python3
"""Score one replayed review against the findings the recorded model wrote for that PR.

The question is per-finding recall: of the defects the baseline reported on this pull
request, which did the candidate also report? It is deliberately not "are these two reports
similar". Two reviews can describe the same race condition in entirely different words, at
different line numbers, under different headings, and that is a hit; two reviews can share
most of their vocabulary while flagging unrelated things, and that is a miss. String
overlap answers the wrong question, so an LLM judge reads both and matches finding by
finding.

The judge is told to be strict about one specific failure mode: crediting a vague candidate
remark as a match for a specific baseline finding. A review that says "consider reviewing
error handling here" has not caught "rescues RecordInvalid but the unique-index race raises
RecordNotUnique". Recall inflated by generous matching is worse than no measurement,
because it reads as reassurance.

What it emits per baseline finding: matched true/false, the candidate text that matched,
and a one-line reason. Aggregate recall is then a count, not a model's impression of a
number — models are poor at holding a ratio in their head and good at judging one pair at a
time, so the arithmetic is done here.

`severity` comes from the record, not the judge: it was derived from the baseline text when
the dataset was built, so weighting is stable across runs and across judges.

Findings the candidate reported that the baseline did not are counted but never penalised.
The baseline is a previous model, not ground truth — it missed things too, and a candidate
that finds more is not thereby worse. They are surfaced as `extra_findings` for a human to
look at, because a large number of them is interesting on its own.

The judge model is deliberately not either arm: asking a model to grade itself against a
competitor invites a thumb on the scale.

Usage:
  judge_findings.py --expected expected.md --actual findings.md --record <id> \
      [--severity blocking] [--model anthropic/claude-haiku-4.5]
Prints a JSON verdict. Exits 0 even when the candidate scores nothing — a zero is a
result, not a failure — and nonzero only when the judge itself could not be reached.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_JUDGE = "anthropic/claude-haiku-4.5"
# Severity weights for the headline number. A missed blocker is the failure this whole
# exercise exists to detect; a missed nitpick is noise.
WEIGHTS = {"blocker": 3.0, "blocking": 2.0, "non-blocking": 1.0}

SYSTEM = """You compare two code-review reports on the same pull request.

The BASELINE report lists findings a previous reviewer produced. The CANDIDATE report is a
different model reviewing the same diff. For each finding in the BASELINE, decide whether
the CANDIDATE reported the same underlying defect.

Rules:
- Match on the DEFECT, not the wording, heading, ordering, severity label or line number.
  The same bug described differently is a match.
- Do NOT match a specific baseline finding to a vague candidate remark. "Consider checking
  error handling" does not match "rescues RecordInvalid but the race raises RecordNotUnique".
  When the candidate is too general to show it found this specific defect, it is not a match.
- A candidate finding may match at most one baseline finding.
- List candidate findings that match no baseline finding separately. These are not errors.

Return ONLY JSON:
{"baseline_findings": [{"summary": "<= 12 words", "matched": true|false,
  "candidate_text": "<quote or null>", "reason": "<= 20 words"}],
 "extra_findings": [{"summary": "<= 12 words"}]}"""


def call_judge(model, expected, actual, api_key, timeout=180):
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content":
                f"BASELINE REPORT:\n\n{expected}\n\n---\n\nCANDIDATE REPORT:\n\n{actual}"},
        ],
    }).encode()
    request = urllib.request.Request(
        OPENROUTER_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    return payload["choices"][0]["message"]["content"]


def parse_verdict(text):
    """The judge's JSON, tolerating a fenced code block around it.

    A judge that returns unparseable output is a judging failure, not a candidate failure,
    so this raises rather than scoring zero — a silent zero would look like a regression."""
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.S)
    if fence:
        stripped = fence.group(1).strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(f"judge returned no JSON object: {text[:200]}")
    verdict = json.loads(stripped[start:end + 1])
    if not isinstance(verdict.get("baseline_findings"), list):
        raise ValueError("judge returned no baseline_findings list")
    return verdict


def score(verdict, severity):
    """Recall over the baseline's findings, plus the severity-weighted form.

    With one severity per record the weighted number only differs from the plain one once
    records are aggregated — which is the point, since that is where a run's headline
    figure comes from."""
    findings = verdict.get("baseline_findings") or []
    total = len(findings)
    matched = sum(1 for f in findings if f.get("matched"))
    weight = WEIGHTS.get(severity, 1.0)
    return {
        "baseline_count": total,
        "matched_count": matched,
        "missed_count": total - matched,
        "recall": (matched / total) if total else None,
        "weight": weight,
        "weighted_total": total * weight,
        "weighted_matched": matched * weight,
        "extra_count": len(verdict.get("extra_findings") or []),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--expected", required=True, help="baseline findings file")
    parser.add_argument("--actual", required=True, help="candidate findings file")
    parser.add_argument("--record", required=True)
    parser.add_argument("--arm", default="")
    parser.add_argument("--severity", default="blocking", choices=sorted(WEIGHTS))
    parser.add_argument("--model", default=os.environ.get("JUDGE_MODEL", DEFAULT_JUDGE))
    parser.add_argument("--out", help="write the verdict here as well as to stdout")
    args = parser.parse_args(argv)

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("OPENROUTER_API_KEY must be set", file=sys.stderr)
        return 2

    expected = open(args.expected).read().strip()
    actual = open(args.actual).read().strip()

    if not expected:
        print(f"{args.record}: baseline findings are empty; nothing to score",
              file=sys.stderr)
        return 3

    # An empty candidate report is a real outcome — the replay ran and the model found
    # nothing — so it scores zero rather than erroring. Distinguishing it from a crashed
    # replay is the caller's job, which is why the workflow only judges completed replays.
    if not actual:
        verdict = {"baseline_findings": [], "extra_findings": [],
                   "note": "candidate produced no findings"}
        result = {"record": args.record, "arm": args.arm, "severity": args.severity,
                  "judge_model": args.model, "verdict": verdict,
                  "score": {"baseline_count": 0, "matched_count": 0, "missed_count": 0,
                            "recall": 0.0, "weight": WEIGHTS[args.severity],
                            "weighted_total": 0.0, "weighted_matched": 0.0,
                            "extra_count": 0, "empty_candidate": True}}
    else:
        try:
            raw = call_judge(args.model, expected, actual, api_key)
            verdict = parse_verdict(raw)
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError) as error:
            print(f"judge failed for {args.record}: {error}", file=sys.stderr)
            return 1
        result = {"record": args.record, "arm": args.arm, "severity": args.severity,
                  "judge_model": args.model, "verdict": verdict,
                  "score": score(verdict, args.severity)}

    output = json.dumps(result, indent=2)
    if args.out:
        with open(args.out, "w") as stream:
            stream.write(output + "\n")
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
