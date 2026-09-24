#!/usr/bin/env bash
# Write OUT_DIR/attribution.json: which OpenRouter endpoints served the replay and how
# long each call took. Measured from pi's recorded generation ids, not assumed from the
# routing preference sent beforehand. Runs after a timed-out replay too, since every turn
# that finished before the kill already wrote its id.
#
# Env: OPENROUTER_API_KEY, REPLAY_DIR, OUT_DIR.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

mkdir -p "$OUT_DIR"
python3 "$root/scripts/pi/openrouter.py" attribute \
  "$REPLAY_DIR/repo/first-pass-output.jsonl" --api-key "$OPENROUTER_API_KEY" \
  > "$OUT_DIR/attribution.json"
echo "Served by: $(jq -c '.calls_by_provider // {}' "$OUT_DIR/attribution.json")"
