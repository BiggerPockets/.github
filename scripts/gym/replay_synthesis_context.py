#!/usr/bin/env python3
"""Rebuild the exact context one recorded Stage-2 synthesis pass saw, at its pinned commit.

Sibling to replay_context.py, which does this for Stage 1. The difference is one file:
Stage 2's prompt also reads first-pass-findings.md, and here that file is NOT regenerated
by replaying Stage 1 — it is the exact text the recorded pass was handed, taken straight
from the dataset record. Regenerating it from a fresh Stage-1 replay would test two model
changes at once (Stage 1 finding something different, Stage 2 synthesizing differently)
and blame the sum on Stage 2. See export_synthesis_findings.py for why the record already
carries this text without needing to join against the pi.first_pass span.

pr.diff, ticket.json and conversations.json are rebuilt exactly as they are for Stage 1 —
same merge-base diff, same conversation truncated to the review's own timestamp, same
"no ticket key" fallback — because Stage 2 reads all three itself; only the first-pass
handoff differs from what a Stage 1 replay would produce.

Usage:
  scripts/gym/replay_synthesis_context.py --record <id> \
      --dataset gym/deepseek-synthesis-findings.yaml --workdir /path
Writes <workdir>/repo/{pr.diff,ticket.json,conversations.json,first-pass-findings.md}.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from replay_context import (  # noqa: E402
    build_conversations, build_diff, fetch_repo, fetch_ticket, load_record,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--record", required=True)
    parser.add_argument("--dataset", default="gym/deepseek-synthesis-findings.yaml")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--checkout", help="default: <workdir>/repo")
    parser.add_argument("--ticket-key")
    args = parser.parse_args(argv)

    record = load_record(args.dataset, args.record)
    source = record["input"]
    repo, pr = source["repo"], source["pr"]
    head_sha, base_sha = source.get("head_sha"), source.get("base_sha")
    if not head_sha or not base_sha:
        print(f"{args.record} is not pinned to a commit; cannot replay it faithfully",
              file=sys.stderr)
        return 3

    os.makedirs(args.workdir, exist_ok=True)
    checkout = args.checkout or os.path.join(args.workdir, "repo")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    try:
        fetch_repo(repo, head_sha, base_sha, checkout, token)
    except RuntimeError as error:
        print(f"could not fetch {repo}@{head_sha[:12]}: {error}", file=sys.stderr)
        return 4

    diff_lines = build_diff(checkout, base_sha, head_sha, os.path.join(checkout, "pr.diff"))
    ticket_available = fetch_ticket(args.ticket_key, os.path.join(checkout, "ticket.json"))
    counts = build_conversations(repo, pr, record["metadata"]["reviewed_at"],
                                 os.path.join(checkout, "conversations.json"), token)

    findings = source.get("first_pass_findings") or "(no first-pass findings recorded)\n"
    with open(os.path.join(checkout, "first-pass-findings.md"), "w") as stream:
        stream.write(findings)

    print(json.dumps({
        "record": args.record, "repo": repo, "pr": pr,
        "head_sha": head_sha, "checkout": checkout,
        "diff_lines": diff_lines, "ticket_available": ticket_available,
        "conversation_counts": counts,
        "conversation_cutoff": record["metadata"]["reviewed_at"],
        "expected_verdict": record["expected_output"]["verdict"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
