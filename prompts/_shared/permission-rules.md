Permission rules belong in Pundit policies under `app/policies/`. Flag code that decides
for itself who may do something when a policy predicate already answers the question, and
name the predicate to call instead:
- An inline check where a policy exists — `user.admin?`, `user.moderator?`,
  `current_user.pro?` and the like, written into a controller, serializer, payload object,
  view, or job. Grep `app/policies/` for a predicate covering the same question before
  accepting one. A duplicated rule gives the same answer today and diverges silently the
  moment the policy widens: a serializer that decided for itself who may see a link goes on
  hiding it from readers who are now allowed to open it, and no test fails.
- Reaching past a policy to the record for a state the policy already names — calling
  `author.cooling_off?` where `Forums::PostPolicy#cooling_off?` exists, for instance. This
  is easy to miss because the model call is correct in isolation and cheap: judge it on
  whether it is the right abstraction, NOT on whether it is a column read. A single
  attribute read that duplicates a policy predicate is still a finding.
- Serializers and payload objects are the most common place for this and the least likely
  to be caught, because the permission decision is one key among many in a hash. Review
  them as carefully as controllers.
- Copying an inline check from surrounding code or from `main` does not excuse it. If the
  diff touches the line, it should route through the policy.
Not a finding: an inline check where no policy exists for that question, or where the
controller expresses its rule as a bare `before_action` filter (`ensure_moderator`,
`moderator_status_required`) with no policy behind it. There the nearest policy predicate,
or the filter's own condition, is the best available. Also not a finding: a policy method
that reads a record attribute — that is where such a read belongs.
