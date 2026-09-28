#!/usr/bin/env bash
# Write the run summary from every replay's counts, and point at where the per-record
# results are: the run's folder in BiggerPockets/pi-gym-data and, when the run has one,
# its Datadog experiment.
#
# Env: DATADOG_PROJECT, EXPERIMENT_ID, and the runner's GITHUB_RUN_ID and
# GITHUB_RUN_ATTEMPT. Reads results/, the downloaded replay artifacts.
set -uo pipefail
gym="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 "$gym/summarize_run.py" --results-dir results --out summary.md --json summary.json
status=$?

{
  cat summary.md
  echo
  echo "Per-record results: BiggerPockets/pi-gym-data," \
       "\`results/<dataset>/$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT-<model>/\`."
  if [ -n "${EXPERIMENT_ID:-}" ]; then
    echo "Also in Datadog LLM Observability, project \`$DATADOG_PROJECT\`," \
         "experiment \`$EXPERIMENT_ID\`."
  fi
} >> "$GITHUB_STEP_SUMMARY"
exit "$status"
