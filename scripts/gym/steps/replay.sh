#!/usr/bin/env bash
# Replay one first-pass review with the same pi invocation and system prompt as the
# production first-pass job, in the checkout replay_context.py rebuilt.
#
# A pass that ends on OpenRouter's "402 Payment Required" without findings is retried, up
# to MAX_ATTEMPTS passes in all, after a pause. OpenRouter reserves credit against every
# in-flight request, so a 402 means other requests briefly held the balance, not that the
# model failed. Every attempt's events are appended to first-pass-output.jsonl, so the
# attribution step still sees the generations an interrupted attempt was billed for.
#
# Writes to REPLAY_DIR: run.json ({started_ns, ended_ns, pi_exit, attempts}), findings.md,
# and on failure failure.txt. pi's error log can quote the model's partial output, so it
# goes to failure.txt, which is posted to Datadog, and never to this public job log.
#
# Env: OPENROUTER_API_KEY, FIRST_PASS_PROMPT, MODEL, REPLAY_DIR.
set -uo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPLAY_DIR/repo" || exit 1

MAX_ATTEMPTS=3
RETRY_PAUSE_S=60

printf '%s' "$FIRST_PASS_PROMPT" > first-pass-prompt.txt
: > first-pass-output.jsonl
: > first-pass-error.log
started_ns=$(date +%s%N)
attempt=0
while :; do
  attempt=$((attempt + 1))
  timeout 900 pi --mode json \
    --model "$MODEL" \
    --system-prompt "$(cat "$root/prompts/first-pass-system.md")" \
    --thinking medium \
    --tools read,grep,glob,bash \
    --no-context-files --no-skills --no-extensions \
    --no-themes --no-prompt-templates \
    @first-pass-prompt.txt \
    > attempt-output.jsonl 2> attempt-error.log
  pi_exit=$?
  cat attempt-output.jsonl >> first-pass-output.jsonl
  cat attempt-error.log >> first-pass-error.log
  if [ "$attempt" -ge "$MAX_ATTEMPTS" ] \
      || [ -n "$(python3 "$root/scripts/pi/final-message.py" attempt-output.jsonl)" ] \
      || ! python3 "$root/scripts/pi/payment-required.py" \
        attempt-output.jsonl attempt-error.log; then
    break
  fi
  pause=$((RETRY_PAUSE_S * attempt))
  echo "::warning::attempt $attempt ended on 402 Payment Required; retrying in ${pause}s"
  sleep "$pause"
done
ended_ns=$(date +%s%N)

jq -n --argjson started_ns "$started_ns" --argjson ended_ns "$ended_ns" \
  --argjson pi_exit "$pi_exit" --argjson attempts "$attempt" \
  '{started_ns: $started_ns, ended_ns: $ended_ns, pi_exit: $pi_exit, attempts: $attempts}' \
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
