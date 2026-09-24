#!/usr/bin/env python3
"""Record a gym run as a Datadog LLM Observability experiment.

One gym run is one experiment: one model replayed against the gym dataset that
`upload_gym_dataset.py` keeps in Datadog. Each replayed record becomes one experiment
span, carrying the model's review as its output and the recorded findings as its
expected output, with the judge's scores attached as evaluation metrics. That is where
per-record results live. They quote the private code under review, so they go to
Datadog rather than to this public repository's artifacts or logs.

Three subcommands, one per point in the workflow:

  create  — before any replay. Resolves the project and dataset by name, confirms the
            dataset is in that project and holds every record the run will replay,
            creates the experiment, prints its id.
  record  — once per replay. Posts the span and its metrics. A failed replay is posted
            too, as an errored span with no recall metrics, so the experiment shows
            which records did not run instead of silently having fewer rows.
  finish  — after every replay. Marks the experiment completed, or failed.

Spans are posted directly to the experiment's events route rather than traced through
the SDK. That is the same route dd-trace-py uses to post replayed spans with their
metrics in one call, and it needs nothing beyond the API and application keys.

Each span is tagged `dataset_record_id:<id>`. Datadog stores dataset records under the
id they have in the YAML, so a replay's record id is already the Datadog record id.

Usage:
  DD_API_KEY=... DD_APP_KEY=... datadog_experiment.py create \\
      --project biggiepockets-review-gym --dataset sol-first-pass-findings \\
      --model openai/gpt-5.6-luna --judge-model anthropic/claude-haiku-4.5
  datadog_experiment.py record --experiment-id <id> --record <record-id> ...
  datadog_experiment.py finish --experiment-id <id> --status completed
"""
import argparse
import json
import os
import secrets
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from upload_gym_dataset import (  # noqa: E402
    UNSTABLE, V2, DatadogError, find_project, request_json)

# The logical experiment name every gym run shares. Datadog appends a unique suffix per
# run (`ensure_unique`), so runs of this pipeline can be listed together over time.
EXPERIMENT_NAME = "first-pass-recall"
SCORE_METRICS = ("recall", "weighted_recall", "matched_count", "missed_count",
                 "baseline_count", "extra_count")


def credentials():
    api_key = os.environ.get("DD_API_KEY")
    app_key = os.environ.get("DD_APP_KEY")
    if not api_key or not app_key:
        raise DatadogError("DD_API_KEY and DD_APP_KEY must be set")
    return os.environ.get("DD_SITE", "datadoghq.com"), api_key, app_key


def find_dataset(site, api_key, app_key, project_id, name):
    """(id, current_version) of the dataset called `name` inside the project.

    Read from the project-scoped listing: the org-wide one ignores a project filter,
    and a dataset of the same name also exists outside this project."""
    path = f"{V2}/{project_id}/datasets?filter%5Bname%5D={urllib.parse.quote(name)}"
    payload = request_json(site, api_key, app_key, "GET", path)
    for item in payload.get("data") or []:
        attributes = item.get("attributes") or {}
        if attributes.get("name") == name and item.get("id"):
            return item["id"], int(attributes.get("current_version") or 0)
    raise DatadogError(f"no dataset named {name} in project {project_id}")


def dataset_record_ids(site, api_key, app_key, project_id, dataset_id):
    """Every record id in the dataset's current version, following the listing's cursor."""
    base = f"{V2}/{project_id}/datasets/{dataset_id}/records"
    ids, cursor = set(), None
    while True:
        path = base + (f"?page%5Bcursor%5D={urllib.parse.quote(cursor)}" if cursor else "")
        payload = request_json(site, api_key, app_key, "GET", path)
        ids.update(item["id"] for item in payload.get("data") or [] if item.get("id"))
        cursor = (payload.get("meta") or {}).get("after")
        if not cursor:
            return ids


def create_body(dataset_id, project_id, dataset_version, model, judge_model,
                prompt_version, run_url):
    tags = [f"model:{model}", f"judge_model:{judge_model}"]
    if prompt_version:
        tags.append(f"prompt_version:{prompt_version}")
    return {"data": {"type": "experiments", "attributes": {
        "name": EXPERIMENT_NAME,
        "description": f"First-pass recall of {model} against the recorded findings.",
        "dataset_id": dataset_id,
        "project_id": project_id,
        "dataset_version": dataset_version,
        "config": {"model": model, "judge_model": judge_model,
                   "prompt_version": prompt_version or "", "run_url": run_url or ""},
        "metadata": {"tags": tags},
        "ensure_unique": True,
        "run_count": 1,
    }}}


def experiment_tags(experiment_id, project_id, dataset_id, model):
    return [f"experiment_id:{experiment_id}", f"project_id:{project_id}",
            f"dataset_id:{dataset_id}", f"model:{model}"]


def metric(label, value, span_id, trace_id, experiment_id, timestamp_ms):
    """One evaluation metric in the shape the events route expects: the value sits under
    `<type>_value`, so a boolean and a score are different keys."""
    metric_type = "boolean" if isinstance(value, bool) else "score"
    return {"metric_source": "custom", "span_id": span_id, "trace_id": trace_id,
            "timestamp_ms": timestamp_ms, "metric_type": metric_type, "label": label,
            f"{metric_type}_value": value, "error": None, "tags": [],
            "experiment_id": experiment_id}


def record_body(experiment_id, tags, record, findings, expected, verdict, attribution,
                started_ns, ended_ns, failure=None):
    """The span and metrics for one replay.

    `verdict` is the judge's output file, or None when the replay failed. A failed replay
    gets an errored span and only the `timed_out` metric; its recall is not zero, it is
    unmeasured, and posting a zero would read as the model missing everything."""
    span_id = str(secrets.randbits(63) or 1)
    trace_id = secrets.token_hex(16)
    ended_ns = ended_ns or time.time_ns()
    started_ns = started_ns or ended_ns - 1
    timestamp_ms = ended_ns // 1_000_000
    attribution = attribution or {}

    metrics = [metric("timed_out", bool(attribution.get("timed_out")),
                      span_id, trace_id, experiment_id, timestamp_ms)]
    metadata = {"attribution": attribution}
    if verdict is not None:
        score = verdict.get("score") or {}
        for label in SCORE_METRICS:
            if score.get(label) is not None:
                metrics.append(metric(label, score[label], span_id, trace_id,
                                      experiment_id, timestamp_ms))
        metrics.append(metric("empty_output", bool(score.get("empty_candidate")),
                              span_id, trace_id, experiment_id, timestamp_ms))
        metadata.update(severity=verdict.get("severity"),
                        judge_model=verdict.get("judge_model"),
                        judge_verdict=verdict.get("verdict"))
    if failure:
        metadata["failure"] = failure

    span = {
        "span_id": span_id,
        "trace_id": trace_id,
        "name": "first_pass_replay",
        "start_ns": started_ns,
        # The backend rejects a zero duration.
        "duration": max(ended_ns - started_ns, 1),
        "meta": {
            "span.kind": "experiment",
            "input": {"record": record},
            "output": findings,
            "expected_output": expected,
            "metadata": metadata,
        },
        "status": "error" if failure else "ok",
        "tags": tags + [f"dataset_record_id:{record}"],
    }
    return {"data": {"type": "experiments", "attributes": {
        "scope": "experiments", "metrics": metrics, "tags": tags, "spans": [span]}}}


def read_json(path):
    if not path or not os.path.exists(path):
        return None
    with open(path) as stream:
        return json.load(stream)


def read_text(path):
    if not path or not os.path.exists(path):
        return ""
    with open(path) as stream:
        return stream.read()


def read_int(path):
    try:
        return int(read_text(path).strip())
    except ValueError:
        return None


def cmd_create(args):
    site, api_key, app_key = credentials()
    project_id = find_project(site, api_key, app_key, args.project)
    if not project_id:
        raise DatadogError(f"no LLM Obs project named {args.project}")
    dataset_id, version = find_dataset(site, api_key, app_key, project_id, args.dataset)
    # The replays read the dataset from pi-gym-data; the experiment points at the Datadog
    # copy. A record missing from the copy would post a span linked to nothing, so the
    # run stops here and names what to re-upload.
    if args.records:
        with open(args.records) as stream:
            planned = {job["record"] for job in json.load(stream)["include"]}
        missing = planned - dataset_record_ids(site, api_key, app_key, project_id, dataset_id)
        if missing:
            raise DatadogError(
                f"{len(missing)} planned record(s) are not in Datadog dataset "
                f"{args.dataset}: {', '.join(sorted(missing)[:10])}. Re-upload the dataset "
                f"with upload_gym_dataset.py so it matches pi-gym-data.")
    body = create_body(dataset_id, project_id, version, args.model, args.judge_model,
                       args.prompt_version, args.run_url)
    payload = request_json(site, api_key, app_key, "POST", f"{UNSTABLE}/experiments", body)
    experiment_id = (payload.get("data") or {}).get("id")
    if not experiment_id:
        raise DatadogError(f"experiment create returned no id: {json.dumps(payload)[:300]}")
    request_json(site, api_key, app_key, "PATCH", f"{UNSTABLE}/experiments/{experiment_id}",
                 {"data": {"type": "experiments", "attributes": {"status": "running"}}})
    print(json.dumps({"experiment_id": experiment_id, "project_id": project_id,
                      "dataset_id": dataset_id, "dataset_version": version}))


def cmd_record(args):
    site, api_key, app_key = credentials()
    tags = experiment_tags(args.experiment_id, args.project_id, args.dataset_id, args.model)
    body = record_body(
        args.experiment_id, tags, args.record,
        findings=read_text(args.findings), expected=read_text(args.expected),
        verdict=read_json(args.verdict), attribution=read_json(args.attribution),
        started_ns=read_int(args.started_ns), ended_ns=read_int(args.ended_ns),
        failure=args.failure)
    request_json(site, api_key, app_key, "POST",
                 f"{UNSTABLE}/experiments/{args.experiment_id}/events", body)
    print(f"{args.record}: posted to experiment {args.experiment_id}")


def cmd_finish(args):
    site, api_key, app_key = credentials()
    attributes = {"status": args.status}
    if args.error:
        attributes["error"] = args.error
    request_json(site, api_key, app_key, "PATCH",
                 f"{UNSTABLE}/experiments/{args.experiment_id}",
                 {"data": {"type": "experiments", "attributes": attributes}})
    print(f"experiment {args.experiment_id}: {args.status}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create")
    create.add_argument("--project", required=True)
    create.add_argument("--dataset", required=True)
    create.add_argument("--model", required=True)
    create.add_argument("--judge-model", required=True)
    create.add_argument("--prompt-version")
    create.add_argument("--run-url")
    create.add_argument("--records", help="the planned matrix JSON; every record in it "
                                          "must exist in the Datadog dataset")
    create.set_defaults(func=cmd_create)

    record = sub.add_parser("record")
    record.add_argument("--experiment-id", required=True)
    record.add_argument("--project-id", required=True)
    record.add_argument("--dataset-id", required=True)
    record.add_argument("--model", required=True)
    record.add_argument("--record", required=True)
    record.add_argument("--findings", help="the model's review")
    record.add_argument("--expected", help="the recorded findings")
    record.add_argument("--verdict", help="the judge's verdict JSON; omit for a failed replay")
    record.add_argument("--attribution", help="the endpoint-attribution JSON")
    record.add_argument("--started-ns", help="file holding the replay's start time in ns")
    record.add_argument("--ended-ns", help="file holding the replay's end time in ns")
    record.add_argument("--failure", help="why the replay failed; marks the span errored")
    record.set_defaults(func=cmd_record)

    finish = sub.add_parser("finish")
    finish.add_argument("--experiment-id", required=True)
    finish.add_argument("--status", required=True, choices=["completed", "failed"])
    finish.add_argument("--error")
    finish.set_defaults(func=cmd_finish)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except DatadogError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
