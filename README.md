# .github

Org-wide GitHub defaults and shared reusable workflows.

## Reviews by BiggiePockets

`.github/workflows/biggiepockets-review.yml` is a **reusable** workflow that runs a
two-stage AI code review on a pull request:

1. **Codex first pass** — reviews the diff against the PR's JIRA ticket and writes findings.
2. **Claude verify & synthesize** — validates Codex's findings, reviews the diff
   independently (grepping for callers/tests, factoring in the existing PR discussion),
   checks the change against the ticket's acceptance criteria, and decides a single verdict.

The **BiggiePockets** service account then submits the resulting `approve` /
`request_changes` review on the PR. If the PR has no `BIG-XXXXX` key in its title (or the
ticket can't be fetched), the review degrades gracefully to a diff-based review instead of
failing.

Codex and Claude run as separate GitHub Actions jobs. Codex uploads the reviewed commit's
diff, ticket/discussion context, and findings as a short-lived artifact; Claude downloads
that immutable handoff. If Claude is rate-limited, use **Re-run failed jobs** on the workflow
run. GitHub reruns only the Claude job, reusing the completed Codex pass instead of invoking
Codex again.

The review logic lives centrally in this repo. Each consuming repo only adds a thin
**caller** workflow that owns the triggers and gating and delegates to this one.

### Installing it in a repo

Do this once per repo you want BiggiePockets to review.

#### 1. Install the Claude GitHub app

The Claude verification stage uses [`anthropics/claude-code-action`](https://github.com/anthropics/claude-code-action).
Install the official [Claude GitHub app](https://github.com/apps/claude) for the organization
once; you do **not** need to reinstall it per repo. Claude's model requests authenticate
through OpenRouter using the shared `OPENROUTER_API_KEY` described below, so no personal
Claude Code OAuth token is required.

#### 2. Add the caller workflow

Create `.github/workflows/biggiepockets-review.yml` in the target repo:

```yaml
name: BiggiePockets Code Review

# Thin caller for the org-wide reusable review workflow in BiggerPockets/.github.
# This file owns the triggers and gating; the review logic lives centrally.
on:
  pull_request:
    types: [review_requested]
  workflow_dispatch:
    inputs:
      pr:
        description: 'PR number to review'
        required: true
        type: string

# The reusable workflow's jobs need these scopes. Declare them explicitly so the
# caller works regardless of the repo's default token permissions.
permissions:
  contents: read
  pull-requests: read
  id-token: write

jobs:
  review:
    # React to a manual dispatch, or to BiggiePockets specifically being requested.
    if: >-
      github.event_name == 'workflow_dispatch' ||
      github.event.requested_reviewer.login == 'BiggiePockets'
    uses: BiggerPockets/.github/.github/workflows/biggiepockets-review.yml@main
    with:
      pr: ${{ github.event.pull_request.number || inputs.pr }}
      # The BiggerPockets/.github ref to resolve review prompts from. Defaults to `main`,
      # so callers tracking `@main` can omit it. If you pin the `uses:` ref above to a tag
      # or SHA, pass the matching ref here too — otherwise prompts silently track main.
      registry_ref: main
    secrets: inherit
```

#### 3. Make the secrets available

The reusable workflow consumes several secrets via `secrets: inherit`: credentials for the
the AI review provider (`OPENROUTER_API_KEY`, shared by both the Codex and Claude stages),
an Atlassian email + API token to fetch the PR's JIRA ticket for intent, and a personal access
token for the BiggiePockets service account that submits the review. Configure them as
**organization secrets** (recommended — set once, available to every repo) or as per-repo
secrets if you prefer to scope them.

It also reports per-review traces to the `biggiepockets-review` app in Datadog LLM
Observability via `secrets.DATADOG_API_KEY`: verdict, timing, prompt template and version
(tracked as prompts, see below), the model each
stage ran (`CODEX_MODEL`/`CLAUDE_MODEL` env vars in the workflow — both are OpenRouter
model slugs and must be set), and the actual findings text from Codex and the summary Claude wrote,
so review quality is inspectable, not just counted. This secret is optional — reviews still
run and post normally without it, but no metrics are reported.

The exact secret names each step expects are visible in the `env:` and `with:` blocks of
[`.github/workflows/biggiepockets-review.yml`](.github/workflows/biggiepockets-review.yml).

#### 4. Set workflow permissions

The reusable workflow's jobs need `pull-requests: read` and `id-token: write` (OIDC
authentication for `claude-code-action`). The caller YAML declares these in its
`permissions` block, but GitHub caps those declarations at whatever the repo's default
token setting allows. Repos set to **"Read repository contents"** (the restrictive
default) deny `id-token: write` regardless of what the YAML says — the workflow fails
with `startup_failure` before any job runs.

Go to **Settings → Actions → General → Workflow permissions** on the target repo and set:

- **"Read and write permissions"**
- **"Allow GitHub Actions to create and approve pull requests"** (checked)

#### 5. Give BiggiePockets access

The **BiggiePockets** service account must have access to the repo so it can be requested
as a reviewer and post the review. Add it as a collaborator (or via a team) with at least
write access.

### Using it

Once installed, trigger a review either way:

- **Request a review** — add **BiggiePockets** as a reviewer on the PR. The workflow fires
  on `review_requested` and only runs when BiggiePockets specifically is the requested
  reviewer.
- **On demand** — run the `BiggiePockets Code Review` workflow via **Actions →
  workflow_dispatch** and pass the PR number. (Available once the caller file is on the
  repo's default branch.)

### Prompt registry and the prompt A/B test

The review-stage prompts are not inline in the workflow. They live in this repo under
`prompts/` and are resolved at runtime by `scripts/resolve-prompts.sh`:

```
prompts/
  registry.json                              # arms + control arm + split + codex prompt
  codex-first-pass.md                        # Stage 1 prompt (template)
  claude-synthesize.md                       # Stage 2 control arm (template)
  claude-synthesize-thesis-first.md          # Stage 2 thesis-first arm (template)
  _shared/{privacy,migration-data,perf,parsing,navigation}-rules.md  # shared rule blocks
```

- **Templates + shared blocks.** Each prompt references the shared rule blocks via
  `{{@prompts/_shared/<name>.md}}`, so the Codex and Claude prompts can never drift out of
  sync. Prompts resolve `{{PR}}`, `{{PROMPT_NAME}}`, `{{PROMPT_VERSION}}` too.
- **Content-derived versions.** `prompt_version` is a content hash of the template plus the
  shared blocks it includes — it changes only when that prompt's text changes, not per PR
  or per arm, so Datadog LLM Obs can attribute quality to the exact prompt text that ran.
- **One arm per pull request.** Two Claude prompts sit in the registry — `control` and
  `thesis-first` — and each review runs exactly one of them. `scripts/resolve-prompts.sh`
  hashes `<repo>:<pr>` into a bucket 0-99 and assigns the PR to the experiment arm when
  that bucket falls under `experiment_split_percent` (50 today), so the arms split traffic
  evenly and every review costs a single Claude pass. Assignment is a pure function of
  repo and PR number: re-running a review reuses the same arm, and one PR never sees two
  review styles.
- **The assigned arm decides.** Whichever arm a PR draws writes the posted summary *and*
  the approve / request-changes verdict. No separate control gate holds the decision back,
  which is the tradeoff for one pass per review: an experiment prompt affects real review
  outcomes on its share of PRs. Set `experiment_split_percent` to `0` to route every
  review to `control_arm` without removing the arm. The arm is never named in the review
  comment — a reviewer who knows which prompt wrote a summary can't judge it blind.
- **Comparison is between PRs, not within one.** No PR is reviewed twice, so there is no
  paired A/B to diff on a single PR. Compare the arms by grouping Datadog on `arm` across
  many reviews — verdict rate, latency, tokens. That needs volume before it means
  anything; a gap in approve rate over a dozen PRs is noise.
- **Prompt Tracking.** Every LLM span carries the prompt that produced it under
  `meta.input.prompt` — the registry template with its `{{PR}}`-style placeholders intact,
  plus the values that filled them as `variables`, plus `id`/`name`/`version`. Keeping the
  placeholders is what makes each prompt one tracked prompt in Datadog rather than a new
  template per PR, so the [Prompts view](https://app.datadoghq.com/llm/traces) shows call
  volume, latency, tokens, and a version diff per prompt, and any span can be replayed in
  the Playground with its exact template and variables. A version starts when the prompt
  text changes (a Roll), since `version` is the same content hash reported as a tag.
- **Datadog.** Each review is one trace, tagged with the arm that ran it:

  ```
  biggiepockets.review → codex.review, claude.synthesize
  ```

  A tag key resolves to one value per submitted payload, so an `arm` tag is only
  trustworthy while a payload carries a single arm — which it does by construction now
  that one arm runs per review. Tags include `arm`, `arm_role` (`control`/`experiment`),
  `prompt_name`, `prompt_version`, `verdict`, `assignment_bucket`, and
  `experiment_split_percent`. Recording the bucket and the split keeps an assignment
  auditable: changing the split later can't rewrite what an already-recorded review ran
  under. A stable `run_id` (`repo-pr-runid`) joins offline evals and panel ratings to the
  exact review.

  Spans carrying an `arm_agreement`, `experiment_verdict`, or `label_assignment` tag came
  from an earlier setup and are not comparable to these; exclude them when grouping by
  arm.

**Registry operations** (kept distinct so a formatting experiment can't silently change the
production prompt):

- **Roll** — edit a prompt or shared-rule file; its content-derived `prompt_version` bumps.
- **Apply** — point `control_arm` or an arm at a different stored prompt in
  `registry.json`, or change `experiment_split_percent` (no version change). Callers tracking `@main` pick the change up on their next run. A
  caller that pins `uses:` to a tag or SHA needs BOTH that `@ref` and its `registry_ref`
  input bumped in lockstep — Apply owns that ref-bump explicitly.
- **Split** — add a new arm entry in `registry.json` + its prompt file, and give it a
  share of traffic via `experiment_split_percent`.
- **Merge** — fold a variant's content into another prompt and remove the arm.

A `validate-prompts.yml` workflow guards the registry: it fails a PR if a template has
dangling includes, `registry.json` references a missing prompt, two arms point at the
same prompt, an arm's prompt fails to resolve, arm assignment isn't stable for a fixed PR,
the resolver isn't deterministic, or a shared-rule edit doesn't bump versions.
