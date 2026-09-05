Synthesize a single review decision for pull request #{{PR}}.

1. Read ticket.json. If "available" is true, treat its summary and description as the
   intended behavior, and its acceptance_criteria (Atlassian Document Format JSON) as
   GUIDELINES rather than a hard contract. The AC describe the minimum the ticket set out to
   achieve, not the ceiling on what counts as correct: don't reject a change merely for not
   matching them verbatim. Judge whether the change satisfies the ticket's intent, and weigh
   git history and the PR discussion (steps 3-4) as more authoritative evidence of what was
   actually meant.
2. Read Codex's first-pass findings in codex-findings.md (if missing or empty, proceed
   with your own review).
3. Read conversations.json: the PR's existing discussion (issue_comments, review_comments
   with file/line, and prior reviews). Factor this context into your review: respect
   decisions already settled in the thread, don't re-raise concerns the author has already
   addressed or that were explicitly accepted, and weigh unresolved objections raised by
   human reviewers. Treat the conversation as more reliable than the ticket's literal
   acceptance criteria: if the thread shows the work was refined or explicitly accepted
   beyond the AC in a sound way, honor that.
4. Independently review the diff in pr.diff. Do NOT rely on the hunks alone: use `git log` /
   `git show` on the branch to inspect commit history and grep for callers and related tests
   to judge impact.
   When the diff adds or changes image assets, check that they are provided at retina
   quality (e.g. a 2x asset or an `srcset`/`@2x` variant); flag raster images that ship
   only at 1x where a high-DPI version is expected. {{@prompts/_shared/migration-data-rule.md}}
5. Enforce task completeness and reject incomplete tasks, half-measures, placeholders, or
   deferred work per these rules as blocking issues:
   {{@prompts/_shared/completeness-rules.md}}
6. Enforce these BiggerPockets member-privacy rules and treat a genuine violation as a
   blocking issue:
   {{@prompts/_shared/privacy-rules.md}}
7. Check for batch/task performance hot spots per these rules — an N+1 or O(n^2)+
   pattern on a full-table #process/#collection is a genuine finding, not a nitpick;
   flag it with a suggested fix:
   {{@prompts/_shared/perf-rules.md}}
8. Check how the diff parses structured values, per these rules:
   {{@prompts/_shared/parsing-rules.md}}
9. Check that in-app navigational links use React Router's Link rather than a raw `<a>`
   tag, per these rules:
   {{@prompts/_shared/navigation-rules.md}}
10. Check whether the diff renames, moves, or deletes a name that is persisted outside
   the codebase and read back after deploy, per these rules:
   {{@prompts/_shared/rename-compatibility-rules.md}}
11. Check how the diff carries a value across a DB column / serializer / TypeScript API
   type / component boundary, per these rules:
   {{@prompts/_shared/nullability-boundary-rules.md}}
12. Validate which of Codex's findings are real (discard false positives), add any genuine
   issues Codex missed, and (when a ticket is available) judge genuine misses of the ticket's
   intent or clear scope creep — but give credit when the author went beyond the literal
   acceptance criteria in a sound way rather than flagging it as non-compliant.
13. Decide ONE verdict. Be pragmatic: use "request_changes" only when there is at least one
   genuine, blocking issue (such as a bug, regression, privacy violation, half-finished task,
   placeholder, or deferred work); otherwise "approve". A change that exceeds the AC without
   breaking the ticket's intent is a reason to approve, not to block.

Write a file named verdict.json in the current working directory with EXACTLY this shape:
  {"verdict": "approve" | "request_changes", "summary": "<concise markdown>"}

Write the summary in a THESIS-FIRST structure:

- OPEN with a 1-3 sentence thesis paragraph stating the verdict and the single most
  important reason, e.g. "Requesting changes: the schema migration also updates data and
  should be a one-off rake task — see details below."
- Follow with ONE short supporting paragraph per finding. Each paragraph explains, in a
  sentence or two: the file/line, why it matters, and the suggested direction. If there is
  nothing to change, say so in the thesis paragraph and briefly note you have no blocking
  findings.
- Cover the same substance a normal review would (which Codex findings you confirmed,
  anything you added, how the change measures against the ticket, how the existing PR
  discussion informed your decision) but prioritized and compressed: the thesis first, then
  details. Target roughly 150-200 words total, always leading with the verdict.