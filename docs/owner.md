# Use Things Orchestrator v2

Install the supervised HTTP service with [install.md](install.md) before the
first client session.

Ask to read Today, Inbox, Week, Repeating, Logbook, Projects, Areas, tags, or
Trash. Search by a distinctive title fragment, or read exact IDs.

Captures create Tasks or Projects. A new Project may include nested new Tasks.
Either kind may have a fixed or after-completion repeat. Updates change only
explicit item-local fields: the title, notes, start, deadline, reminder, or an
RT1 repeat rule. Repeat updates can pause, resume, stop, or create the next
copy. Stop keeps generated copies, materializes the hidden template as a fresh
ordinary item on the template's next date, then removes its old graph. Project
headings, Tasks, and checklist rows are preserved with fresh IDs. Stop applies
directly through the authenticated v2 mutation path. Create Next and Stop require Things' native next date; if it is
absent, the server returns `read_fresh` and writes nothing. Create Next does the
same when that native date already has a generated copy. Completion and
recoverable Trash use their own tools.

Completing a Project completes its open action descendants in the same frozen
operation, excluding structural headings and hidden repeat templates. RT2
recurrence facts are read-only. The server rejects RT2 schedule, repeat, and
lifecycle writes.

Every mutation carries a fresh UUID or ULID. The client may reuse it only for
the exact same transport retry.

Recoverable Trash and repeat Stop apply directly. A configured MCP bearer or
stdio connection authorizes every bounded v2 mutation; there is no separate
owner approval step.

If the server reports blocking operation IDs, stop writes. Retry the exact same
pending request to settle it through read-back without replaying the Cloud
write. A fully classified `partial` is terminal and includes its receipt. Any
correction is a new operation with a fresh request ID.

Advanced Project scopes, coaching mutations, Areas and tag-registry mutation,
rich-note replacement, and permanent deletion are deferred.
