# Owner guide

Once [scripts/setup](../scripts/setup) and one client in
[clients.md](clients.md) are done, ask in normal language.

New titles are short action phrases in the language of existing titles.
The model can keep native checklists, Markdown notes, tags, order, and waiting
state.

The server evaluates Today, Logbook, and reminders in the timezone stored by
`login`. Run `login --timezone Europe/Berlin` again after a permanent move.

The model has three tools. `things_read` gets current, bounded facts.
`things_commit` applies routine changes. Area changes, broad batches, Trash,
repeat-rule or future-template changes, registry cleanup, rich-note replacement, permanent
deletion, and closing a Project with open actions need approval.
These changes write nothing until you accept them. The model asks one plain
question and keeps tool IDs private. Each write needs a Cloud read-back before
the tool reports success.

The tool schemas tell the model how to send safe requests and recover from a
stopped call. The model skill helps it select a small, useful Things form. It
also keeps internal tool terms out of the reply.

A clear capture normally takes one tool call. An exact edit normally takes one
focused `purpose=change` read and one commit. A repeat-rule change first uses
`purpose=recurrence` with the exact Task id to inspect the native template and
generated-copy relationship. If the request also changes normal Task fields,
make a separate `purpose=change` read for the target before using contextual
refs; do not reuse recurrence facts as change refs. A Project restructure uses
one complete Project read and one editable desired-state draft. Work that the
draft does not list stays in place. The model can include related edits in the
same commit. If the context changes or expires, structured recovery tells the
model which fresh read it needs. Risky work adds one approval step.

The model asks one short question and changes nothing when a date is not named
as start or deadline, a reminder has no clock time, two items match, a new
Project has no stated outcome, or a permanent delete does not name exact Trash
items.

## Talk

- “Capture a task to renew my password in Things.”
- “What should I focus on in Things today?”
- “Review my Areas and suggest one cleanup.”
- “Make this repeating task run every two weeks.”
- “Repeat this every Monday and Friday.”
- “Restore the task I just moved to Trash.”
- “Group this Project with headings and clean up its old tag.”
- “Put a date on the contract for 4 September.”
- “Remind me about the invoice at 09:00.”

Capability evidence is in the [proof matrix](capability-proof.md).

Follow what the model asks. Tools missing or a write stops:
[recovery.md](recovery.md).
