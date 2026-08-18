For in-app navigational links in the React/React Router frontend, the code should use
React Router's `Link` (or `NavLink` where active-state styling is wanted) rather than a raw
`<a href>` tag as much as possible. A plain `<a>` triggers a full page reload and re-mounts
the app, while `Link` does client-side navigation, preserves app state, and avoids the
flash and performance cost of a round trip. Prefer `Link` for any link that points to
another route within the app (e.g. to a member profile, property, or section served by the
single-page app).

Treat a raw `<a href>` used for internal in-app navigation as a finding and suggest `Link`:
- Any `<a href="...">` where the target is a route the app itself renders (in-app route)
  should be a `<Link to="...">` from `react-router-dom`.
- HTML-only output where React's component model isn't available (e.g. server-rendered
  Rails views, mailers, or raw HTML strings) is out of scope — this rule targets React
  components that already have React Router available.

Don't block a PR merely for an external link, an anchor jump within a page (`<a href="#...">`),
a download/`target="_blank"` that is genuinely external or non-routing, or a one-off case
where `Link`'s features (like preventing re-renders or passing location state) aren't wanted.
The rule is about reaching for a raw `<a>` for route navigation where the idiomatic `Link`
is available — flag it with the file/line and the suggested `Link` destination.
