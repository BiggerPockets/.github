Flag unbounded work inside a web request when cost grows with uploaded rows, collection
records, client items, query matches, or other uncontrolled input. Look for:
- Per-element writes and callbacks. Check create/update callbacks before treating a loop
  as cheap. Suggest an ActiveJob and 202 response with a pollable resource.
- Per-element HTTP requests, uploads, mail, or other external calls. Suggest a job and
  vendor batching where available.
- Expensive setup repeated across a multi-request flow, such as re-parsing an upload or
  re-validating every row during polling. Record the first pass's results.

Also flag background-job moves missing either requirement:
- **Server-recorded progress and failure.** Require a determinate count or percentage
  exposed through an endpoint or channel, not a spinner or client timer. The poller must
  also be able to read a terminal failure state.
- **Idempotence and resume.** Retried jobs must record completed work and skip it on
  re-run when effects are not repeatable. Flag repeated side effects and `retry_on` when
  `perform` is unsafe to run twice.

Don't flag small, bounded work (a fixed handful of records, one save, or constant calls),
Maintenance Tasks or jobs already off the request path, or synchronous work the response
depends on. Give every finding a file/line reference and concrete fix: the loop to move,
poll response, and state the job must record for safe retries.
