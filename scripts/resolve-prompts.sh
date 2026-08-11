#!/usr/bin/env bash
# resolve-prompts.sh — resolve the BiggiePockets review prompts from the registry.
#
# Every review runs BOTH arms of the summary-format experiment as independent Claude
# passes over the same Codex findings. Each arm's prompt is a versioned template in
# prompts/<name>.md; prompts/registry.json names the codex prompt, the arms, and which
# arm supplies the official gate verdict.
#
# Shared rule blocks (privacy, migration-data, perf) are stored once in
# prompts/_shared/ and injected into every prompt that references them via {{@path}}
# markers, so the Codex and Claude prompts can never drift out of sync.
#
# The resolved text substitutes late-binding placeholders used for provenance only:
#   {{PR}}, {{PROMPT_NAME}}, {{PROMPT_VERSION}}, {{ARM}}
#
# prompt_version is DERIVED from content, never hand-set: a hash of the template file
# plus every shared block it includes. It changes if and only if that prompt's text
# changes (a Roll), stays stable across PRs, and never encodes an arm — the arm is
# reported separately so Datadog LLM Obs can hold both (prompt_name/version = source
# of truth for the text; the arm = the test condition).
#
# Inputs (env): REGISTRY_DIR, PR.
# Outputs: codex_prompt{,_name,_version}; gate_arm; one triple per arm:
#   <arm>_prompt, <arm>_prompt_name, <arm>_prompt_version.

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
GATE_ARM=$(jq -r '.gate_arm // empty' "$REGISTRY_FILE")
ARMS=()
while IFS= read -r arm; do ARMS+=("$arm"); done < <(jq -r '.arms | keys[]' "$REGISTRY_FILE")

for name in "$CODEX_NAME" "$GATE_ARM"; do
  if [ -z "$name" ]; then
    echo "::error::registry.json is missing codex_prompt or gate_arm" >&2
    exit 1
  fi
done

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
if ! printf '%s\n' "${ARMS[@]}" | grep -qx -- "$GATE_ARM"; then
  echo "::error::gate_arm '$GATE_ARM' is not a declared arm" >&2
  exit 1
fi

# Expand {{@path}} includes (path relative to REGISTRY_DIR), recording each included
# file on _INCLUDED (de-duplicated). Shared blocks do not nest today; if they ever do,
# switch to a recursive resolver.
_INCLUDED=()
expand_template() {
  local file="$1" out="" line rel incfile f seen
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ "$line" =~ \{\{@([^}]+)\}\} ]]; then
      rel="${BASH_REMATCH[1]}"
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
      out+="$(cat "$incfile")"
    else
      out+="$line"$'\n'
    fi
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

prompt_text() { # name version -> resolved text
  local name="$1" version="$2" text
  _INCLUDED=()
  text="$(expand_template "$REGISTRY_DIR/prompts/$name.md")"
  text="${text//\{\{PR\}\}/$PR}"
  text="${text//\{\{PROMPT_NAME\}\}/$name}"
  text="${text//\{\{PROMPT_VERSION\}\}/$version}"
  text="${text//\{\{ARM\}\}/$name}"
  printf '%s' "$text"
}

write_output() { # name value — multiline-safe via GITHUB_OUTPUT heredoc; fixed
   # delimiter keeps the whole output file byte-for-byte reproducible.
  local name="$1" value="$2"
  printf '%s<<_BIGGIEPOCKETS_PROMPT_EOF_\n%s\n_BIGGIEPOCKETS_PROMPT_EOF_\n' "$name" "$value" >> "$GITHUB_OUTPUT"
}

codex_version="$(prompt_version "$CODEX_NAME")"
codex_text="$(prompt_text "$CODEX_NAME" "$codex_version")"
write_output "codex_prompt" "$codex_text"
write_output "codex_prompt_name" "$CODEX_NAME"
write_output "codex_prompt_version" "$codex_version"

write_output "gate_arm" "$GATE_ARM"

outputs="codex=$CODEX_NAME@$codex_version gate=$GATE_ARM"
for arm in "${ARMS[@]}"; do
  prompt=$(jq -r --arg a "$arm" '.arms[$a]' "$REGISTRY_FILE")
  version="$(prompt_version "$prompt")"
  text="$(prompt_text "$prompt" "$version")"
  # Output names are used in ${{ steps.resolve.outputs.<name> }} GitHub expressions,
  # which cannot carry hyphens, so arm-key hyphens map to underscores.
  outname="${arm//-/_}"
  write_output "${outname}_prompt" "$text"
  write_output "${outname}_prompt_name" "$prompt"
  write_output "${outname}_prompt_version" "$version"
  outputs+=" $arm=$prompt@$version"
done

echo "Resolved $MAP_VERSION prompts: $outputs"