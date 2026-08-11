Synthesize a single review decision for pull request #{{PR}}.

1. Read ticket.json. If "available" is true, treat its summary, description, and
   acceptance_criteria (Atlassian Document Format JSON) as the intended behavior.
2. Read Codex's first-pass findings in codex-findings.md (if missing or empty, proceed
   with your own review).
3. Read conversations.json: the PR's existing discussion (issue_comments, review_comments
   with file/line, and prior reviews). Factor this context into your review: respect
   decisions already settled in the thread, don't re-raise concerns the author has already
   addressed or that were explicitly accepted, and weigh unresolved objections raised by
   human reviewers.
4. Independently review the diff in pr.diff. Do NOT rely on the hunks alone: for changed
   functions, methods, or signatures, grep for callers and related tests to judge impact.
   When the diff adds or changes image assets, check that they are provided at retina
   quality (e.g. a 2x asset or an `srcset`/`@2x` variant); flag raster images that ship
   only at 1x where a high-DPI version is expected. {{@prompts/_shared/migration-data-rule.md}}
5. Enforce these BiggerPockets member-privacy rules and treat a genuine violation as a
   blocking issue:
   {{@prompts/_shared/privacy-rules.md}}
6. Check for batch/task performance hot spots per these rules — an N+1 or O(n^2)+
   pattern on a full-table #process/#collection is a genuine finding, not a nitpick;
   flag it with a suggested fix:
   {{@prompts/_shared/perf-rules.md}}
7. Validate which of Codex's findings are real (discard false positives), add any genuine
   issues Codex missed, and (when a ticket is available) check for missed acceptance
   criteria or scope creep.
8. Decide ONE verdict. Be pragmatic: use "request_changes" only when there is at least one
   genuine, blocking issue; otherwise "approve".

Write a file named verdict.json in the current working directory with EXACTLY this shape:
  {"verdict": "approve" | "request_changes", "summary": "<concise markdown>"}
In the summary, note which of Codex's findings you confirmed, anything you added, how the
change measures against the ticket, how the existing PR discussion informed your decision,
and the rationale for the decision.