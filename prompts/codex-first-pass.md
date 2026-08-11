Perform a first-pass code review of this pull request and output a concise
markdown findings report. Your final message IS the report — it is captured
verbatim and handed to a second reviewer, so do not add conversational preamble.

1. Read ticket.json. If "available" is true, treat its summary and acceptance_criteria
   as the intended behavior: flag missed requirements and scope creep alongside the
   standard diff analysis. If it is not available, do a diff-based review.
2. Review the diff in pr.diff. Do NOT rely on the hunks alone: for changed functions,
   methods, or signatures, grep the repository for callers and related tests to judge
   impact.
3. Enforce these BiggerPockets member-privacy rules and flag any violation with a
   file/line reference:
   {{@prompts/_shared/privacy-rules.md}}
4. {{@prompts/_shared/migration-data-rule.md}}
5. Check the diff for batch/task performance hot spots per these rules, and flag each
   genuine one with a file/line reference and a suggested fix:
   {{@prompts/_shared/perf-rules.md}}
6. Report concrete issues — bugs, regressions, security problems, member-privacy
   violations, and (when a ticket is available) missed acceptance criteria or scope
   creep — each with a file/line reference and a brief rationale. If nothing is blocking,
   say so briefly.