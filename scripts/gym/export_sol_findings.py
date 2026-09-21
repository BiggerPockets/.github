#!/usr/bin/env python3
"""Export first-pass review findings from Datadog LLM Observability into a gym dataset.

Stage 1 writes its findings into the `codex.review` span, and the review workflow tags
that span with the model that produced it (`codex_model`), the repo and PR it reviewed,
and the verdict Stage 2 ultimately reached. That is enough to reconstruct, after the
fact, exactly what a given first-pass model caught — which is what this script does.

Why this exists: the first-pass model is set by the workflow's `codex_model` input
(`vars.CODEX_MODEL`, defaulting to openai/gpt-5.6-luna), so one organization variable
changes what reaches a human reviewer across every repo at once. Nothing in the review itself notices
a model that quietly stops reporting a class of defect, because a finding that is never
written leaves no trace. The findings a *previous* model wrote are the only record of
what was catchable on those diffs, so they become the regression set: run a candidate
first-pass model over the same pull requests and check it still reports them.

What it selects, and why that is narrower than "every finding":

- `@status:ok` and `codex_findings_lines > 0` — the pass actually finished and wrote
  findings. Errored passes carry no findings text and would be empty rows.
- `verdict:request_changes` by default — Stage 2 independently verified the diff and
  decided changes were required. That verdict is the closest thing to ground truth
  available without re-adjudicating every finding by hand, and it is the set whose loss
  would actually cost something. Pass `--verdict any` to export the rest as well; those
  rows are weaker evidence, because Stage 2 approving the PR often means it judged the
  first pass's findings not worth blocking on.
- One record per (repo, PR), keeping the most recent qualifying pass. A PR is reviewed
  again on every push, and the span is reported once per Stage-2 attempt, so the raw
  query returns the same review several times over.

Records whose findings text reports nothing (a pass that concluded "no blocking
findings" while Stage 2 requested changes on its own reasoning) are dropped: there is
no finding in them for a candidate model to miss.

Every record is pinned to the commit that was reviewed, and that is the whole reason
this file is usable later. A pull request is not a stable artifact: commits land on it
after a review, its base branch advances, and it eventually merges and closes. The
findings stored here cite exact `file:line` anchors, so replaying "PR 31230" against
whatever that PR looks like today would show a candidate model different code from the
one Sol read — and every drifted anchor would score as a missed finding that was never
there to miss. That failure is silent and it biases the result in the alarming
direction, which is the worst way for a regression check to be wrong.

The span itself carries no commit SHA, but its `run_id` tag embeds the GitHub Actions
run, and that run records the `head_sha` it checked out. So each record resolves to
`head_sha` (the exact reviewed commit) plus the `base_ref`/`base_sha` it was diffed
against; a harness reconstructs the review diff as
`git diff $(git merge-base <base_sha> <head_sha>) <head_sha>`. Both anchors are needed:
a sizeable minority of these pull requests are stacked on another branch rather than on
`main`, so assuming `main` silently produces the wrong diff for them. Pinning the commit
also restores the whole tree rather than just the diff, which matters because Stage 1
explores the repo and its findings routinely cite files the pull request never touched.

Two things this cannot pin. A commit can become unreachable if its branch was deleted
after a force-push, in which case the record cannot be replayed exactly — `head_sha` is
recorded either way so the harness can detect and skip it rather than review the wrong
code. And the JIRA ticket a finding argues against ("the ticket requires X") is live and
may since have been edited; nothing in the span or the run captures its state at review
time, so a finding that turns on acceptance criteria can go stale even with the diff
pinned correctly.

Retention bounds what can be recovered. LLM Obs holds spans for a limited window, so
this exports what is still queryable, not all history — re-run it periodically and
merge rather than expecting one run to be complete. `--since`/`--until` set the window.
Commit resolution needs an authenticated `gh` CLI; `--no-resolve-commits` skips it and
emits unpinned records, which are weaker and should not be the default.

PII: the findings are model-written prose about source code, and Datadog's sensitive
data scanner has already masked matches in the stored span. Email addresses are
scrubbed again here so the committed file does not depend on that scanner's config.

Usage:
  DD_API_KEY=... DD_APP_KEY=... scripts/gym/export_sol_findings.py \
      --model openai/gpt-5.6-sol --out gym/sol-first-pass-findings.yaml

Exits non-zero when credentials are missing or Datadog cannot be reached, so a
scheduled refresh fails loudly instead of committing a truncated dataset.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

import yaml

SPANS_SEARCH_URL = "https://api.{site}/api/v2/llm-obs/v1/spans/events/search"
PAGE_LIMIT = 100
# A single pull request can accumulate many reported passes; this bounds a runaway
# export rather than the useful result, which dedupes down to one row per PR.
MAX_SPANS = 5000

EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# A pass that wrote findings text but reported nothing actionable. Such a row would
# assert "the candidate model must also find nothing", which is not a regression test.
EMPTY_FINDINGS = re.compile(
    r"no blocking (findings|issues)|no findings|nothing blocking", re.IGNORECASE)


class Block(str):
    """A string that YAML should emit as a literal block, not a quoted one-liner."""


def _block_representer(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


yaml.add_representer(Block, _block_representer)


def redact(text):
    """Blank anything that reads as personal data. Findings quote code, not members,
    so this is a backstop rather than the primary control."""
    return EMAIL.sub("[redacted-email]", text)


def tag_map(tags):
    """Datadog returns tags as a flat "key:value" list. Values can themselves contain
    colons (`codex_model:openai/gpt-5.6-sol`), so split only on the first one, and keep
    the first occurrence when a key repeats."""
    out = {}
    for tag in tags or []:
        key, _, value = tag.partition(":")
        out.setdefault(key, value)
    return out


def search_spans(site, api_key, app_key, query, since, until):
    """Page through the LLM Obs spans search API, yielding raw span dicts.

    Raises on a transport or API error: a partial export that looks complete is worse
    than no export, because it silently shrinks the regression set."""
    url = SPANS_SEARCH_URL.format(site=site)
    cursor = None
    seen = 0
    while seen < MAX_SPANS:
        page = {"limit": PAGE_LIMIT}
        if cursor:
            page["cursor"] = cursor
        body = json.dumps({"data": {"type": "spans", "attributes": {
            "filter": {"from": since, "to": until, "query": query},
            "page": page,
        }}}).encode()
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "DD-API-KEY": api_key,
                "DD-APPLICATION-KEY": app_key,
                "Content-Type": "application/json",
            })
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)

        spans = payload.get("data") or []
        if not spans:
            return
        for span in spans:
            yield span
            seen += 1
        cursor = (((payload.get("meta") or {}).get("page") or {}).get("after"))
        if not cursor:
            return


def findings_text(span):
    """The first pass's findings as written. The span's output is either a plain value
    or a message list depending on how the harness reported it; both have carried real
    findings across the harness migrations this repo has been through."""
    attributes = span.get("attributes") or {}
    output = ((attributes.get("meta") or {}).get("output")) or {}
    value = output.get("value")
    if isinstance(value, str) and value.strip():
        return value.strip()
    messages = output.get("messages")
    if isinstance(messages, list):
        for message in reversed(messages):
            content = (message or {}).get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
    return ""


def run_number(run_id_tag):
    """The GitHub Actions run id out of the workflow's `run_id` tag, which is shaped
    `<owner>/<repo>-pr<N>-<run>`. Split from the right: owner and repo may themselves
    contain hyphens (`biggerpockets/pockets-app`)."""
    if not run_id_tag:
        return None
    candidate = run_id_tag.rsplit("-", 1)[-1]
    return candidate if candidate.isdigit() else None


def _gh(path, jq):
    """One `gh api` read. Returns None on any failure — a record that cannot be pinned
    is still worth exporting with a null SHA, because the harness can then skip it
    deliberately instead of silently reviewing the wrong commit."""
    try:
        result = subprocess.run(["gh", "api", path, "--jq", jq],
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def resolve_commit(repo, pr, run_id_tag, cache):
    """The commit the review actually read, plus what it was diffed against.

    `head_sha` comes from the Actions run, which is the exact checkout that produced
    these findings; `base_ref`/`base_sha` come from the pull request. The run is the
    authority for the head, not the PR: the PR's head has moved on every push since,
    which is precisely the drift being pinned against."""
    run = run_number(run_id_tag)
    if not run:
        return {}
    if (repo, run) in cache:
        return cache[(repo, run)]

    resolved = {"head_sha": _gh(f"/repos/{repo}/actions/runs/{run}", ".head_sha")}
    if resolved["head_sha"]:
        base = _gh(f"/repos/{repo}/pulls/{pr}",
                   '{ref: .base.ref, sha: .base.sha} | @json')
        if base:
            try:
                parsed = json.loads(base)
                resolved["base_ref"] = parsed.get("ref")
                resolved["base_sha"] = parsed.get("sha")
            except ValueError:
                pass
    cache[(repo, run)] = resolved
    return resolved


def severity(text):
    """How hard the pass pushed. Blocking findings are the ones whose loss matters most,
    so the gym can weight or filter on this without re-reading every record."""
    lowered = text.lower()
    if re.search(r"\[p0\]|\bblocker\b", lowered):
        return "blocker"
    if "blocking" in lowered:
        return "blocking"
    return "non-blocking"


def to_record(span, model, commit=None):
    """One dataset row, or None when the span carries no usable finding.

    `commit` is the resolved {head_sha, base_ref, base_sha} for the review. It is
    merged into `input` rather than `metadata` because it is part of what the candidate
    model must be pointed at, not commentary about the row."""
    attributes = span.get("attributes") or {}
    tags = tag_map(attributes.get("tags"))
    repo, pr = tags.get("repo"), tags.get("pr")
    if not repo or not pr:
        return None
    text = findings_text(span)
    if not text or EMPTY_FINDINGS.search(text):
        return None
    text = redact(text)
    commit = commit or {}

    start_ns = span.get("start_ns") or attributes.get("start_ns") or 0
    reviewed_at = datetime.datetime.fromtimestamp(
        start_ns / 1e9, datetime.UTC).isoformat(timespec="seconds") if start_ns else None

    level = severity(text)
    return {
        "id": f"{repo.split('/')[-1]}-pr{pr}",
        "_sort_key": (repo, int(pr) if pr.isdigit() else 0),
        "_start_ns": start_ns,
        "_run_id": tags.get("run_id"),
        "input": {
            "repo": repo,
            "pr": int(pr) if pr.isdigit() else pr,
            "head_sha": commit.get("head_sha"),
            "base_ref": commit.get("base_ref"),
            "base_sha": commit.get("base_sha"),
            "instruction": f"Review pr:{pr} against ticket.json/pr.diff",
        },
        "expected_output": {"findings": Block(text + "\n")},
        "metadata": {
            "severity": level,
            "stage2_verdict": tags.get("verdict"),
            "findings_lines": int(tags.get("codex_findings_lines") or 0),
            "stage2_arm": tags.get("arm"),
            "reviewed_at": reviewed_at,
            # Which first-pass prompt produced these findings. A replay run under a
            # different prompt version is comparing two things at once, so the planner
            # filters on this by default.
            "codex_prompt_version": tags.get("codex_prompt_version"),
            "trace_id": span.get("trace_id") or attributes.get("trace_id"),
            "span_id": span.get("span_id") or attributes.get("span_id"),
        },
        "tags": [
            f"repo:{repo}",
            f"pr:{pr}",
            f"source_model:{model}",
            "stage:first_pass",
            f"severity:{level}",
        ],
    }


def dedupe(records):
    """One row per (repo, PR), keeping the latest qualifying pass. Re-reviews of the
    same PR describe the same diff at different points; the newest is the one whose
    findings survived into the verdict being relied on."""
    best = {}
    for record in records:
        key = record["_sort_key"]
        if key not in best or record["_start_ns"] > best[key]["_start_ns"]:
            best[key] = record
    ordered = sorted(best.values(), key=lambda r: r["_sort_key"])
    for record in ordered:
        record.pop("_sort_key")
        record.pop("_start_ns")
    return ordered


def build_query(model, span_name, ml_app, verdict):
    query = (f"@ml_app:{ml_app} @name:{span_name} codex_model:{model} "
             f"-codex_findings_lines:0 @status:ok")
    if verdict != "any":
        query += f" verdict:{verdict}"
    return query


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="openai/gpt-5.6-sol",
                        help="first-pass model slug to export findings for")
    parser.add_argument("--ml-app", default="biggiepockets-review")
    parser.add_argument("--span-name", default="codex.review")
    parser.add_argument("--verdict", default="request_changes",
                        help="Stage-2 verdict to require, or 'any'")
    parser.add_argument("--since", default="now-30d")
    parser.add_argument("--until", default="now")
    parser.add_argument("--name", default="sol-first-pass-findings",
                        help="dataset name recorded in the file")
    parser.add_argument("--out", default="gym/sol-first-pass-findings.yaml")
    parser.add_argument("--no-resolve-commits", action="store_true",
                        help="skip the gh lookups that pin each record to the reviewed "
                             "commit; records are then replayable only approximately")
    args = parser.parse_args(argv)

    api_key = os.environ.get("DD_API_KEY")
    app_key = os.environ.get("DD_APP_KEY")
    if not api_key or not app_key:
        print("DD_API_KEY and DD_APP_KEY must be set", file=sys.stderr)
        return 2
    site = os.environ.get("DD_SITE", "datadoghq.com")

    query = build_query(args.model, args.span_name, args.ml_app, args.verdict)
    try:
        spans = list(search_spans(site, api_key, app_key, query, args.since, args.until))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as error:
        print(f"Datadog spans search failed: {error}", file=sys.stderr)
        return 1

    # Resolve commits only for the spans that survive dedupe: a PR reviewed twenty times
    # would otherwise cost twenty GitHub lookups to answer the same question once.
    staged = dedupe([r for r in (to_record(s, args.model) for s in spans) if r])
    if not staged:
        print("No qualifying spans found — check the window and the model slug.",
              file=sys.stderr)
        return 1

    records = []
    cache = {}
    for record in staged:
        run_id_tag = record.pop("_run_id", None)
        if not args.no_resolve_commits:
            commit = resolve_commit(record["input"]["repo"], record["input"]["pr"],
                                    run_id_tag, cache)
            record["input"].update({
                "head_sha": commit.get("head_sha"),
                "base_ref": commit.get("base_ref"),
                "base_sha": commit.get("base_sha"),
            })
        records.append(record)

    unpinned = [r["id"] for r in records if not r["input"].get("head_sha")]
    if unpinned:
        print(f"Warning: {len(unpinned)} record(s) could not be pinned to a commit and "
              f"cannot be replayed exactly: {', '.join(unpinned[:5])}"
              f"{' …' if len(unpinned) > 5 else ''}", file=sys.stderr)

    document = {
        "version": 1,
        "dataset": {
            "name": args.name,
            "description": (
                f"First-pass review findings produced by {args.model} that Stage 2 "
                f"verified into a {args.verdict} verdict. Used to check whether a "
                f"replacement first-pass model still catches them."),
        },
        "source": {
            "ml_app": args.ml_app,
            "span_name": args.span_name,
            "model": args.model,
            "query": query,
            "window": {"from": args.since, "to": args.until},
            "exported_at": datetime.datetime.now(datetime.UTC).date().isoformat(),
        },
        "records": records,
    }

    with open(args.out, "w") as stream:
        yaml.dump(document, stream, sort_keys=False, allow_unicode=True,
                  width=100, default_flow_style=False)
    print(f"Wrote {len(records)} records from {len(spans)} spans to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
