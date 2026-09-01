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

If read-back proves that none of the writes landed, the operation becomes
terminal `not_applied`. If every write landed, it becomes `applied`. A fully
classified mixed result becomes terminal `partial` and includes its exact
receipt. Corrective work uses a fresh request ID.

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
