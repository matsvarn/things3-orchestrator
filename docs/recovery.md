# Recover an operation

Read-only tools and `things_receipt` remain available while an outcome fence
blocks writes.

If an operation is `pending`, retry the exact same tool arguments and
`request_id`. The server reconciles read-back only. It never reposts writes.

If an operation is `partial`, do not retry or start corrective work. Inspect
its exact receipt, then record one host-only resolution:

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
approvals and read back every unresolved legacy fence from the account journal.
