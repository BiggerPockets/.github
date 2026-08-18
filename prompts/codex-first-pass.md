Perform a first-pass code review of this pull request and output a concise
markdown findings report. Your final message IS the report — it is captured
verbatim and handed to a second reviewer, so do not add conversational preamble.

1. Read ticket.json. If "available" is true, treat its summary as the intended behavior and
   its acceptance_criteria as GUIDELINES, not a hard contract — they describe the minimum the
   ticket set out to achieve, not the ceiling on what counts as correct. Do not flag a change
   just because it doesn't match the acceptance criteria verbatim: first judge whether the
   change actually satisfies the ticket's intent, and give particular credit when the author
   went beyond the literal criteria (see step 2 for how to weigh that against history and
   context). Only treat a deviation as a blocker if it genuinely fails the ticket's purpose.
   If the ticket is not available, do a diff-based review.
2. Review the diff in pr.diff. Do NOT rely on the hunks alone: use `git log` / `git show` on
   the branch and grep the repository for callers and related tests to judge impact and
   intent. Git history and the PR's context are usually more reliable evidence of what was
   meant than the ticket's literal acceptance criteria: when a commit, the diff, or the
   conversation shows the work was refined or extended beyond the AC in a sound way, treat
   that as an improvement rather than scope creep.
3. Enforce these BiggerPockets member-privacy rules and flag any violation with a
   file/line reference:
   {{@prompts/_shared/privacy-rules.md}}
4. {{@prompts/_shared/migration-data-rule.md}}
5. Check the diff for batch/task performance hot spots per these rules, and flag each
   genuine one with a file/line reference and a suggested fix:
   {{@prompts/_shared/perf-rules.md}}
6. Check how the diff parses structured values, per these rules:
   {{@prompts/_shared/parsing-rules.md}}
7. Report concrete issues — bugs, regressions, security problems, member-privacy
   violations, and genuine misses of the ticket's intent or clear scope creep — each with a
   file/line reference and a brief rationale. Do not list "doesn't match acceptance criteria"
   as an issue by itself; only raise it when the deviation harms the intent. If nothing is
   blocking, say so briefly.