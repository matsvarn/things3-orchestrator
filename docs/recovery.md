# Recover an operation

Read-only tools and `things_receipt` remain available while an outcome fence
blocks writes.

If an operation is `pending`, do not retry it as a write. Force read-back from
the CLI; this never reposts the frozen writes:

```console
uv run things-orchestrator operation-reconcile op_EXAMPLE
```

If read-back proves that none of the writes landed, settle it with the signed
owner factor:

```console
uv run things-orchestrator operation-settle-not-applied op_EXAMPLE
```

If an operation is `partial`, do not retry or start corrective work. Inspect
its exact receipt, then record one CLI-only resolution:

```console
uv run things-orchestrator operation-accept-partial op_EXAMPLE accepted_as_is
uv run things-orchestrator operation-accept-partial op_EXAMPLE superseded
```

Resolution performs no Cloud write. Corrective work uses a fresh request ID
after the fence releases.

If a risky operation is `awaiting_owner`, show, approve, or decline it in a
private local or SSH terminal. Expired or changed preconditions make it
`stale` and write nothing.

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
legacy fence.
