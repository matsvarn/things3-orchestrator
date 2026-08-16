# Clarify unclear work

Use this note when the right Things form depends on meaning only the owner can
supply.

## Find the deciding question

Use the title, notes, children, dates, tags, and location as clues. Treat the
owner's words as the source of purpose and importance.

Ask one short question when the answer can change the form, destination, or
date. Keep the existing item unchanged while the choice is open.

- For a broad Task, ask for the first visible action.
- For a vague Project, ask what finished looks like.
- For an empty Project, ask whether to plan it, keep it for later, or cancel it.
- For waiting work, identify what must happen and when follow-up becomes useful.
- For Someday, ask whether the idea is active now only when that choice matters.
- For an Area, ask which ongoing responsibility it represents.

When titles match, use the parent, notes, tags, and exact Things identity to
tell items apart. If they still match, ask the owner which item they mean.

Use headings when they reveal stable Project sections. Rename or reorder a heading
when the section meaning changes. Delete it with an organize draft's
`delete_headings`; never use item `lifecycle`. Deleting it keeps its Tasks in the Project.

For an exact edit, search a named existing item before editing it. Create only
when the owner asks to add. Use one `purpose=change` read and its context refs
in one commit. Define local refs before use; create parent tags before children.
For repeating work, search first, then use `purpose=recurrence` with the exact
Task id, then `purpose=change` only when editable context is needed. Change the
repeating template for future copies and the generated copy for the current
cycle. Batch both changes when both must match. A complete repeat rule keeps
the existing Task identity and metadata.
For a Task, Project, or heading order anchor outside the returned facts, use one bounded `include` lookup by exact id or unique active find. Use `after` for item or heading order; use `today_after` only for a Task on Today. Use `within` only with `find`; resolve an ambiguous include before preparing a commit.
Use `start=evening` for evening work. Stop repetition when new copies must stop.

Keep the answer in the owner's words. Finish when each unclear choice has one supported interpretation or one concise question for the owner.
