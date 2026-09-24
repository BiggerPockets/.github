#!/usr/bin/env bash
# Mark the run's Datadog experiment completed when every replay job succeeded, failed
# otherwise.
#
# Env: DD_API_KEY, DD_APP_KEY, EXPERIMENT_ID, REPLAY_RESULT (the replay job's result).
set -euo pipefail
gym="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

status=failed
[ "$REPLAY_RESULT" != success ] || status=completed
python3 "$gym/datadog_experiment.py" finish --experiment-id "$EXPERIMENT_ID" --status "$status"
