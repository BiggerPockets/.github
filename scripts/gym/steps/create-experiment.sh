#!/usr/bin/env bash
# Create the Datadog experiment this run records into, and publish its ids as the
# `experiment_id`, `project_id` and `dataset_id` step outputs.
#
# Env: DD_API_KEY, DD_APP_KEY, DATADOG_PROJECT, DATASET_FILE, MODEL, JUDGE_MODEL,
# PROMPT_VERSION. Reads matrix.json, written by plan.sh.
set -euo pipefail
gym="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

created=$(python3 "$gym/datadog_experiment.py" create \
  --project "$DATADOG_PROJECT" \
  --dataset-file "$DATASET_FILE" \
  --matrix matrix.json \
  --model "$MODEL" \
  --judge-model "$JUDGE_MODEL" \
  --prompt-version "$PROMPT_VERSION" \
  --run-url "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID")

jq -r 'to_entries[] | "\(.key)=\(.value)"' <<<"$created" >> "$GITHUB_OUTPUT"
echo "Datadog experiment: $(jq -r .experiment_id <<<"$created")"
