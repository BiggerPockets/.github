When the diff renames, moves, or deletes a class, module, constant, or symbol, check
whether its old name is persisted somewhere and read back after deploy — code already
running wrote that name down, and the new code cannot resolve it. Flag it, say what holds
the stale name, and ask for a temporary alias or subclass with a note on when to remove
it. Where the old name survives:

- Sidekiq/ActiveJob queues, retry and scheduled sets, and cron registrations.
- `maintenance_tasks_runs`, for a task under `app/tasks/maintenance/`.
- STI `type`, serialized or polymorphic `*_type`, GlobalIDs, and ActiveHash ids.
- Flipper feature keys, cache keys, Redis keys, and experiment/variant names.
- Keys or values sent to a third party and later read back, such as Stripe metadata or a
  webhook event name.

Renaming only the Ruby constant while deliberately leaving the persisted name alone — a
table, a column, a metadata key, a route path — is correct, and is not a finding.
