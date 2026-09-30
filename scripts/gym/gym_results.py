#!/usr/bin/env python3
"""Save one replay's result as a JSON file in BiggerPockets/pi-gym-data.

The private repo is the durable, per-record copy of every gym run. Each replay writes
one file:

  results/<dataset name>/<run id>-<attempt>-<model label>/<record id>.json

holding everything needed to analyse the run without re-running it: the record's pull
request and commits, the size of the reviewed diff, the model, the prompt, the judge's
scores and verdict, the model's review, the recorded findings, and why the replay failed
when it did. The review, the findings and the verdict quote the private code under review, which
is why this goes to the private repo and never to this public repository's logs or
artifacts.

One file per replay, written through the GitHub contents API, lets every matrix job save
its own result without a checkout and without two jobs ever editing the same file. A
re-run of the workflow gets a new attempt number, so its files sit beside the first
attempt's rather than colliding with them.

The diff size is counted from the `pr.diff` that `replay_context.py` built for the
replay, which is the diff the model actually reviewed.

The prompt is recorded as three values rather than as text. `replay_prompt_version` is
the content-derived version `resolve-prompts.sh` gave the prompt the replay ran with, and
`registry_sha` is the commit of BiggerPockets/.github it ran from, which recovers the exact
text from git. `recorded_prompt_version` is the version that produced the recorded
findings. The replay is scored against the findings the author went on to fix, so a
difference between the two versions does not change what is being sought.

Usage:
  gym_results.py --dataset gym-data/gym/x.yaml --record repo-pr1 --model openai/gpt-5.6-luna \\
      --judge-model anthropic/claude-haiku-4.5 --replay-dir replay --run-id 123 \\
      --run-attempt 1 --run-url https://... --prompt-name first-pass \\
      --prompt-version 3f4a8e3d3003 --registry-sha <sha> \\
      [--attribution out/attribution.json] [--repo BiggerPockets/pi-gym-data]
Needs GH_TOKEN with write access to Contents on the results repository.
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

import yaml

from datadog_experiment import TIMEOUT_EXIT, failure_reason, load_replay
from plan_matrix import model_label

RESULTS_REPO = "BiggerPockets/pi-gym-data"
# A contents write moves the branch head, so two jobs committing at the same moment can
# lose the race with a 409. A retry re-reads the head and succeeds.
ATTEMPTS = 5


class ResultsError(RuntimeError):
    pass


def diff_stats(path):
    """{files, additions, deletions} for a unified diff, or None when there is no diff
    (the replay failed before its context was built)."""
    if not os.path.exists(path):
        return None
    files = additions = deletions = 0
    with open(path, errors="replace") as stream:
        for line in stream:
            if line.startswith("diff --git "):
                files += 1
            elif line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1
    return {"files": files, "additions": additions, "deletions": deletions}


def load_dataset(path):
    with open(path) as stream:
        document = yaml.safe_load(stream) or {}
    name = (document.get("dataset") or {}).get("name")
    if not name:
        raise ResultsError(f"{path} has no dataset.name")
    return name, {r.get("id"): r for r in document.get("records") or []}


def result_row(dataset_name, record, model, judge_model, run, prompt, replay, diff):
    """The saved result for one replay. `record` is the dataset record it replayed, and
    `prompt` is {name, version, registry_sha} for the prompt the replay ran with."""
    source = record.get("input") or {}
    metadata = record.get("metadata") or {}
    verdict = replay["verdict"]
    score = (verdict or {}).get("score") or {}
    pi_exit = replay["run"].get("pi_exit")
    started, ended = replay["run"].get("started_ns"), replay["run"].get("ended_ns")
    return {
        "dataset": dataset_name,
        "record": record.get("id"),
        "repo": source.get("repo"),
        "pr": source.get("pr"),
        "head_sha": source.get("head_sha"),
        "base_sha": source.get("base_sha"),
        "severity": metadata.get("severity"),
        "model": model,
        "judge_model": judge_model,
        "run": run,
        "replay_prompt_name": prompt.get("name"),
        "replay_prompt_version": prompt.get("version"),
        "registry_sha": prompt.get("registry_sha"),
        "recorded_prompt_version": metadata.get("codex_prompt_version"),
        "diff": diff,
        "status": "error" if failure_reason(replay) else "ok",
        "failure": failure_reason(replay),
        "pi_exit": pi_exit,
        "timed_out": None if pi_exit is None else pi_exit == TIMEOUT_EXIT,
        "duration_s": (ended - started) / 1e9 if started and ended else None,
        "score": score or None,
        "judge_verdict": (verdict or {}).get("verdict"),
        "findings": replay["findings"],
        "expected": replay["expected"],
        "attribution": replay["attribution"],
    }


def result_path(dataset_name, run, model, record_id):
    folder = f"{run['id']}-{run['attempt']}-{model_label(model)}"
    return f"results/{dataset_name}/{folder}/{record_id}.json"


def put_file(repo, path, content, message, token):
    """Create `path` in `repo`'s default branch holding `content`."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    body = json.dumps({"message": message,
                       "content": base64.b64encode(content.encode()).decode()}).encode()
    for attempt in range(1, ATTEMPTS + 1):
        request = urllib.request.Request(url, data=body, method="PUT", headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            with urllib.request.urlopen(request, timeout=60):
                return
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:300]
            if error.code != 409 or attempt == ATTEMPTS:
                raise ResultsError(f"PUT {repo}/{path} -> {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            if attempt == ATTEMPTS:
                raise ResultsError(f"PUT {repo}/{path} failed: {error}") from error
        time.sleep(attempt)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", required=True, help="the gym dataset YAML")
    parser.add_argument("--record", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--judge-model", required=True)
    parser.add_argument("--replay-dir", required=True)
    parser.add_argument("--attribution", help="the endpoint-attribution JSON")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--run-url")
    parser.add_argument("--prompt-name", help="the replay's resolved prompt name")
    parser.add_argument("--prompt-version", help="the replay's resolved prompt version")
    parser.add_argument("--registry-sha", help="the BiggerPockets/.github commit replayed from")
    parser.add_argument("--repo", default=RESULTS_REPO)
    args = parser.parse_args(argv)

    token = os.environ.get("GH_TOKEN")
    if not token:
        print("GH_TOKEN must be set", file=sys.stderr)
        return 1
    try:
        dataset_name, records = load_dataset(args.dataset)
        if args.record not in records:
            raise ResultsError(f"record {args.record} not found in {args.dataset}")
        run = {"id": args.run_id, "attempt": args.run_attempt, "url": args.run_url}
        # An unresolved prompt reaches here as an empty string; store it as unknown.
        prompt = {"name": args.prompt_name or None, "version": args.prompt_version or None,
                  "registry_sha": args.registry_sha or None}
        replay = load_replay(args.replay_dir, args.attribution)
        diff = diff_stats(os.path.join(args.replay_dir, "repo", "pr.diff"))
        row = result_row(dataset_name, records[args.record], args.model, args.judge_model,
                         run, prompt, replay, diff)
        path = result_path(dataset_name, run, args.model, args.record)
        put_file(args.repo, path, json.dumps(row, indent=2) + "\n",
                 f"Gym result: {args.record} ({args.model}, run {args.run_id})", token)
    except ResultsError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"{args.record}: saved to {args.repo}/{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
