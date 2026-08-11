If the diff includes a schema migration, check whether it also updates data rather than
only structure — e.g. enabling/disabling/renaming a feature flag, backfilling a column, or
other one-time data changes. Flag these: that kind of change belongs in a one-off rake
task, done directly via the console/UI, or another one-time maintenance mechanism, not
baked into a schema migration.