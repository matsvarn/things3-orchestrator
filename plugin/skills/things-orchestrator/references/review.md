# Review

Search named existing items and edit them. Create only when asked to add. Newly spoken loops during Empty your head or Be creative follow Capture, one Inbox Task each. If a living notes-hit hides a Trash title, search with `within=trash`. Do not copy revisions.

## Weekly review

Start with `view=diagnostics`, then `view=audit`. Then Inbox to zero, Today leftovers, `view=logbook`, `view=week`, Waiting, Projects with no useful next action, then Someday. `view=week` is Upcoming starts, not the calendar. Apple Calendar is not in Cloud. Ask them to scan it. Glance at Someday every couple of months.

Review lists return a context and short refs. A truncated page is not complete. Continue the same read to add the rest to that context, then commit keep, someday, trash, or file against it. `view=logbook` defaults to the last 14 days. Use `ids` for notes and checklists on up to 10 exact items. If `truncated_fields` is present, read that exact id.

Separate what Things shows from what still needs judgment. Check Inbox, Projects without a useful next action, duplicates, waiting follow-ups, dates, Someday, harness leftovers (`test_residue`), and whether each Area is still one responsibility. Propose the smallest change set. Preserve all other work. Finish when every reviewed item and affected Area is accounted for, or one owner question remains.

## Full reorganization

Use one opening sentence. Then report only a material finding, blocker, or owner choice. Do not narrate reads, retries, or the next lookup.

Read `view=diagnostics` and `view=audit` with `limit=40`. Continue the same audit read until complete. The audit replaces separate Area and Inbox scans. Read details only for items the change can affect. Open one Project and include all small affected Projects when the combined context stays below 120 items.

Distinguish Areas by ongoing responsibility, not item count. An empty or thin Area stays when the owner names that responsibility. Ask when its meaning is unclear. Do not remove it because it is sparse.

The owner may keep any tag. For a tag the agent proposes, name the repeated filter, show at least two real items, and check that a native view does not already answer it. Cross-Area use is evidence, not a requirement. When the owner asks for a new setup, propose the smallest provisional starter set from their named workflows. Define each filter and example items. Recheck it at the first useful weekly review. Create a review Task only when asked.

Resolve dependencies before writing. A Project title names its finished outcome. Its Tasks supply visible actions. An active Task inside a Someday Project needs an explicit owner choice. A grab-bag Project needs a coherent finish, a split, or cancellation. Do not finish with a known incoherent Project or a note that work "should probably" move later.

Preserve notes unless the owner accepts their exact replacements. After choices settle, show one exact before-and-after manifest. Include Area names and order, tag changes, every title and home change, lifecycle, dates, tag assignments, and full note replacements. Name permanent deletion, Trash, cancellation, note replacement, and date changes. State what remains unchanged.

Send the accepted full reorganization as one commit and one approval. Include Areas, order, tags, moves, title edits, organize drafts, and recoverable cleanup. Do not stage intermediate states. Split only when the tool schema or structured recovery requires it.

After approval, follow the result. When the receipt proves the requested order and changes, do not reread the full library. If the receipt reports pending, stale, partial, or a mismatch, do not report success. Retry only as instructed. The final reply gives mutation counts, final Area order, final tag catalog, and unresolved exceptions.

## Area redesign

An Area `id` lists its children. A Project `id` or `view=project` is the writable neighborhood. `view=area` also takes `within`.

Repair tag names and parent relationships when they reduce duplicate filters. Tag deletion uses `change_tags.delete_permanently`. Preserve rich notes unless the owner accepts a full Markdown replacement. Restore accidental cleanup.

When a change needs approval, ask one short question in the owner's words. Present the complete manifest and every destructive class. Keep the plan ID private.

## Organize or merge

Read once. The Project is Area, layout, hidden occupants, and Trash contents when applicable. `purpose=organize` is the editable draft, not a second look. Empty open sections can have hidden occupants. Send ordered sections with `unlisted=keep`, or `delete_headings` alone. Include another Project to organize both in the same commit. Batch related normal changes with the draft. Use context refs.

If structured recovery asks for new context, read and rebuild once. If a response is lost or a result is pending or unknown, repeat the same request with no new facts. Use structured recovery only for stale or expired context.

For a merge, organize the source and include the destination. Move the children you want to keep, then set the source Project to `lifecycle=trash`. Remaining descendants go to Trash with it. A heading can use `into` only to follow its source Project during that merge.

## Delete

Use `lifecycle=trash` only for an ordinary Task or Project delete because it is recoverable. Project trash moves remaining descendants to Trash. Restore the Project to bring that subtree back. Use `organize.delete_headings` or heading `lifecycle=delete_permanently` to delete Project headings. Assigned work stays in the Project without the heading.

Use `change_tags.delete_permanently` for tag deletion. Every permanent Task or Project deletion target must already be in Trash, including Tasks and empty Projects. Do not use `view=trash` alone to tear a Project down. Read the Project. If it is in Trash, that read lists the contained records. Use `lifecycle=delete_permanently` with `delete_contents=true`, then approve the plan. Permanently delete only named Trash items.
