Request changes for comments the diff adds or rewrites that a reader does not need. Code
says what it does; a comment earns its place only by telling the next reader something the
code, its names, and its tests cannot: why it is written this way, a hazard that is not
visible at the call site, or a constraint that comes from outside the code. Every other
comment is noise the next change has to read past and keep in sync, and an AI-written
change tends to add a great deal of it.

Report each of these as blocking, under category `maintainability`:

- Restating the code. A comment that says in prose what the next lines or the name already
  say: `# Adds the default plan, unless it is already there.` above
  `def self.add_default_plan` with a `find_or_create_by!`, `# The rows in the file at path`
  above `def self.rows_in(path)`, `# Imports every row in the file` above a one-line loop,
  or a spec helper's comment describing what its name and body show.
- Narrating history, process, or the change itself. Where the data came from step by step,
  which ticket or deploy step runs it, what a previous version did, what was considered and
  dropped, what "now" happens, or which PR added it. That belongs in the commit message or
  the PR description. A single line naming a source the code cannot show (an export's
  origin URL and date) is fine; a paragraph walking through how it was produced is not.
- Explaining the surrounding system where it is not used. A migration that describes the
  product feature the column will serve, a class header that tours which other classes call
  it and why, a constant comment that re-documents what another file already documents, or
  a chain of "see X, which is loaded by Y" cross-references.
- An essay where a line would do. A comment that carries a genuine reason but takes a
  paragraph to say what one or two lines can, such as a dozen lines on why a cache key
  includes the locale where "Keyed on locale so a translated page never serves another
  language's copy." says it all.
- Repeating the obvious about a library or language. Explaining what `find_or_create_by!`,
  `update_all`, `disable_ddl_transaction!`, or a standard React hook does.
- Restating the type, the arguments, or the return value when the signature and the
  body already make them plain.

Do not report:

- A comment that gives a non-obvious reason, a workaround and what it works around, a
  hazard the next editor would otherwise walk into, an ordering or concurrency constraint,
  or a unit or format the code cannot express, as long as it is about as short as that
  reason allows. `# Too big to load with every record; read and written through SQL only.`
  above an `ignored_columns` line is the kind of comment to keep.
- A comment another rule in this review requires, such as a backfill stating whether it
  fires callbacks or a workaround naming the upstream defect. Those still have to be short.
- Comments tools read: magic comments, linter directives, schema annotations, type
  annotations, license headers, and documentation a generator publishes for a public API.
- A comment the diff only edits to keep it true, such as updating a count it states.
- Comments the diff does not add or rewrite. Pre-existing noise is not this change's
  defect.

Report all of a file's unneeded comments as one finding. Keep it compact: list the line
numbers to delete, and give replacement wording only for an essay that should shrink to a
line. Do not soften this to "consider trimming": a PR that adds comments of the kinds above
gets `request_changes`.
