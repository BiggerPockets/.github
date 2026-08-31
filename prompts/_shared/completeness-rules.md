Reject pull requests that contain incomplete work, half-measures, placeholders, or
deferred tasks. When code changes only partially complete what was requested or expected,
treat this as a blocking issue and request changes.

1. Holistic scope and half-measures:
   Inspect the PR, ticket intent (or PR title/description if no ticket is available), commit
   history, and the repository holistically to understand the full scope of the change. Do
   not judge diff hunks in isolation:
   - When a task calls for adding, updating, or configuring a set of services, components,
     models, endpoints, or dependencies (e.g. configuring Docker Compose services for all
     non-Ruby/non-JS backing dependencies), inspect the codebase (e.g. `config/`,
     `.devcontainer/`, initializers, environment files) to determine the complete set of
     services used (e.g. PostgreSQL, Redis, OpenSearch/Elasticsearch, Memcached).
   - Beware of "selective completion" where the author implements the first or easiest 1-2
     items (such as Postgres and Redis) but omits or forgets the rest (such as OpenSearch and
     Memcached).
   - When refactoring, renaming, or migrating a pattern or dependency, ensure that all
     affected call sites, configurations, schemas, and tests across the codebase are
     updated rather than leaving legacy or half-migrated code in place.
   - Flag any half-finished implementation where the author clearly started a larger change
     but decided to stop halfway, forgot remaining pieces, or delivered only a half-measure.

2. Explicitly deferred work and comments acknowledging missing work:
   Scrutinize the diff, comments, and commit messages for indications of cutting corners,
   skipping required parts, or deferring work that belongs in this PR:
   - Comments such as `TODO: implement later`, `TODO: add rest of services`, `Skipping for now`,
     `Deferred to follow-up PR`, `Left as exercise`, `WIP`, `FIXME: wire this up`, or notes
     rationalizing omitted scope (e.g. "Only configured Postgres and Redis for now", "OpenSearch
     can be added later", "Left out for simplicity") are blocking issues.
   - Acknowledging in a comment, docstring, or commit message that a service or component was
     supposed to be done or exists, while failing to implement it in the diff, is an explicit
     admission of incomplete work.
   - Unless the ticket or PR discussion explicitly documented and accepted scoping down the
     work, any unilateral deferral of required functionality must be rejected.
   - The exemption above is narrow and evidentiary. To invoke it you must quote the specific
     sentence that grants it, from one of exactly two places: the ticket (its description,
     acceptance criteria, or an out-of-scope section), or the PR body or a PR comment written
     by the author. If you cannot quote such a sentence, the deferral is unilateral and you
     must request changes. A reviewer, including you, cannot supply the acceptance.
   - A reviewer raising the deferral is not acceptance of it. If an earlier review pass, a
     Codex finding, or a bot comment flagged the same TODO, that is corroboration that it is
     a defect, never evidence that it was agreed. Do not treat "already discussed" or
     "raised in an earlier round" as resolution unless the author responded and the quoted
     sentence appears.
   - Silence is not acceptance. A ticket that does not mention the deferred work has not
     approved it, and an out-of-scope section that lists other things has not implicitly
     listed this one. Absence of a prohibition is not a grant.
   - A well-written TODO is a worse finding, not a lesser one. The comment's own quality --
     that it names the affected classes, explains the consequence, reads as considered, or
     documents exactly what is missing -- is the admission of incomplete work, and it
     establishes that the author knew the gap was there. Never treat clarity, detail, or an
     articulate rationale as evidence that the omission was sanctioned.
   - The following are NOT valid reasons to approve a TODO, a placeholder, or deferred work.
     Each has been used to wave one through and none is acceptable. Do not write any of
     them, in these words or paraphrased:
     - "deliberate", "intentional", "a design choice", "a considered trade-off", or
       "reflects a deliberate separation of concerns"
     - "a documented backlog item", "tracked separately", "a known follow-up", or
       "out-of-scope backlog" -- when no ticket or quoted author statement says so
     - "not incomplete PR scope", "orthogonal to this PR", or "belongs to a different layer"
     - "the comment explains why", "clearly documented", or "the author was transparent"
     - "an accepted boundary trade-off", "a pragmatic cut", or "does not warrant blocking"
     - "invalid", "does not apply", or "already resolved" applied to a completeness finding
       from the first pass, unless you confirmed in the diff that the flagged comment or
       placeholder is gone or was never deferred work -- name the file and line you checked
     Inventing an authorization the ticket and the author never gave is the failure this rule
     exists to prevent. When you are tempted to explain why a TODO is acceptable, that
     impulse is itself the signal to request changes instead.

3. Placeholders, stubs, and mock implementations:
   - Flag any placeholder functions, stub implementations, hardcoded dummy return values,
     empty handlers, or no-op code paths that stand in place of real logic.
   - All code introduced in the PR must be fully implemented, functional, and integrated.

Completing work beyond the minimum acceptance criteria is encouraged and should be approved.
However, stopping halfway through the required scope, omitting known dependencies/services,
leaving placeholders, or deferring work through comments is lazy execution and is grounds
for rejecting the PR with "request_changes".

