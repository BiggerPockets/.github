#!/usr/bin/env bash
# score.sh — score one review verdict against one eval case.
#
# Usage: evals/score.sh <case.json> <verdict.json>
#
# A case asserts two things about a review of a known pull request:
#   expect_verdict  the verdict the review must reach ("approve" | "request_changes")
#   must_match      rows of acceptable phrasings; the summary must match at least one
#                   entry in EVERY row (rows are ANDed, entries within a row are ORed)
#
# Matching is case-insensitive substring matching, which is deliberately crude: it can
# only tell that a review talked about the right thing, never that its reasoning was
# sound. Keep the entries broad enough that a correct review phrased differently still
# passes, and narrow enough that a review which found a DIFFERENT problem fails. When a
# case cannot be expressed that way, score it on the verdict alone (empty must_match)
# and record why in the case's "notes".
#
# Exits 0 when the case passes, 1 when it fails, 2 on bad input. Prints one line per
# assertion so a failure says which one and what the review said instead.

set -euo pipefail

CASE_FILE="${1:?usage: score.sh <case.json> <verdict.json>}"
VERDICT_FILE="${2:?usage: score.sh <case.json> <verdict.json>}"

for f in "$CASE_FILE" "$VERDICT_FILE"; do
  [ -f "$f" ] || { echo "score: $f not found" >&2; exit 2; }
done

CASE_ID=$(jq -r '.id' "$CASE_FILE")
PR=$(jq -r '.pr' "$CASE_FILE")
EXPECT=$(jq -r '.expect_verdict' "$CASE_FILE")

# A review that crashed leaves no verdict. Treat that as a case failure rather than a
# pass, so an eval run can't come back green because the pipeline never produced output.
GOT=$(jq -r '.verdict // empty' "$VERDICT_FILE")
SUMMARY=$(jq -r '.summary // empty' "$VERDICT_FILE" | tr '[:upper:]' '[:lower:]')
if [ -z "$GOT" ]; then
  echo "FAIL $CASE_ID (pr #$PR): review produced no verdict"
  exit 1
fi

failed=0

if [ "$GOT" = "$EXPECT" ]; then
  echo "  pass  verdict is $GOT"
else
  echo "  FAIL  verdict is $GOT, expected $EXPECT"
  failed=1
fi

rows=$(jq -r '.must_match | length' "$CASE_FILE")
for i in $(seq 0 $((rows - 1))); do
  [ "$rows" -eq 0 ] && break
  matched=""
  while IFS= read -r phrase; do
    lower=$(printf '%s' "$phrase" | tr '[:upper:]' '[:lower:]')
    case "$SUMMARY" in *"$lower"*) matched="$phrase"; break;; esac
  done < <(jq -r --argjson i "$i" '.must_match[$i][]' "$CASE_FILE")
  if [ -n "$matched" ]; then
    echo "  pass  summary mentions '$matched'"
  else
    echo "  FAIL  summary mentions none of: $(jq -r --argjson i "$i" '.must_match[$i] | join(", ")' "$CASE_FILE")"
    failed=1
  fi
done

if [ "$failed" -eq 0 ]; then
  echo "PASS $CASE_ID (pr #$PR)"
else
  echo "FAIL $CASE_ID (pr #$PR)"
  # Echo the summary on a failure only — it is the evidence for why the case failed, and
  # printing it on every case would bury the result lines.
  echo "--- review summary ---"
  jq -r '.summary // "(none)"' "$VERDICT_FILE"
  echo "----------------------"
fi
exit "$failed"
