#!/usr/bin/env bash
# Write the run summary from every replay's counts, and point at the Datadog experiment
# holding the per-record results.
#
# Env: DATADOG_PROJECT, EXPERIMENT_ID. Reads results/, the downloaded replay artifacts.
set -uo pipefail
gym="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 "$gym/summarize_run.py" --results-dir results --out summary.md --json summary.json
status=$?

{
  cat summary.md
  echo
  echo "Per-record results: Datadog LLM Observability experiments, project" \
       "\`$DATADOG_PROJECT\`, experiment \`$EXPERIMENT_ID\`."
} >> "$GITHUB_STEP_SUMMARY"
exit "$status"
