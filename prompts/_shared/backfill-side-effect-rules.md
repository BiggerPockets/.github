Find every path in the diff that writes records in bulk outside a member's own action: a
Maintenance Task (`app/tasks/maintenance/`), a rake task, a data migration, an import or
sync job, a one-off script, or a loop in a deploy hook. Also treat as one a code path that
creates a missing record lazily the first time something reads it (on a GET, in a
component or view render, in a serializer, in a `find_or_create_by` behind a read), since
it ends up running once per record just like a backfill.

For each one, trace what its writes actually trigger before judging it:
- Model callbacks on every model it saves, including those from included concerns and
  `after_commit` / `after_create_commit` hooks, `touch: true` and counter caches on
  associations, and any `dependent:` option on a destroy.
- Side effects of the service, builder or `create_*` method it calls instead of saving
  the model directly. A creation service often enqueues jobs or mailers itself.
- What those reach: email, push, in-app notifications, digests, follower or subscriber
  alerts, activity-feed entries, webhooks, third-party API calls, search indexing,
  embeddings, and audit or version rows.

Then require that the code states its intent. A comment on the task class, or at the write
call, must say whether the backfill should run those side effects, naming the ones that
matter (e.g. "Opens each topic as history: no follower alerts, no activity-feed entry;
search indexing still runs"). The code must match the comment:
- Saves that run callbacks (`save`, `create`, `update`, a service call) must show how the
  unwanted side effects are suppressed: a flag the callbacks honor, a dedicated
  silent/historical path, or a write that bypasses callbacks.
- Writes that skip callbacks (`update_column(s)`, `update_all`, `insert_all`,
  `upsert_all`, `delete_all`, raw SQL) must say which skipped effects don't matter, or
  restore the ones that do (reindex, cache bust, counter reset) explicitly.
- A spec should pin the stated intent, e.g. that running the task enqueues no mail or
  notification job, or that it does reindex.

Treat it as blocking when the write reaches a member-facing side effect (email, push,
notification, alert, feed entry, webhook) and nothing states whether that was intended,
or when the stated intent and the code disagree. A backfill touches every row it selects,
and a notification it sends cannot be recalled. For a write that reaches only internal
effects (indexing, caches, counters, audit rows) and states no intent, report it as
non-blocking.

When the diff adds a lazy "create it if it's missing" path, also ask what creates those
records ahead of time and whether that backfill covers every record the new path will
reach, not just a subset (e.g. only the rows with existing data to import). Rows the
backfill misses will be created by the lazy path, with whatever side effects it runs, at
the rate readers and crawlers hit them. Writes on a GET or during a render are a finding
in their own right: prefer rendering nothing, or enqueueing the creation, over writing in
the read path.

Be defensible: every finding must name the specific callback, job, mailer or method you
traced, with its file and line, and the write in the diff that reaches it. Don't flag a
write whose models you traced and found to have no such callbacks, a task that already
states its intent and matches it, or a member's own single action.
