# Common-caller tools with host-owned approval

## Status

Accepted and implemented for the default-eight cutover. Advanced scopes and
coaching remain deferred release gates.

## Problem

The three v1 tools hide a large caller language. A caller must choose read
purposes, copy revisions or context references, build operation arrays, order
local dependencies, interpret retry instructions, and mediate approval. The
transaction implementation already owns useful safety behavior. It validates
revisions, journals an intent before Cloud I/O, batches writes, forces a Cloud
pull, and reconciles uncertain outcomes.

V2 keeps that behavior and replaces the caller interface. Approval also moves
outside MCP. The model can propose work, but it cannot authorize it.

## Caller usage

Capture one Task without a preliminary read:

```json
{
  "tool": "things_capture",
  "arguments": {
    "request_id": "0198f0ee-98d4-7bd5-91ba-8e76019b2735",
    "items": [{"kind": "task", "title": "Renew passport"}]
  }
}
```

Update an exact item without copying a revision:

```json
{
  "tool": "things_update",
  "arguments": {
    "request_id": "0198f0ef-3923-79b6-96a8-2bf28eac0d67",
    "items": [
      {
        "id": "task:abc",
        "set": {
          "title": "Send signed contract",
          "deadline": "2026-09-04"
        }
      }
    ]
  }
}
```

After the default eight have independently passed their release gate, inspect a
broad Project only when the owner enabled advanced scope editing before server
startup:

```json
{
  "tool": "things_open_scope",
  "arguments": {"scope": {"kind": "project", "id": "project:abc"}}
}
```

`things_submit_scope` accepts the returned opaque scope ID and a desired
document. It is Project-only, bounded to that Project, and always requires
owner approval. Omitted fields and members remain unchanged. It cannot address
another Project, Areas, tags, recurrence, Trash, rich-note replacement, or
permanent deletion.

## Public interface

The default MCP interface has eight tools:

- `things_view` reads Today, Inbox, Week, Logbook, Projects, Areas, tags, or
  Trash.
- `things_find` searches by owner text and optional exact container.
- `things_get` reads one to fifty exact item IDs.
- `things_capture` creates one atomic batch of Tasks or Projects, optionally
  including new Tasks nested under a new Project.
- `things_update` sets only explicitly named ordinary item-local fields on one
  atomic batch of existing items. It cannot complete, trash, reorder, edit
  structure, checklists, recurrence, registries, or permanently delete.
- `things_complete` completes one atomic batch. A Project must have no open
  actions, and its complete Project scope stays frozen through application.
- `things_trash` moves one atomic batch to recoverable Trash.
  Project Trash freezes and renders the complete descendant scope.
- `things_receipt` returns immutable operation rows with restart-safe
  pagination.

Only after the default eight pass their own gate, an owner configuration fixed
at server startup may also expose:

- `things_open_scope` returns one complete, bounded, writable document.
- `things_submit_scope` computes and submits the minimal internal change set.

Mutation calls require an opaque UUID or ULID `request_id`. The server owns current reads,
revisions, preconditions, risk, ordering, local references, plans, and
read-back. None of those fields appear in default mutation schemas.

The idempotency key is `(account, API version, request_id)`. Its request hash
covers the tool name, schema version, and canonical arguments. The same hash
returns the same immutable operation; a different payload is rejected.
Terminal operations never reprepare. A request rejected by an existing account
fence creates no operation and does not consume its request ID.

Permanent deletion is not available in MCP or the current CLI release.

## Domain shape

Every mutation becomes one immutable operation:

```text
OperationDraft
  canonical v2 command
  complete private preconditions
  requested effects

OperationManifest
  account binding
  API and schema versions
  request ID and request hash
  operation ID
  safety-policy digest
  complete manifest and manifest hash
  preconditions
  ordered internal writes
  state
```

Operation state is one of:

```text
awaiting_owner
pending
applied
unchanged
not_applied
partial
partial_resolved
stale
declined
rejected
```

Each state permits only its own transition data. The wire adapter projects the
state into flat MCP JSON for clients that reject JSON Schema unions.

Every observed title, note, checklist row, and tag label has source
`things_cloud` and trust `untrusted`. Anything derived from those values keeps
that taint. Tainted data may appear only in marked data fields. It cannot
determine instructions, states, actions, IDs, preconditions, risk, approval,
dispositions, or recovery. Host rendering escapes control characters,
ANSI/OSC sequences, newlines, and ambiguous delimiters. Human labels are
supplementary to typed IDs and actions.

Preservation is an invariant, not a caller or mutation profile: omitted fields
and members remain unchanged. Operations bind a versioned safety-policy digest.
Owner configuration may increase caution but never weaken it. `gtd-coach`, if
later justified, is a separate read-only workflow. It cannot populate writes,
alter defaults or manifests, or change risk.

On the first sight of a mutation request, the server force-refreshes Cloud and
records every affected target, parent, destination, membership, ordering, tag,
Project-layout, and recurrence precondition needed by that action. An operation
never reparses, rebases, or adopts newer state. A changed precondition produces
`stale`; continuation requires a new request ID.

## Owner authority

An MCP mutation that requires approval returns `awaiting_owner` and its
operation ID. It does not return the manifest hash, an approval token, or an
approval tool.

The interactive host command loads the operation from the account journal and
renders every manifest entry, warning, preservation claim, and destructive
effect. A TTY is only a routing boundary; it is not proof of a human owner. The
command therefore requires a second owner factor that is unavailable to MCP.
The initial implementation uses a human-entered approval passphrase whose
salted `scrypt` verifier is available only to the CLI approval component. The
raw passphrase is never stored, accepted through arguments, environment
variables, ordinary stdin pipes, or generic `yes`, or emitted to logs. A
terminal can still be automated and does not prove human presence. The MCP
server and agent runtime receive no approving capability. A deployment without
this separation cannot execute approval-required operations or permanent
deletion.

The CLI session uses a local or SSH terminal with access to the private account
configuration. Approval binds successful owner-factor verification, the account,
API version, action, operation ID, tool, canonical manifest hash, safety-policy
digest, and expiry.

The command rechecks current Cloud state before execution. A changed
precondition produces `stale` and writes nothing. An expired approval also
becomes `stale`.

## Outcome fence and receipts

The legal state machine is:

```text
new -> rejected | stale | awaiting_owner | pending
awaiting_owner -> pending | stale | declined
pending -> applied | unchanged | not_applied | partial
partial -> partial_resolved
```

Only `awaiting_owner`, `pending`, and `partial` are nonterminal. There are no
durable `prepared` or `ready` states. For routine work, immutable operation
creation, the `pending` state, and account-fence claim happen in one
`BEGIN IMMEDIATE` SQLite transaction. For approved work, authorization,
`awaiting_owner -> pending`, and the fence claim happen in one such transaction.
Two processes that use the same state database cannot claim different
operations for the same account.

Terminal settlement, its response, all receipt rows, and the receipt hash are
one journal transaction. An apparently unchanged request first claims the
account fence as `pending`, then refreshes and rechecks its frozen preconditions
and desired observations before settling `unchanged` without a Cloud write.

The fence remains for `pending` and `partial`. It covers every write path:
ordinary MCP mutation, CLI approval, retained-v1 reconciliation, and future
adapters. Read-only calls and receipt
inspection continue. Another mutation returns the blocking operation ID,
creates no operation, consumes no request ID, and writes nothing. A pending
fence cannot be blindly force-cleared.

Recovery may force-refresh and observe a pending operation from the CLI. If
read-back proves that no frozen write landed, a signed CLI action can settle it
as `not_applied`. Recovery never reposts the old writes. A partial operation records every applied and
not-applied row and never replays the remainder. Exact CLI-only resolution
records `accepted_as_is` or `superseded`, atomically moves
`partial -> partial_resolved`, and releases the fence without Cloud I/O. Any
corrective work is a fresh operation with a fresh request ID and manifest.

Receipt rows contain a stable sequence, action, exact target identity, result,
and only the touched fields or state needed to verify that action. They do not
copy complete untouched notes, checklists, or tags. Pagination authenticates
the account, operation ID, next sequence, receipt hash, and cursor version. It
never substitutes `N more` for omitted rows. Canonical manifest and receipt
hashing are explicitly versioned.

Full settled manifests and receipt bodies use a fixed seven-day retention
period. Pending and partial rows are not pruned.
Expired awaiting-owner rows become `stale` and may then be pruned. After
pruning, the journal keeps a content-minimized tombstone with the request hash,
operation ID, final state, manifest hash, and receipt hash so an old retry
cannot create duplicate work. It retains no raw request ID or owner text.

## Module placement

- `v2.py` owns public models, tool descriptions, private immutable
  `OperationDraft` and `OperationManifest` models, translation into shared
  application primitives, and taint-preserving output projection.
- `workspace.py` remains the one transaction engine during migration. It owns
  preparation, preconditions, risk, plans, application, and reconciliation.
- `journal.py` owns immutable operation creation, compare-and-set state
  transitions, CLI approval state, the account fence, append-only exact
  receipt rows, tombstones, and retention.
- `server.py` is the MCP adapter. It exposes v2 tools and no approval path.
- `cli.py` owns CLI-only operation display, owner-factor enrollment and
  verification, approval, decline, partial resolution, and retention and
  caution configuration.
- `cloud.py` remains the Things Cloud adapter and forced read-back authority.

V2 does not create a second transaction engine, but it also does not compile
into the v1 public language. It never constructs `CommitCall`, contexts, short
refs, local keys, or v1 source-document and GTD heuristics. V1 and v2 may share
only private preparation, application, read-back, and reconciliation
primitives. Transaction lifecycle code may move into a smaller module only
when that extraction deletes duplicated ownership.

## Migration

1. Build the journal and authority foundation without exposing v2 tools:
   immutable operation creation, compare-and-set transitions, the account
   fence, append-only receipts, retention and tombstones, owner authorization,
   and the retained-v1 classifier.
2. Add the private `OperationDraft -> OperationManifest` seam and two vertical
   slices: one routine `things_capture` path and one approval-required
   `things_trash` path.
3. Complete and independently prove the default eight, including no-rebase
   idempotency, cross-process fencing, pending reconciliation, partial
   resolution, hostile text, exact pagination, pruning, and restart safety.
4. Perform an atomic cutover: stop writers; checkpoint and back up SQLite;
   namespace retained rows as v1; classify every row; start v2; reject all v1
   tool names; and update CLI, trust, recovery, docs, examples, and the Skill.
   Quarantine `prepared` and `needs_approval`. Inspect `pending` without replay.
   Only all-desired-matched evidence may settle a retained row as applied.
   Partial, none-matched without frozen-before proof, malformed, and unknown
   evidence remains fenced until a signed CLI-only owner resolution records
   `accepted_as_is` or `superseded` without Cloud I/O. Tombstone quarantined,
   auto-applied, signed-resolved, and already-terminal v1 rows by scrubbing
   owner plan content while pending rows retain their exact recovery evidence. If more
   than one unresolved retained row exists, block all writes and surface every
   ID rather than choosing or discarding one.
5. Only after that gate, evaluate and implement the Project-only advanced scope
   tools. Evaluate any read-only coaching workflow separately.

## Tradeoffs accepted

- More tool names make ordinary calls smaller and independently testable.
- A stable request ID remains public because transport retries need an owner
  operation identity.
- CLI approval adds one terminal interaction for risky work.
- A partial outcome blocks unrelated writes until the owner resolves it.
- Advanced desired-state editing is deferred and separately gated because its
  document grammar costs more caller attention than ordinary tools.
- Old public calls break at the version boundary. Pending old receipts remain
  observable so the migration cannot cause a blind replay.
- Things Cloud has no conditional-write primitive. The server force-refreshes
  and rechecks every frozen precondition immediately before POST, but another
  writer can still change Cloud state between that read and the write. Receipt
  read-back and the account fence detect outcomes; they cannot eliminate this
  external read-to-write race.
- CLI separation is not a same-UID security boundary. Code running as the
  serving OS identity can replace the journal, pinned key, or process. Stronger
  adversaries require a separate OS account or host.

## Alternatives rejected

- The three-tool v1 interface remains a hidden transaction language.
- One public transaction tool exposes internal operations and preconditions.
- A goal-only interface bakes one GTD interpretation into server behavior.
- A scope-only interface makes ordinary updates learn contexts and member
  references.
- Public revisions make the model coordinate server-owned concurrency.
- Model-callable approval cannot prove owner authority.
- Permanent deletion in default discovery has more downside than caller value.

## Proof boundary

Memory and SQLite tests can prove schemas, state transitions, restart safety,
idempotency, and exact receipts. Cloud adapter tests can prove envelope and
read-back behavior against recorded protocol fixtures. Only the owner can
prove the interactive approval experience and repeated usefulness on a real
Things account.
