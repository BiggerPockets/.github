#!/usr/bin/env bash
# publish.sh — publish one scored eval case to Datadog.
#
# Usage: DD_API_KEY=... evals/publish.sh <case.json> <verdict.json> <review-meta.json> <0|1>
#   the final argument is the score: 1 when the case passed, 0 when it failed.
#
# Publishes the result twice, on purpose, because the two answer different questions:
#
#   1. An LLM Obs evaluation metric joined to the review's Claude span. This puts the
#      case result on the same trace as the review that produced it, so a case can be
#      read next to the summary it scored and grouped by prompt_version alongside every
#      other review the pipeline reports.
#   2. A custom metric series, biggiepockets.review_eval.case_passed. This is what a
#      dashboard or a monitor can query — evaluation metrics are not metrics and cannot
#      be alerted on.
#
# Failures warn rather than fail the run: a publishing outage should not turn a passing
# eval set red. Each request's HTTP status and body are printed, because a silently
# dropped submission looks exactly like a submission nobody made.
#
# The span join needs span_id in DECIMAL (trace_id may be decimal or 32-char hex), while
# the review workflow allocates both as hex. Converting is the whole reason this script
# is not two inline curls.

set -euo pipefail

CASE_FILE="${1:?usage: publish.sh <case.json> <verdict.json> <review-meta.json> <0|1>}"
VERDICT_FILE="${2:?missing verdict.json}"
META_FILE="${3:?missing review-meta.json}"
PASSED="${4:?missing score (0|1)}"

DD_SITE="${DD_SITE:-datadoghq.com}"

if [ -z "${DD_API_KEY:-}" ]; then
  echo "Skipping Datadog publish: DD_API_KEY not set"
  exit 0
fi

CASE_ID=$(jq -r '.id' "$CASE_FILE")
EXPECTED=$(jq -r '.expect_verdict' "$CASE_FILE")
ACTUAL=$(jq -r '.verdict // "none"' "$VERDICT_FILE")

# A review that died before writing review-meta.json has no span to join to. The custom
# metric still goes out — a case that could not run is a result worth graphing — but say
# so rather than posting an eval metric with an invented join key.
if [ -f "$META_FILE" ]; then
  ML_APP=$(jq -r '.ml_app' "$META_FILE")
  ARM=$(jq -r '.arm // "unknown"' "$META_FILE")
  PROMPT_NAME=$(jq -r '.prompt_name // "unknown"' "$META_FILE")
  PROMPT_VERSION=$(jq -r '.prompt_version // "unknown"' "$META_FILE")
  REGISTRY_REF=$(jq -r '.registry_ref // "unknown"' "$META_FILE")
  PR=$(jq -r '.pr' "$META_FILE")
  TRACE_HEX=$(jq -r '.trace_id // empty' "$META_FILE")
  SPAN_HEX=$(jq -r '.claude_span_id // empty' "$META_FILE")
else
  echo "::warning::$CASE_ID has no review-meta.json; publishing the metric without a span join"
  ML_APP="biggiepockets-review"
  ARM="unknown"; PROMPT_NAME="unknown"; PROMPT_VERSION="unknown"
  REGISTRY_REF="${REGISTRY_REF:-unknown}"
  PR=$(jq -r '.pr' "$CASE_FILE")
  TRACE_HEX=""; SPAN_HEX=""
fi

TAGS=(
  "case:$CASE_ID"
  "pr:$PR"
  "expected_verdict:$EXPECTED"
  "actual_verdict:$ACTUAL"
  "arm:$ARM"
  "prompt_name:$PROMPT_NAME"
  "prompt_version:$PROMPT_VERSION"
  "registry_ref:$REGISTRY_REF"
)
tags_json=$(printf '%s\n' "${TAGS[@]}" | jq -R . | jq -s .)

post() { # post <url> <payload> <what>
  local url="$1" payload="$2" what="$3" code
  code=$(curl -sS -o /tmp/dd-publish-body.txt -w '%{http_code}' \
    -X POST "$url" \
    -H "Content-Type: application/json" \
    -H "DD-API-KEY: $DD_API_KEY" \
    -d "$payload") || code="000"
  if [ "$code" = "000" ] || [ "$code" -ge 300 ] 2>/dev/null; then
    echo "::warning::$what failed for $CASE_ID (HTTP $code): $(cat /tmp/dd-publish-body.txt 2>/dev/null)"
    return 0
  fi
  echo "$what accepted for $CASE_ID (HTTP $code)"
}

now_ms=$(( $(date +%s) * 1000 ))
passed_bool=false
[ "$PASSED" = "1" ] && passed_bool=true

if [ -n "$TRACE_HEX" ] && [ -n "$SPAN_HEX" ]; then
  # 64-bit span ids exceed what bash arithmetic can hold, so convert in python.
  span_dec=$(python3 -c "import sys; print(int(sys.argv[1], 16))" "$SPAN_HEX")
  eval_payload=$(jq -n \
    --arg ml_app "$ML_APP" \
    --arg trace_id "$TRACE_HEX" \
    --arg span_id "$span_dec" \
    --arg label_pass "review_eval.case_passed" \
    --arg label_case "review_eval.case" \
    --arg case_id "$CASE_ID" \
    --argjson passed "$passed_bool" \
    --argjson ts "$now_ms" \
    --argjson tags "$tags_json" \
    '{data: {type: "evaluation_metric", attributes: {metrics: [
        {eval_scope: "span",
         join_on: {span: {span_id: $span_id, trace_id: $trace_id}},
         ml_app: $ml_app, timestamp_ms: $ts,
         metric_type: "boolean", label: $label_pass,
         boolean_value: $passed, tags: $tags},
        {eval_scope: "span",
         join_on: {span: {span_id: $span_id, trace_id: $trace_id}},
         ml_app: $ml_app, timestamp_ms: $ts,
         metric_type: "categorical", label: $label_case,
         categorical_value: $case_id, tags: $tags}
      ]}}}')
  post "https://api.$DD_SITE/api/intake/llm-obs/v2/eval-metric" "$eval_payload" "LLM Obs eval metric"
fi

series_payload=$(jq -n \
  --argjson value "$PASSED" \
  --argjson ts "$(date +%s)" \
  --argjson tags "$tags_json" \
  '{series: [{metric: "biggiepockets.review_eval.case_passed",
              type: 3,
              points: [{timestamp: $ts, value: $value}],
              tags: $tags}]}')
post "https://api.$DD_SITE/api/v2/series" "$series_payload" "Custom metric"
