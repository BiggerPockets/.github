A job that `include ActiveJob::Continuable` and defines a `step` must carry a cursor a
resume can actually use. Treat a step without one as a blocking issue and request changes:

- A `step` that iterates a collection but only calls `step.checkpoint!` with no cursor.
  Resuming reruns the step from the start, so the continuation records no progress.
- A cursor that's set but never read back when the step starts — e.g. `find_each` without
  `start: step.cursor`, or an API listing that ignores `step.cursor`. Resuming redoes the
  whole walk instead of picking up where it left off.
- Collecting the entire collection up front (`.to_a`, `auto_paging_each.select`, `flat_map`
  over all pages, etc.) before any checkpoint, so the expensive part of the job is never
  checkpointed.
- A cursor that isn't serializable or isn't monotonic.

What correct looks like: `find_each(start: step.cursor) { |r| ...; step.advance!(from: r.id) }`
for ActiveRecord (`start:` is inclusive, so advance past the id). For an external API, a
cursor the next request can page from — e.g. a Stripe `created` timestamp used as
`created: { lt: step.cursor || cutoff }` — set with `step.set!` after each item. Expect a
resume test using `ActiveJob::Continuation::TestHelper#interrupt_job_during_step` with a
real `cursor:` to cover it.
