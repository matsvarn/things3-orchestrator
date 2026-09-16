# Capability proof

Created: 2026-08-16

This document records the proof level for each public capability. A row is
complete only when its model contract, memory behavior, Cloud envelope, and
read-back behavior agree.

Human workflow runs are tracked separately in
[`dogfood.md`](dogfood.md). Automated proof does not mark a human run complete.

## v0.10.0 routines gate

The optional built-in routine is outside the eight-tool MCP surface. Automated
tests prove the exact directly assigned `AI` trigger, tag-only baseline,
settlement edge cases, metadata-only event, durable at-least-once outbox,
receiver-specific acknowledgement rules, account fencing, disabled
zero-resource path, safe status model, and receiver-instruction consistency.
They use fake history pages, fake clocks, and local webhook receivers. They do
not connect to Things Cloud, Grok, or Hermes.

The content-free owner record in [dogfood.md](dogfood.md) reports one private
VPS result: history reached `live`; an untagged negative control did not
deliver; directly assigning exact `AI` to the fresh candidate delivered one
event; and Grok read and updated that selected task through MCP. The record
does not include the exact deployed commit SHA, Grok client version, installed
skill state, or owner intervention details. It is not general Grok or Hermes
compatibility proof.

## 2026-09-01 authenticated-write gate

The public v2 contract has no owner-approval state or action. Focused memory
and server tests prove direct recoverable Trash and repeat Stop, pending exact
retry without repost, terminal partial receipts, and no-I/O retirement of
legacy `awaiting_owner` rows. The disposable live acceptance gate now performs
cleanup in the same authenticated run and passes only after immutable receipt
and exact Trash read-back. Live proof still requires running that gate against
the exact deployed commit and account.

## v0.7.0 recurrence gate

The v2 contract and memory adapter cover RT1 repeat inspection, creation,
conversion, rule edits, pause, resume, stop, completion, and "Create Next
Copy" for Tasks and Projects. The Project cases clone the native root,
headings, heading assignments, Tasks, and checklist rows with fresh IDs.
Cloud fixture tests assert each entity type and sparse bookkeeping payload.
Stop is a frozen authenticated operation that keeps generated copies and
turns the hidden template into a fresh ordinary item on its next date before
removing the template graph. A stopped Project preserves its headings, Tasks,
heading assignments, and checklist rows with fresh IDs.

A disposable native Things Project established the observed graph and
lifecycle payloads. The run covered pause, resume, "Create Next Copy", Stop,
and after-completion advancement. Native Stop cleared repeat links on generated
copies, created a scheduled ordinary Project graph with fresh IDs, then deleted
the hidden template graph. It also confirmed that a Task assigned to a heading
uses `agr` without a duplicate `pr`. The v2 live-host proof remains a separate
deployment gate. RT2 stays read-only because this proof did not establish
native RT2 write deltas.

## v0.6.0 owner-safe interface gate

The current public proof target is the exact default eight: bounded reads,
Task/Project capture, ordinary explicit-field updates, completion, recoverable
Trash, and immutable receipts. Focused regression tests cover opaque
idempotency keys, immutable private manifests, cross-process fencing, signed
host authorization, persisted-manifest integrity, legacy quarantine, bounded
read cursors, receipt HMAC cursors, taint propagation, retention, and permanent
content-minimized tombstones. The v0.6.0 migration did not make live Things
Cloud calls; retained Cloud fixtures and earlier live probes establish only the
private batch and read-back primitives.

Advanced Project scopes, mutation coaching, registries, recurrence, checklist
editing, rich-note replacement, and permanent deletion are not public v0.6.0
capabilities. They require a later safety gate.

## Live evidence

Run the read-only Cloud probe with native parity on a Mac that has Things 3:

```console
uv run python scripts/probe_cloud_capabilities.py --read-only-live-probe --native-parity
```

The probe reads every public Today and Inbox page and compares their IDs with
the native Things lists. It prints counts and equality results. It does not
print task IDs, titles, or notes. A mismatch exits with status 1.

The probe proves read parity for the current account and Mac at the time of the
run. It does not prove mutation behavior, remote-host configuration, or another
account. Deterministic Cloud fixtures cover mutation contracts.

Before a release that changes public write behavior, run the separate public-
MCP acceptance workflow against the exact candidate deployment:

```console
uv run python scripts/run_live_acceptance.py \
  --url http://127.0.0.1:8787/mcp \
  --expect-commit 0123456789abcdef0123456789abcdef01234567 \
  --state ~/.local/state/things-orchestrator/release-acceptance.json \
  --live-write-acceptance
```

The workflow creates a uniquely named disposable Project pair and Inbox Task.
It proves whole-batch validation with a field and item index, move-in-place plus
Anytime, direct and inherited tag identity, exact checklist patching,
`within`-only pagination and stale-cursor rejection, immutable receipts, and
direct recoverable Trash through the same authenticated v2 path. It uses two
existing tag IDs only on the disposable records; it does not change the tag
registry. Before credentials or writes, the command rejects plaintext remote
URLs and requires `/health` plus MCP initialize to match the local package and
the exact `--expect-commit`. It binds that URL and commit into the mode-0600
state file before mutation and reuses exact request IDs after a crash. An
unresolved `pending` cleanup stops without replay; rerunning the exact command
reconciles it through read-back. A terminal `partial` reports its immutable
receipt and fails the release gate until fresh corrective work is completed.

The run passes only after the immutable receipt is applied and every
recorded fixture ID is visible in recoverable Trash. Keep the private state
file until that final `cleaned` result. This proves the exercised account,
server, and version only; it does not prove another account or client.

A read-only history audit inspected 2,568 existing events. It found 88 native
recurrence-linked creates and 88 completion events. In 64 cases, Things made
the next linked copy in a later history group. Completion and next-copy create
were never in one group. The orchestrator therefore verifies completion of the
current copy and lets native Things create the later copy.

## Safety boundaries

Things Project trees are flat. The live probe covers a native Project with
headings, a Task, and a checklist. A memory contract also injects a non-native
nested Project and proves that defensive cleanup walks deepest-first.

Repeat rule updates preserve every unknown rule field. Creation writes the
complete observed rule with version, anchor, end sentinel, count, and skip
metadata. The public interface uses semantic mode, unit, interval, and weekday
names. Cloud codes stay inside the recurrence module.
