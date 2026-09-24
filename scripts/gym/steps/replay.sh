#!/usr/bin/env bash
# Replay one first-pass review with the same pi invocation and system prompt as the
# production first-pass job, in the checkout replay_context.py rebuilt.
#
# Writes to REPLAY_DIR: run.json ({started_ns, ended_ns, pi_exit}), findings.md, and on
# failure failure.txt. pi's error log can quote the model's partial output, so it goes to
# failure.txt, which is posted to Datadog, and never to this public job log.
#
# Env: OPENROUTER_API_KEY, FIRST_PASS_PROMPT, MODEL, REPLAY_DIR.
set -uo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPLAY_DIR/repo" || exit 1

printf '%s' "$FIRST_PASS_PROMPT" > first-pass-prompt.txt
started_ns=$(date +%s%N)
timeout 900 pi --mode json \
  --model "$MODEL" \
  --system-prompt "$(cat "$root/prompts/first-pass-system.md")" \
  --thinking medium \
  --tools read,grep,glob,bash \
  --no-context-files --no-skills --no-extensions \
  --no-themes --no-prompt-templates \
  @first-pass-prompt.txt \
  > first-pass-output.jsonl 2> first-pass-error.log
pi_exit=$?
ended_ns=$(date +%s%N)

jq -n --argjson started_ns "$started_ns" --argjson ended_ns "$ended_ns" \
  --argjson pi_exit "$pi_exit" \
  '{started_ns: $started_ns, ended_ns: $ended_ns, pi_exit: $pi_exit}' \
  > "$REPLAY_DIR/run.json"

python3 "$root/scripts/pi/final-message.py" first-pass-output.jsonl \
  > "$REPLAY_DIR/findings.md"
if [ ! -s "$REPLAY_DIR/findings.md" ]; then
  echo "::error::replay produced no findings (pi exit $pi_exit)" >&2
  { echo "replay produced no findings (pi exit $pi_exit)"
    tail -60 first-pass-error.log; } > "$REPLAY_DIR/failure.txt"
  exit 1
fi
echo "Replay finished (pi exit $pi_exit)."
