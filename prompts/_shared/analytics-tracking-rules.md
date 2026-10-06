The React frontend already has a generic mechanism for tracking a feature flag's exposure in
Segment/Amplitude: `ENABLED_FEATURES_TO_TRACK` in `frontend/utils/analytics.ts`. Each entry
pairs a flag name with a path `constraint`, and `trackSegmentPage` uses it to identify the flag
as a boolean user trait and include it in the page event's `enabled_features` array whenever the
constraint matches — with no per-component code.

When a diff adds a NEW Segment/Amplitude event (a `useTrackSegmentEvent`/`trackSegmentEvent`
call, or a new `useEffect` whose only job is firing one) whose sole purpose is recording that a
reader saw something gated by a feature flag — not a distinct user action like a click or a
form submission — check whether adding that flag to `ENABLED_FEATURES_TO_TRACK` would capture
the same signal (the flag being on for a reader on the relevant page(s)) before the bespoke
event is treated as necessary. Flag it with a file/line reference when it looks like exposure
tracking has been reinvented per-component rather than registered once in the shared list, and
suggest adding the flag/constraint entry instead.

Don't flag it when the event carries information the shared mechanism can't express — e.g. a
property tied to specific content the flag doesn't determine (which record, which variant of
several), a count of how many times something happened rather than whether a flag was on, or an
event that fires on a genuine interaction (a click, a submission) rather than passive exposure.
The rule targets exposure-only tracking of a flag's on/off state, not tracking in general.
