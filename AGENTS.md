# Working in this repository

## This repository is public. The repositories it reviews are not.

`BiggerPockets/.github` is public. `biggerpockets/biggerpockets`, `biggerpockets/pockets-app`
and `biggerpockets/claude-skills` are private. Anything committed here is world-readable, and
so is every workflow run's logs and artifacts.

**Never commit anything derived from those private repositories.** That includes eval
datasets, review findings, failure samples, prompt fixtures and test data built from real
pull requests. The risk is not obvious from looking at such a file: a list of review findings
reads as prose about code quality, but it is a set of `file:line` anchors into private source
paired with a description of the defect at each one — a map of the codebase and its weak
points. Keep those in a private repository and have the workflow here read them from there.

Run output carries the same content as the input. A replay's findings, a judge verdict and a
job log all quote the code under review, and on a public repository they stay readable for as
long as they are retained. Treat a workflow that prints private source to a log as the same
disclosure as committing it.

Before adding any file built from another repository's contents:

```sh
gh api repos/<org>/<repo> --jq .visibility
```

If it returns `private`, the derived file does not belong here.

Repository *names* and public identifiers are fine. Source paths, code excerpts, ticket
contents and pull request bodies are not.

## Secrets

Workflows here are `workflow_call`. Their secrets resolve from the **caller**, not from this
repository, so a secret that exists in this repo's settings is not available to a reusable
workflow invoked from elsewhere — the caller must pass it or use `secrets: inherit`.

Any token this repository's own workflows hold is a token held by a public repository. Scope
it to the minimum: read-only, and only the repositories the job actually needs.

## Conventions

- Keep the README accurate for a first-time reader. Describe the current design plainly
  rather than contrasting it with an earlier one.
- Tests live in `tests/`, run with `python -m unittest discover tests`. `tests.yml` runs
  them on every pull request.
- A workflow's `run:` block is shell that nothing parses until it executes, and a step
  under `continue-on-error` reports success when it dies on a syntax error. The suite
  parses every `run:` block with `bash -n`, so run it after editing a workflow. Inside a
  single-quoted jq program, an apostrophe in a comment ends the program.
- A gym run (`gym-experiment.yml`) evaluates exactly one model. Evaluate only the model
  you were asked to evaluate. Do not add the recorded model, a "control", or any second
  model as a comparison run unless explicitly asked.
