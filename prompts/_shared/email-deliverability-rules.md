When the diff lets an email address be added — a form field, an API endpoint, an
admin/console path, an import, a mailing list signup, or any other place that accepts
an address for later use — the code must verify the address is deliverable with an MX
record lookup on the address's domain before the address is accepted (or before anything
is queued that will later send to it). Treat accepting an email address without that
check as a blocking finding, and flag it with the file/line and the suggested check.

- MX lookup: query the domain's MX records (e.g. Ruby's `Resolv::DNS`, or whatever
  resolver the app already uses) and reject the address when the domain has no MX record
  — that domain runs no mail server, so no message to it can ever be delivered. An MX
  lookup only checks that the domain is set up to receive mail; it must not block on the
  mailbox existing and must not send a test message to the address.
- Don't block on resolver choice: the point is that the domain's mail servers were
  actually checked before the address is accepted. Whether the lookup lives in the model
  (validation), a service object, or a job matters less than that it happens at all.
- Watch for lookups that never run: a check that only runs on a happy path, is gated
  behind a flag that defaults off, swallows errors into a nil-safe `rescue` that accepts
  the address anyway, or verifies format only (a regex for `name@domain`) does NOT satisfy
  this rule. If the add succeeds when the lookup fails or is skipped, flag it.

The ONE exception is logging-only collection: when the code records an address solely
because it observed that the address exists (e.g. stashing an email surfaced in a third
party's payload, or noting it for the record) and nothing in the change — or in any code
that will consume what this change stores — intends to send an actual message to that
address, the MX lookup is not required, and don't flag the absence of one.
