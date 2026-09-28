#!/usr/bin/env bash
# Create the Datadog experiment this run records into, and publish its ids as the
# `experiment_id`, `project_id` and `dataset_id` step outputs.
#
# Datadog is a secondary copy of the results; the primary one is the per-replay files in
# BiggerPockets/pi-gym-data. So when the experiment cannot be created (keys missing or
# rejected, dataset out of sync) this warns and publishes no ids, and the run goes on
# without Datadog rather than stopping.
#
# Env: DD_API_KEY, DD_APP_KEY, DATADOG_PROJECT, DATASET_FILE, MODEL, JUDGE_MODEL,
# PROMPT_VERSION. Reads matrix.json, written by plan.sh.
set -euo pipefail
gym="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! created=$(python3 "$gym/datadog_experiment.py" create \
    --project "$DATADOG_PROJECT" \
    --dataset-file "$DATASET_FILE" \
    --matrix matrix.json \
    --model "$MODEL" \
    --judge-model "$JUDGE_MODEL" \
    --prompt-version "$PROMPT_VERSION" \
    --run-url "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"); then
  echo "::warning::Could not create the Datadog experiment (error above); this run is" \
       "not recorded in Datadog. Results are still saved to pi-gym-data."
  exit 0
fi

jq -r 'to_entries[] | "\(.key)=\(.value)"' <<<"$created" >> "$GITHUB_OUTPUT"
echo "Datadog experiment: $(jq -r .experiment_id <<<"$created")"
