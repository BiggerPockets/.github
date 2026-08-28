#!/usr/bin/env bash
# resolve-prompts.sh — resolve the BiggiePockets review prompts from the registry.
#
# prompts/registry.json declares the codex prompt, the auditor prompt, the arbitrator
# prompt, and the panel seats. A review runs the auditor prompt once per seat (caspar,
# balthazar, melchior) in independent jobs, then the arbitrator prompt once over the
# three verdicts those jobs produced. This script emits every prompt each review needs;
# the workflow decides which model runs in which seat.
#
# The auditor prompt is emitted ONCE and every seat runs that same text. Nothing in it
# is parameterized by seat, so the three audits differ only by model and by the
# independent judgment of the run — never by wording.
#
# Shared rule blocks (privacy, migration-data, perf) are stored once in
# prompts/_shared/ and injected into every prompt that references them via {{@path}}
# markers, so the Codex and auditor prompts can never drift out of sync.
#
# The resolved text substitutes late-binding placeholders used for provenance only:
#   {{PR}}, {{PROMPT_NAME}}, {{PROMPT_VERSION}}, {{AUDITORS}}
#
# Alongside each resolved text, the unsubstituted template (shared blocks expanded,
# those placeholders left intact) is emitted as <prefix>_prompt_template. It is what
# Datadog LLM Obs Prompt Tracking records as the prompt template, with the substituted
# values reported as its variables, so runs of one prompt group together instead of
# splitting into a new template per PR.
#
# prompt_version is DERIVED from content, never hand-set: a hash of the template file
# plus every shared block it includes. It changes if and only if that prompt's text
# changes (a Roll) and stays stable across PRs, so Datadog can attribute a change in
# review quality to the exact prompt text that produced it.
#
# Inputs (env): REGISTRY_DIR, PR.
# Outputs: codex_prompt{,_name,_version,_template}; auditor_prompt{,_name,_version,_template};
# arbiter_prompt{,_name,_version,_template}; auditors (JSON array of seat names, which the
# workflow feeds straight into its fan-out matrix); auditor_count.

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
AUDITOR_NAME=$(jq -r '.auditor_prompt // empty' "$REGISTRY_FILE")
ARBITER_NAME=$(jq -r '.arbiter_prompt // empty' "$REGISTRY_FILE")

# Validate the config so a bad edit is caught at review time instead of silently
# running a stale prompt or an undersized panel.
: "${MAP_VERSION:?registry.json missing version}"
for field in codex_prompt auditor_prompt arbiter_prompt; do
  name=$(jq -r --arg f "$field" '.[$f] // empty' "$REGISTRY_FILE")
  if [ -z "$name" ]; then
    echo "::error::registry.json is missing $field" >&2
    exit 1
  fi
  if [ ! -f "$REGISTRY_DIR/prompts/$name.md" ]; then
    echo "::error::$field is '$name' but prompts/$name.md does not exist" >&2
    exit 1
  fi
done

if ! jq -e '.auditors | type == "array"' "$REGISTRY_FILE" >/dev/null; then
  echo "::error::registry.json .auditors must be an array of seat names" >&2
  exit 1
fi
AUDITORS=()
while IFS= read -r seat; do AUDITORS+=("$seat"); done < <(jq -r '.auditors[]' "$REGISTRY_FILE")
if [ "${#AUDITORS[@]}" -lt 2 ]; then
  echo "::error::registry.json declares ${#AUDITORS[@]} auditor(s); a panel needs at least 2" >&2
  exit 1
fi
# Seat names become artifact names and shell identifiers downstream, and a duplicate
# would have two jobs overwrite one seat's verdict — shrinking the panel invisibly.
for seat in "${AUDITORS[@]}"; do
  if ! printf '%s' "$seat" | grep -qE '^[a-z][a-z0-9-]*$'; then
    echo "::error::auditor seat '$seat' must be lowercase alphanumeric/dashes" >&2
    exit 1
  fi
done
if [ "$(printf '%s\n' "${AUDITORS[@]}" | sort -u | wc -l)" -ne "${#AUDITORS[@]}" ]; then
  echo "::error::registry.json .auditors contains duplicate seat names" >&2
  exit 1
fi

AUDITOR_LIST=$(printf '%s, ' "${AUDITORS[@]}"); AUDITOR_LIST="${AUDITOR_LIST%, }"

# Expand {{@path}} includes (path relative to REGISTRY_DIR), recording each included
# file on _INCLUDED (de-duplicated). A marker may appear inline within a line: text
# before/after the marker is preserved and the included content is always followed by
# a newline so the next line of the template can't be concatenated onto it. Shared
# blocks do not reference other shared blocks today; if they ever do, switch to a
# recursive resolver.
_INCLUDED=()
expand_template() {
  local file="$1" out="" line rest pre rel
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
      if [ "${#_INCLUDED[@]}" -gt 0 ]; then
        for f in "${_INCLUDED[@]}"; do
          if [ "$f" = "$incfile" ]; then seen=1; break; fi
        done
      fi
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
    # Test the length, not "${_INCLUDED[@]:-}": that form expands an empty array to one
    # empty string, and a prompt with no {{@}} includes (the arbitrator has none) would
    # `cat ""` and abort the resolve under `set -e`.
    if [ "${#_INCLUDED[@]}" -gt 0 ]; then
      for f in "${_INCLUDED[@]}"; do cat "$f"; done
    fi
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
  text="${text//\{\{AUDITORS\}\}/$AUDITOR_LIST}"
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
emit_prompt "auditor" "$AUDITOR_NAME"
emit_prompt "arbiter" "$ARBITER_NAME"

# Emitted as compact JSON so the workflow can hand it directly to a matrix `include`.
write_output "auditors" "$(jq -c '.auditors' "$REGISTRY_FILE")"
write_output "auditor_count" "${#AUDITORS[@]}"

echo "Resolved v$MAP_VERSION prompts: codex=$CODEX_NAME auditor=$AUDITOR_NAME arbiter=$ARBITER_NAME panel=[$AUDITOR_LIST]"
