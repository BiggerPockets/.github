#!/usr/bin/env bash
# Download what import_run.py reads about one gym run into $RUN_DIR: the run, its jobs,
# the plan job's log and each replay's artifact. Nothing downloaded is printed, because
# the artifacts quote the private code under review.
#
# Env: GH_TOKEN, GITHUB_REPOSITORY, RUN_ID, RUN_DIR.
set -euo pipefail

runs="repos/$GITHUB_REPOSITORY/actions/runs/$RUN_ID"
mkdir -p "$RUN_DIR/artifacts"

gh api "$runs" > "$RUN_DIR/run.json"
gh api --paginate "$runs/jobs?per_page=100" --jq '.jobs[]' | jq -s . > "$RUN_DIR/jobs.json"

plan_job=$(jq -r '.[] | select(.name == "plan") | .id' "$RUN_DIR/jobs.json")
[ -n "$plan_job" ] || { echo "::error::run $RUN_ID has no plan job"; exit 1; }
gh api --allow-escape-sequences "repos/$GITHUB_REPOSITORY/actions/jobs/$plan_job/logs" \
  > "$RUN_DIR/plan.log"

gh run download "$RUN_ID" --repo "$GITHUB_REPOSITORY" --dir "$RUN_DIR/artifacts" \
  --pattern 'gym-*--*'
artifacts=("$RUN_DIR"/artifacts/*/)
echo "Fetched ${#artifacts[@]} replay artifacts from run $RUN_ID."
