#!/usr/bin/env bash
# Publish the pull request's BIG ticket key as the `key` step output, empty when the title
# carries none. The key lives on the title, not the dataset record. A record without one
# replays as a diff-only review, which is what production does for those pull requests.
#
# Env: GH_TOKEN, REPO, PR.
set -euo pipefail

title=$(gh api "/repos/$REPO/pulls/$PR" --jq .title || true)
key=$(grep -oiE 'BIG-[0-9]+' <<<"$title" | head -1 | tr '[:lower:]' '[:upper:]' || true)
echo "key=$key" >> "$GITHUB_OUTPUT"
echo "Ticket: ${key:-none}"
