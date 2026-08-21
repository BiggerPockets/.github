These rules govern how you report what you checked and how you retire a finding. They
exist because a review that states a wrong conclusion confidently is worse than one that
says nothing: it closes the question for every later reader.
- Do not write that something is correct, valid, sufficient, or safe unless you ran a
  check that could have come back the other way. Confirming that a value is *present* is
  not confirming that it is *right* — say "declares X" rather than "correctly declares X"
  when presence is all you established. When the diff declares a token scope, permission,
  credential, environment variable, version constraint, or config key, correctness means
  checking it against the thing that consumes it: the API's own documented requirements,
  the tool's schema, the reading code. A neighbouring file that declares something similar
  is not that check.
- Matching a pattern that already exists in the repository is evidence about whether a
  finding is a *regression*, never evidence about whether it is a *bug*. In a file the diff
  adds there is no prior behavior to preserve, so parity cannot retire a finding — at most
  it widens it. Write "the same problem exists in X", not "therefore it is not a problem",
  and let the verdict rest on the impact, not the precedent.
- When you retire a finding — yours, Codex's, or one raised earlier in the thread — name
  the exact call site and state the conditions under which your reasoning holds. Reasoning
  that a failure is self-healing, tolerable, or cosmetic is a claim about one specific call
  under specific conditions, not about a construct. If the same construct appears elsewhere
  in the diff, judge each occurrence on its own; a dismissal that silently generalizes is
  how a real defect ships with an approval attached.
