#!/usr/bin/env python3
"""Record a gym run as a Datadog LLM Observability experiment.

One gym run is one experiment: one model replayed against the Datadog copy of the gym
dataset, which `upload_gym_dataset.py` keeps in sync with the YAML file. Each replayed
record becomes one experiment span. The span carries the model's review as its output and
the recorded findings as its expected output, with the judge's scores attached as
evaluation metrics. That is where per-record results live. They quote the private code
under review, so they go to Datadog rather than to this public repository's artifacts or
logs.

Three subcommands, one per point in the workflow:

  create  — before any replay. Resolves the project by name and the dataset by the name
            the YAML file gives it, checks that the dataset holds every record the run
            will replay, creates the experiment, prints its ids as JSON.
  record  — once per replay. Posts the span and its metrics. A failed replay is posted
            too, as an errored span with no recall metrics, so the experiment shows which
            records did not run instead of silently holding fewer rows.
  finish  — after every replay. Marks the experiment completed, or failed.

Spans are posted straight to the experiment's events route, the same route dd-trace-py
uses to post replayed spans together with their metrics. It needs nothing beyond the API
and application keys.

Each span is tagged `dataset_record_id:<id>`. Datadog stores dataset records under the id
they have in the YAML, so a replay's record id is already its Datadog record id.

`record` reads one replay directory, written by the replay job's steps:

  run.json      {"started_ns", "ended_ns", "pi_exit"} from the replay step
  findings.md   the model's review
  expected.md   the recorded findings for the record
  verdict.json  the judge's verdict; absent when the replay or the judge failed
  failure.txt   why the replay failed; absent when it did not
"""
import argparse
import json
import os
import secrets
import sys
import time
import urllib.parse

import yaml

from datadog_api import UNSTABLE, V2, DatadogError, credentials, find_project, request_json

# The name every gym run's experiment shares. Datadog appends a unique suffix per run
# (`ensure_unique`), so the runs of this pipeline can be listed together over time.
EXPERIMENT_NAME = "first-pass-recall"
SCORE_METRICS = ("recall", "weighted_recall", "matched_count", "missed_count",
                 "baseline_count", "extra_count")
# `timeout` exits 124 when it kills the command it runs.
TIMEOUT_EXIT = 124


def dataset_name(dataset_file):
    with open(dataset_file) as stream:
        name = ((yaml.safe_load(stream) or {}).get("dataset") or {}).get("name")
    if not name:
        raise DatadogError(f"{dataset_file} has no dataset.name")
    return name


def find_dataset(site, api_key, app_key, project_id, name):
    """(id, current_version) of the dataset called `name` inside the project.

    Read from the project-scoped listing: the org-wide one ignores a project filter, and
    a dataset with the same name also exists outside this project."""
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
                prompt_version, run_url, extra_tags=()):
    tags = [f"model:{model}", f"judge_model:{judge_model}"]
    if prompt_version:
        tags.append(f"prompt_version:{prompt_version}")
    tags.extend(extra_tags)
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


def status_body(status, error=None):
    attributes = {"status": status}
    if error:
        attributes["error"] = error
    return {"data": {"type": "experiments", "attributes": attributes}}


def load_replay(replay_dir, attribution_file):
    """Everything one replay left behind, as a dict. Missing files read as empty: which
    ones are missing is exactly what says how far the replay got."""
    def text(path):
        if not path or not os.path.exists(path):
            return ""
        with open(path) as stream:
            return stream.read()

    def data(path):
        raw = text(path)
        return json.loads(raw) if raw.strip() else None

    def in_replay(name):
        return os.path.join(replay_dir, name)

    return {
        "run": data(in_replay("run.json")) or {},
        "findings": text(in_replay("findings.md")),
        "expected": text(in_replay("expected.md")),
        "verdict": data(in_replay("verdict.json")),
        "failure": text(in_replay("failure.txt")).strip(),
        "attribution": data(attribution_file) or {},
    }


def failure_reason(replay):
    """Why this replay has no score, or None when it has one."""
    if replay["verdict"] is not None:
        return None
    if replay["failure"]:
        return replay["failure"]
    if replay["findings"].strip():
        return "the judge did not produce a verdict"
    return "the replay did not complete"


def metric(label, value, span_id, trace_id, experiment_id, timestamp_ms):
    """One evaluation metric. The value sits under `<type>_value`, so a boolean and a
    score are sent under different keys."""
    metric_type = "boolean" if isinstance(value, bool) else "score"
    return {"metric_source": "custom", "span_id": span_id, "trace_id": trace_id,
            "timestamp_ms": timestamp_ms, "metric_type": metric_type, "label": label,
            f"{metric_type}_value": value, "error": None, "tags": [],
            "experiment_id": experiment_id}


def record_body(experiment_id, tags, record, replay):
    """The span and metrics for one replay.

    A replay with no verdict gets an errored span and at most the `timed_out` metric. Its
    recall is not zero, it is unmeasured, and posting a zero would read as the model
    missing everything."""
    span_id = str(secrets.randbits(63) or 1)
    trace_id = secrets.token_hex(16)
    run = replay["run"]
    ended_ns = run.get("ended_ns") or time.time_ns()
    started_ns = run.get("started_ns") or ended_ns - 1
    timestamp_ms = ended_ns // 1_000_000

    metrics = []

    def add(label, value):
        metrics.append(metric(label, value, span_id, trace_id, experiment_id, timestamp_ms))

    # A replay with no recorded exit status is not known to have finished in time.
    if run.get("pi_exit") is not None:
        add("timed_out", run["pi_exit"] == TIMEOUT_EXIT)
    metadata = {"attribution": replay["attribution"], "pi_exit": run.get("pi_exit")}

    verdict = replay["verdict"]
    if verdict is not None:
        score = verdict.get("score") or {}
        for label in SCORE_METRICS:
            if score.get(label) is not None:
                add(label, score[label])
        add("empty_output", bool(score.get("empty_candidate")))
        metadata.update(severity=verdict.get("severity"),
                        judge_model=verdict.get("judge_model"),
                        judge_verdict=verdict.get("verdict"))

    failure = failure_reason(replay)
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
            "output": replay["findings"],
            "expected_output": replay["expected"],
            "metadata": metadata,
        },
        "status": "error" if failure else "ok",
        "tags": tags + [f"dataset_record_id:{record}"],
    }
    return {"data": {"type": "experiments", "attributes": {
        "scope": "experiments", "metrics": metrics, "tags": tags, "spans": [span]}}}


def experiment_tags(experiment_id, project_id, dataset_id, model):
    return [f"experiment_id:{experiment_id}", f"project_id:{project_id}",
            f"dataset_id:{dataset_id}", f"model:{model}"]


def resolve_project(site, api_key, app_key, name):
    project_id = find_project(site, api_key, app_key, name)
    if not project_id:
        raise DatadogError(f"no LLM Obs project named {name}")
    return project_id


def find_experiment(site, api_key, app_key, project_id, tags):
    """The experiment in the project that carries every one of `tags`, as its attributes
    plus `id`, or None. The filter is a containment match; the tags are re-checked here
    so a looser match on the server cannot pass for a duplicate."""
    query = urllib.parse.urlencode([
        ("filter[project_id]", project_id),
        ("filter[metadata]", json.dumps({"tags": list(tags)}, separators=(",", ":"))),
    ])
    payload = request_json(site, api_key, app_key, "GET", f"{V2}/experiments?{query}")
    for item in payload.get("data") or []:
        attributes = item.get("attributes") or {}
        if set(tags) <= set((attributes.get("metadata") or {}).get("tags") or []):
            return {**attributes, "id": item.get("id")}
    return None


def start_experiment(site, api_key, app_key, project_id, dataset_file, records, model,
                     judge_model, prompt_version, run_url, extra_tags=()):
    """Create a running experiment of `model` over `records`, and return its ids as
    {experiment_id, project_id, dataset_id}."""
    name = dataset_name(dataset_file)
    dataset_id, version = find_dataset(site, api_key, app_key, project_id, name)

    # The replays read the YAML file; the experiment points at the Datadog copy. A record
    # missing from the copy would post a span linked to nothing, so nothing is created.
    missing = set(records) - dataset_record_ids(site, api_key, app_key, project_id, dataset_id)
    if missing:
        raise DatadogError(
            f"{len(missing)} record(s) are not in Datadog dataset {name}: "
            f"{', '.join(sorted(missing)[:10])}. Re-upload the dataset file with "
            f"upload_gym_dataset.py.")

    body = create_body(dataset_id, project_id, version, model, judge_model,
                       prompt_version, run_url, extra_tags)
    payload = request_json(site, api_key, app_key, "POST", f"{UNSTABLE}/experiments", body)
    experiment_id = (payload.get("data") or {}).get("id")
    if not experiment_id:
        raise DatadogError(f"experiment create returned no id: {json.dumps(payload)[:300]}")
    request_json(site, api_key, app_key, "PATCH", f"{UNSTABLE}/experiments/{experiment_id}",
                 status_body("running"))
    return {"experiment_id": experiment_id, "project_id": project_id, "dataset_id": dataset_id}


def post_replay(site, api_key, app_key, ids, model, record, replay):
    """Post one replay, as loaded by `load_replay`, to the experiment `ids` names."""
    tags = experiment_tags(ids["experiment_id"], ids["project_id"], ids["dataset_id"], model)
    request_json(site, api_key, app_key, "POST",
                 f"{UNSTABLE}/experiments/{ids['experiment_id']}/events",
                 record_body(ids["experiment_id"], tags, record, replay))


def finish_experiment(site, api_key, app_key, experiment_id, status):
    request_json(site, api_key, app_key, "PATCH", f"{UNSTABLE}/experiments/{experiment_id}",
                 status_body(status))


def cmd_create(args):
    site, api_key, app_key = credentials()
    with open(args.matrix) as stream:
        records = {job["record"] for job in json.load(stream)["include"]}
    ids = start_experiment(site, api_key, app_key,
                           resolve_project(site, api_key, app_key, args.project),
                           args.dataset_file, records, args.model, args.judge_model,
                           args.prompt_version, args.run_url)
    print(json.dumps(ids))


def cmd_record(args):
    site, api_key, app_key = credentials()
    ids = {"experiment_id": args.experiment_id, "project_id": args.project_id,
           "dataset_id": args.dataset_id}
    post_replay(site, api_key, app_key, ids, args.model, args.record,
                load_replay(args.replay_dir, args.attribution))
    print(f"{args.record}: posted to experiment {args.experiment_id}")


def cmd_finish(args):
    site, api_key, app_key = credentials()
    finish_experiment(site, api_key, app_key, args.experiment_id, args.status)
    print(f"experiment {args.experiment_id}: {args.status}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create")
    create.add_argument("--project", required=True)
    create.add_argument("--dataset-file", required=True,
                        help="the gym dataset YAML; its dataset.name names the Datadog dataset")
    create.add_argument("--matrix", required=True, help="the planned matrix JSON")
    create.add_argument("--model", required=True)
    create.add_argument("--judge-model", required=True)
    create.add_argument("--prompt-version")
    create.add_argument("--run-url")
    create.set_defaults(func=cmd_create)

    record = sub.add_parser("record")
    record.add_argument("--experiment-id", required=True)
    record.add_argument("--project-id", required=True)
    record.add_argument("--dataset-id", required=True)
    record.add_argument("--model", required=True)
    record.add_argument("--record", required=True)
    record.add_argument("--replay-dir", required=True)
    record.add_argument("--attribution", help="the endpoint-attribution JSON")
    record.set_defaults(func=cmd_record)

    finish = sub.add_parser("finish")
    finish.add_argument("--experiment-id", required=True)
    finish.add_argument("--status", required=True, choices=["completed", "failed"])
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
