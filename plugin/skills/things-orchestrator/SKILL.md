---
name: things-orchestrator
description: Research a thread, changelog, dump, or mode before you write. Use for capture, Today, Inbox, weekly review, schedule, change, complete, cancel, delete, repeat, or organize in Things 3. Write only accepted work. One finish per title.
---

# Things

Say what you found. Say what changed. Ask when the owner has to choose. During work, report only a material finding, blocker, or owner choice. Do not narrate tool loading, retries, or the next lookup.

Use `things_read`, `things_commit`, and `things_approve`. Follow each returned `next` and `instruction`.

Thread, changelog, docs, links, dump, from this, adopt vs skip, or set up a mode: read [research](references/research.md) before any write. A source packet is not automatically a dump. If one durable result is clear and the owner authorized creation, research then create in the same turn.

Split finishes. Distill. Do not paste a brief, thread, or changelog. Do not write Decide, Think about, or Work on. For a required choice, name the visible result: `Review and mark the proposed rules`.

The server asks and writes nothing when a create is still a dump. Follow `ask`.

Process Inbox: read [form](references/form.md).

Before you create a Project, checklist, headings, or more than one sitting, read [form](references/form.md). If this turn is an unresolved dump, research owns it. Ask only when two supported readings change the durable result or Things form. Do not create that Project and do not invent next actions while that choice is open.

Routine creates apply at once. Do not promise a plan or another approval. Follow the tool if it returns an approval step.

Weekly review or Area: read [review](references/review.md). Merge, teardown, or `purpose=organize`: read [review](references/review.md).

## Capture

Create each loop once in Inbox. Two decided actions are two Inbox Tasks. Title is the action plus the object. Drop filler: remind me, please, to, the.

Example: "Remind me to renew my passport." → create `Renew passport` in Inbox.

If they named a reusable filter, same commit `ensure_tags` with that exact title and `tag_ids` as that `$key`. Do not invent a second tag name. Do not open tags first. `waiting=true` already reuses Waiting. If they named an existing Area or Project, send `into_title` in that same commit. Unnamed stays Inbox.

If they already named a Project and its next actions, keep those exact titles. A sitting that needs a path or URL gets notes on that Task in the same commit. Do not file Read after sources were already gathered.

When the owner commits to one durable result, create its complete supported finish path in dependency order. The first Task is available now. Keep known later Tasks visible; do not hide them in notes. A plan is open only when the result or inclusion of work is undecided, not because the owner did not preapprove each Task title.

Do not search first unless the owner signals that matching work may exist. If it does, bind that item instead of creating a second copy.

Never infer or browse for an Area during create. Use one only when the owner names it or an existing matching item proves it. Set start, deadline, reminder, Today, or headings only when the owner named them or the accepted Project stages need headings.

Open Today or Inbox only when they asked to review that list.

Done when each loop is one Inbox Task with an action-plus-object title, or research already stopped the turn.

## Today

Open Today only when they asked to focus or review that list.

Read `view=today` or send an empty read. Continue the same read if truncated. Sections are Overdue, Evening, Today, and Waiting. Waiting is not a next action. Do not invent calendar events.

Ask what they will start before the day ends, what to postpone, and in what order. Change nothing until they answer. Evening is `start=evening`. A later day is a start date. Anytime is `start=null`. Arrange with `today_after`. Write existing items only.

Done when keep, postpone, and order are answered, or the turn was a read with one question.

## Tags

Rename or reparent a tag with `view=tags`, then `change_tags`.

Never find a tag with `purpose=change`.

Reuse `tag:` ids from a prior result. If none is in hand, `ensure_tags` plus that `$key` on the Task. `view=tags` only for rename or reparent.

## Change

Read with one distinctive title token. Never send the owner sentence as `find`.

Examples:

- "The contract must be signed by 4 September." → find `contract`
- "Mark Test build done and rename Draft notes." → find `Test build`
- "Order the packing checklist" → find `Pack`

If the find is empty, retry one shorter token. Do not create.

When two items match, or the target is not unique, ask one short question. Change nothing until the owner answers.

Then one commit on the returned item. Use the returned ref.

## Schedule

First bind the existing item with one title token (`Sam`, `invoice`, `contract`).

- Evening → `start=evening`. A start, not a clock time.
- Today → `start=today`.
- A real latest finish → `deadline`.
- A reminder needs a clock time. If none was given, ask. Do not invent one.

If a date is given without saying start or deadline, ask which it is. Do not set either.

Do not create a second Task. "Remind me about X" changes existing X.

To repeat a Task, search first, then `purpose=recurrence` on the exact Task. Confirm the current copy and template. Change the repeating template for future copies. Change the generated copy for the current cycle. Change the generated copy for current work. Batch both changes when both must match. A complete repeat rule on an ordinary Task keeps it as the current copy and creates its future template. Stopping repetition keeps linked copies as Tasks.

## Delete

A Project is one read: Area, layout, hidden occupants, and if it is in Trash, the contained records. Trash then permanent. Do not use `view=trash` alone. If a permanent delete is not named as exact Trash items, list the candidates and ask. Do not start a permanent-delete plan.
