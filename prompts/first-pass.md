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
3. Enforce task completeness and reject incomplete tasks, half-measures, placeholders, or
   deferred work per these rules as blocking issues:
   {{@prompts/_shared/completeness-rules.md}}
4. Enforce these BiggerPockets member-privacy rules and flag any violation with a
   file/line reference:
   {{@prompts/_shared/privacy-rules.md}}
5. Enforce the email-deliverability rules and flag any violation with a
   file/line reference:
   {{@prompts/_shared/email-deliverability-rules.md}}
6. {{@prompts/_shared/migration-data-rule.md}}
7. Check the diff for batch/task performance hot spots per these rules, and flag each
   genuine one with a file/line reference and a suggested fix:
   {{@prompts/_shared/perf-rules.md}}
8. Check whether the diff does per-element work inside a web request that belongs in a
   background job, and whether a job it adds reports progress and is safe to retry:
   {{@prompts/_shared/blocking-request-rules.md}}
9. Check how the diff parses structured values, per these rules:
   {{@prompts/_shared/parsing-rules.md}}
10. Check that in-app navigational links use React Router's Link rather than a raw `<a>`
   tag, per these rules:
   {{@prompts/_shared/navigation-rules.md}}
11. Check whether the diff renames, moves, or deletes a name that is persisted outside
   the codebase and read back after deploy, per these rules:
   {{@prompts/_shared/rename-compatibility-rules.md}}
12. Check whether the diff makes a new environment variable mandatory without giving
   development, CI, and review apps a value for it, per these rules:
   {{@prompts/_shared/env-var-rules.md}}
13. Check that every URL path the diff adds or changes is reachable through the layers in
   front of its handler — nginx, Rack middleware, route order — per these rules:
   {{@prompts/_shared/route-reachability-rules.md}}
14. When the diff publishes or changes an interface an outside caller drives, check it
   against these rules:
   {{@prompts/_shared/interface-contract-rules.md}}
15. Judge the value of the specs the diff adds or changes, per these rules, and report a
   useless spec with a file/line reference and the assertion that would make it fail:
   {{@prompts/_shared/spec-value-rules.md}}
16. Report concrete issues — bugs, regressions, security problems, member-privacy
   violations, incomplete tasks/half-measures/placeholders/deferred work, specs that cannot
   fail usefully, and genuine misses of the ticket's intent or clear scope creep — each with
   a file/line reference and a brief rationale. Do not list "doesn't match acceptance criteria" as an issue by itself; only
   raise it when the deviation harms the intent. If nothing is blocking, say so briefly.
17. End the report with every finding listed again as one fenced `findings` block of JSON, in
   the same order as the prose. It is read by machine, so it must parse and hold exactly the
   findings above:

   ```findings
   [{"severity": "blocking",
     "category": "correctness",
     "locations": [{"path": "app/models/user.rb", "start_line": 42, "end_line": 48}],
     "summary": "One sentence naming the defect."}]
   ```

   - `severity`: `blocker`, `blocking` or `non-blocking`.
   - `category`: the one that names the defect. When a numbered step above is the reason
     for the finding, use that step's category:
     - `completeness` (step 3), `privacy` (4), `email` (5), `data` (6), `performance`
       (7 and 8), `parsing` (9), `navigation` (10), `compatibility` (11), `configuration`
       (12), `routing` (13), `interface` (14), `tests` (15).
     - Otherwise `correctness` for a wrong result, crash or regression; `security` for a
       vulnerability; `intent` for a miss of the ticket's intent or scope creep;
       `maintainability` for code that works but will mislead or trap the next change.
   - `locations`: every place the defect lives, as repository-relative paths and the line
     numbers in the pull request's head. When the defect shows in one place and its cause
     is in another, list both. Omit `start_line` and `end_line` only for a defect in a file
     as a whole, such as a file that should exist and does not.
   - `summary`: one sentence, no code.

   With no findings, end with an empty block: `[]`.
