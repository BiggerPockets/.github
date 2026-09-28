When the diff adds or changes a URL path the app serves — a Rails route, a React Router
route, a redirect, or a link to a new page — check that a request for that path actually
reaches the code that handles it. Layers in front of the handler match paths first, and a
spec that calls the controller or renders the component directly passes while production
never sees the request. Look at each layer the path crosses, in the order it crosses them:

- The nginx config (on Heroku, `config/nginx.conf.erb`). A prefix `location /foo` matches
  every path that starts with those characters, so it captures `/foo-bar` and `/foobar`,
  not only `/foo` and `/foo/...`. A regex `location ~` can capture a path by pattern, and
  nginx checks regex locations before it settles on a prefix match. A `return 301` or a
  `proxy_pass` to another upstream in the matching block means the request never reaches
  Rails.
- Rack middleware and redirect rules, such as `Rack::Rewrite` or a custom middleware that
  redirects or rewrites legacy paths.
- `config/routes.rb` ordering. An earlier route, a glob (`*path`), a catch-all, or a
  constraint can claim the path before the new route is reached.
- The frontend router. An earlier or wildcard route can render in place of the new one.

Treat a new path that shares a leading string with an existing legacy prefix, redirect, or
proxied section as the case to check hardest: `/guides-pro` beside `/guides`, `/blog-tools`
beside `/blog`. Grep the nginx config and middleware for the path's first segment and its
shorter prefixes, and work out which block wins for the new path, including with a
trailing slash and with a query string.

Flag it as a blocking issue when an upstream layer redirects, proxies elsewhere, or
rewrites the new path, and name the file and line of the block that captures it. Suggest
narrowing that block — an exact match (`location = /foo`), a path boundary in the regex
(`/foo(?=/|$)`), or an explicit block for the new path — and a spec that exercises the
real routing layer rather than the controller alone.
