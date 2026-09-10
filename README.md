# .github

Org-wide GitHub defaults and shared reusable workflows.

## Reviews by BiggiePockets

`.github/workflows/biggiepockets-review.yml` is a **reusable** workflow that runs a
two-stage AI code review on a pull request:

1. **Codex first pass** — reviews the diff against the PR's JIRA ticket and writes findings.
2. **pi verify & synthesize** — validates Codex's findings, reviews the diff
   independently (grepping for callers/tests, factoring in the existing PR discussion),
   checks the change against the ticket's acceptance criteria, and decides a single verdict.

The **BiggiePockets** service account then submits the resulting `approve` /
`request_changes` review on the PR. If the PR has no `BIG-XXXXX` key in its title (or the
ticket can't be fetched), the review degrades gracefully to a diff-based review instead of
failing.

Codex and pi run as separate GitHub Actions jobs. Codex uploads the reviewed commit's
diff, ticket/discussion context, and findings as a short-lived artifact; pi downloads
that immutable handoff. If the pi pass is rate-limited, use **Re-run failed jobs** on the workflow
run. GitHub reruns only the pi job, reusing the completed Codex pass instead of invoking
Codex again.

The review logic lives centrally in this repo. Each consuming repo only adds a thin
**caller** workflow that owns the triggers and gating and delegates to this one.

### Installing it in a repo

Do this once per repo you want BiggiePockets to review.

#### 1. Install nothing

The Stage-2 verification stage runs [`@earendil-works/pi-coding-agent`](https://www.npmjs.com/package/@earendil-works/pi-coding-agent),
a plain npm CLI that the workflow installs onto the runner itself (`npm install -g`).
There is no GitHub app, no OIDC, and no per-repo installation. pi authenticates to
OpenRouter with the shared `OPENROUTER_API_KEY` described below — no OAuth login or
personal token is required.

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

# The reusable workflow's jobs need read scopes. Declare them explicitly so the
# caller works regardless of the repo's default token permissions.
permissions:
  contents: read
  pull-requests: read

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
the AI review provider (`OPENROUTER_API_KEY`, shared by both the Codex and pi stages),
an Atlassian email + API token to fetch the PR's JIRA ticket for intent, and a personal access
token for the BiggiePockets service account that submits the review. Configure them as
**organization secrets** (recommended — set once, available to every repo) or as per-repo
secrets if you prefer to scope them.

It also reports per-review traces to the `biggiepockets-review` app in Datadog LLM
Observability via `secrets.DATADOG_API_KEY`: verdict, timing, prompt template and version
(tracked as prompts, see below), the model each
stage ran (`CODEX_MODEL`/`PI_MODEL` env vars in the workflow — both are OpenRouter
model slugs and must be set), and the actual findings text from Codex and the summary pi wrote,
so review quality is inspectable, not just counted. This secret is optional — reviews still
run and post normally without it, but no metrics are reported.

The exact secret names each step expects are visible in the `env:` and `with:` blocks of
[`.github/workflows/biggiepockets-review.yml`](.github/workflows/biggiepockets-review.yml).

#### Cost tracking (Datadog LLM Observability)

`secrets.DATADOG_API_KEY` also turns on cost tracking. Every LLM span in the review
trace carries its token usage — input, output, total, and cached-read counts — under
`metrics`, and Datadog prices the span from its own model pricing catalog. Cost is
therefore attributed per pass and per model on the same trace as the quality metrics,
and shows up in LLM Observability's spend views without a separate report.

For the catalog to recognise a model, a span has to name it the way Datadog does: the
bare model and the provider that originated it. The workflow runs everything through
OpenRouter, whose slugs look like `openai/gpt-5.6-sol`, so `scripts/llm-usage.py`
splits the slug into `model_name: gpt-5.6-sol` / `model_provider: openai` and records
the routing as a `gateway:openrouter` tag.

Cost is never computed in the reporting script: `scripts/llm-usage.py` holds no rate
table. Instead each span carries whichever of the two things Datadog needs. For a model
in the catalog, token counts are enough. For one it does not carry, the span reports a
`total_cost` metric taken at face value.

The two passes record their usage differently. pi runs in `--mode json` and writes a
JSON event stream; every assistant message carries a usage object with token counts and
`cost.total` — a price computed by pi from the model's OpenRouter list rates, the same
rates OpenRouter bills against, so it is the amount the pass is charged. The Stage-2
model and its rates are pinned in `scripts/pi/models.json` (the catalog pi ships
predates the model, and the live catalog refresh is a background fetch, not a startup
step — a committed pin is what makes a fresh runner deterministic); if you roll the
Stage-2 model, update that file in the same commit. Codex writes running token counters
to a session rollout on its own runner, and since its action exposes no usage output,
the workflow reads that rollout in the Codex job and hands the totals to the reporting
job. In both cases only usage objects are read — never message content, transcripts,
prompts, or diffs. (The Claude Code harness that previously ran Stage 2 translated
usage into Anthropic's schema, so OpenRouter's reported cost regularly did not survive
to the span — the pi pass records usage itself and is what this repo now trusts.)

Two **organization-level variables** (`vars`, not secrets — **Settings → Secrets and
variables → Actions → Variables** at the org level) configure where the trace lands.
Both are optional, and nothing here is committed to the repository:

- `DD_SITE` — the Datadog site to report to (e.g. `datadoghq.eu`, `us5.datadoghq.com`).
  Defaults to the public `datadoghq.com`. Set this to the organization's actual site;
  a private or internal Datadog hostname belongs in this variable and nowhere else.
- `DD_LLMOBS_ML_APP` — the LLM Obs `ml_app` the review trace is grouped under.
  Defaults to `biggiepockets-review`.

Cost tracking is best-effort and never fails a review. With no `DATADOG_API_KEY` the
whole reporting step is skipped, and a missing event stream, an absent rollout, or
malformed usage data degrades to fewer metrics on the span.

#### 4. Set workflow permissions

With the move off `claude-code-action`, the review no longer needs OIDC (`id-token`),
so the default **"Read repository contents"** setting is fine — repos that previously
had to loosen their workflow permissions for the review can leave them restrictive.
The reusable workflow's jobs declare `contents: read` and `pull-requests: read` only;
the review itself is submitted with the `BIGGIEPOCKETS_PAT` secret, not the repo's
`GITHUB_TOKEN`. Callers that already declare `id-token: write` in their caller file can
remove it, but leaving it is harmless.

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
  _shared/{completeness,privacy,migration-data,perf,parsing,navigation}-rules.md  # shared rule blocks
```

- **Templates + shared blocks.** Each prompt references the shared rule blocks via
  `{{@prompts/_shared/<name>.md}}`, so the Codex and Stage-2 prompts can never drift out of
  sync. Prompts resolve `{{PR}}`, `{{PROMPT_NAME}}`, `{{PROMPT_VERSION}}` too.
- **Content-derived versions.** `prompt_version` is a content hash of the template plus the
  shared blocks it includes — it changes only when that prompt's text changes, not per PR
  or per arm, so Datadog LLM Obs can attribute quality to the exact prompt text that ran.
- **One arm per pull request.** Two Stage-2 prompts sit in the registry — `control` and
  `thesis-first` — and each review runs exactly one of them. `scripts/resolve-prompts.sh`
  hashes `<repo>:<pr>` into a bucket 0-99 and assigns the PR to the experiment arm when
  that bucket falls under `experiment_split_percent` (50 today), so the arms split traffic
  evenly and every review costs a single Stage-2 pass. Assignment is a pure function of
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
  biggiepockets.review → codex.review, pi.synthesize
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
