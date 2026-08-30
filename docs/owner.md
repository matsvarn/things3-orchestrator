# Use Things Orchestrator v2

Install the local service with `scripts/setup` before the first client session.

Ask to read Today, Inbox, Week, Repeating, Logbook, Projects, Areas, tags, or
Trash. Search by a distinctive title fragment, or read exact IDs.

Captures create Tasks or Projects. A new Project may include nested new Tasks.
Either kind may have a fixed or after-completion repeat. Updates change only
explicit item-local fields: the title, notes, start, deadline, reminder, or an
RT1 repeat rule. Repeat updates can pause, resume, stop, or create the next
copy. Stop keeps generated copies, materializes the hidden template as a fresh
ordinary item on the template's next date, then removes its old graph. Project
headings, Tasks, and checklist rows are preserved with fresh IDs. Stop requires
host approval. Create Next and Stop require Things' native next date; if it is
absent, the server returns `read_fresh` and writes nothing. Completion and
recoverable Trash use their own tools.

Completing a Project completes its open action descendants in the same frozen
operation, excluding structural headings and hidden repeat templates. RT2
recurrence facts are read-only. The server rejects RT2 schedule, repeat, and
lifecycle writes.

Every mutation carries a fresh UUID or ULID. The client may reuse it only for
the exact same transport retry.

Recoverable Trash and repeat Stop return `awaiting_owner`. Chat confirmation
cannot approve them. Use `operation-show` and `operation-approve` on the host.
Enroll the owner factor once with `owner-factor`.

Restart the server after enrolling or rotating that factor so the journal pins
the new public verification key.

If the server reports blocking operation IDs, stop writes. Pending work may
settle through read-back. A partial needs `accepted_as_is` or `superseded` on
the host. Any correction is a new operation.

Advanced Project scopes, coaching mutations, Areas and tag mutation, checklist
editing, rich-note replacement, and permanent deletion are deferred.
