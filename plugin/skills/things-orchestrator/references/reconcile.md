# Review, merge, and organize

Select one view, one exact id, or one find; only a Project view uses `within`, and never combine a view with id or find. Search named existing items and edit them; create only when asked to add. Define local refs before use and parent tags before children. Do not copy revisions.

## Weekly review or Area redesign

Review the full requested range. Separate what Things shows from what still needs judgment. Check Inbox, Projects without a useful next action, duplicates, waiting follow-ups, dates, Someday, and whether each Area is still one responsibility. Propose the smallest change set. Preserve all other work. Finish when every reviewed item and affected Area is accounted for, or one owner question remains.

Repair tag names and parent relationships when they reduce duplicate filters. Tag deletion uses `change_tags.delete_permanently`. Preserve rich notes unless the owner accepts a full Markdown replacement. Restore accidental cleanup.

When a change needs approval, ask one short question in the owner's words. Keep the plan ID private.

## Organize or merge

Read once with `purpose=organize`. Send one editable draft with ordered sections and `unlisted=keep`. Batch related normal changes with the draft. Use `context refs`. If structured recovery asks for new context, read and rebuild once. If a response is lost or a result is pending or unknown, repeat the same request with no new facts. Use structured recovery only for stale or expired context.

For an atomic merge, move every active visible direct child to an active destination, then set the source Project to `lifecycle=trash` only in one commit. If completed, trashed, template, or hidden children exist, do not use atomic merge; choose separate safe cleanup.

## Delete

Use `lifecycle=trash` only for an ordinary Task or Project delete because it is recoverable. Every permanent Task or Project deletion target must already be in Trash, including Tasks and empty Projects. For a non-empty Project, read it completely, use `lifecycle=delete_permanently` with `delete_contents=true`, then approve the plan. Permanently delete only named Trash items.
