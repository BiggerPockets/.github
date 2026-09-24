#!/usr/bin/env python3
"""Upload a gym dataset YAML into a Datadog LLM Observability experiments dataset.

The YAML in gym/ is the reviewable artifact — it diffs, it takes PR comments, and it
survives independently of any one Datadog org. This script is the one-way bridge from
that file into the place experiments actually run: an LLM Obs dataset inside a project,
against which a candidate first-pass model can be scored.

Datadog's experiments API separates the two halves deliberately, and so does this
script. A *project* groups experiments; a *dataset* holds the rows they iterate. Both
are addressed by name here and created when absent, so a first run bootstraps and every
later run appends to the same place rather than forking a parallel copy.

A dataset belongs to a project by virtue of the URL it is created under — the project's
UUID is a path segment, not a body field. Sending `project_id` in the payload instead
does not fail: Datadog returns 200 and files the dataset under the org's default
project, where no experiment in the intended project can reach it. The project-scoped
read-back at the end of `create_dataset` is what catches that.

Record shape is what an evaluator will destructure, so it is kept flat and identical
across every row: `input` carries the repo/PR *and the commit* the candidate model must
review — `head_sha` with its `base_ref`/`base_sha`, because the pull request itself has
moved on since and replaying it by number would review the wrong code —
`expected_output.findings` carries the prose the previous model wrote for that PR, and
`metadata` carries the severity and the Stage-2 verdict an evaluator can weight on. An
experiment reads `input`, checks out `head_sha`, runs the candidate first pass, and
scores its output against `expected_output.findings` — the question being asked is "did
it report this defect", not "did it phrase it the same way", so the evaluator wants a
judge, not string equality.

Each row keeps the `id` it has in the YAML, so a failing experiment result names the
source PR directly, and keeps its `tags`, which the batch-update endpoint stores as
first-class record dimensions.

Writes go through `batch_update`, which deduplicates on record id. Re-uploading the same
YAML therefore reconciles rather than doubling the dataset, and cuts a new version each
time. `--dry-run` prints what would be sent and calls nothing.

A project is looked up by name and created when absent. Pass `--project-id` when the
project already exists and its UUID is known: it skips the lookup, so a name that the
search does not return cannot lead to a second project with the same name.

Usage:
  DD_API_KEY=... DD_APP_KEY=... scripts/gym/upload_gym_dataset.py \
      --project 'biggiepockets-review-gym' --file gym/sol-first-pass-findings.yaml
"""
import argparse
import json
import sys
import time

import yaml

from datadog_api import (UNSTABLE, V2, DatadogError, credentials, find_project,
                         request_json)

# Datadog rejects very large writes; rows here are a few KB each, so this stays well
# under the limit while keeping the number of round trips small.
BATCH_SIZE = 50
# A dataset shows up in its project's listing a moment after it is created, so the
# placement check retries before it calls a dataset misplaced.
MEMBERSHIP_RETRIES = 5
MEMBERSHIP_BACKOFF = 1.0


def project_datasets(site, api_key, app_key, project_id):
    """Every dataset in the project, as `{name: id}`.

    The org-wide `/datasets` listing ignores a `project_id` filter and returns the whole
    org, so it cannot answer "is this dataset in that project". This route can: the
    project is a path segment, and the response is scoped to it."""
    payload = request_json(site, api_key, app_key, "GET",
                           f"{V2}/{project_id}/datasets")
    found = {}
    for item in payload.get("data") or []:
        name = (item.get("attributes") or {}).get("name")
        if name and item.get("id"):
            found[name] = item["id"]
    return found


def entity_id(payload):
    """The id Datadog assigned, whether it wraps the object or a list of one."""
    data = payload.get("data")
    if isinstance(data, list):
        data = data[0] if data else None
    if isinstance(data, dict) and data.get("id"):
        return data["id"]
    return payload.get("id")


def create_project(site, api_key, app_key, name):
    body = {"data": {"type": "projects",
                     "attributes": {"name": name, "description": ""}}}
    payload = request_json(site, api_key, app_key, "POST", f"{UNSTABLE}/projects", body)
    project_id = entity_id(payload)
    if not project_id:
        raise DatadogError(
            f"created project {name} but the response carried no id: "
            f"{json.dumps(payload)[:300]}")
    return project_id


def create_dataset(site, api_key, app_key, project_id, name, description):
    """Create the dataset under the project and confirm it landed there."""
    body = {"data": {"type": "datasets",
                     "attributes": {"name": name, "description": description}}}
    payload = request_json(site, api_key, app_key, "POST",
                           f"{UNSTABLE}/{project_id}/datasets", body)
    dataset_id = entity_id(payload)
    if not dataset_id:
        # The dataset exists either way — a create response this function cannot read
        # is not a reason to make the operator start over, so ask for it by name.
        dataset_id = project_datasets(site, api_key, app_key, project_id).get(name)
    if not dataset_id:
        raise DatadogError(
            f"created dataset {name} but could not determine its id, from the "
            f"response ({json.dumps(payload)[:200]}) or by looking it up by name")

    # The listing lags the create by a moment, so a single miss means nothing. Only a
    # dataset still absent after the retries is one that went somewhere else.
    for attempt in range(MEMBERSHIP_RETRIES):
        if dataset_id in project_datasets(site, api_key, app_key, project_id).values():
            return dataset_id
        time.sleep(MEMBERSHIP_BACKOFF * (attempt + 1))
    raise DatadogError(
        f"dataset {name} ({dataset_id}) is not in project {project_id}. Nothing "
        f"was uploaded.")


def append_records(site, api_key, app_key, project_id, dataset_id, records):
    """Insert rows, deduplicating on record id so a re-run reconciles.

    Every write cuts a new dataset version, which is what makes an experiment result
    reproducible: it names the version it iterated."""
    body = {"data": {"type": "datasets", "id": dataset_id, "attributes": {
        "insert_records": records,
        "update_records": [],
        "delete_records": [],
        "deduplicate": True,
        "create_new_version": True,
    }}}
    return request_json(site, api_key, app_key, "POST",
                        f"{V2}/{project_id}/datasets/{dataset_id}/batch_update", body)


def to_api_records(document):
    """Flatten the YAML rows into the shape the batch-update endpoint stores.

    The row keeps the `id` it has in the file. That is what deduplication keys on, so a
    re-upload reconciles instead of doubling the dataset, and it is what ties a failing
    experiment result back to the pull request it came from.

    `tags` are sent as they appear in the file. They are stored as record dimensions, so
    an experiment can slice its results by severity or source model without reaching
    into metadata."""
    out = []
    for record in document.get("records") or []:
        out.append({
            "id": record.get("id"),
            "input": record.get("input"),
            "expected_output": record.get("expected_output"),
            "metadata": dict(record.get("metadata") or {}),
            "tags": list(record.get("tags") or []),
        })
    return out


def load(path):
    with open(path) as stream:
        document = yaml.safe_load(stream)
    if not isinstance(document, dict) or not document.get("records"):
        raise ValueError(f"{path} has no records")
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", default="gym/sol-first-pass-findings.yaml")
    parser.add_argument("--project", required=True,
                        help="LLM Obs experiments project (created when absent)")
    parser.add_argument("--project-id",
                        help="the project's UUID, to skip resolving it by name")
    parser.add_argument("--dataset",
                        help="dataset name; defaults to dataset.name in the file")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be sent and call nothing")
    args = parser.parse_args(argv)

    try:
        document = load(args.file)
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"Could not read {args.file}: {error}", file=sys.stderr)
        return 2

    dataset_name = args.dataset or (document.get("dataset") or {}).get("name")
    description = (document.get("dataset") or {}).get("description", "")
    records = to_api_records(document)

    if args.dry_run:
        print(f"project: {args.project}")
        print(f"dataset: {dataset_name} ({len(records)} records)")
        print(json.dumps(records[0], indent=2, default=str, ensure_ascii=False))
        return 0

    try:
        site, api_key, app_key = credentials()
    except DatadogError as error:
        print(error, file=sys.stderr)
        return 2

    try:
        project_id = args.project_id
        if not project_id:
            project_id = find_project(site, api_key, app_key, args.project)
        if not project_id:
            project_id = create_project(site, api_key, app_key, args.project)
            print(f"Created project {args.project} ({project_id})")

        dataset_id = project_datasets(site, api_key, app_key, project_id).get(
            dataset_name)
        if not dataset_id:
            dataset_id = create_dataset(site, api_key, app_key, project_id,
                                        dataset_name, description)
            print(f"Created dataset {dataset_name} ({dataset_id})")
        else:
            print(f"Appending to existing dataset {dataset_name} ({dataset_id})")

        for start in range(0, len(records), BATCH_SIZE):
            batch = records[start:start + BATCH_SIZE]
            append_records(site, api_key, app_key, project_id, dataset_id, batch)
            print(f"  uploaded {start + len(batch)}/{len(records)}")
    except DatadogError as error:
        print(f"Upload failed: {error}", file=sys.stderr)
        return 1

    print(f"Uploaded {len(records)} records to {args.project}/{dataset_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
