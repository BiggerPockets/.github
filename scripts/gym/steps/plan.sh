#!/usr/bin/env bash
# Plan the replay matrix for one model and publish it as the `matrix` step output.
#
# Env: DATASET_FILE, MODEL, JUDGE_MODEL, and optionally LIMIT, SEVERITY, RECORD_IDS and
# PROMPT_VERSION (set only when replays are scoped to the current first-pass prompt).
set -euo pipefail
gym="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

args=(--dataset "$DATASET_FILE" --model "$MODEL" --judge-model "$JUDGE_MODEL"
      --out matrix.json)
[ -z "${LIMIT:-}" ] || args+=(--limit "$LIMIT")
[ -z "${SEVERITY:-}" ] || args+=(--severity "$SEVERITY")
[ -z "${RECORD_IDS:-}" ] || args+=(--record-ids "$RECORD_IDS")
[ -z "${PROMPT_VERSION:-}" ] || args+=(--prompt-version "$PROMPT_VERSION")

python3 "$gym/plan_matrix.py" "${args[@]}"
echo "matrix=$(cat matrix.json)" >> "$GITHUB_OUTPUT"
