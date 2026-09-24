#!/usr/bin/env bash
# Score the replay against the recorded findings.
#
# The full verdict quotes the code under review, so it goes to REPLAY_DIR/verdict.json,
# which is posted to Datadog and never uploaded here. OUT_DIR/score.json gets the counts
# only, which is all the run summary reads.
#
# Env: OPENROUTER_API_KEY, JUDGE_MODEL, RECORD, LABEL, SEVERITY, REPLAY_DIR, OUT_DIR.
set -euo pipefail
gym="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 "$gym/judge_findings.py" \
  --expected "$REPLAY_DIR/expected.md" \
  --actual "$REPLAY_DIR/findings.md" \
  --record "$RECORD" \
  --label "$LABEL" \
  --severity "$SEVERITY" \
  --model "$JUDGE_MODEL" \
  --out "$REPLAY_DIR/verdict.json"

mkdir -p "$OUT_DIR"
jq '{record, label, severity, judge_model, score}' "$REPLAY_DIR/verdict.json" \
  > "$OUT_DIR/score.json"
