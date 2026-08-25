Reject pull requests that contain incomplete work, half-measures, placeholders, or
deferred tasks. When code changes only partially complete what was requested or expected,
treat this as a blocking issue and request changes.

1. Holistic scope and half-measures:
   Inspect the PR, ticket intent, commit history, and the repository holistically to
   understand the full scope of the change. Do not judge diff hunks in isolation:
   - When a task calls for adding, updating, or configuring a set of services, components,
     models, endpoints, or files (e.g. configuring Docker Compose services for all non-Ruby/
     non-JS dependencies), verify that ALL required services/components are implemented
     (e.g. PostgreSQL, Redis, OpenSearch, Memcached), not just an easy subset.
   - When refactoring, renaming, or migrating a pattern or dependency, ensure that all
     affected call sites, configurations, schemas, and tests across the codebase are
     updated rather than leaving legacy or half-migrated code in place.
   - Flag any half-finished implementation where the author clearly started a larger change
     but decided to stop halfway, forgot remaining pieces, or delivered only a half-measure.

2. Explicitly deferred work and TODO comments:
   Scrutinize the diff and commit history for comments, annotations, or commit messages
   that indicate cutting corners or deferring work that belongs in this PR:
   - Comments such as `TODO: implement later`, `TODO: add rest of services`, `Skipping for now`,
     `Deferred to follow-up PR`, `Left as exercise`, `WIP`, `FIXME: wire this up`, or similar
     deferrals are blocking issues.
   - Unless the ticket or PR discussion explicitly documented and accepted scoping down the
     work, any unilateral deferral of required functionality must be rejected.

3. Placeholders, stubs, and mock implementations:
   - Flag any placeholder functions, stub implementations, hardcoded dummy return values,
     empty handlers, or no-op code paths that stand in place of real logic.
   - All code introduced in the PR must be fully implemented, functional, and integrated.

Completing work beyond the minimum acceptance criteria is encouraged and should be approved.
However, stopping halfway through the required scope, omitting known dependencies/services,
leaving placeholders, or deferring work through comments is lazy execution and is grounds
for rejecting the PR with "request_changes".

