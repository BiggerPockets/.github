Explicitly look for performance hot spots introduced or worsened by the diff and flag each
genuine one with a file/line reference and a suggested fix. This is a finding class the
reviewer always checks for, not a blanket ban on loops:
- N+1 queries: a DB query issued inside a loop — exists?, find, where, or any query call
  per iteration instead of a batched/preloaded lookup, e.g. issuing a fresh
  SimplifiedForums::Location.exists? for every candidate while iterating a location list.
  Suggest the fix: batch into a single query, preload associations, or add an index on the
  lookup column.
- O(n^2)+ nested iteration: an inner loop re-scanning the same in-memory collection for
  each element of an outer loop — e.g. a method that re-scans the full list per candidate
  and is called per row processed. Suggest restructuring to a hash/index lookup before
  looping.
Pay particular attention to Maintenance Task #process and #collection implementations (and
workers/jobs in general), which run over full tables where these costs compound at scale.
Don't block a PR merely for touching a loop: only flag hot paths that run per-element/
per-row over data that grows with production, and always pair the finding with a concrete
fix.