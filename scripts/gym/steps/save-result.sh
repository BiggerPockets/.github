#!/usr/bin/env bash
# Save this replay's result, whether it succeeded or not, as a JSON file in the private
# BiggerPockets/pi-gym-data repo. See scripts/gym/gym_results.py.
#
# Env: GH_TOKEN (write access to Contents on pi-gym-data), DATASET_FILE, RECORD, MODEL,
# JUDGE_MODEL, REPLAY_DIR, OUT_DIR, and the runner's GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT,
# GITHUB_SERVER_URL and GITHUB_REPOSITORY.
set -euo pipefail
gym="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 "$gym/gym_results.py" \
  --dataset "$DATASET_FILE" \
  --record "$RECORD" \
  --model "$MODEL" \
  --judge-model "$JUDGE_MODEL" \
  --replay-dir "$REPLAY_DIR" \
  --attribution "$OUT_DIR/attribution.json" \
  --run-id "$GITHUB_RUN_ID" \
  --run-attempt "$GITHUB_RUN_ATTEMPT" \
  --run-url "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"
