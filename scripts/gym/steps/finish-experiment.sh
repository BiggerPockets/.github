#!/usr/bin/env bash
# Mark the run's Datadog experiment completed when every replay job succeeded, failed
# otherwise. Skipped when the run has no experiment, and a failed update only warns.
#
# Env: DD_API_KEY, DD_APP_KEY, EXPERIMENT_ID, REPLAY_RESULT (the replay job's result).
set -euo pipefail
gym="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -z "${EXPERIMENT_ID:-}" ]; then
  echo "No Datadog experiment for this run."
  exit 0
fi

status=failed
[ "$REPLAY_RESULT" != success ] || status=completed
python3 "$gym/datadog_experiment.py" finish --experiment-id "$EXPERIMENT_ID" --status "$status" \
  || echo "::warning::Could not mark Datadog experiment $EXPERIMENT_ID $status (error above)."
