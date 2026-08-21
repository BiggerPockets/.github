# Review prompt evals

A regression set for the BiggiePockets review prompts in `prompts/`. Each case is a real
pull request whose correct review outcome is known, usually because the review that
actually ran got it wrong and the defect was found later. Running the set answers one
question: would today's prompts catch it?

The runner lives in the repository whose pull requests the cases point at
(`.github/workflows/eval-review-prompts.yml` in `BiggerPockets/biggerpockets`), because
the reusable review workflow reviews the caller's own repo. It calls the same review
workflow that posts real reviews, with `dry_run: true` so nothing is submitted, and
scores each resulting `verdict.json` with `evals/score.sh`.

Run it against a prompt branch before merging a prompt change:

```
gh workflow run eval-review-prompts.yml --repo BiggerPockets/biggerpockets \
  -f registry_ref=<your prompt branch>
```

Every case costs one Codex pass plus one Claude pass, so the set is run on demand rather
than on every prompt push.

## Adding a case

Write `evals/cases/<id>.json`:

```json
{
  "id": "short-kebab-case-id",
  "pr": 12345,
  "why": "What the diff does, what is wrong with it, and what the real review did.",
  "expect_verdict": "request_changes",
  "must_match": [["pull-requests", "permission scope"], ["403", "permission"]],
  "notes": "Anything a later reader needs to judge whether the assertions are fair."
}
```

`must_match` rows are ANDed and the entries within a row are ORed, matched
case-insensitively against the review summary. The point of a row is to separate "found
this bug" from "found some other bug and requested changes anyway" — a case whose only
assertion is the verdict proves very little on its own. Matching cannot judge whether the
reasoning was sound, so keep rows broad enough to survive different phrasing and put
anything a reader needs to re-judge the case in `notes`.

A case needs a stable pull request to point at. A merged PR works: the runner diffs
`refs/pull/<n>/head` against the merge base, so the diff stays what it was. Two things to
watch:

- Do not point a case at a PR whose discussion contains the answer. The prompts weigh PR
  comments, so a thread that explains the defect turns the case into a reading exercise.
  When the original PR has been annotated after the fact, open a replay PR at the original
  head and close it, and point the case at the replay.
- Keep at least one case whose expected verdict is `approve`. Without one, a prompt that
  requests changes on everything scores perfectly.
