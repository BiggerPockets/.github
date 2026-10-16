#!/usr/bin/env bash
# Fail the job unless the first-pass prompt it resolved is the gym's one prompt.
#
# Every gym run replays the same prompt, so runs of different models are comparable. The
# prompt is read from GYM_PROMPT_SHA, and this check catches anything that would replay
# other text: an edit to that pin, or a change to how resolve-prompts.sh builds a prompt.
#
# Env: GYM_PROMPT_SHA, GYM_PROMPT_VERSION, RESOLVED_VERSION.
set -euo pipefail

if [ "$RESOLVED_VERSION" != "$GYM_PROMPT_VERSION" ]; then
  echo "::error::resolved first-pass prompt '$RESOLVED_VERSION', but the gym replays only" \
       "$GYM_PROMPT_VERSION (commit $GYM_PROMPT_SHA)." >&2
  exit 1
fi
echo "Replaying first-pass prompt $GYM_PROMPT_VERSION from commit $GYM_PROMPT_SHA."
