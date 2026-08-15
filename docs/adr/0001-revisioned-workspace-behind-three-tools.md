# Revisioned workspace behind three tools

The model-facing Interface is `things_read`, `things_commit`, and
`things_approve`.

One `ThingsWorkspace` Module owns identity, revision checks, task-system
meaning, risk, approvals, idempotent intents, and result instructions. The
model sees exact public IDs. It does not see Things Cloud wire fields.

`things_read` returns fresh, bounded facts. Exact item reads include native
checklists, Markdown notes, tags, order, recurrence facts, and a revision.

`things_commit` captures new work without a prior read. Existing work needs an
exact ID and revision. A durable `intent_id` binds retries to one payload.
Routine work applies at once. Broad, Area, and risky Project changes return an
immutable plan and write nothing.

`things_approve` accepts only the staged `plan_id`. It checks expiry and all
bound revisions before it writes.

The Cloud adapter coalesces changes by UUID before commit. It forces a Cloud
pull after commit and verifies the pulled state. An uncertain outcome returns
`retry_same`; the journal prevents a blind repost.

We rejected CRUD tools because they repeat identity and safety rules. We
rejected caller-authored Cloud operations because they expose a shallow seam.
We keep recurrence rule writes disabled until their Cloud behavior is proven.
