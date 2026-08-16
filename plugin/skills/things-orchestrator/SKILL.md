---
name: things-orchestrator
description: Manage an owner's work in Things 3. Use for capture, search, review, scheduling, changes, completion, cancellation, deletion, repetition, or organization. Also use for Things lists, Projects, Areas, headings, waiting work, checklists, notes, or tags.
---

# Things

Use the owner's words and natural Things terms. Say what you found, changed, or
need the owner to choose. Keep internal planning labels out of the reply.

Use `things_read`, `things_commit`, and `things_approve`; follow each returned `next` and `instruction`. Select one view, one exact id, or one find; only a Project view uses `within`, and never combine a view with id or find.

Use the shortest safe path:

- Clear capture: commit once.
- Exact edit: search named existing items and edit them; create only when asked to add. Read once with `purpose=change` and an exact item or a unique find. Use returned context refs and send the ref alone. For a Task, Project, or heading order anchor outside the returned facts, add a bounded `include` lookup with one exact id or one unique active find. Use `after` for item or heading order; use `today_after` only for a Task on Today. Use `within` only with `find`; do not build a global registry. If an include is missing or ambiguous, choose from its candidates and read again. For an atomic Project merge, move every active visible direct child to an active destination Project, then set the source Project to `lifecycle=trash` only in one approved commit. If completed, trashed, template, or hidden children exist, do not use atomic merge and choose separate safe cleanup. Do not target a completed, trashed, deleted, hidden, or detached destination. Do not copy revisions. Batch related changes in one commit.
- Repeat change: search first, then read with `purpose=recurrence` and the exact Task id. Confirm the current copy and template, then use `purpose=change` only when editable context is needed. Change the template for future work and the generated copy for the current cycle. Batch related changes in one commit.
- Project restructure: read the Project once with `purpose=organize`, using its exact id or one unique Project `find`. The read includes source members plus bounded active destination Project refs and their Area anchors. Send one editable draft with ordered sections. Define local refs before use and parent tags before children. Use heading and Task refs; delete headings with `organize.delete_headings`, never `lifecycle`; tag deletion uses `change_tags.delete_permanently`. Create only useful headings. Use `unlisted=keep`. Batch related normal changes with the draft.
- Project merge: use an exact source Project `purpose=organize` read. For every active visible direct child, send one contextual change with the child ref and an active destination Project ref as `into`; include the source Project ref with `lifecycle=trash` only in the same commit. The approved batch moves all source children, preserves headings and assignments, checks the destination Project and Area revisions, then trashes the now-empty source. If completed, trashed, template, or hidden children exist, do not use atomic merge; choose separate safe cleanup. If the bounded context is incomplete or ambiguous, follow its structured recovery.
- Follow structured recovery. Get the requested new context and rebuild once.
- If a response is lost or a result is pending or unknown, repeat the same request with no new facts. Do not rebuild from a fresh read; use structured recovery only for stale or expired context.
When a change needs approval, ask one short question in the owner's words. State the visible change and its important consequence. Keep the plan ID, expiry, receipt, revisions, status names, and tool instructions private. Continue after a clear yes.

## Choose the smallest useful form

- Use a Task for one clear action. Start its title with the visible action and name the object or result.
- Add a Things checklist for small, known steps that share the Task's finish.
- Use a Project when several actions can finish separately. Add only the next
  one to three useful actions unless the owner supplied a fixed plan.
- Add headings when they make a multi-part Project easier to scan. Keep each
  Task under the heading that describes its current group.
- Put context, links, plans, and finish criteria in Markdown notes. Keep
  executable steps in Things checklist rows or Project Tasks.
- Use an Area for an ongoing responsibility without a finish.
- Use a start date when work becomes available. Use a deadline for a real
  latest finish. Add a reminder only for a useful, time-specific start cue. Use semantic `start=evening` for evening work.
- Use repeating work for a real cadence. Use fixed mode for a calendar rhythm.
  Use after-completion mode when the next cycle starts after this one finishes.
- Use a tag when it will help the owner filter similar work later. Prefer an
  exact match. Nest it only when the parent gives stable, useful context.
- Use Someday when the owner wants to keep an idea outside active work.

Preserve the owner's dates and importance. Do not infer urgency. Add a date,
reminder, repeat, tag, heading, or more structure only for a useful reason.

Stop planning when the owner can start, has the needed context, and can tell
when the work is finished. Let a later review shape steps that can still change.

## Work with the owner

Capture clear, accepted work directly. For changes or reviews, use current
Things facts and preserve anything the owner did not ask to change.

Use `lifecycle=trash` only for an ordinary Task or Project delete because it is recoverable. Restore it when the owner reverses that choice.
Every permanent Task or Project deletion target must already be in Trash, including Tasks and empty Projects. For a non-empty Project, read it completely, use `lifecycle=delete_permanently` with `delete_contents=true`, then approve the plan. Permanently delete only after an explicit request.

Preserve rich notes by default. Replace rich formatting with Markdown only when the owner wants a full replacement and accepts that visible consequence.

For repeat changes, the recurrence read verifies the generated copy and its repeating template before mutation. Change the template for future work and the generated copy for current work. A complete repeat rule keeps the current Task and creates its future template. Include other requested edits in that commit. The current checklist keeps its completion state.
Future checklist rows start open. Stopping repetition keeps linked copies as Tasks.

When one unclear choice can change the Task, Project, destination, or date, keep the item as it is and ask one short question.

Read [clarifying unclear work](references/task-system.md) when a Project, Area,
waiting item, or Someday choice is unclear. Read [reviewing the system](references/reconcile.md)
for a weekly review, full review, or Area redesign. Read [decision research](references/research.md)
when current external facts can change what belongs in Things.

Finish when every requested item is accounted for. Give the owner verified facts, a verified change, or the one needed question.
