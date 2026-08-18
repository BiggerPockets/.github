When code parses or builds a structured format, it must use a purpose-built parser rather
than a hand-rolled regular expression. Treat a regexp used as a parser as a blocking issue
and name the parser to use instead. This applies to every language in the diff — Ruby,
JavaScript, and TypeScript alike:
- URIs and URLs — splitting or rebuilding a URL, reading its host, path, or scheme, or
  comparing domains. Ruby: `URI.parse` or `Addressable::URI`. JS/TS: the `URL` constructor
  (with a base for relative paths), or `node:path`/`node:url` for filesystem paths and
  `file:` URLs. Never a regexp over the string.
- Query strings — Ruby: `Rack::Utils.parse_nested_query`, `URI.encode_www_form`, or
  `CGI.parse`. JS/TS: `URLSearchParams` (or `url.searchParams`), never manual splitting on
  `&` and `=` and never manual `encodeURIComponent` reassembly of a whole query string.
- HTML and XML — Ruby: `Nokogiri`. JS/TS: `DOMParser` in the browser, or the HTML parser
  already in the project on the server. Never a regexp matching tags or attributes, and in
  the browser never build markup by string concatenation where `textContent` or the
  framework's escaping would do.
- JSON and YAML — Ruby: `JSON.parse`, `YAML.safe_load`. JS/TS: `JSON.parse`.
- Dates and times — Ruby: `Time.zone.parse`, `Date.iso8601`, `ActiveSupport::Duration`.
  JS/TS: the date library already in the project (or `Intl`/`Temporal` where available), not
  a regexp pulling fields out of a date string.
- CSV — Ruby: the `CSV` library; JS/TS: a CSV parser, not `split(",")`, which breaks on
  quoted fields. Email addresses and cookies: the framework or library validator/parser
  already in use.
A regexp is still the right tool for narrow matching where no parser applies — a simple
format assertion, a substring search, a scan for a token in free text. The rule is about
extracting or assembling the parts of a structured value: reaching for a regexp there is a
correctness bug waiting to happen (encoded characters, userinfo, ports, subdomains,
internationalized hosts, and trailing-slash or case differences all defeat it), and for
host or origin checks it is a security problem, since a regexp that isn't anchored to a
parsed host can be satisfied by an attacker-controlled domain.
