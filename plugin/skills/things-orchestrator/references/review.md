# Review and change

Use `things_view`, `things_find`, and `things_get` to inspect current work. Use
`things_update` only for explicit ordinary item-local fields. Use
`things_complete` and `things_trash` for their named lifecycle actions.

Every mutation gets a fresh UUID or ULID. If `things_trash` returns
`awaiting_owner`, surface its operation ID and do not replay it. The owner
reviews and approves it with the host command. Continue unrelated writes; stop
all writes only when `blocking_operation_ids` is nonempty.
Never use chat confirmation as approval.
