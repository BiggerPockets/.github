A rename or deletion is only safe when every name it changes lives in the codebase. Some
names are written down elsewhere — in Redis, in a database column, in a third party's
records — by code that is already running, and are read back by the new code after the
deploy. Renaming one of those without leaving the old name resolvable strands the data
that still uses it, and the failure lands after merge, on production, with nothing in the
diff pointing at it.

When the diff renames, moves, or deletes a class, module, constant, or symbol, work out
whether its old name is persisted anywhere and read back. Flag it when it is, and say what
holds the stale name. The usual sources:

- A Sidekiq or ActiveJob class. The queue and the retry/scheduled sets store the class as a
  string; a job enqueued before the deploy raises NameError on constantize and keeps
  retrying for weeks. Watch for a job registered on a cron schedule, whose queue is rarely
  empty. A subclass of the renamed job left behind under the old name, or an alias
  constant, resolves it; either way the diff should say when it can be removed.
- A maintenance task (`app/tasks/maintenance/`). `maintenance_tasks_runs` stores the task
  name, so a paused or errored run cannot be resumed or even rendered after the rename.
- An STI `type` column, a serialized or polymorphic `*_type` column, a GlobalID, or an
  `ActiveHash` id: every existing row keeps the old string.
- A Flipper feature key, a cache key, a Redis key, or an experiment/variant name. Renaming
  the key silently resets the value — the flag reads false for everyone, the cache misses,
  the experiment reassigns its buckets — rather than raising.
- A key or value the app has already sent to a third party and later reads back, such as
  Stripe subscription metadata or a webhook event name.

Renaming the Ruby constant while deliberately leaving the persisted name alone is the
correct move, not an inconsistency: a table name, a column name, a metadata key, or a
route path that stays put while the class around it is renamed needs no finding.
