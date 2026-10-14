A bug is fixed where the wrong value is produced, not where it shows up. A downstream
workaround leaves every other consumer wrong, and when the consumer is a shipped mobile
app, a server-side fix also reaches the builds already on members' devices, which a
client-side one never does.

1. Spot a downstream workaround:
   - The diff filters out, rewrites, defaults, or guards against a value it receives from
     another system: an API response, a gem or package, a shared service, a queue
     payload, a webhook.
   - The value is wrong at its source, not merely unexpected in this one consumer. A
     client choosing to hide something the API is right to return is not a workaround.
   - Check the ticket and PR body for the source. A ticket that names files, classes,
     endpoints, or queries in another repository or layer is strong evidence that the
     fix belongs there.

2. When the source is a system BiggerPockets owns (our API, our shared libraries, our
   services), treat the workaround as blocking unless one of these holds:
   - The PR body or ticket names the upstream fix (a ticket key or a pull request) and
     says why the downstream guard is needed as well, for example to cover a window
     before the upstream fix deploys.
   - The ticket or an author-written PR comment explicitly asks for a downstream-only
     fix. Quote the sentence. A reviewer cannot grant this.

3. Do not accept these as reasons for the workaround:
   - "The upstream code isn't in this repository" or "the fix is out of scope here". That
     is why the fix belongs in a different pull request, not a reason to approve this one.
   - "It is a reasonable defensive measure" or "belt and braces", with no upstream fix
     named.
   - "Non-blocking: worth fixing upstream too". If the upstream fix is what resolves the
     ticket, this PR does not.

4. A workaround for a defect in a third-party system we do not own (a vendor API, an
   external package) is acceptable. Ask only that a comment names the upstream defect
   and, where one exists, the issue tracking it.
