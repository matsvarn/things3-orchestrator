# Contextual desired state behind the three tools

## Status

Accepted.

## Context

Exact revisions protect writes, but they make models copy identity and state.
Narrow reads also make repeat changes and Project restructures take many calls.
Compact models often lose an item, repeat template, heading, or revision.

The public interface must keep simple capture fast. It must also preserve the
existing three tools and all valid legacy requests.

## Decision

`things_read` accepts a task purpose. `purpose=change` returns one exact target
with the parent, heading, and repeat facts that the change can need. A trashed
Project also includes the contained records restore or purge will write. It
accepts an exact ID or a narrow `find` selector, but creates a context only
when the selector matches exactly one active item.
`purpose=organize` returns one complete Project layout. Use the default review
purpose with `view=system` for an exact Area and Project registry read.
`purpose=recurrence` returns one exact Task and verifies the native template
and generated-copy relationship before a repeat mutation.

Each focused read returns an opaque, account-bound context and short refs. The
context result omits revisions because the ref is authoritative. A commit can
accept copied identity only when it exactly agrees with that ref.
Contexts expire. They hold evidence, not authority.

`things_commit` accepts context refs for normal changes. It also accepts an
editable Project draft. The draft has ordered sections, existing or new
headings, Task refs, heading deletion, and `unlisted=keep`. A commit can contain
normal changes and a draft.

The workspace converts contextual requests to exact revisioned operations.
It then uses the existing prepare, journal, apply, and read-back path.

Structured recovery describes the fresh read or rebuild that a failed context
needs. The model must not reuse refs from an old context.

## Invariants

- A context belongs to one account and one complete read scope.
- A stale, expired, missing, or incomplete context cannot write.
- An omitted draft item stays in its current location.
- `unlisted=keep` is the only supported omission rule.
- A contextual commit has the same revision and safety checks as a legacy commit.
- Risky work still creates one immutable plan and needs `things_approve`.
- A normal review page stays at 40 items. A complete context can hold 120 items.
- A scope above 120 items returns guided recovery and writes nothing.

## Consequences

Simple capture stays a one-call path. Exact changes and Project restructures
normally use one read and one commit. Models no longer copy revisions or build
low-level heading operations.

The server stores short-lived context and compiles drafts. This adds internal
state and validation. The context store and compiler stay behind the workspace,
so Cloud and memory adapters do not depend on the model-facing form.

Legacy exact IDs and revisions remain valid. This permits a gradual client
migration and keeps existing clients compatible.

## Proof

Direct integration tests cover context ownership, expiry, conflict, complete
bundles, short refs, draft compilation, omission preservation, recovery, and
approvals. The public proof suite checks the same safety and state invariants
through the supported interface.
