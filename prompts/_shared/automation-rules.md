When the diff adds or changes CI configuration, a GitHub Actions workflow, a scheduled
job, or any other unattended automation, review it as production code whose only user is
a log nobody reads. Flag each genuine problem with a file/line reference:
- Every external call must have its failure surfaced. A non-2xx response or a failed
  request that is swallowed into an empty string or an ignored exit status turns a broken
  job into a green one. `curl -sf ... || true`, an unchecked exit status before a success
  log line, and a pipeline without `set -o pipefail` are all this defect. Require that the
  status be captured and either fail the step or emit a `::warning::`/`::error::`
  annotation naming what could not be reached.
- Check that the token scopes and permissions the job declares actually cover the API
  calls it makes, against the endpoint's documented requirements rather than against a
  sibling workflow. A too-narrow scope does not fail loudly; it returns 403 and the script
  proceeds down whatever branch an empty response selects. GitHub Actions in particular:
  a `permissions:` block replaces the default set rather than adding to it, and reading or
  writing comments on a pull request needs `pull-requests` even though the endpoint path
  says `issues`.
- A guard that fails closed must be able to tell "nothing to do" from "could not tell". A
  conservative default is right, but if the safe branch is also what an outage, a
  permission error, or an empty response selects, then the automation can no-op forever
  while every run reports success. Require that the two cases log differently and that the
  error case is visible without reading the log line by line.
- Judge whether the job's success signal means anything. A step that exits 0 on the path
  where it did nothing, or that prints a success message before verifying the action took
  effect, gives no way to notice the feature is dead. Say what the job should assert, or
  what it should count, so that a run which accomplished nothing is distinguishable from a
  run with nothing to accomplish.
