# Capability proof

Date: 2026-08-16

Every capability group in this document passed all six release gates:

1. Model guidance names when the operation improves work.
2. The memory adapter has an end-to-end contract.
3. The Cloud adapter has an envelope and read-back fixture.
4. A disposable live Cloud record passes the lifecycle.
5. Risky effects use one approval plan.
6. A forced pull matches the semantic postcondition.

## Proof matrix

| Capability group | Model | Memory | Cloud fixture | Live Cloud | Approval | Read-back |
| --- | --- | --- | --- | --- | --- | --- |
| Repeat inspect and generated-copy edit | Yes | Yes | Yes | Yes | Defined | Yes |
| Repeat create or convert, mode, unit, interval, and weekly pattern | Yes | Yes | Yes | Yes | Yes | Yes |
| Future-template and current-copy metadata batching | Yes | Yes | Yes | Yes | Yes | Yes |
| Stop repeat and keep linked copies | Yes | Yes | Yes | Yes | Yes | Yes |
| Task and Project Trash or restore | Yes | Yes | Yes | Yes | Yes | Yes |
| Task purge and descendant-first Project purge | Yes | Yes | Yes | Yes | Yes | Yes |
| Heading create, rename, assign, clear, and reorder | Yes | Yes | Yes | Yes | Defined | Yes |
| Heading deletion with assignment cleanup | Yes | Yes | Yes | Yes | Yes | Yes |
| Tag create, assign, rename, reparent, and delete | Yes | Yes | Yes | Yes | Yes | Yes |
| Markdown write and explicit rich-note replacement | Yes | Yes | Yes | Yes | Yes | Yes |

The behavior tests are the executable release gate. They exercise the public
interface through the memory adapter and inspect each Cloud envelope and
read-back. The live probe then verifies the same high-risk transitions against
Things Cloud. No capability becomes available from a status declaration alone.

## Live evidence

Run the disposable proof harness with:

```console
uv run python scripts/probe_cloud_capabilities.py --apply-live-probes
```

The harness creates records with one unique `__TO_PROBE__` prefix. It records
their exact UUIDs. Its cleanup deletes only those UUIDs. High-risk tag, note,
repeat, and Project transitions use the public commit and approval path. The
2026-08-16 run passed these transitions:

- nested tag create, rename, reparent, and delete;
- heading rename, reorder, and assigned-Task cleanup on delete;
- Project Trash, structure-preserving restore, and descendant-first tree purge;
- Task Trash, restore, and purge;
- structured rich-note write and explicit Markdown replacement;
- repeat template plus generated-copy creation;
- existing-Task conversion with identity and metadata preservation;
- future-template and current-copy metadata changes in one approved batch;
- full repeat rule change and generated-copy edit;
- generated-copy completion;
- repeat removal while the linked copy remains;
- forced Cloud read-back after each mutation.

A read-only history audit inspected 2,568 existing events. It found 88 native
recurrence-linked creates and 88 completion events. In 64 cases, Things made
the next linked copy in a later history group. Completion and next-copy create
were never in one group. The orchestrator therefore verifies completion of the
current copy. It lets the native Things lifecycle create the later copy.

The same history showed native stop-repeat groups that delete a template and
clear `rt` links on completed or open copies. The implementation clears all
known linked copies and deletes the template in one approved transaction.

Rich-note history contained 60 native structured-note events. The Cloud probe
accepted the same structural form. Normal Markdown changes preserve rich notes
by stopping. `replace_rich_note: true` is the explicit, approval-bound full
replacement path.

## Safety boundaries

Permanent Project deletion is a descendant-first transaction through one
approved `things_commit` plan. A direct tombstone for a non-empty Project can
leave detached Tasks and is not used.

Things Project trees are flat. The live probe covers a native Project with
headings, a Task, and a checklist. A memory contract also injects a non-native
nested Project and proves that defensive cleanup still walks deepest-first.

Repeat rule updates preserve every unknown rule field. Creation writes the
complete observed rule with version, anchor, end sentinel, count, and skip
metadata. The public interface uses semantic mode, unit, interval, and weekday
names; Cloud codes stay inside the recurrence module.
