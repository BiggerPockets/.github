When reviewing changes that add or modify rate limiting, enforce the application's established
approach for inbound Rails request throttling:

- Prefer Rails' built-in controller `rate_limit` feature for limiting requests handled by
  Rails controllers. If a change implements that same inbound request throttling with custom
  counters, cache/Redis keys, middleware, Rack::Attack rules, or hand-written before-actions,
  request changes and explain that it should use Rails' built-in `rate_limit` instead.
- Do not flag a valid use of Rails' built-in `rate_limit`, or rate limiting that serves a
  different purpose such as limiting outbound calls to an external service, protecting a
  non-Rails component, or preserving an existing integration whose behavior is not replaced
  by Rails' controller limiter.
- Apply this rule only when the diff adds or changes the implementation. Do not report
  unrelated pre-existing rate limiting code. Confirm the code path and behavior before
  reporting; the words "rate limit" alone are not evidence.
