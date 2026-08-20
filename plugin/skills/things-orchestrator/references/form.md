# Form

Use the owner's words and natural Things terms. Preserve the owner's dates and importance. Do not infer urgency. Preserve anything the owner did not ask to change.

If this turn is a thread, changelog, or source packet, research owns the complete branch. Do not continue here.

## Choose the smallest useful form

Walk these questions in order. Stop when the form is named.

1. More than one sitting, and the finish is unclear? Ask only when two supported finishes or forms remain. Ask one Project or two only when both readings fit. Otherwise use the sole supported Project.
2. Will one sitting finish it? Yes: a Task. Write for the owner who will reopen Things without this chat. The title is a natural action phrase that names the object and visible result. Natural does not mean terse. Known sub-steps of that sitting, packing, a shopping list, a known process: a Things checklist. Rows share that finish. Write call, draft, open, buy, list, read. Do not write Decide, Think about, Work on, or Assess. A blocking step goes first.
3. No: a Project. Several Tasks finish separately. If the result or inclusion of work is undecided, create only the next one to three useful actions after the owner chooses. When the owner commits to one durable result, create its complete supported finish path now in dependency order. The first Task is available now. The owner need not preapprove each Task title. Each title is physical, visible, and startable when reached. Order shows dependencies; do not invent Waiting or dates. If a Task needs a path or URL, put it in that Task's notes. Opening the Project is not required to start. Projects cannot enter Inbox. Two named finishes are two Projects, or one after they say they share a finish. Independent finishes are Project Tasks, not checklist rows. Do not file Read after sources were already gathered. A gathered source can still require an evidence result. Optional, maybe, or continual work stays off this Project.
4. Extra info to do the work: distill it. A Project may use semantic fields. `outcome` names the durable result. `finished_when` holds its finish criteria. `keep_in_mind` holds only shared constraints. A Task may add one `finish`, plus `start_here`, `approach`, and structured `sources` when useful. Source Projects require these fields as [research](research.md) specifies. Ordinary capture and explicit note edits may still use Markdown notes. Write from the owner's viewpoint: `my chats`, not `the owner`. Do not paste the brief. Keep executable steps in checklist rows or Project Tasks. A committed later action is a Project Task. The server renders Project note styling. Send `note_style` only for an explicit one-time owner choice.

```
{
  "outcome": "One kitchen tap is ordered.",
  "finished_when": ["The chosen tap fits the sink and budget."],
  "keep_in_mind": ["Measure the sink before comparing taps."]
}
```

Each source is `{label, location}` on the Task that uses it. Use a short label and the full URL or path. Do not write source Markdown.

5. Ongoing hat with no finish: Area.
6. Not acting now: Someday.
7. Someone else's ball: Waiting, `waiting=true`.
8. When: start date when work becomes available. Deadline for a real latest finish. Reminder only for a useful, time-specific start cue.
9. Reusable filter: Tag.

Notes explain context or method. If notes contain a required prerequisite, selection, owner review, validation, or delivery, split it into its own Task. Each Task produces one visible result or performs one delivery. Add the artifact type to a Project title when the owner's label can mean a workflow, mode, or skill.

Headings scan a multi-part Project. For six or more Tasks across two or more distinct stages, infer two to four short natural headings that describe the owner's path. Use explicit owner-named headings at any size. Rename or reorder a heading when the section meaning changes. Delete it with `organize.delete_headings`. `lifecycle=trash` is recoverable teardown. Deleting it keeps its Tasks in the Project.

Checklist only on a Task. `tasks` only on a Project. Use it for the whole ordered plan when every Task needs only a title, finish, semantic context, native checklist, and `heading_title`. Research uses `document=source` and a `finish` on every Task. Use `heading_title` on every Task or none. Heading groups stay contiguous. If any Task needs another native field, create every Task as a sibling. Do not mix the two forms on one Project. Each title is physical, visible, and startable when reached. Put a needed source in that Task's `sources`.

Never infer or browse for an Area during create. Use one only when the owner names it or an existing matching item proves it.

Stop planning only after every done-when clause and every required result is complete now or has one Task. The owner can start, has the needed context, and can tell when the work is finished.

## Inbox filing

Process Inbox only when the owner asked to review that list.

1. Read `view=inbox`. Continue the same read if the page is truncated.
2. One row at a time. What is it? Is it actionable? Walk Choose the smallest useful form for that row.
3. Bind the existing item. Split mashed finishes into separate items.
4. Set When only from their words. Today, Evening, a start day, Anytime, or Someday. If they named meaning and did not name When, Anytime. Do not leave a filed row in Inbox.

If they dumped several loops and did not ask to process Inbox, Capture owns the split. Research owns a source packet.
If they did not ask to process Inbox, stop. Capture or Research owns that turn.

Done when every reviewed row has a meaning, or one question is open.

## Unclear form

Ask one short question. Keep the existing item unchanged.

- Broad Task: ask for the first visible action.
- Vague Project: ask what finished looks like. Do not create it.
- Empty Project: plan it, keep it for later, or cancel it.
- Waiting: what must happen, and when follow-up is useful.
- Someday: whether it is active now, only when that choice matters.
- Area: which ongoing responsibility it represents.

Finish when each unclear choice has one supported reading or one concise question.
