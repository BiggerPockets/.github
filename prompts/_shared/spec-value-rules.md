A spec earns its place by failing. Judge every spec the diff adds or changes by one
question: if a plausible bug were introduced in the code under test, would this spec go
red, and would that failure reveal a regression worth fixing and point at what to fix? A
spec that answers no is useless — it spends suite time and review attention and buys no
protection — and saying so is a real finding, not a nitpick.

Report a spec as useless only when you can name the reason it cannot fail usefully:
- It asserts the implementation back to itself: it mocks or stubs the very object under
  test and then asserts the stub was called, asserts a literal equals itself, checks only
  `respond_to?`/`be_a`, or expects a method to call the collaborator it just stubbed.
  Change the behavior and it still passes.
- It only fails when something louder fails first: asserting a factory is valid, that a
  route exists which dozens of other specs already traverse, or re-testing a framework
  guarantee (that `validates :x, presence: true` rejects nil, that `belongs_to` returns
  the associated record).
- Its failure names nothing actionable: a broad end-to-end example with no assertion tied
  to the behavior it is named for, so it goes red on any unrelated change and leaves the
  author bisecting to learn why. Ask for a narrower assertion here rather than deletion.
- It pins a private method's internals instead of an observable outcome, so it goes red on
  a safe refactor and stays green on a behavior regression. BiggerPockets does not test
  private methods.

Do NOT flag:
- A spec whose failure you can attribute to a bug someone could plausibly introduce, even
  if the spec looks small or the behavior looks obvious.
- A regression spec for a fixed bug. Pinning behavior that was once wrong is valuable
  precisely because the code now looks like it could not fail.
- Edge-case, boundary, nil, and error-path examples, or the flag-enabled and flag-disabled
  pair this repo requires. Near-duplicate setup is not redundancy.
- A spec that is missing. Absent coverage is a completeness finding, not this one.
- Specs the diff only moves, renames, or reindents.

State the finding as the bug the spec fails to catch — "this passes whether or not
`#deactivate!` persists the score, because the service is stubbed" — and suggest the
assertion that would catch it. A useless spec is not blocking on its own: report it and
still approve unless the review blocks on something else.
