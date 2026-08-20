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

#### 1. Install the Claude GitHub app and add its auth token

The Claude verification stage uses [`anthropics/claude-code-action`](https://github.com/anthropics/claude-code-action),
which needs two things: the official [Claude GitHub app](https://github.com/apps/claude)
installed, and a `CLAUDE_CODE_OAUTH_TOKEN` secret it can authenticate with. From a clone of
the target repo, run the slash command in Claude Code:

```
/install-github-app
```

It walks you through both — but the two halves have very different scopes:

- **App install — once for the whole org.** If the Claude app is already installed
  org-wide, skip the app-installation step; you do **not** need to reinstall it per repo.
- **Auth token — per repo.** `/install-github-app` writes `CLAUDE_CODE_OAUTH_TOKEN` as a
  **repo** secret, not an org secret, so this is the part you actually need on each new repo.
  If you'd rather set it once, add `CLAUDE_CODE_OAUTH_TOKEN` as an **organization secret** by
  hand and skip this command entirely.

You need **admin access** on the repo and an authenticated `gh` CLI. (If the command fails,
install the app manually from https://github.com/apps/claude and add the token by hand.)

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
two AI review providers (`OPENROUTER_API_KEY` for the Codex stage, which reaches its model
through OpenRouter's Responses API), an Atlassian email + API token to fetch the PR's JIRA ticket for
intent, and a personal access token for the BiggiePockets service account that submits the
review. (The Claude auth secret is set by `/install-github-app` in step 1; the rest you add
yourself.) Configure them as **organization secrets** (recommended — set once, available to
every repo) or as per-repo secrets if you prefer to scope them.

It also reports per-review traces to the `biggiepockets-review` app in Datadog LLM
Observability via `secrets.DATADOG_API_KEY`: verdict, timing, prompt template and version
(tracked as prompts, see below), the model each
stage ran (`CODEX_MODEL`/`CLAUDE_MODEL` env vars in the workflow — `CODEX_MODEL` is an
OpenRouter model slug and must be set; leave `CLAUDE_MODEL` empty to use Claude's own default), and the actual findings text from Codex and the summary Claude wrote,
so review quality is inspectable, not just counted. This secret is optional — reviews still
run and post normally without it, but no metrics are reported.

The exact secret names each step expects are visible in the `env:` and `with:` blocks of
[`.github/workflows/biggiepockets-review.yml`](.github/workflows/biggiepockets-review.yml).

#### 4. Give BiggiePockets access

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

### Prompt registry and the shadow-arm experiment

The review-stage prompts are not inline in the workflow. They live in this repo under
`prompts/` and are resolved at runtime by `scripts/resolve-prompts.sh`:

```
prompts/
  registry.json                              # arms + gate_arm + codex prompt name
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
- **Shadow-arm comparison.** Every review runs an experiment arm (`thesis-first`) in
  addition to the gate arm — two independent Claude passes over the same Codex findings
  (arms and their prompts live in `registry.json`; one non-gate arm today). Only the gate
  arm (`control`) reaches the PR: it writes the posted summary and the official approve /
  request-changes decision. The experiment arm's summary is never posted, so a reviewer
  sees exactly one review; its verdict goes to Datadog only, where `arm_agreement` and
  `experiment_verdict` say whether the variant would have decided differently and which
  way. `registry.json` is the only switch. An `arms` map holding nothing but `gate_arm` leaves
  every review single-arm, so a Merge that drops the last experiment arm turns the
  comparison off everywhere at once — and until it does, every review pays for a second
  Claude synthesize pass.
- **Prompt Tracking.** Every LLM span carries the prompt that produced it under
  `meta.input.prompt` — the registry template with its `{{PR}}`-style placeholders intact,
  plus the values that filled them as `variables`, plus `id`/`name`/`version`. Keeping the
  placeholders is what makes each prompt one tracked prompt in Datadog rather than a new
  template per PR, so the [Prompts view](https://app.datadoghq.com/llm/traces) shows call
  volume, latency, tokens, and a version diff per prompt, and any span can be replayed in
  the Playground with its exact template and variables. A version starts when the prompt
  text changes (a Roll), since `version` is the same content hash reported as a tag.
- **Datadog.** Each arm is reported as **its own trace**, so every trace holds exactly one
  arm and carries unambiguous `arm`/`prompt_name`/`prompt_version` tags you can group and
  aggregate on. One trace per arm is required, not stylistic: Datadog resolves a tag key at
  trace scope, so putting both arms in one trace collapses those keys onto whichever span
  was written last and credits one arm's review to the other's prompt.

  ```
  gate trace        biggiepockets.review → codex.review, claude.synthesize.gate
  experiment trace  biggiepockets.review → claude.synthesize.experiment
  ```

  Codex runs once and feeds both arms, so it sits in the gate trace rather than being
  duplicated (which would double-count its latency and tokens); its findings are still the
  recorded input of both arms' spans. Compare arms at the `claude.synthesize.*` spans, which
  are like-for-like — the gate trace's root also spans codex, so root durations are not.
  Both traces share a stable `run_id` (`repo-pr-runid`) and carry `verdict` (the gate arm's
  decision, the one that posts), `experiment_verdict` (the other arm's, so a disagreement
  records which way it went — variant stricter or laxer — and not merely that one happened;
  `n/a` on single-arm runs), and `arm_agreement` (agree/disagree between the arms), so
  offline evals and panel ratings join to the exact review.

**Registry operations** (kept distinct so a formatting experiment can't silently change the
production prompt):

- **Roll** — edit a prompt or shared-rule file; its content-derived `prompt_version` bumps.
- **Apply** — point an arm or `gate_arm` at a different stored prompt in `registry.json`
  (no version change). Callers tracking `@main` pick the change up on their next run. A
  caller that pins `uses:` to a tag or SHA needs BOTH that `@ref` and its `registry_ref`
  input bumped in lockstep — Apply owns that ref-bump explicitly.
- **Split** — add a new arm entry in `registry.json` + its prompt file.
- **Merge** — fold a variant's content into another prompt and remove the arm.

A `validate-prompts.yml` workflow guards the registry: it fails a PR if a template has
dangling includes, `registry.json` references a missing prompt, the resolver isn't
deterministic, or a shared-rule edit doesn't bump versions.
