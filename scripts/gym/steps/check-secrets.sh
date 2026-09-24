#!/usr/bin/env bash
# Fail, naming each one, unless every secret named as an argument is set.
#
# The gym runs in this repository, and a reusable workflow's secrets resolve from its
# caller, so the gym needs its own copies of the review credentials. Checked before
# anything else because the alternative is every replay failing minutes later on an
# opaque credential error.
set -euo pipefail

missing=()
for name in "$@"; do
  [ -n "${!name:-}" ] || missing+=("$name")
done

if [ ${#missing[@]} -eq 0 ]; then
  echo "All required secrets are present."
  exit 0
fi

echo "::error::Missing secrets on BiggerPockets/.github: ${missing[*]}"
echo "Add them with:"
for name in "${missing[@]}"; do
  echo "  gh secret set $name --repo BiggerPockets/.github"
done
echo "Without JIRA_EMAIL/JIRA_API_TOKEN every replay degrades to a diff-only review, which"
echo "is not comparable to the recorded findings and understates recall."
exit 1
