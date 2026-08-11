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
      # Optional: the BiggerPockets/.github ref to resolve review prompts from. Defaults
      # to the workflow's own ref if omitted. Recommended when pinning `uses:` to a
      # specific ref/SHA above — keep the two in sync so prompts stay pinned too.
      registry_ref: main
    secrets: inherit
```

#### 3. Make the secrets available

The reusable workflow consumes several secrets via `secrets: inherit`: credentials for the
two AI review providers, an Atlassian email + API token to fetch the PR's JIRA ticket for
intent, and a personal access token for the BiggiePockets service account that submits the
review. (The Claude auth secret is set by `/install-github-app` in step 1; the rest you add
yourself.) Configure them as **organization secrets** (recommended — set once, available to
every repo) or as per-repo secrets if you prefer to scope them.

It also reports per-review traces to the `biggiepockets-review` app in Datadog LLM
Observability via `secrets.DATADOG_API_KEY`: verdict, timing, prompt version, the model each
stage ran (`CODEX_MODEL`/`CLAUDE_MODEL` env vars in the workflow — leave empty to use each
tool's own default), and the actual findings text from Codex and the summary Claude wrote,
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

### Prompt registry and the dual-arm summary experiment

The review-stage prompts are not inline in the workflow. They live in this repo under
`prompts/` and are resolved at runtime by `scripts/resolve-prompts.sh`:

```
prompts/
  registry.json                              # arms + gate_arm + codex prompt name
  codex-first-pass.md                        # Stage 1 prompt (template)
  claude-synthesize.md                       # Stage 2 control arm (template)
  claude-synthesize-thesis-first.md          # Stage 2 thesis-first arm (template)
  _shared/{privacy,migration-data,perf}-rules.md   # shared rule blocks
```

- **Templates + shared blocks.** Each prompt references the shared rule blocks via
  `{{@prompts/_shared/<name>.md}}`, so the Codex and Claude prompts can never drift out of
  sync. Prompts resolve `{{PR}}`, `{{PROMPT_NAME}}`, `{{PROMPT_VERSION}}` too.
- **Content-derived versions.** `prompt_version` is a content hash of the template plus the
  shared blocks it includes — it changes only when that prompt's text changes, not per PR
  or per arm, so Datadog LLM Obs can attribute quality to the exact prompt text that ran.
- **Dual-arm, within-PR comparison (opt-in).** Add the **`biggiepockets-dual-arm`** label to a
  PR to run an experiment arm (`thesis-first`) in addition to the gate arm — two independent
  Claude passes over the same Codex findings (arms and their prompts live in `registry.json`;
  one non-gate arm today). Both summaries are posted in a single review comment labeled
  **Variant A** / **Variant B** in a per-PR-randomized order (deterministic hash of the PR
  number, so it is balanced across PRs and stable across re-reviews). The arms are not
  disclosed in the comment. The official approve / request-changes gate always comes from
  `gate_arm` (`control`) — the experiment only changes presentation, never the decision.
  PRs **without** the label get the single gate-arm review (one summary, no A/B), i.e. the
  pre-experiment behavior. The label is also the gradual-rollout switch: enable/disable per PR
  with no code change.
- **Datadog.** Each LLM run is a separate span tagged with its `prompt_name`/`prompt_version`
  and `arm`, plus a stable `run_id` (`repo-pr-runid`), `verdict`, `arm_agreement`
  (agree/disagree between the arms), and `label_assignment` (`A=control|B=thesis-first` or
  the reverse) so offline evals and panel ratings can be joined to the exact review.

**Registry operations** (kept distinct so a formatting experiment can't silently change the
production prompt):

- **Roll** — edit a prompt or shared-rule file; its content-derived `prompt_version` bumps.
- **Apply** — point an arm or `gate_arm` at a different stored prompt in `registry.json`
  (no version change). Rolling a change out to PRs that pin `registry_ref` means bumping
  BOTH the `@ref` in that caller's `uses:` AND its `registry_ref` input, in lockstep —
  Apply owns that ref-bump explicitly. Callers that omit `registry_ref` pick up the change
  as soon as the `@ref` they `uses:` moves.
- **Split** — add a new arm entry in `registry.json` + its prompt file.
- **Merge** — fold a variant's content into another prompt and remove the arm.

A `validate-prompts.yml` workflow guards the registry: it fails a PR if a template has
dangling includes, `registry.json` references a missing prompt, the resolver isn't
deterministic, or a shared-rule edit doesn't bump versions.
