A spec covers deleted behavior when it asserts that something the diff removes is gone.
Once the code is deleted there is nothing left for the spec to protect: it pins the
framework's default for a path, constant, column, or method that no longer exists, and
every later reader has to work out why a test guards code that isn't there.

1. Spot one:
   - The diff removes a route, controller, action, endpoint, method, class, column, job,
     feature flag, or UI element, and adds or keeps a spec whose assertions are about
     that removed thing being absent.
   - Typical assertions: a request to the removed path returns 404 or raises a routing
     error, the removed constant is undefined, the removed method raises
     `NoMethodError` or is not `respond_to?`, the removed column is not in
     `column_names`, the removed element is not on the page, the removed flag no longer
     changes anything.
   - Read the spec against the rest of the diff. If what it exercises exists only in
     the deleted lines, it covers deleted behavior.

2. Treat each one as blocking, and ask for the spec to be removed. Deleting the code's
   specs together with the code is the expected change, not a coverage gap.

3. These do not make the spec acceptable:
   - "It is a regression spec": the deleted code raised errors, and the spec proves that
     now gives a 404 or a clean failure. Answering an unknown path or a missing method is
     the framework's job once the code is gone. The fix is the deletion, and nothing
     regresses unless someone writes the code again.
   - "It guards against the route or code being added back by mistake". Restoring
     deleted code is a deliberate change, and its author deletes the spec with it.
   - An exception marker, comment, or allow-list entry that another check needs before
     it accepts the new spec file. That proves the spec was written on purpose, not
     that it covers behavior that still exists.

4. Do not flag:
   - A spec for behavior the diff adds in place of the removed code: an explicit
     redirect, a `410 Gone` response the app now renders, a deprecation message, a
     fallback. That covers new code, even when the old path is involved.
   - A spec for code that changes rather than disappears, such as a fixed branch whose
     new result the spec asserts.
   - A spec the diff edits only to drop its assertions about the removed code.
   - Specs the diff deletes.

This is not a useless-spec finding, and the rule that a useless spec never blocks does
not apply to it. State the spec's file and line, the deleted code it covers, and ask for
the spec to be removed.
