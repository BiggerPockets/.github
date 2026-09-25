Flag a new environment variable (or credential) the diff makes mandatory when the diff does
not also give every environment that runs the code a value for it. A fresh checkout, CI, and
review apps must still boot and render pages without anyone adding a secret by hand. Look for:
- A read that raises or breaks when the key is unset: Ruby `ENV.fetch("KEY")` with no
  default, `ENV["KEY"]` passed straight into something that fails on `nil` (an HMAC, a
  client constructor, `URI.parse`), `Rails.application.credentials.key!`, a required-keys
  check, or in JS/TS `process.env.KEY!` or a throw on a missing value.
- Where the read runs. At boot (an initializer, `config/`, a class body, a constant) it
  stops the app from starting. In a layout, shared partial, `before_action`, or a serializer
  or presenter on a common page, it breaks every page that renders it. Either one is blocking.
  A feature flag around the read is not a defense: local development and review apps seed
  shipped flags on, so the read runs there as soon as the flag is fully rolled out.
- Whether the diff supplies a value everywhere the read can run: a placeholder in
  `config/application.yml.sample`, the review app's `app.json` env, and CI or spec setup.
  A spec that sets the key itself in a `before` block proves only that the spec passes.
Suggest one of: a safe default for development and test (a placeholder secret, or a nil
check that skips the integration), raising only in production; or adding the key to the
sample config, `app.json`, and CI in the same diff. Give every finding a file/line reference
for the read and name which environments are missing the key.

Don't flag keys that already exist before the diff, reads that already have a default or
handle `nil`, or code that only runs in a production-only job or rake task.
