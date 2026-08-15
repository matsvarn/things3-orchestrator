# Owner guide

Once [scripts/setup](../scripts/setup) and one client in
[clients.md](clients.md) are done, ask in normal language.

New titles are short action phrases in the language of existing titles.
The model can keep native checklists, Markdown notes, tags, order, and waiting
state. Existing-item changes use fresh revision facts.

The server evaluates Today, Logbook, and reminders in the timezone stored by
`login`. Run `login --timezone Europe/Berlin` again after a permanent move.

The model has three tools. `things_read` gets current, bounded facts.
`things_commit` applies routine changes. Only Area changes, broad batches, and
closing a Project with open actions need approval. These changes write nothing
until you accept them. The model asks one plain question and keeps tool IDs
private. Each write needs a Cloud read-back before the tool reports success.

The tool schemas tell the model how to send safe requests and recover from a
stopped call. The model skill helps it select a small, useful Things form. It
also keeps internal tool terms out of the reply.

## Talk

- “Capture a task to renew my password in Things.”
- “What should I focus on in Things today?”
- “Review my Areas and suggest one cleanup.”

Current limits are in the [README](../README.md#current-limits).

Follow what the model asks. Tools missing or a write stops:
[recovery.md](recovery.md).
