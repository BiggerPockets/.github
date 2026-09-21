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

Record shape is what an evaluator will destructure, so it is kept flat and identical
across every row: `input` carries the repo/PR *and the commit* the candidate model must
review — `head_sha` with its `base_ref`/`base_sha`, because the pull request itself has
moved on since and replaying it by number would review the wrong code —
`expected_output.findings` carries the prose the previous model wrote for that PR, and
`metadata` carries the severity and the Stage-2 verdict an evaluator can weight on. An
experiment reads `input`, checks out `head_sha`, runs the candidate first pass, and
scores its output against
`expected_output.findings` — the question being asked is "did it report this defect",
not "did it phrase it the same way", so the evaluator wants a judge, not string equality.

Uploading is additive: Datadog versions a dataset on write, and this script never
deletes rows. Re-uploading the same YAML therefore appends duplicates rather than
reconciling, so refresh by exporting into a *new* dataset name (`--dataset`) unless you
mean to extend an existing one. `--dry-run` prints what would be sent and calls nothing.

These endpoints are Datadog's `unstable` LLM Obs experiments API; the path prefix is a
constant below because Datadog moves it when the API stabilizes, and a 404 on every
call is the symptom.

Usage:
  DD_API_KEY=... DD_APP_KEY=... scripts/gym/upload_gym_dataset.py \
      --project 'code-review-gym' --file gym/sol-first-pass-findings.yaml
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

import yaml

API_PREFIX = "/api/unstable/llm-obs/v1"
# Datadog rejects very large writes; rows here are a few KB each, so this stays well
# under the limit while keeping the number of round trips small.
BATCH_SIZE = 50


class DatadogError(RuntimeError):
    pass


def request_json(site, api_key, app_key, method, path, body=None):
    url = f"https://api.{site}{path}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "DD-API-KEY": api_key,
            "DD-APPLICATION-KEY": app_key,
            "Content-Type": "application/json",
        })
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:500]
        raise DatadogError(f"{method} {path} -> {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise DatadogError(f"{method} {path} failed: {error}") from error


def find_by_name(site, api_key, app_key, kind, name):
    """The id of the project/dataset called `name`, or None.

    Datadog's filter is a contains-match on some deployments, so the exact name is
    re-checked here; picking a near-miss would silently write into the wrong dataset."""
    path = f"{API_PREFIX}/{kind}?filter%5Bname%5D={urllib.request.quote(name)}"
    payload = request_json(site, api_key, app_key, "GET", path)
    for item in payload.get("data") or []:
        if ((item.get("attributes") or {}).get("name")) == name:
            return item.get("id")
    return None


def create_project(site, api_key, app_key, name):
    body = {"data": {"type": "projects", "attributes": {"name": name}}}
    payload = request_json(site, api_key, app_key, "POST", f"{API_PREFIX}/projects", body)
    return (payload.get("data") or {}).get("id")


def create_dataset(site, api_key, app_key, project_id, name, description):
    body = {"data": {"type": "datasets", "attributes": {
        "name": name,
        "description": description,
        "project_id": project_id,
    }}}
    payload = request_json(site, api_key, app_key, "POST", f"{API_PREFIX}/datasets", body)
    return (payload.get("data") or {}).get("id")


def append_records(site, api_key, app_key, dataset_id, records):
    body = {"data": {"type": "datasets", "attributes": {"records": records}}}
    return request_json(site, api_key, app_key, "POST",
                        f"{API_PREFIX}/datasets/{dataset_id}/records", body)


def to_api_records(document):
    """Flatten the YAML rows into the shape the datasets API stores.

    The record `id` from the file is carried into metadata rather than sent as the
    Datadog record id: the API assigns its own, and losing the link back to the YAML row
    would make a failing experiment result impossible to trace to a source PR."""
    out = []
    for record in document.get("records") or []:
        metadata = dict(record.get("metadata") or {})
        metadata["record_id"] = record.get("id")
        out.append({
            "input": record.get("input"),
            "expected_output": record.get("expected_output"),
            "metadata": metadata,
            "tags": record.get("tags") or [],
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

    api_key = os.environ.get("DD_API_KEY")
    app_key = os.environ.get("DD_APP_KEY")
    if not api_key or not app_key:
        print("DD_API_KEY and DD_APP_KEY must be set", file=sys.stderr)
        return 2
    site = os.environ.get("DD_SITE", "datadoghq.com")

    try:
        project_id = find_by_name(site, api_key, app_key, "projects", args.project)
        if not project_id:
            project_id = create_project(site, api_key, app_key, args.project)
            print(f"Created project {args.project} ({project_id})")

        dataset_id = find_by_name(site, api_key, app_key, "datasets", dataset_name)
        if not dataset_id:
            dataset_id = create_dataset(site, api_key, app_key, project_id,
                                        dataset_name, description)
            print(f"Created dataset {dataset_name} ({dataset_id})")
        else:
            print(f"Appending to existing dataset {dataset_name} ({dataset_id})")

        for start in range(0, len(records), BATCH_SIZE):
            batch = records[start:start + BATCH_SIZE]
            append_records(site, api_key, app_key, dataset_id, batch)
            print(f"  uploaded {start + len(batch)}/{len(records)}")
    except DatadogError as error:
        print(f"Upload failed: {error}", file=sys.stderr)
        return 1

    print(f"Uploaded {len(records)} records to {args.project}/{dataset_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
