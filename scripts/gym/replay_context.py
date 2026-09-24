#!/usr/bin/env python3
"""Rebuild the exact context one recorded first-pass review saw, at its pinned commit.

Stage 1 does not read a pull request through the GitHub API. It reads a working directory:
the repository checked out at the reviewed commit, a `pr.diff`, a `ticket.json`, and a
`conversations.json`. This script reproduces those four things for one gym record, so a
candidate model is asked the same question the recorded model was asked. Anything that
differs here is measured as a difference in the model, which is how a harness quietly
produces a confident wrong answer.

Three details carry almost all of the fidelity risk:

**The diff must be merge-base, not two-dot.** Production builds it as
`git diff "origin/$BASE_REF...HEAD"` — three dots. Two dots would additionally show
everything that landed on the base branch since the fork, which on a busy repo is most of
the diff and none of the pull request. The record's `base_sha`/`head_sha` are used with the
same three-dot form.

**The conversation must be truncated to the review's own timestamp.** This is the one that
silently destroys the experiment rather than merely degrading it: BiggiePockets posts its
review back onto the pull request, so the discussion as it stands today usually *contains
the findings being tested for*. Replay with today's conversation and a candidate model can
read the answer off the page and appear to reproduce everything. Every comment, review
comment and review is therefore filtered to `created_at < reviewed_at`, the timestamp of
the span the record came from. Comments that arrived after the review are exactly the ones
the recorded model could not have seen.

**A missing ticket is a real state, not an error.** Roughly a quarter of these records have
no BIG key on the pull request, and production degrades to a diff-only review for those.
`ticket.json` is written with `available: false` in that case, which is what the prompt
expects, rather than failing the record.

The repository is fetched by SHA rather than by branch: most of these pull requests are
merged and their branches deleted, so `refs/pull/<n>/head` may no longer point at the
commit that was reviewed, and the branch name may not resolve at all.

Usage:
  scripts/gym/replay_context.py --record <id> --dataset gym/....yaml --workdir /path
Writes pr.diff, ticket.json and conversations.json into the checkout, where the review
reads them, and the record's recorded findings to <workdir>/expected.md for the judge.
Prints a JSON summary.
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

import yaml

JIRA_BASE = "https://biggerpockets.atlassian.net/rest/api/3/issue"
# The Acceptance Criteria field, as the review workflow reads it.
JIRA_FIELDS = "summary,description,customfield_11557"
GITHUB_API = "https://api.github.com"


def run(args, cwd=None, check=True):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])}… failed: {result.stderr.strip()[:300]}")
    return result.stdout


def fetch_repo(repo, head_sha, base_sha, checkout_dir, token):
    """Clone just enough history to diff, then check out the reviewed commit.

    Fetching the two SHAs directly (rather than a branch) is what makes this work for
    merged pull requests whose branches are gone. `--filter=blob:none` keeps the fetch
    cheap on large repositories while still allowing the merge-base walk."""
    url = f"https://x-access-token:{token}@github.com/{repo}.git" if token \
        else f"https://github.com/{repo}.git"
    if not os.path.isdir(os.path.join(checkout_dir, ".git")):
        os.makedirs(checkout_dir, exist_ok=True)
        run(["git", "init", "--quiet"], cwd=checkout_dir)
        run(["git", "remote", "add", "origin", url], cwd=checkout_dir)
    for sha in (head_sha, base_sha):
        if sha:
            run(["git", "fetch", "--quiet", "--filter=blob:none", "origin", sha],
                cwd=checkout_dir)
    run(["git", "checkout", "--quiet", "--force", head_sha], cwd=checkout_dir)


def build_diff(checkout_dir, base_sha, head_sha, destination):
    """The reviewed diff: three-dot, so it shows the pull request's own changes only."""
    diff = run(["git", "diff", f"{base_sha}...{head_sha}"], cwd=checkout_dir)
    with open(destination, "w") as stream:
        stream.write(diff)
    return diff.count("\n")


def fetch_ticket(key, destination):
    """`ticket.json` in the shape the Stage-1 prompt reads. Absent or unreachable tickets
    are written as `available: false`, which the prompt handles as a diff-only review."""
    email = os.environ.get("JIRA_EMAIL")
    token = os.environ.get("JIRA_API_TOKEN")
    payload = {"available": False, "reason": "no ticket key on the pull request"}
    if key and email and token:
        request = urllib.request.Request(
            f"{JIRA_BASE}/{key}?fields={JIRA_FIELDS}",
            headers={"Accept": "application/json"})
        credentials = f"{email}:{token}".encode()
        import base64
        request.add_header("Authorization", "Basic " + base64.b64encode(credentials).decode())
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                fields = (json.load(response) or {}).get("fields") or {}
            payload = {
                "available": True,
                "key": key,
                "summary": fields.get("summary"),
                "description": fields.get("description"),
                "acceptance_criteria": fields.get("customfield_11557"),
            }
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as error:
            payload = {"available": False, "reason": f"fetch failed for {key}: {error}"}
    elif key:
        payload = {"available": False, "reason": "JIRA credentials not configured"}
    with open(destination, "w") as stream:
        json.dump(payload, stream, indent=2)
    return payload["available"]


def gh_paginated(path, token):
    """Every page of a GitHub list endpoint. Returns [] on any failure: a replay without
    the conversation is a degraded review, which is recoverable, while a crashed replay
    loses the record."""
    items = []
    url = f"{GITHUB_API}{path}?per_page=100"
    while url:
        request = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}" if token else "",
        })
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                items.extend(json.load(response))
                link = response.headers.get("Link", "")
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError):
            return items
        url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
    return items


def before(item, cutoff):
    """True when this comment predates the review. Items with no timestamp are dropped:
    including one risks leaking a later comment, excluding one only omits context."""
    stamp = item.get("created_at") or item.get("submitted_at")
    return bool(stamp) and stamp < cutoff


def build_conversations(repo, pr, reviewed_at, destination, token):
    """The PR discussion as it stood when the review ran.

    `reviewed_at` is the cutoff and the whole point — see the module docstring. It is
    compared as an ISO 8601 string, which orders correctly because GitHub and the recorded
    span both emit UTC with the same layout."""
    cutoff = reviewed_at.replace("+00:00", "Z")
    comments = [
        {"author": (c.get("user") or {}).get("login"), "created_at": c.get("created_at"),
         "body": c.get("body")}
        for c in gh_paginated(f"/repos/{repo}/issues/{pr}/comments", token)
        if before(c, cutoff)]
    review_comments = [
        {"author": (c.get("user") or {}).get("login"), "path": c.get("path"),
         "line": c.get("line"), "created_at": c.get("created_at"), "body": c.get("body")}
        for c in gh_paginated(f"/repos/{repo}/pulls/{pr}/comments", token)
        if before(c, cutoff)]
    reviews = [
        {"author": (r.get("user") or {}).get("login"), "state": r.get("state"),
         "submitted_at": r.get("submitted_at"), "body": r.get("body")}
        for r in gh_paginated(f"/repos/{repo}/pulls/{pr}/reviews", token)
        if r.get("body") and before(r, cutoff)]
    payload = {"issue_comments": comments, "review_comments": review_comments,
               "reviews": reviews}
    with open(destination, "w") as stream:
        json.dump(payload, stream, indent=2)
    return {k: len(v) for k, v in payload.items()}


def load_record(dataset_path, record_id):
    with open(dataset_path) as stream:
        document = yaml.safe_load(stream)
    for record in document.get("records") or []:
        if record.get("id") == record_id:
            return record
    raise SystemExit(f"record {record_id} not found in {dataset_path}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--record", required=True)
    parser.add_argument("--dataset", default="gym/sol-first-pass-findings.yaml")
    parser.add_argument("--workdir", required=True,
                        help="where pr.diff/ticket.json/conversations.json are written")
    parser.add_argument("--checkout", help="where to clone the target repo "
                                           "(default: <workdir>/repo)")
    parser.add_argument("--ticket-key", help="BIG key; omit for a diff-only replay")
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

    diff_lines = build_diff(checkout, base_sha, head_sha,
                            os.path.join(checkout, "pr.diff"))
    ticket_available = fetch_ticket(args.ticket_key,
                                    os.path.join(checkout, "ticket.json"))
    counts = build_conversations(repo, pr, record["metadata"]["reviewed_at"],
                                 os.path.join(checkout, "conversations.json"), token)
    with open(os.path.join(args.workdir, "expected.md"), "w") as stream:
        stream.write(record["expected_output"]["findings"])

    print(json.dumps({
        "record": args.record, "repo": repo, "pr": pr,
        "head_sha": head_sha, "checkout": checkout,
        "diff_lines": diff_lines, "ticket_available": ticket_available,
        "conversation_counts": counts,
        "conversation_cutoff": record["metadata"]["reviewed_at"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
