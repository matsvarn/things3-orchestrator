# Authenticated bounded v2 writes

## Status

Accepted on 2026-09-01. This decision supersedes the host-owned approval path
in ADR 0006 for every normal v2 mutation.

## Decision

The configured MCP bearer or access to the stdio server authorizes all eight
bounded v2 tools. Recoverable Trash and repeat Stop apply directly. The public
schema has no `awaiting_owner`, `run_cli`, approval token, or approval tool.

The bearer is shared across clients. There is no per-client identity and no
claim that a human is present. Operators must give the bearer only to agents
that may mutate the Things account.

## Write protocol

Every mutation uses a fresh opaque request ID. The server force-refreshes,
freezes an immutable manifest and its before evidence, journals the operation
as `pending`, claims the account outcome fence, rechecks preconditions, and
posts the frozen batch at most once.

An exact retry never reposts the batch. It force-refreshes and classifies the
frozen writes:

- all desired observations match: `applied`;
- before dispatch, no desired observation matches and every touched field still
  equals its frozen before value: `not_applied`;
- after dispatch, a typed provider response proves rejection without a commit:
  `not_applied`;
- some desired observations match and every write is classified: terminal
  `partial`;
- evidence remains ambiguous, including before-state after dispatch: `pending`,
  with the account fence intact.

Applied, unchanged, not-applied, and partial outcomes have immutable receipt
rows. Corrective work after a partial outcome uses current state and a fresh
request ID.

## Upgrade behavior

Any row left in `awaiting_owner` by an older build becomes `stale` during
cutover or pruning. Retirement performs no Cloud I/O. The stored operation is
never replayed; a caller reads current Things state and sends a fresh request
only if the change is still wanted.

Signed owner-factor code remains solely for explicit retained-v1 recovery. It
is not part of the public v2 mutation path.

## Consequences

Agents can complete bounded client work without an SSH or human handoff. The
authority boundary is simpler and honest: bearer access is write access.
Safety comes from narrow schemas, immutable operations, pre-POST checks,
at-most-once posting, read-back reconciliation, receipts, and recoverable
Trash—not from a separate approval ceremony.
