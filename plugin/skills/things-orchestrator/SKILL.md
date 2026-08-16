---
name: things-orchestrator
description: Manage an owner's work in Things 3. Use for capture, search, review, scheduling, changes, completion, cancellation, deletion, repetition, or organization. Also use for Things lists, Projects, Areas, headings, waiting work, checklists, notes, or tags.
---

# Things

Use the owner's words and natural Things terms. Say what you found, changed, or
need the owner to choose. Keep internal planning labels out of the reply.

Use `things_read`, `things_commit`, and `things_approve`; follow each returned `next` and `instruction`.

Use the shortest safe path. Read each existing target once. Batch all coherent
creates, edits, structure, and cleanup. Ask once when the batch needs approval.

When a change needs approval, ask one short question in the owner's words. State
the visible change and its important consequence. Keep the plan ID, expiry,
receipt, revisions, status names, and tool instructions private. Continue after a clear yes.

## Choose the smallest useful form

- Use a Task for one clear action. Start its title with the visible action and
  name the object or result.
- Add a Things checklist for small, known steps that share the Task's finish.
- Use a Project when several actions can finish separately. Add only the next
  one to three useful actions unless the owner supplied a fixed plan.
- Add headings when they make a multi-part Project easier to scan. Keep each
  Task under the heading that describes its current group.
- Put context, links, plans, and finish criteria in Markdown notes. Keep
  executable steps in Things checklist rows or Project Tasks.
- Use an Area for an ongoing responsibility without a finish.
- Use a start date when work becomes available. Use a deadline for a real
  latest finish. Add a reminder only for a useful, time-specific start cue.
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

Use Trash for an ordinary delete because it is recoverable. Restore it when the
owner reverses that choice. Read Trash when the exact item is not known. Permanently delete only after an explicit request.
For a Project, account for its contents in the same approved cleanup.

Preserve rich notes by default. Replace rich formatting with Markdown only
when the owner wants a full replacement and accepts that visible consequence.

For repeat changes, read the generated copy and its repeating template. Change
the template for future cadence and the generated copy for current work. Stopping repetition keeps linked copies as ordinary Tasks.

When one unclear choice can change the Task, Project, destination, or date,
keep the item as it is and ask one short question.

Read [clarifying unclear work](references/task-system.md) when a Project, Area,
waiting item, or Someday choice is unclear. Read [reviewing the system](references/reconcile.md)
for a weekly review, full review, or Area redesign. Read [decision research](references/research.md)
when current external facts can change what belongs in Things.

Finish when every requested item is accounted for. Give the owner verified facts, a verified change, or the one needed question.
