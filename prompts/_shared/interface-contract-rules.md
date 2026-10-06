A change that publishes an interface — an MCP tool's schema and description, an
endpoint's params, a serializer's payload, a webhook contract — is read by a caller who
cannot see the implementation and can only do what the interface tells them. Check the
interface against the implementation, not only the implementation against itself.

1. Every required input must be obtainable through the interface itself:
   - Grep for the values the implementation reads out of the incoming payload, and check
     that the interface's own schema, description, or documented requirements name each
     one. An input the implementation requires but the interface never names makes the
     documented path fail every time it is followed, and the caller has no way to
     discover what is missing.
   - Pay particular attention to values the existing UI supplies out of band: a hidden
     field, a landing-page widget, a session value, an id resolved by an earlier page.
     Those are exactly the ones a new non-browser caller cannot produce, and the ones an
     author working from the browser flow is least likely to notice are missing.
   - When one operation tells callers to take an input from another ("exactly as X
     enumerates them", "pass the id returned by Y"), open that other operation and
     confirm it actually produces it. A cross-reference to something that does not exist
     reads as complete and is not.

2. Failing on a missing or unusable input must name the input:
   - An interface whose only answer to a missing required value is a generic error, a
     500, or a rescued "something went wrong" leaves the caller nothing to correct. Ask
     that the error name the value, and where one exists, how to obtain it.

3. Values that cross a serialization boundary must be read the way they arrive:
   - Where a payload is parsed from JSON, read back from a jsonb column, pulled off a
     queue, or handed over by an SDK, check the key type on both sides. A hash written
     with symbol keys and read with string keys (or the reverse) does not raise — the
     read returns nil, the value silently reads as absent, and the code takes its
     "nothing was supplied" branch on a fully supplied payload.
   - Tests that build the payload themselves cannot catch this, because the test picks
     the key type the implementation already expects. When a diff adds or changes a
     handler for an externally supplied payload, look for a test that drives it through
     the real boundary — a request spec posting real JSON, not a direct call to the
     handler with a hand-built hash — and flag its absence as a genuine gap rather than a
     style preference.

4. A filter that silently drops unrecognized entries hides both kinds of mistake:
   - `select` / `reject` / `filter_map` against a list of known names drops a newly added
     name as quietly as a bad one, so an omission looks identical to a deliberate
     exclusion. When a diff adds one or depends on one, check what it drops today rather
     than what the comment beside it says it drops.
