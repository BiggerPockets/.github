Check whether the diff does unbounded work inside a web request that belongs in a
background job, and flag each genuine case with a file/line reference and a suggested fix.
The test is not "is this slow" but "does the cost grow with something the request does not
control": rows in an uploaded file, records in a collection, items a client sent, matches
from a query. A controller action, service, or serializer that loops over that and does
real work per element — a save, an HTTP call, a mailer, a model callback, a re-read of a
file — is a finding.
- Per-element writes in a request: creating/updating a record per row or per item, each
  firing its own callbacks. Callbacks compound the cost invisibly, so read what the model
  does on create before judging a loop cheap. Suggest moving the work to an ActiveJob and
  answering 202 with something the client can poll.
- Per-element external calls in a request: an HTTP request, upload, or mail send per
  element. Suggest a job, and batching where the vendor supports it.
- Re-doing expensive setup per request in a multi-request flow: re-parsing an uploaded
  file or re-validating every row on a poll or a second step, when the first pass could
  have recorded what the later one needs.
Two things a request-to-job move must get right, and both are findings when missing:
- **Progress the member can see.** Work that was inline was at least bounded by the
  request; once it is a job, a member left on a spinner cannot tell "running" from
  "broken". Expect determinate progress — a count or percentage the server records as it
  goes and an endpoint or channel that reports it — not an indeterminate spinner, and not
  a client-side timer guessing. A job that can also fail terminally needs a state the
  poller can read, or the progress bar simply stops moving.
- **Idempotence and resume.** A job is retried (a deploy SIGTERM is the common case), so
  work that is not repeatable — anything that creates records or sends mail — must record
  what it has finished with and skip it on a re-run. Flag a retried job that would redo
  its side effects, and flag `retry_on` on a job whose `perform` is not safe to run twice.
Don't flag work that is genuinely bounded and small (a fixed handful of records, a single
save, a constant number of calls regardless of input), a Maintenance Task or job that is
already off the request path, or a synchronous call the request's own response
legitimately depends on. Always pair the finding with the concrete fix: which loop to
move, what the poll endpoint should answer, and what the job should record to be safe to
retry.
