---
name: things-orchestrator
description: Manage an owner's work in Things 3. Use for capture, search, review, scheduling, changes, completion, cancellation, deletion, repetition, or organization.
---

# Things

Say what you found. Say what changed. Ask when the owner has to choose.

Use `things_read`, `things_commit`, and `things_approve`. Follow each returned `next` and `instruction`.

## Capture

Create once. Title is the action plus the object. Drop filler: remind me, please, to, the.

Example: "Remind me to renew my passport." → create `Renew passport` in Inbox.

For a Project with named next actions, keep those exact titles. Example: replace the kitchen tap → project `Replace kitchen tap` with `Find three suitable taps`, `Measure the sink`, `Order one`.

Do not search first unless a matching item is likely already there.

## Tags

Rename or reparent a tag with `view=tags`, then `change_tags`.

Never find a tag with `purpose=change`.

Assigning an existing tag to a Task is a Task change: find the Task, then add the tag.

## Change

Read with one distinctive title token. Never send the owner sentence as `find`.

Examples:

- "The contract must be signed by 4 September." → find `contract`
- "Mark Test build done and rename Draft notes." → find `Test build`
- "Order the packing checklist" → find `Pack`

If the find is empty, retry one shorter token. Do not create.

Then one commit on the returned item. Use the returned ref.

## Schedule

First bind the existing item with one title token (`Sam`, `invoice`, `contract`).

- Evening → `start=evening`. Not a clock time. Not a reminder.
- Today → `start=today`.
- A real latest finish → `deadline`.
- A reminder needs a clock time. If none was given, ask. Do not invent one.

Do not create a second Task. "Remind me about X" changes existing X.

## Clarify

When two items match, or the target is not unique, ask one short question.

If the owner asked for a Project but did not say what finished looks like, ask. Do not create that Project and do not invent next actions.

If a date is given without saying start or deadline, ask which it is. Do not set either.

Change nothing until the owner answers.

Do not open Today or Inbox unless the owner asked to review a list.

A Project is one read: Area, layout, hidden occupants, and if it is in Trash, the contained records. Trash then permanent. Do not use `view=trash` alone. If a permanent delete is not named as exact Trash items, list the candidates and ask. Do not start a permanent-delete plan.

Read [form, headings, and repeats](references/task-system.md) when the Things form, a heading, or a repeat rule is the work. Read [reviewing the system](references/reconcile.md) for a weekly review, Area redesign, merge, teardown, or `purpose=organize`. Read [decision research](references/research.md) when current external facts can change what belongs in Things.
