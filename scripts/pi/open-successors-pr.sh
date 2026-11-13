#!/usr/bin/env bash
# Commit the models.json entries model_successors.py added and open (or refresh) one
# pull request for them on a fixed branch, so a week that finds the same releases
# updates that PR instead of opening another.
#
# Usage: open-successors-pr.sh <successors.json>
set -euo pipefail

result="$1"
branch="model-catalog-successors"

error=$(jq -r '.error // empty' "$result")
if [ -n "$error" ]; then
  echo "::warning::model_successors.py: $error"
fi
if [ "$(jq '.successors | length' "$result")" -eq 0 ]; then
  echo "Every pinned model is its family's newest release."
  exit 0
fi
if git diff --quiet -- scripts/pi/models.json; then
  echo "::warning::Successors were found but models.json is unchanged."
  exit 0
fi

git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"
git switch -C "$branch"
git add scripts/pi/models.json
git commit -m "Add newer releases of pinned models to the pi catalog"
git push --force origin "$branch"

body=$(mktemp)
{
  echo "The weekly model check found newer releases of models pinned in \`scripts/pi/models.json\` and adds them to it."
  echo
  echo "| Pinned | Newer release | Released | \$/1M in → out (pinned) | \$/1M in → out (newer) |"
  echo "| --- | --- | --- | --- | --- |"
  jq -r '.successors[] | "| `\(.pinned)` | `\(.successor)` | \(.released // "?") | \(.pinned_rates.input // "?") → \(.pinned_rates.output // "?") | \(.successor_rates.input // "?") → \(.successor_rates.output // "?") |"' "$result"
  echo
  echo "Listing a model changes no review: each stage still runs the model its workflow input or Actions variable names. Moving a stage onto one of these is a separate change, made once someone has checked it reviews as well."
  echo
  echo "Opened by the \`Weekly pi model freshness check\` workflow. GitHub doesn't run CI on pull requests a workflow opens, so close and reopen this PR to run the tests."
} > "$body"

existing=$(gh pr list --head "$branch" --state open --json number --jq '.[0].number')
if [ -n "$existing" ]; then
  gh pr edit "$existing" --body-file "$body"
else
  gh pr create --head "$branch" --title "Add newer releases of pinned models to the pi catalog" --body-file "$body"
fi
