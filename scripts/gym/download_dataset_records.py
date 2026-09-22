#!/usr/bin/env python3
"""Pull every record of a Datadog LLM Obs experiments dataset straight to a local file.

Why this exists, instead of reading the dataset through an MCP tool: dataset records are
private-repo content (see README.md's "Model gym" section), and the MCP tools available
for reading them cap out at a few records per call with no way to write the result
straight to disk — each call inlines its records into whatever is calling it. Pulling
~100 records that way means ~30+ calls, each one putting private findings text
somewhere they don't need to be. This script instead talks to Datadog's REST API
directly with urllib and writes each page straight into the output file; nothing it
reads is ever printed, logged, or interpreted by anything upstream of `open(...).write`.

Use this to *recover* a dataset whose source spans have since aged out of LLM Obs span
retention (export_sol_findings.py/export_synthesis_findings.py rebuild from spans, which
is the normal path, but the spans do not live forever; the uploaded dataset does). It
reconstructs the same YAML shape those two scripts write, so the file downloaded here
is a drop-in replacement for the ones they'd normally produce.

Usage:
  DD_API_KEY=... DD_APP_KEY=... scripts/gym/download_dataset_records.py \
      --project-id 1bef0819-6c00-4012-a984-cb44458e1a67 \
      --dataset-id e8e4e548-99ae-421c-97ba-90ac5e2daa46 \
      --out gym/sol-first-pass-findings.yaml
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

import yaml

UNSTABLE = "/api/unstable/llm-obs/v1"
V2 = "/api/v2/llm-obs/v1"
PAGE_LIMIT = 100


class Block(str):
    """A string YAML should emit as a literal block (`|`), not a quoted one-liner —
    matches export_sol_findings.py's Block so the resulting file reads the same way."""


yaml.add_representer(
    Block, lambda dumper, data: dumper.represent_scalar(
        "tag:yaml.org,2002:str", str(data), style="|"))


def request_json(site, api_key, app_key, path):
    url = f"https://api.{site}{path}"
    request = urllib.request.Request(
        url, method="GET",
        headers={"DD-API-KEY": api_key, "DD-APPLICATION-KEY": app_key,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:800]
        raise RuntimeError(f"GET {path} -> {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"GET {path} failed: {error}") from error


def fetch_dataset(site, api_key, app_key, project_id, dataset_id):
    """The dataset's own name/description, for the file's header."""
    for prefix in (V2, UNSTABLE):
        try:
            payload = request_json(site, api_key, app_key,
                                   f"{prefix}/{project_id}/datasets/{dataset_id}")
            attrs = (payload.get("data") or {}).get("attributes") or {}
            if attrs:
                return attrs.get("name"), attrs.get("description", "")
        except RuntimeError:
            continue
    return None, ""


def fetch_records(site, api_key, app_key, project_id, dataset_id):
    """Every record, paginated. Tries the v2 records route first, falling back to
    unstable — Datadog has moved this prefix before (see upload_gym_dataset.py), so a
    404 on one is expected to mean "try the other", not "the dataset is empty"."""
    for prefix in (V2, UNSTABLE):
        cursor = None
        records = []
        page_num = 0
        try:
            while True:
                path = f"{prefix}/{project_id}/datasets/{dataset_id}/records?page[limit]={PAGE_LIMIT}"
                if cursor:
                    path += f"&page[cursor]={urllib.request.quote(cursor)}"
                payload = request_json(site, api_key, app_key, path)
                page_num += 1
                batch = payload.get("data") or []
                if not batch and page_num == 1:
                    raise RuntimeError("empty first page")
                records.extend(batch)
                cursor = (((payload.get("meta") or {}).get("page") or {}).get("after"))
                if not cursor or not batch:
                    return records
        except RuntimeError as error:
            print(f"  {prefix} records route failed ({error}); trying the other prefix",
                  file=sys.stderr)
            continue
    raise RuntimeError(
        "could not read records from either the v2 or unstable API prefix — the route "
        "may have moved; check Datadog's current LLM Obs experiments API docs")


def to_yaml_record(item):
    """One Datadog API record -> the YAML row shape export_sol_findings.py produces."""
    record_id = item.get("id") or (item.get("attributes") or {}).get("id")
    attrs = item.get("attributes") or item
    input_ = attrs.get("input") or {}
    expected = attrs.get("expected_output") or {}
    metadata = dict(attrs.get("metadata") or {})
    findings = expected.get("findings")

    row = {
        "id": record_id,
        "input": {
            "repo": input_.get("repo"),
            "pr": input_.get("pr"),
            "head_sha": input_.get("head_sha"),
            "base_ref": input_.get("base_ref"),
            "base_sha": input_.get("base_sha"),
            "instruction": input_.get("instruction"),
        },
        "expected_output": {
            "findings": Block((findings or "").rstrip("\n") + "\n"),
        },
        "metadata": metadata,
        "tags": list(attrs.get("tags") or []),
    }
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    api_key = os.environ.get("DD_API_KEY")
    app_key = os.environ.get("DD_APP_KEY")
    if not api_key or not app_key:
        print("DD_API_KEY and DD_APP_KEY must be set", file=sys.stderr)
        return 2
    site = os.environ.get("DD_SITE", "datadoghq.com")

    name, description = fetch_dataset(site, api_key, app_key, args.project_id, args.dataset_id)
    try:
        raw_records = fetch_records(site, api_key, app_key, args.project_id, args.dataset_id)
    except RuntimeError as error:
        print(f"Could not fetch records: {error}", file=sys.stderr)
        return 1

    records = [to_yaml_record(r) for r in raw_records]
    pinned = sum(1 for r in records if r["input"].get("head_sha") and r["input"].get("base_sha"))

    document = {
        "version": 1,
        "dataset": {"name": name or args.dataset_id, "description": description},
        "records": records,
    }

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as stream:
        yaml.dump(document, stream, sort_keys=False, allow_unicode=True,
                  width=100, default_flow_style=False)

    print(f"Wrote {len(records)} records ({pinned} pinned to a commit) to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
