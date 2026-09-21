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

Both stages run [`@earendil-works/pi-coding-agent`](https://www.npmjs.com/package/@earendil-works/pi-coding-agent),
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
      # gpt-5.6-luna. Set it to put this repo on a stronger model; the slug must be one of
      # the models pinned in `scripts/pi/models.json` with `stage1` in its `stages` list.
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

### Model gym: regression set for the first-pass model

Stage 1's model is set by the `codex_model` workflow input — `vars.CODEX_MODEL`, falling
back to `openai/gpt-5.6-luna` — so changing one organization variable changes what reaches a
human reviewer across every repo at once. Nothing in a review notices a model that quietly stops reporting a
class of defect: a finding that is never written leaves no trace, the run still passes, and
Stage 2 verifies only what it was handed. The findings a *previous* first-pass model wrote
are the only surviving record of what was catchable on those diffs, so they become the
regression set.

`sol-first-pass-findings.yaml` is that set for `openai/gpt-5.6-sol`: one record per pull
request, holding the PR to review and the findings Sol reported for it.

**The dataset is not stored in this repository, and must not be.** This repository is
public. The findings quote private source: roughly 226 distinct `file:line` anchors across
`biggerpockets/biggerpockets`, `biggerpockets/pockets-app` and `biggerpockets/claude-skills`,
each paired with a description of a specific defect in that file. That is a map of private
code and its weak points. Keep the dataset in a private repository and point the workflow
at it; `gym/` is gitignored here so it cannot be committed by accident.

```
scripts/gym/export_sol_findings.py   # Datadog LLM Obs spans -> that YAML
scripts/gym/upload_gym_dataset.py    # that YAML -> a Datadog LLM Obs experiments dataset
```

The same constraint applies to anything a run produces. A replay's `findings.md`, the judge
verdicts, and the job logs all quote the private code under review, and on a public
repository those are world-readable for as long as they are retained.

**Every record is pinned to the commit that was reviewed**, and that is what makes the
file usable weeks later. A pull request is not a stable artifact: commits land on it after
a review, its base branch advances, and it eventually merges and closes — 95 of the 97
pull requests here are already closed. The findings cite exact `file:line` anchors, so
replaying "PR 31230" against whatever that PR looks like today would show a candidate
model different code from the one Sol read, and every drifted anchor would score as a
missed finding that was never there to miss. That failure is silent and it biases the
result toward false alarm, which is the worst way for a regression check to be wrong.

The span carries no SHA, but its `run_id` tag embeds the GitHub Actions run, and that run
records the `head_sha` it checked out. So each record carries `head_sha` (the exact
reviewed commit) plus the `base_ref`/`base_sha` it was diffed against, and a harness
reconstructs the review diff as:

```sh
git diff $(git merge-base <base_sha> <head_sha>) <head_sha>
```

Both anchors are needed: 15 of these 97 pull requests are stacked on another branch
rather than on `main`, so assuming `main` silently produces the wrong diff for them. Pinning the commit also restores the *whole tree*, not just the diff — Stage 1
explores the repo, so its findings routinely cite collaborating files the PR never
touched, and those only resolve at the reviewed commit.

Two things this cannot pin. A commit can become unreachable if its branch was deleted
after a force-push; `head_sha` is recorded either way, so a harness can detect and skip
such a row rather than review the wrong code. And the JIRA ticket a finding argues
against ("the ticket requires X") is live and may since have been edited — nothing in the
span or the run captures its state at review time, so a finding that turns on acceptance
criteria can go stale even with the diff pinned correctly. Treat a candidate's "miss" on
a ticket-intent finding as a prompt to check the ticket, not as an automatic regression.

**What's in it, and what isn't.** Records are drawn from the `codex.review` span, which
carries the first pass's findings text plus the repo, PR, model and Stage-2 verdict as tags.
Included are passes that finished (`@status:ok`), wrote findings
(`codex_findings_lines > 0`), and whose review Stage 2 verified into `request_changes` —
that verdict is the closest available ground truth short of re-adjudicating every finding by
hand, and it is the set whose loss would actually cost something. Passes Stage 2 approved are
excluded by default (`--verdict any` includes them), because an approval usually means Stage 2
judged those findings not worth blocking on. A pass whose text reports nothing actionable is
dropped: there is no finding in it for a candidate model to miss.

Each PR appears once. A PR is re-reviewed on every push and the span is reported once per
Stage-2 attempt, so the raw query returns the same review many times over; the most recent
qualifying pass wins.

**Retention bounds it.** LLM Obs holds spans for a limited window, so the file covers what was
still queryable when it was exported — not all history. Re-run the exporter periodically and
merge rather than expecting one run to be complete; `--since`/`--until` set the window.

**Running it.** The exporter refreshes or widens the set; the uploader pushes it into a
Datadog LLM Obs experiments project, creating the project and dataset when absent:

```sh
pip install -r scripts/gym/requirements.txt

# Needs an authenticated `gh` as well as the Datadog keys: the reviewed commit comes
# from the GitHub Actions run, not from the span.
DD_API_KEY=... DD_APP_KEY=... python3 scripts/gym/export_sol_findings.py \
    --model openai/gpt-5.6-sol --since now-30d --out ../private-gym/sol-first-pass-findings.yaml

python3 scripts/gym/upload_gym_dataset.py --project 'code-review-gym' --dry-run
DD_API_KEY=... DD_APP_KEY=... python3 scripts/gym/upload_gym_dataset.py \
    --project 'code-review-gym'
```

`DD_SITE` selects the Datadog site, as elsewhere in this repo. Uploading is additive and never
deletes rows, so re-uploading the same file appends duplicates — refresh into a new
`--dataset` name unless you mean to extend an existing one. `--dry-run` needs no credentials
and prints the first record exactly as it would be sent.

Both keys are required, and the application key is the one usually missing: sending spans (what
the review workflow does) needs only an API key, while reading spans and writing datasets need
an application key too.

The datasets API stores `input`, `expected_output` and `metadata`. It has no per-record tags
field in this version — it accepts one and drops it, and a read-back shows `tags: []` — so the
uploader folds the YAML's tag dimensions into `metadata` instead. `repo`, `pr` and `severity`
are skipped there because they are already structured fields; `source_model` is the one that
earns a per-record slot, since it is what tells rows apart the moment a second model's findings
are appended to the same dataset.

**Scoring a candidate.** An experiment reads `input`, checks out `head_sha`, runs the
candidate first-pass model over the diff reconstructed above, and compares its output to
`expected_output.findings`. The
question is *did it report this defect*, not *did it phrase it the same way*, so the evaluator
wants an LLM judge rather than string equality. `metadata.severity` (`blocker`, `blocking`,
`non-blocking`) lets a judge weight a missed blocking finding above a missed nitpick, and
`metadata.trace_id`/`span_id` link every row back to the review it came from.

Every record has the same shape, deliberately — an experiment iterates all of them through one
evaluator, and a single row with a different shape breaks the run or, worse, silently scores
wrong. Keep new records structurally identical to their neighbours.

#### Running a gym experiment

`.github/workflows/gym-experiment.yml` (Actions → **Gym experiment** → Run workflow) replays
the recorded reviews against candidate models and reports how much of what the recorded model
found each candidate still finds.

**It needs its own secrets.** `biggiepockets-review.yml` is a `workflow_call` workflow, so the
credentials it names resolve from the *calling* repo through `secrets: inherit` — they are not
secrets of this repo, and a review has never actually run here. Organization secrets do not
reach here either: this repository is public so that other apps can call the review workflow,
and the organization's secrets are scoped to private repositories. That is the same reason
`model-freshness-check.yml` carries its own `DD_API_KEY`/`DD_APP_KEY` rather than inheriting
them. The gym needs its own copies:

```sh
gh secret set OPENROUTER_API_KEY --repo BiggerPockets/.github   # runs the replays
gh secret set GYM_REPO_TOKEN     --repo BiggerPockets/.github   # reads the target repos
gh secret set JIRA_EMAIL         --repo BiggerPockets/.github   # ticket context
gh secret set JIRA_API_TOKEN     --repo BiggerPockets/.github
```

`GYM_REPO_TOKEN` is deliberately not the review service account's `BIGGIEPOCKETS_PAT`. These
secrets live in a **public** repository, and that PAT can write — it exists to submit reviews.
The gym only ever reads, so a fine-grained PAT with read-only **Contents** and **Pull requests**
on the repositories in the dataset is sufficient, and a leak of it cannot change anything. The
JIRA token cannot be narrowed the same way; if that matters, run the gym from a private caller
instead (this workflow can be converted to `workflow_call` without making this repo private —
only a thin caller file moves).

The JIRA pair is not optional in practice. Without it every replay degrades to a diff-only
review, while the recorded findings were written with the ticket in hand — many of them turn on
ticket intent, so the candidate would be marked down for missing findings it was never given
the information to make. The workflow checks all four up front and stops rather than producing
a number that looks like a regression.

**Run two arms.** The default `arms` input is the candidate *and* the model the dataset was
recorded from, and that is not padding. A review is not deterministic: the baseline model does
not reproduce its own recorded findings at 100%, and how far short it falls is the noise floor
for the whole measurement. A candidate at 65% means nothing until you know the baseline scores
70% (a small real gap) or 95% (a large one). The summary refuses to draw a conclusion when only
one arm ran.

Each record becomes one matrix job per arm, so a replay invokes `openai/codex-action` exactly
the way the production first pass does — same prompt, endpoint and read-only sandbox — differing
only in checking out the recorded commit. Start with `limit: 3`, read the findings yourself to
confirm the judge is calling matches sensibly, then spend the full run.

Three things the harness controls for, each of which would otherwise quietly bias the result:

- **The commit**, as described above.
- **The conversation.** BiggiePockets posts its review back onto the pull request, so today's
  discussion usually contains the findings being tested for. Every comment is filtered to
  `created_at < reviewed_at`, so a candidate cannot read the answer off the page.
- **The prompt version.** The findings in a record were produced by a specific first-pass
  prompt. Of the 97 records, 73 were recorded under the prompt as it stands today and 24 under
  earlier versions; replaying those 24 would measure the prompt edit and the model swap together
  and report the sum as a model difference. The run is therefore scoped by default to records
  the current prompt produced (`match_prompt_version`).

Scoring is per-finding recall judged by a third model — the question is *did it report this
defect*, not *did it phrase it the same way*, so string comparison is the wrong instrument.
Severity weighting comes from the dataset, not the judge, so it cannot drift between runs.
Findings a candidate reports that the baseline missed are counted as `extra` and never
penalised: the baseline is a previous model, not ground truth.

**Concurrency is bounded by credit, not throughput.** OpenRouter reserves credit against every
in-flight request rather than charging only what a request finally costs, so running many
large-context replays at once returns `402 Payment Required: This request would exceed your
available credits given your current in-flight requests`. A 402 loses that replay instead of
queueing it. A full run at 8-way parallelism failed this way on well over half its jobs while a
3-record run at the same setting passed cleanly, so the symptom only appears at scale. Tune
`max_parallel` (default 3) and top up the balance before a wide run.

That key is shared organization-wide, so this ceiling is shared with real reviews happening at
the same time — a wide gym run can starve production PR reviews of credit, not just itself.

Results land in the run summary and in the `gym-summary` artifact.

**Member data.** The findings are model-written prose about source code, not member records.
Datadog's sensitive data scanner masks matches in the stored span before this ever reads them,
and the exporter scrubs email addresses again so the committed file doesn't depend on that
scanner's configuration.
