#!/usr/bin/env python3
"""Score one replayed Stage-2 decision against the recorded baseline decision.

Two independent questions, because a synthesis pass can fail either one without failing
the other:

1. **Verdict agreement** — did the candidate reach the same approve / request_changes
   decision as the baseline, given the identical Stage-1 findings? This is the number
   that matters most: it is the decision a human reviewer would otherwise have made, and
   it is computed by string comparison, not a judge, because there is nothing to
   interpret — the two verdicts either match or they don't.

2. **Concern coverage** — when the baseline requested changes, did the candidate's
   summary raise the same blocking concerns, or did it approve (or request changes for
   unrelated reasons)? Two summaries can share a verdict while one identifies the real
   defect and the other invents a different objection, which verdict agreement alone
   cannot see. An LLM judge reads both summaries and matches concern by concern, the same
   way judge_findings.py matches findings — deliberately not either arm, so scoring
   doesn't invite a model to grade itself against a competitor.

Concern coverage is only meaningful when the baseline blocked the PR: an approve baseline
has no blocking concern for the candidate to have missed, so those records report
verdict_agreement only and leave the judge uncalled.

Usage:
  judge_synthesis.py --expected-verdict request_changes --expected-summary baseline.md \
      --actual-verdict approve --actual-summary candidate.md --record <id> \
      [--model anthropic/claude-haiku-4.5]
Prints a JSON verdict. Exits 0 even when the candidate disagrees on everything — that is
a result, not a failure — and nonzero only when the judge itself could not be reached.
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

SYSTEM = """You compare two code-review decision summaries for the same pull request,
both written after the reviewer decided to REQUEST CHANGES.

The BASELINE summary is what a previous reviewer wrote. The CANDIDATE summary is a
different model's summary for the same request-changes decision. For each blocking
concern raised in the BASELINE, decide whether the CANDIDATE raises the same underlying
concern.

Rules:
- Match on the CONCERN, not the wording. The same objection stated differently is a match.
- A CANDIDATE concern too vague to show it identified this specific concern is not a
  match ("needs more testing" does not match "the retry loop has no backoff and will
  hammer the payment API").
- List candidate concerns that match no baseline concern separately; these are not errors.

Return ONLY JSON:
{"baseline_concerns": [{"summary": "<= 12 words", "matched": true|false,
  "candidate_text": "<quote or null>", "reason": "<= 20 words"}],
 "extra_concerns": [{"summary": "<= 12 words"}]}"""


def call_judge(model, expected, actual, api_key, timeout=180):
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content":
                f"BASELINE SUMMARY:\n\n{expected}\n\n---\n\nCANDIDATE SUMMARY:\n\n{actual}"},
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
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.S)
    if fence:
        stripped = fence.group(1).strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(f"judge returned no JSON object: {text[:200]}")
    verdict = json.loads(stripped[start:end + 1])
    if not isinstance(verdict.get("baseline_concerns"), list):
        raise ValueError("judge returned no baseline_concerns list")
    return verdict


def coverage_score(verdict):
    concerns = verdict.get("baseline_concerns") or []
    total = len(concerns)
    matched = sum(1 for c in concerns if c.get("matched"))
    return {
        "baseline_count": total,
        "matched_count": matched,
        "missed_count": total - matched,
        "recall": (matched / total) if total else None,
        "extra_count": len(verdict.get("extra_concerns") or []),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--expected-verdict", required=True, choices=["approve", "request_changes"])
    parser.add_argument("--actual-verdict", required=True)
    parser.add_argument("--expected-summary", required=True, help="baseline summary file")
    parser.add_argument("--actual-summary", required=True, help="candidate summary file")
    parser.add_argument("--record", required=True)
    parser.add_argument("--arm", default="")
    parser.add_argument("--model", default=os.environ.get("JUDGE_MODEL", DEFAULT_JUDGE))
    parser.add_argument("--out")
    args = parser.parse_args(argv)

    verdict_agreement = args.actual_verdict == args.expected_verdict
    result = {
        "record": args.record, "arm": args.arm, "judge_model": args.model,
        "expected_verdict": args.expected_verdict, "actual_verdict": args.actual_verdict,
        "verdict_agreement": verdict_agreement,
        "coverage": None,
    }

    # Coverage is only asked of records where the baseline blocked the PR — an approve
    # baseline names no blocking concern for a candidate to have missed, and a candidate
    # that also approved wrote a summary the judge has nothing to check it against.
    if args.expected_verdict == "request_changes":
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            print("OPENROUTER_API_KEY must be set", file=sys.stderr)
            return 2
        expected = open(args.expected_summary).read().strip()
        actual = open(args.actual_summary).read().strip() if verdict_agreement else ""

        if not expected:
            print(f"{args.record}: baseline summary is empty; skipping coverage",
                  file=sys.stderr)
        elif not verdict_agreement:
            # An approving candidate wrote no case against any blocking concern; scoring
            # its (nonexistent) coverage as zero would double-count the same failure the
            # verdict-agreement number already reports.
            result["coverage"] = {"skipped_reason": "candidate did not request changes"}
        else:
            try:
                raw = call_judge(args.model, expected, actual, api_key)
                judge_verdict = parse_verdict(raw)
            except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError) as error:
                print(f"judge failed for {args.record}: {error}", file=sys.stderr)
                return 1
            result["judge_verdict"] = judge_verdict
            result["coverage"] = coverage_score(judge_verdict)

    output = json.dumps(result, indent=2)
    if args.out:
        with open(args.out, "w") as stream:
            stream.write(output + "\n")
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
