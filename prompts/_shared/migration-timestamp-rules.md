Check the version timestamp of every migration file the diff adds under `db/migrate/` (the
leading `YYYYMMDDHHMMSS` in a name like `20261124153712_add_index_to_users.rb`):

- If the timestamp ends in `0000` (minutes and seconds both zero, e.g. `20261124000000` or
  `20261124150000`), request changes. `rails g migration` stamps the current time to the
  second, so a round on-the-hour version was almost certainly typed by hand. Hand-picked
  versions collide with other migrations, and a version that sorts before migrations already
  run on other branches or environments can be skipped or run out of order. Ask the author
  to regenerate it with `bin/rails g migration` and update the matching version in
  `db/schema.rb` or `db/structure.sql`.
- Apply this only to migration files the diff adds. Do not report migrations that already
  exist on the base branch.
