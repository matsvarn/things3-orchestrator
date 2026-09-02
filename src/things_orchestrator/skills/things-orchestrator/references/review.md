# Review and change

Use `things_view`, `things_find`, and `things_get` to inspect current work. Use
`things_update` only for explicit ordinary item-local fields. Use
`things_complete` and `things_trash` for their named lifecycle actions.

Every mutation gets a fresh UUID or ULID. Recoverable Trash applies directly.
If a result is `pending`, retry only the exact same request ID and arguments so
the server can reconcile read-back without reposting the write. Stop all writes
only when `blocking_operation_ids` is nonempty. A terminal `partial` includes
the receipt needed before sending any fresh corrective request.
