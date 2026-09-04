# Recover an operation

Read-only tools and `things_receipt` remain available while an outcome fence
blocks writes.

If an operation is `pending`, retry the exact same tool, request ID, and
arguments. The server force-refreshes and classifies current Cloud state; it
never reposts the frozen writes. `operation-reconcile` provides the same
read-back from a private host terminal:

```console
uv run things-orchestrator operation-reconcile op_EXAMPLE
```

Before dispatch, frozen before-state evidence can settle the operation as
`not_applied`. After dispatch starts, that evidence alone is insufficient
because a delayed commit may still land. Only a provider response that proves
the POST was rejected (currently HTTP 409) can then settle `not_applied`. Every
write matching becomes `applied`; a fully classified mixed result becomes
terminal `partial` and includes its exact receipt. Timeouts, unreachable
responses, HTTP 500 responses, and unavailable read-back remain `pending`.
Corrective work uses a fresh request ID.

Legacy `awaiting_owner` rows from older builds are retired as `stale` without
Cloud I/O. Never replay their stored batch. Read current Things state and send
a fresh request if the change is still wanted.

Retained v1 `prepared` and `needs_approval` rows are quarantined. Retained
`pending` rows block v2 writes until current Cloud state classifies them.
Multiple unresolved retained rows block all writes. Never select or discard one
automatically.

Run `uv run things-orchestrator migration-report` to quarantine retained v1
approvals and list every unresolved legacy fence from the local account journal.
This command does not contact Things Cloud.
For a retained pending row with a complete frozen v1 write plan, run
`uv run things-orchestrator legacy-reconcile INTENT_ID`. It force-refreshes,
classifies current evidence, and never replays the old write. Only evidence
that every desired write is current settles the row as applied. Partial,
none-matched, malformed, or otherwise unknown evidence remains fenced.

After inspecting the journal backup and current Things state, explicitly record
the owner decision without a Cloud write:

```console
uv run things-orchestrator legacy-resolve INTENT_ID accepted_as_is
uv run things-orchestrator legacy-resolve INTENT_ID superseded
```

This resolution requires the CLI-only owner factor and durably records the
classification, resolution, and signed authorization before releasing the
legacy fence. The CLI renders the complete retained plan with terminal-control
escaping before it asks for the passphrase. Resolution atomically verifies the
signed fingerprint and full-plan digest, then scrubs the legacy plan into a
content-minimized tombstone.

## Recover routines

Routine failure does not hold the MCP mutation fence. Public health and MCP
requests remain available if Cloud polling, SQLite, or webhook delivery stops.

Start by stopping new polls and deliveries:

```console
things-orchestrator routines disable
things-orchestrator service install
things-orchestrator routines status
things-orchestrator support-bundle
```

A changed Things history identity automatically clears the tag projection and
unsettled candidates, then performs a new tag-only baseline. It preserves
pending, delivered, and dead event rows and does not replay historical tasks.

Pending deliveries retry with the same event ID. A receiver acknowledgement
can be lost after the receiver acted, so recovery depends on receiver-side
deduplication. Hermes sends that ID as `X-Request-ID`. Grok sends it as
`event_id` in the body. Grok may start a duplicate Bot run after
accept-before-commit. The Bot instruction must say: "Treat event_id as the
idempotency key and refuse to act if you have already acted on that event_id."
Delivered rows remain as compact tombstones.
Dead rows retain only the metadata body and attempt result. This first slice has
no automatic dead-letter replay command.

If the routines database is damaged or ownership cannot be acquired, keep
routines disabled and restore the matching `routines.json` and account-scoped
database from a private backup. Do not delete the database to force a retry:
that removes deduplication tombstones and creates a new account event namespace.
Inspect receiver records before deciding how to handle a dead event.
