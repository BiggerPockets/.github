#!/usr/bin/env bash
# resolve-prompts.sh — resolve the BiggiePockets review prompts from the registry.
#
# prompts/registry.json declares the codex prompt, one or more arms (each a stored
# prompt template), the control arm, and what percentage of pull requests an
# experiment arm gets. This script picks ONE arm per review — hash('<repo>:<pr>')
# mod 100, below experiment_split_percent means the experiment arm — and emits only
# that arm's prompt. The assigned arm both posts its summary and decides the review,
# so arms are compared BETWEEN pull requests and each review costs one Claude pass.
#
# Shared rule blocks (privacy, migration-data, perf) are stored once in
# prompts/_shared/ and injected into every prompt that references them via {{@path}}
# markers, so the Codex and Claude prompts can never drift out of sync.
#
# The resolved text substitutes late-binding placeholders used for provenance only:
#   {{PR}}, {{PROMPT_NAME}}, {{PROMPT_VERSION}}
#
# Alongside each resolved text, the unsubstituted template (shared blocks expanded,
# those three placeholders left intact) is emitted as <prefix>_prompt_template. It is
# what Datadog LLM Obs Prompt Tracking records as the prompt template, with the
# substituted values reported as its variables, so runs of one prompt group together
# instead of splitting into a new template per PR.
#
# prompt_version is DERIVED from content, never hand-set: a hash of the template file
# plus every shared block it includes. It changes if and only if that prompt's text
# changes (a Roll), stays stable across PRs, and never encodes an arm — the arm is
# reported separately so Datadog LLM Obs can hold both (prompt_name/version = source
# of truth for the text; the arm = the test condition).
#
# Inputs (env): REGISTRY_DIR, PR, GITHUB_REPOSITORY (falls back to "unknown", which
# only changes which arm a PR lands in, never whether assignment is deterministic).
# Outputs: codex_prompt{,_name,_version,_template}; arm_prompt{,_name,_version,_template}
# for the ASSIGNED arm; assigned_arm (its key); control_arm; experiment_arm_key (the
# first non-control arm, or empty); experiment_split_percent; assignment_bucket (0-99,
# so an assignment can be recomputed and audited from the tag alone).

set -euo pipefail

: "${REGISTRY_DIR:?REGISTRY_DIR must point at the checkout of BiggerPockets/.github}"
: "${PR:?PR must be set}"

REGISTRY_FILE="$REGISTRY_DIR/prompts/registry.json"
if [ ! -f "$REGISTRY_FILE" ]; then
  echo "::error::prompts/registry.json not found in registry at $REGISTRY_FILE" >&2
  exit 1
fi

MAP_VERSION=$(jq -r '.version // empty' "$REGISTRY_FILE")
CODEX_NAME=$(jq -r '.codex_prompt // empty' "$REGISTRY_FILE")
CONTROL_ARM=$(jq -r '.control_arm // empty' "$REGISTRY_FILE")
SPLIT_PERCENT=$(jq -r '.experiment_split_percent // 0' "$REGISTRY_FILE")
ARMS=()
while IFS= read -r arm; do ARMS+=("$arm"); done < <(jq -r '.arms | keys[]' "$REGISTRY_FILE")

for name in "$CODEX_NAME" "$CONTROL_ARM"; do
  if [ -z "$name" ]; then
    echo "::error::registry.json is missing codex_prompt or control_arm" >&2
    exit 1
  fi
done
if ! printf '%s' "$SPLIT_PERCENT" | grep -qE '^[0-9]+$' || [ "$SPLIT_PERCENT" -gt 100 ]; then
  echo "::error::experiment_split_percent must be an integer 0-100, got '$SPLIT_PERCENT'" >&2
  exit 1
fi

# Validate the config so a bad edit is caught at review time instead of silently
# running a stale/duplicate prompt.
: "${MAP_VERSION:?registry.json missing version}"
for arm in "${ARMS[@]}"; do
  prompt=$(jq -r --arg a "$arm" '.arms[$a] // empty' "$REGISTRY_FILE")
  if [ -z "$prompt" ]; then
    echo "::error::Arm '$arm' has no prompt mapping in prompts/registry.json" >&2
    exit 1
  fi
  if [ ! -f "$REGISTRY_DIR/prompts/$prompt.md" ]; then
    echo "::error::Arm '$arm' maps to '$prompt' but prompts/$prompt.md does not exist" >&2
    exit 1
  fi
done
if ! printf '%s\n' "${ARMS[@]}" | grep -qx -- "$CONTROL_ARM"; then
  echo "::error::control_arm '$CONTROL_ARM' is not a declared arm" >&2
  exit 1
fi

# Expand {{@path}} includes (path relative to REGISTRY_DIR), recording each included
# file on _INCLUDED (de-duplicated). A marker may appear inline within a line: text
# before/after the marker is preserved and the included content is always followed by
# a newline so the next line of the template can't be concatenated onto it. Shared
# blocks do not reference other shared blocks today; if they ever do, switch to a
# recursive resolver.
_INCLUDED=()
expand_template() {
  local file="$1" out="" line rest pre rel post
  while IFS= read -r line || [ -n "$line" ]; do
    rest="$line"
    while [[ "$rest" == *'{{@'* ]]; do
      pre="${rest%%\{\{@*}"
      rel="${rest#*\{\{@}"; rel="${rel%%\}*}"
      incfile="$REGISTRY_DIR/$rel"
      if [ ! -f "$incfile" ]; then
        echo "::error::Include '$rel' (referenced by $(basename "$file")) not found in registry" >&2
        exit 1
      fi
      seen=0
      for f in "${_INCLUDED[@]:-}"; do
        if [ "$f" = "$incfile" ]; then seen=1; break; fi
      done
      if [ "$seen" -eq 0 ]; then _INCLUDED+=("$incfile"); fi
      out+="$pre"
      out+="$(cat "$incfile")"
      rest="${rest#*\{\{@${rel}\}\}}"
      if [ -n "$rest" ]; then out+=$'\n'; fi
    done
    out+="$rest"$'\n'
  done < "$file"
  printf '%s' "$out"
}

prompt_version() { # name -> content-derived version (template + shared includes)
  local name="$1"
  _INCLUDED=()
  expand_template "$REGISTRY_DIR/prompts/$name.md" > /dev/null
  {
    cat "$REGISTRY_DIR/prompts/$name.md"
    for f in "${_INCLUDED[@]:-}"; do cat "$f"; done
  } | sha256sum | cut -c1-12
}

prompt_template() { # name -> include-expanded text, placeholders left intact
  local name="$1"
  _INCLUDED=()
  expand_template "$REGISTRY_DIR/prompts/$name.md"
}

prompt_text() { # name version -> resolved text
  local name="$1" version="$2" text
  _INCLUDED=()
  text="$(expand_template "$REGISTRY_DIR/prompts/$name.md")"
  text="${text//\{\{PR\}\}/$PR}"
  text="${text//\{\{PROMPT_NAME\}\}/$name}"
  text="${text//\{\{PROMPT_VERSION\}\}/$version}"
  printf '%s' "$text"
}

emit_prompt() { # emit_prompt <output prefix> <prompt name> — writes text/name/version
  local prefix="$1" name="$2" version
  version="$(prompt_version "$name")"
  write_output "${prefix}_prompt" "$(prompt_text "$name" "$version")"
  write_output "${prefix}_prompt_template" "$(prompt_template "$name")"
  write_output "${prefix}_prompt_name" "$name"
  write_output "${prefix}_prompt_version" "$version"
}

write_output() { # name value — multiline-safe via GITHUB_OUTPUT heredoc; fixed
  # delimiter keeps the whole output file byte-for-byte reproducible.
  local name="$1" value="$2"
  printf '%s<<_BIGGIEPOCKETS_PROMPT_EOF_\n%s\n_BIGGIEPOCKETS_PROMPT_EOF_\n' "$name" "$value" >> "$GITHUB_OUTPUT"
}

emit_prompt "codex" "$CODEX_NAME"

# Experiment arm = the first arm that is not the control arm; there may be none (a
# single-arm registry, e.g. after the experiment is merged away).
EXPERIMENT_ARM=""
for arm in "${ARMS[@]}"; do
  if [ "$arm" != "$CONTROL_ARM" ]; then EXPERIMENT_ARM="$arm"; break; fi
done

# Assign this PR to one arm. sha256 over "<repo>:<pr>" rather than the PR number alone,
# so two repos don't hand the same arm to their matching PR numbers, and the low 32 bits
# mod 100 give the bucket. Deterministic by construction: a re-review of the same PR
# resolves the same arm, and the bucket is emitted so any assignment can be re-derived
# from telemetry without rerunning this script.
ASSIGN_KEY="${GITHUB_REPOSITORY:-unknown}:$PR"
BUCKET=$(( 16#$(printf '%s' "$ASSIGN_KEY" | sha256sum | cut -c1-8) % 100 ))
ASSIGNED_ARM="$CONTROL_ARM"
if [ -n "$EXPERIMENT_ARM" ] && [ "$BUCKET" -lt "$SPLIT_PERCENT" ]; then
  ASSIGNED_ARM="$EXPERIMENT_ARM"
fi

write_output "control_arm" "$CONTROL_ARM"
write_output "experiment_arm_key" "$EXPERIMENT_ARM"
write_output "experiment_split_percent" "$SPLIT_PERCENT"
write_output "assignment_bucket" "$BUCKET"
write_output "assigned_arm" "$ASSIGNED_ARM"
emit_prompt "arm" "$(jq -r --arg a "$ASSIGNED_ARM" '.arms[$a]' "$REGISTRY_FILE")"

echo "Resolved $MAP_VERSION prompts: codex=$CODEX_NAME assigned=$ASSIGNED_ARM (bucket $BUCKET, split ${SPLIT_PERCENT}% to ${EXPERIMENT_ARM:-none})"
