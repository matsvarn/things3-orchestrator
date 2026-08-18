# Review, merge, and organize

Select one view, one exact id, one find, or one `ids` list; never combine a view with id or find. A Project view uses `within` as `project:<id>`. An Area view uses `within` as `area:<id>` for the Area, loose tasks, and Projects. `view=audit` lists every active item once. `view=diagnostics` lists conflicts. Search named existing items and edit them; create only when asked to add. Define local refs before use and parent tags before children. Do not copy revisions.

`start=null` clears Today, Evening, Someday, and reminders and keeps the current Project or Area. `into=anytime` moves the item to root Anytime. `today_after` may follow a sibling moved to Today in the same commit.

## Weekly review or Area redesign

Start with `view=audit` or `view=diagnostics`, then `view=area` for each Area that needs a decision. Use `ids` for notes and checklists on up to 10 exact items; if `truncated_fields` is present, read that exact id. Separate what Things shows from what still needs judgment. Check Inbox, Projects without a useful next action, duplicates, waiting follow-ups, dates, Someday, and whether each Area is still one responsibility. Propose the smallest change set. Preserve all other work. Finish when every reviewed item and affected Area is accounted for, or one owner question remains.

Repair tag names and parent relationships when they reduce duplicate filters. Tag deletion uses `change_tags.delete_permanently`. Preserve rich notes unless the owner accepts a full Markdown replacement. Restore accidental cleanup.

When a change needs approval, ask one short question in the owner's words. Keep the plan ID private.

## Organize or merge

Read once with `purpose=organize`. Send one editable draft with ordered sections and `unlisted=keep`. Batch related normal changes with the draft. Use `context refs`. If structured recovery asks for new context, read and rebuild once. If a response is lost or a result is pending or unknown, repeat the same request with no new facts. Use structured recovery only for stale or expired context.

For a merge, organize the source and include the destination. Move the children you want to keep, then set the source Project to `lifecycle=trash`. Remaining descendants go to Trash with it.

## Delete

Use `lifecycle=trash` only for an ordinary Task or Project delete because it is recoverable. Project trash also moves remaining descendants to Trash; restore the Project to bring that subtree back. Every permanent Task or Project deletion target must already be in Trash, including Tasks and empty Projects. For a non-empty Project, read it completely, use `lifecycle=delete_permanently` with `delete_contents=true`, then approve the plan. Permanently delete only named Trash items.
