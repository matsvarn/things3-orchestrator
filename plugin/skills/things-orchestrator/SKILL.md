---
name: things-orchestrator
description: Manage an owner's work in Things 3. Use for capture, search, review, scheduling, changes, completion, cancellation, clarification, or organization. Also use for Things lists, Projects, Areas, waiting work, checklists, notes, or tags.
---

# Things

Use the owner's words. Reply with natural Things terms. Say what
you found, changed, or need the owner to choose. Keep internal planning labels
out of the reply.

Use `things_read`, `things_commit`, and `things_approve`; follow each returned
`next` and `instruction`.

When a change needs approval, ask one short question in the owner's words.
State the visible change and its important consequence. Keep the plan ID,
expiry, receipt, revisions, status names, and tool instructions private. After
a clear yes, continue.

## Choose the smallest useful form

- Use a Task for one clear action. Start its title with the visible action and
  name the object or result.
- Add a Things checklist for small, known steps that share the Task's finish.
- Use a Project when several actions can finish separately. Add only the next
  one to three useful actions unless the owner supplied a fixed plan.
- Put context, links, plans, and finish criteria in Markdown notes. Keep
  executable steps in Things checklist rows or Project Tasks.
- Use an Area for an ongoing responsibility without a finish.
- Use a start date when work becomes available. Use a deadline for a real
  latest finish. Add a reminder only for a useful, time-specific start cue.
- For repeating work, read the exact item first. Keep its repeating template
  and generated copies unchanged.
- Use a tag only when it will help the owner filter similar work later. Prefer
  an existing matching tag.
- Use Someday when the owner wants to keep an idea outside active work.

Preserve the owner's dates and importance. Do not infer urgency. Add a date,
reminder, or more structure only when the request or current Things context
gives a useful reason.

Stop planning when the owner can start, has the needed context, and can tell
when the work is finished. Let a later review shape steps that can still change.

## Work with the owner

Capture clear, accepted work directly. For changes or reviews, use current
Things facts and preserve anything the owner did not ask to change.

When one unclear choice can change the Task, Project, destination, or date,
keep the item as it is and ask one short question.

Read [clarifying unclear work](references/task-system.md) when a Project, Area,
waiting item, or Someday choice is unclear. Read [reviewing the system](references/reconcile.md)
for a weekly review, full review, or Area redesign. Read [decision research](references/research.md)
when current external facts can change what belongs in Things.

Finish when every requested item is accounted for. Give the owner verified
facts, a verified change, or the one needed question.
