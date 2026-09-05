When a value crosses the boundary from a DB column into a serializer, into a TypeScript
API type, and into a component, verify its actual nullability at the source rather than
trusting a type further down the chain:
- Check `db/schema.rb` for the column backing any field a serializer passes through
  (`status: parent.status`, `foo: record.foo`, etc.). A column without `null: false` can
  hold NULL in production even if every factory in the test suite sets a value, and the
  serializer must handle that case (a default, an explicit null branch, or a documented
  reason the column can never actually be null despite the schema).
- Treat an unchecked TypeScript `as` assertion narrowing a nullable value to a non-null
  type (e.g. `status: raw.status as string` where the API can return `null`) as a finding.
  The assertion must be justified against the actual schema/serializer, not just against
  the shape the author expects the API to return. Flag it and ask for either a real type
  (`string | null`) with the component handling `null`, or a comment citing why the source
  column is guaranteed non-null.
- This applies most sharply to a new frontend page/component reading a field for the first
  time — a nullable column that has been silently null in production for years only
  becomes a crash once something finally calls a method on it (`.charAt`, `.toUpperCase`,
  etc.) without a null check.

Separately, weigh blast radius when a diff adds a call that can throw inside a React
component subtree sitting under an error boundary (React Router's route error boundary,
or any explicit `ErrorBoundary`): an uncaught throw there takes down everything the
boundary covers, not just the one card or row that failed. Flag a new, unguarded method
call on a value that can be null/undefined (per the point above) more strongly when it
sits in a shared list/page component rather than in an isolated leaf that already has its
own boundary or fallback.

Don't flag routine null-safe code (optional chaining, a default, a type that already
includes `null`/`undefined` and is handled), and don't demand `string | null` for a value
backed by a `null: false` column — the point is to check the real nullability, not to add
a null case to everything reflexively.
