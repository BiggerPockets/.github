#!/usr/bin/env python3
"""Record a gym run whose results are GitHub artifacts as Datadog LLM Obs experiments.

Such a run left one artifact per replay and may have replayed several models. Each model
it replayed becomes its own experiment, in the same shape `datadog_experiment.py` gives a
gym run's experiment. A model none of whose replays was scored is skipped: that means the
run broke, and says nothing about the model.

Every imported experiment is tagged `github_run_id:<id>` and `workflow_sha:<sha>`, the
second because the replay harness changed between runs. Re-running the import skips a
model already imported for the run, so it is safe to repeat.

`--run-dir` holds what `steps/fetch-run.sh` downloaded:

  run.json    GitHub's record of the workflow run
  jobs.json   its jobs, with step timings
  plan.log    the plan job's log, which printed the replay matrix
  artifacts/  one directory per artifact, as `gh run download` writes them

and each replay's artifact, `gym-<arm>--<record>/`, holds:

  out/<arm>--<record>.json              the judge's verdict; absent when not scored
  out/<arm>--<record>-attribution.json  endpoint attribution and pi's exit status
  replay/findings.md                    the model's review
"""
import argparse
import datetime
import json
import os
import sys

import yaml

import datadog_experiment as dx
from datadog_api import DatadogError, credentials

GYM_WORKFLOW = "gym-experiment.yml"
MATRIX_MARKER = '{"include":'
REPLAY_STEP_PREFIX = "Replay"


def read_text(path):
    with open(path) as stream:
        return stream.read()


def load_json(path):
    return json.loads(read_text(path))


def planned_jobs(plan_log):
    """The replay matrix the plan job printed: a list of {record, arm, model, ...}."""
    for line in plan_log.splitlines():
        start = line.find(MATRIX_MARKER)
        if start >= 0:
            return json.loads(line[start:])["include"]
    raise DatadogError("the plan job's log holds no replay matrix")


def expected_findings(dataset_file):
    """{record id: the recorded findings the replay was judged against}."""
    with open(dataset_file) as stream:
        document = yaml.safe_load(stream) or {}
    return {record["id"]: record["expected_output"]["findings"]
            for record in document.get("records") or []}


def iso_ns(timestamp):
    moment = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return int(moment.timestamp()) * 1_000_000_000


def replay_timings(jobs):
    """{job name: {started_ns, ended_ns}}, taken from the replay step, or the whole job
    when the replay step never ran."""
    timings = {}
    for job in jobs:
        steps = [step for step in job.get("steps") or []
                 if step["name"].startswith(REPLAY_STEP_PREFIX) and step.get("completed_at")]
        span = steps[0] if steps else job
        if span.get("started_at") and span.get("completed_at"):
            timings[job["name"]] = {"started_ns": iso_ns(span["started_at"]),
                                    "ended_ns": iso_ns(span["completed_at"])}
    return timings


def normalize_verdict(verdict, findings):
    """The verdict with every score the judge now reports: `weighted_recall` derived from
    the weighted totals, and `empty_candidate` from the review itself."""
    score = dict(verdict.get("score") or {})
    if "weighted_recall" not in score and score.get("weighted_total"):
        score["weighted_recall"] = score["weighted_matched"] / score["weighted_total"]
    score.setdefault("empty_candidate", not findings.strip())
    return {**verdict, "score": score}


def exit_status(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def load_artifact(artifact_dir, arm, record, expected, timing):
    """One replay's artifact, in the shape `datadog_experiment.load_replay` returns."""
    def path(*parts):
        return os.path.join(artifact_dir, *parts)

    verdict_file = path("out", f"{arm}--{record}.json")
    attribution_file = path("out", f"{arm}--{record}-attribution.json")
    findings_file = path("replay", "findings.md")

    attribution = load_json(attribution_file) if os.path.exists(attribution_file) else {}
    findings = read_text(findings_file) if os.path.exists(findings_file) else ""
    verdict = None
    if os.path.exists(verdict_file):
        verdict = normalize_verdict(load_json(verdict_file), findings)
    return {
        "run": {**timing, "pi_exit": exit_status(attribution.get("pi_exit"))},
        "findings": findings,
        "expected": expected,
        "verdict": verdict,
        "failure": "",
        "attribution": {key: attribution[key]
                        for key in ("primary", "providers", "calls_by_provider", "metrics")
                        if key in attribution},
    }


def replays_by_model(run_dir, dataset_file):
    """{model: {"prompt_version", "replays": {record: replay}}} for every replay the
    run left an artifact for."""
    jobs = planned_jobs(read_text(os.path.join(run_dir, "plan.log")))
    timings = replay_timings(load_json(os.path.join(run_dir, "jobs.json")))
    expected = expected_findings(dataset_file)
    artifacts = os.path.join(run_dir, "artifacts")

    models = {}
    for job in jobs:
        arm = job.get("label") or job["arm"]
        artifact_dir = os.path.join(artifacts, f"gym-{arm}--{job['record']}")
        if not os.path.isdir(artifact_dir):
            continue
        model = models.setdefault(job["model"], {
            "prompt_version": job.get("prompt_version") or "", "replays": {}})
        model["replays"][job["record"]] = load_artifact(
            artifact_dir, arm, job["record"], expected.get(job["record"], ""),
            timings.get(f"{arm} · {job['record']}", {}))
    return models


def judge_model(replays):
    for replay in replays.values():
        if replay["verdict"] and replay["verdict"].get("judge_model"):
            return replay["verdict"]["judge_model"]
    return ""


def import_model(site, api_key, app_key, project_id, dataset_file, run, model, planned):
    replays = planned["replays"]
    scored = sum(replay["verdict"] is not None for replay in replays.values())
    if not scored:
        print(f"{model}: skipped, none of its {len(replays)} replays was scored")
        return

    tags = [f"github_run_id:{run['id']}", f"workflow_sha:{run['head_sha'][:7]}"]
    existing = dx.find_experiment(site, api_key, app_key, project_id,
                                  [f"model:{model}", *tags])
    if existing:
        if existing.get("status") == "running":
            raise DatadogError(
                f"{model}: experiment {existing['id']} is from an import that did not "
                f"finish. Delete it in Datadog, then re-run the import.")
        print(f"{model}: skipped, already imported as experiment {existing['id']}")
        return

    ids = dx.start_experiment(site, api_key, app_key, project_id, dataset_file,
                              records=replays, model=model,
                              judge_model=judge_model(replays),
                              prompt_version=planned["prompt_version"],
                              run_url=run["html_url"], extra_tags=tags)
    for record, replay in replays.items():
        dx.post_replay(site, api_key, app_key, ids, model, record, replay)
    status = "completed" if run.get("conclusion") == "success" else "failed"
    dx.finish_experiment(site, api_key, app_key, ids["experiment_id"], status)
    print(f"{model}: {len(replays)} replays ({scored} scored) as experiment "
          f"{ids['experiment_id']}, {status}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", required=True, help="written by steps/fetch-run.sh")
    parser.add_argument("--dataset-file", required=True,
                        help="the gym dataset YAML the run replayed")
    parser.add_argument("--project", required=True, help="the Datadog LLM Obs project")
    args = parser.parse_args(argv)

    try:
        site, api_key, app_key = credentials()
        project_id = dx.resolve_project(site, api_key, app_key, args.project)
        run = load_json(os.path.join(args.run_dir, "run.json"))
        if not run.get("path", "").endswith(GYM_WORKFLOW):
            raise DatadogError(f"run {run.get('id')} is not a {GYM_WORKFLOW} run")
        for model, planned in replays_by_model(args.run_dir, args.dataset_file).items():
            import_model(site, api_key, app_key, project_id, args.dataset_file, run,
                         model, planned)
    except DatadogError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
