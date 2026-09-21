# .github

Org-wide GitHub defaults and shared reusable workflows.

## Reviews by BiggiePockets

`.github/workflows/biggiepockets-review.yml` is a **reusable** workflow that runs a
two-stage AI code review on a pull request:

1. **First pass** — reviews the diff against the PR's JIRA ticket and writes findings.
2. **Verify & synthesize** — validates the first pass's findings, reviews the diff
   independently (grepping for callers/tests, factoring in the existing PR discussion),
   checks the change against the ticket's acceptance criteria, and decides a single verdict.

Both stages run on [pi](https://www.npmjs.com/package/@earendil-works/pi-coding-agent)
against models pinned in `scripts/pi/models.json`, each entry listing the stages allowed
to run it. One harness means one allowlist, one set of published rates behind the cost
telemetry, and the same early failure on an unpinned slug for either stage.

The **BiggiePockets** service account then submits the resulting `approve` /
`request_changes` review on the PR. If the PR has no `BIG-XXXXX` key in its title (or the
ticket can't be fetched), the review degrades gracefully to a diff-based review instead of
failing.

The two stages run as separate GitHub Actions jobs. The first pass uploads the reviewed
commit's diff, ticket/discussion context, and findings as a short-lived artifact; Stage 2
downloads that immutable handoff. If Stage 2 is rate-limited, use **Re-run failed jobs** on
the workflow run. GitHub reruns only the Stage 2 job, reusing the completed first pass
instead of paying for it again.

Some identifiers still read `codex` — the `codex` job, `codex-findings.md`, the
`codex_model` input. They are external contracts (a required status check name, a
filename the Stage 2 prompts reference, the `workflow_call` API) rather than descriptions
of the harness. The Datadog span names it: `pi.first_pass`.

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
      # OpenRouter slug for the Stage 2 (pi) pass. Omit it to review on the org default.
      # Set it to put this repo on a different model — the slug must be one of the models
      # pinned in `scripts/pi/models.json`. Reading it from a repository variable lets the
      # repo be moved between pinned models without a pull request.
      pi_model: ${{ vars.PI_MODEL || 'deepseek/deepseek-v4.1-flash' }}
      # OpenRouter slug for the Stage 1 first pass. Omit it to review on the org default,
      # gpt-5.6-luna. Set it to put this repo on a stronger model; the slug must be one
      # OpenRouter accepts.
      codex_model: ${{ vars.CODEX_MODEL || 'openai/gpt-5.6-luna' }}
    secrets: inherit
```

#### 3. Make the secrets available

The reusable workflow consumes several secrets via `secrets: inherit`: credentials for the
the AI review provider (`OPENROUTER_API_KEY`, shared by both stages),
an Atlassian email + API token to fetch the PR's JIRA ticket for intent, and a personal access
token for the BiggiePockets service account that submits the review. Configure them as
**organization secrets** (recommended — set once, available to every repo) or as per-repo
secrets if you prefer to scope them.

It also reports per-review traces to the `biggiepockets-review` app in Datadog LLM
Observability via `secrets.DATADOG_API_KEY`: verdict, timing, prompt template and version
(tracked as prompts, see below), the model each
stage ran (`CODEX_MODEL`/`PI_MODEL` env vars in the workflow — both are OpenRouter
model slugs and must be set; each comes from its input, `codex_model` and `pi_model`,
so a repo's models are visible in its own traces), and the actual findings text from the
first pass and the summary Stage 2 wrote,
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

Both passes record usage the same way. pi runs in `--mode json` and writes a JSON event
stream; every assistant message carries a usage object with token counts and `cost.total`
— a price computed by pi from the model's OpenRouter list rates, the same rates OpenRouter
bills against, so it is the amount the pass is charged. Every model either stage may run
on is pinned with its rates in `scripts/pi/models.json` (the catalog pi ships predates
them, and the live catalog refresh is a background fetch, not a startup step — a committed
pin is what makes a fresh runner deterministic); adding or rolling a model means changing
that file in the same commit. Each stage reads its own event stream and hands the totals
to the reporting job. Only usage objects are read — never message content, transcripts,
prompts, or diffs.

Each span also carries a **`turn_count`**. pi reports tokens as a running session total,
so a multi-turn pass counts its conversation prefix once per turn: a first pass showing
1.15M input tokens is a ~29-turn session over an ~80k working context, not an 1.15M-token
prompt. Without the turn count those two are indistinguishable, and sizing a context
window off the cumulative figure would demand roughly fourteen times what the pass needs.

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

### Weekly model freshness check

`.github/workflows/model-freshness-check.yml` runs every Monday and checks the models
pinned in `scripts/pi/models.json` against OpenRouter's live catalog. It never edits that
file — choosing a review model is a judgment call on review quality that no API can make —
it opens (or comments on) an issue when something is worth a look:

- a pinned rate no longer matches OpenRouter's list price, which means the cost we report
  to Datadog is wrong until someone updates the pin;
- a pinned model has dropped out of the catalog, or its uptime has slipped below 95%;
- a cheaper reasoning-capable model with comparable context and healthy uptime has shipped,
  **and** reviews no worse than the weakest pinned model.

**Each stage is checked separately** (`--stage=stage1|stage2`), because they pin different
models and run very different workloads, and each gets its own chart and its own issue
thread. The chart plots effective $/1M against the model's coding score from llm-stats.com's
leaderboard (`index_code`), so price is read against the capability that matters for a code
review rather than against raw context length.

The shortlist is ranked **strongest first, not cheapest first**. Every candidate has already
passed the "cheaper than what's pinned" filter, so ranking on price again just re-answers
"what is cheapest" — which fills all five slots with the bottom of the catalog and buries the
model actually worth switching to. Two models are left off entirely: one that reviews worse
than the weakest pinned model (a downgrade, not a candidate), and one the leaderboard doesn't
cover (no capability number to weigh its price against). A *pinned* model with no score is
still charted, in a separate "no score" column — what is already running has to be shown
either way. If the leaderboard is unreachable the check falls back to price ranking, since a
degraded shortlist beats an empty one.

Two things make those numbers mean what they say:

- **Effective $/1M, not the list input rate.** Review prompts are almost entirely re-sent
  context, so the prompt-cache discount dominates real cost. The effective rate blends each
  model's input/cacheRead/output rates by the stage's own token mix, measured from its spans
  in Datadog LLM Observability over a trailing 90 days. Reading that mix correctly depends on
  which harness recorded the span: pi reports fresh input and cache reads as **disjoint**
  counts that add, while Codex reports input as the **whole** figure with cache reads already
  inside it. Adding the two on a Codex span double-counts the cache and inflates the apparent
  fresh-input share by an order of magnitude, which makes cache-hostile models look cheap.
- **Working context, not billed input.** A model is flagged as having inadequate context
  against what the pass has to *hold*, which is not what it is billed for. Tokens are
  reported as a running session total, so a multi-turn pass counts its prefix once per turn.
  The turn-aware estimate uses the accumulated fresh input plus output for a multi-turn pass,
  and the whole input for a single-turn one (a cache read is a billing discount, not a
  smaller prompt). Gating on the cumulative figure instead would rule out every candidate,
  including the model currently running the stage. Spans with no `turn_count` are left out of
  the estimate rather than guessed at, so no model is flagged on context until enough spans
  carry one.

Without `DD_API_KEY`/`DD_APP_KEY` the check still runs; it falls back to the raw input rate
and draws no context threshold.

### Prompt registry and the prompt A/B test

The review-stage prompts are not inline in the workflow. They live in this repo under
`prompts/` and are resolved at runtime by `scripts/resolve-prompts.sh`:

```
prompts/
  registry.json                              # arms + control arm + split + codex prompt
  codex-first-pass.md                        # Stage 1 prompt (template)
  first-pass-system.md                       # Stage 1 system prompt (not registry-versioned)
  claude-synthesize.md                       # Stage 2 control arm (template)
  claude-synthesize-thesis-first.md          # Stage 2 thesis-first arm (template)
  _shared/{completeness,privacy,migration-data,perf,parsing,navigation,rename-compatibility,spec-value}-rules.md  # shared rule blocks
```

- **The Stage 1 system prompt.** `prompts/first-pass-system.md` replaces pi's stock system
  prompt for the first pass. pi's default casts the model as an editor that writes files and
  spends a long block on pi's own documentation — both wrong for a read-only review, and the
  latter is noise. Ours states how to operate: keep going rather than yield early, verify
  before claiming, report security findings rather than soften them, use git history to
  establish intent, cite real paths and line numbers, and treat the final message as the
  deliverable. *What* to review stays in `codex-first-pass.md`. It sits outside the registry's
  arm/version machinery, so its SHA-256 prefix is tagged onto the span as
  `first_pass_system_version` — editing it changes review behavior as surely as a Roll does
  and has to be just as visible in Datadog.
- **Templates + shared blocks.** Each prompt references the shared rule blocks via
  `{{@prompts/_shared/<name>.md}}`, so the Stage 1 and Stage-2 prompts can never drift out of
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
  biggiepockets.review → pi.first_pass, pi.synthesize
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
