#!/usr/bin/env bash
# Post this replay to the run's Datadog experiment, whether it succeeded or not.
#
# Env: DD_API_KEY, DD_APP_KEY, EXPERIMENT_ID, PROJECT_ID, DATASET_ID, MODEL, RECORD,
# REPLAY_DIR, OUT_DIR.
set -euo pipefail
gym="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 "$gym/datadog_experiment.py" record \
  --experiment-id "$EXPERIMENT_ID" \
  --project-id "$PROJECT_ID" \
  --dataset-id "$DATASET_ID" \
  --model "$MODEL" \
  --record "$RECORD" \
  --replay-dir "$REPLAY_DIR" \
  --attribution "$OUT_DIR/attribution.json"
