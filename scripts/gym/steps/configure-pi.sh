#!/usr/bin/env bash
# Configure pi the way the production first-pass job does: scripts/pi/models.json with
# OpenRouter's routing preference for MODEL applied. Fails, by name, when MODEL is not
# pinned for stage1 there.
#
# Env: MODEL. Exports PI_CODING_AGENT_DIR through GITHUB_ENV.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
models="$root/scripts/pi/models.json"

pinned=$(jq -r '.providers.openrouter.models[]
                  | select((.stages // ["stage1"]) | index("stage1")) | .id' "$models")
if ! grep -qxF "$MODEL" <<<"$pinned"; then
  echo "::error::'$MODEL' is not pinned for stage1 in scripts/pi/models.json" \
       "(have: $(paste -sd, - <<<"$pinned"))." >&2
  exit 1
fi

# Routing is a preference, not a requirement: without it pi still runs, on whichever
# endpoint OpenRouter picks.
routing=$(python3 "$root/scripts/pi/openrouter.py" routing "$MODEL") || routing=""
if ! jq -e 'type == "object"' <<<"$routing" > /dev/null 2>&1; then
  echo "::warning::no OpenRouter routing for $MODEL; OpenRouter will choose the endpoint"
  routing='{}'
fi

config="$RUNNER_TEMP/pi-config"
mkdir -p "$config"
jq --argjson routing "$routing" --arg model "$MODEL" \
  '{providers: {openrouter: (.providers.openrouter
      | {baseUrl, apiKey, api,
         models: [.models[]
           | del(.stages)
           | if .id == $model and ($routing | length) > 0
             then .compat = ((.compat // {}) + {openRouterRouting: $routing})
             else . end]})}}' \
  "$models" > "$config/models.json"
echo "PI_CODING_AGENT_DIR=$config" >> "$GITHUB_ENV"
