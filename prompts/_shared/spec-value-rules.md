A spec is useless if its failure would neither reveal a regression worth fixing nor
point at what to fix.

Report it only for one of these reasons:
- It asserts the implementation back to itself, such as by stubbing the object under
  test and checking the stub, comparing a literal with itself, checking only
  `respond_to?` or `be_a`, or checking that a method calls its stubbed collaborator.
- It tests something whose failure would already break louder coverage, such as factory
  validity, a widely traversed route, or a framework guarantee.
- It has no assertion tied to its named behavior, so unrelated changes can fail it
  without identifying the cause. Request a narrower assertion, not deletion.
- It pins private internals instead of an observable outcome, so safe refactors fail it
  while behavior regressions can pass. BiggerPockets does not test private methods.

Do not flag:
- A spec that catches a plausible bug, however small or obvious the behavior looks.
- A regression spec for a fixed bug.
- Edge, boundary, nil, or error-path coverage, or required flag-enabled and
  flag-disabled pairs. Repeated setup is not redundancy.
- Missing coverage. That is a completeness finding.
- A spec the diff only moves, renames, or reindents.

State the bug the spec fails to catch and the assertion that would catch it. This finding
never blocks a PR by itself. Report it and approve unless another finding blocks.
