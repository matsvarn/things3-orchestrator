# Capability proof

Date: 2026-08-16

This document records the proof level for each public capability. A row is
complete only when its model contract, memory behavior, Cloud envelope, and
read-back behavior agree.

- `Yes` means the layer has a passing proof.
- `Partial` means the live probe covers only the named slice.
- `Exercised` means the live probe used the approval plan and approval call.
- `Defined` means the approval contract is tested but this live row did not
  need, or did not exercise, an approval call.
- `Not required` means the operation is safe without approval.

## Proof matrix

| Capability | Model | Memory | Cloud fixture | Live Cloud | Approval evidence | Forced read-back |
| --- | --- | --- | --- | --- | --- | --- |
| Capture a Task | Yes | Yes | Yes | Partial (`recurrence.create_template_and_instance` only) | Not required | Yes |
| Capture a Project and Area | Yes | Yes | Yes | Yes (`ax.project_move_to_area`) | Exercised for Area registry | Yes |
| Schedule start, deadline, reminder, and placement | Yes | Yes | Yes | Partial (`recurrence.convert_existing_task` only) | Defined | Yes |
| Checklist add, change, remove, order, and preservation | Yes | Yes | Yes | Yes (`recurrence.convert_existing_task`, `project.restore_tree`) | Exercised in repeat conversion | Yes |
| Task and Project Trash or restore | Yes | Yes | Yes | Yes (`task.restore`, `project.restore_tree`) | Exercised | Yes |
| Task purge and descendant-first Project purge | Yes | Yes | Yes | Yes (`task.purge`, `project.purge_tree_descendants_first`) | Exercised | Yes |
| Task, Project, and heading placement | Yes | Yes | Yes | Yes (`ax.project_move_to_area`, `ax.organize_draft`) | Defined; move path exercised | Yes |
| Area registry create and Project-to-Area placement | Yes | Yes | Yes | Yes (`ax.project_move_to_area`) | Exercised for create; move needs no approval | Yes |
| Context refs for exact change and placement | Yes | Yes | Yes | Yes (`ax.context_change`, `ax.project_move_to_area`) | Not required | Yes |
| Editable Project organize drafts | Yes | Yes | Yes | Yes (`ax.organize_draft`) | Exercised | Yes |
| Atomic Project merge | Yes | Yes | Yes | Partial (`ax.project_merge`) | Exercised | Yes (`ax.project_merge_readback`) |
| Heading create, rename, assign, clear, and reorder | Yes | Yes | Yes | Yes (`ax.organize_draft`, `heading.reorder`, `heading.rename`, `heading.clear_assignment`, `heading.delete_with_assignments`) | Defined | Yes |
| Heading deletion with assignment cleanup | Yes | Yes | Yes | Yes (`heading.delete_with_assignments`) | Exercised | Yes |
| Tag create, assign, rename, reparent, and delete | Yes | Yes | Yes | Yes (`tag.create_hierarchy`, `tag.assign_task_readback`, `tag.rename_reparent`, `tag.delete`) | Exercised | Yes |
| Markdown write and explicit rich-note replacement | Yes | Yes | Yes | Yes (`note.write_rich_structure`, `note.replace_rich_with_markdown`) | Exercised for replacement | Yes |
| Repeat inspect, create, convert, edit, complete, and stop | Yes | Yes | Yes | Yes (`recurrence.inspect_relationship`, `recurrence.create_template_and_instance`, `recurrence.convert_existing_task`, `recurrence.change_full_rule`, `recurrence.change_generated_copy`, `recurrence.complete_current_copy`, `recurrence.remove_keep_copy`) | Exercised | Yes |

The behavior tests are the executable release gate. They exercise the public
interface through the memory adapter and inspect each Cloud envelope. The
probe adds disposable live Cloud cases. `Partial` does not mean unsupported;
it means that the live case does not cover every input variation in the row.

## Model behavior gate

The unit suite proves accepted calls and stored Things records. It does not
prove that a model derives the best call from a natural source packet.

Before a release changes the skill or tool schema, use a fresh session in two
supported clients. Give each client one realistic source-heavy create request.
The run passes only when it has:

- at most one research update, then one concise result;
- no Things or Area read for a clearly new Project;
- no owner question when one supported durable result remains;
- one material fact or exclusion for every named source and relevant reply;
- one Project with the complete evidence, design, review, test, and delivery path;
- native headings, labeled Task-local sources, and no inferred Area; and
- one commit, unless the server itself requires approval.

Start a new client session after the server and skill update. This refreshes
the tool schema before the run.

## Live evidence

Run the disposable proof harness with:

```console
uv run python scripts/probe_cloud_capabilities.py --apply-live-probes
```

The harness creates records with one unique `__TO_PROBE__` prefix. It stores
their exact UUIDs and deletes only those UUIDs in `finally`. High-risk tag,
note, repeat, Area-registry, and Project-lifecycle transitions use the public
commit and approval path.

The live contextual proofs are named in the JSON output:

- `ax.context_change`: read one exact Task with `purpose=change`, then change
  it with its context ref.
- `ax.project_move_to_area`: create source and destination Areas plus a
  Project through the public path, read the Project once with
  `purpose=change` and include the destination Area, then move it with the
  Project and destination Area refs.
- `ax.organize_draft`: read one Project with `purpose=organize`, create a
  heading, assign a Task, preserve unlisted work, and verify the layout.
- `ax.project_merge`: read one exact source Project with `purpose=organize`
  and include the destination, move the children you want to keep, trash the
  source in one approved commit, and force an exact Task read-back. Cleanup
  deletes only the five probe UUIDs.
- `recurrence.inspect_relationship`: read the repeat template and generated
  copy with `purpose=recurrence`, then verify both sides before mutation.
- `tag.assign_task_readback`: add a disposable tag to a probe Task, refresh
  Cloud, and verify the exact tag on an exact read-back.
- `heading.clear_assignment`: assign a disposable Task to a heading, clear
  that assignment through the public commit path, refresh Cloud, and verify
  the exact Task read-back has no heading.

The `ax.project_move_to_area` case forces a Cloud refresh and then performs an
exact Project read-back. The read-back must report the destination Area.

The 2026-08-16 run passed these additional transitions:

- Area and Project capture in one approved public commit;
- Project placement from one Area to another with short context refs;
- forced Cloud read-back of the moved Project;
- contextual exact change, with no copied revision;
- editable Project organization with a new heading and preserved unlisted
  work.

The result keys are the claim boundary. For example, a run can claim tag
assignment only when `tag.assign_task_readback` passes. It cannot extend that
claim to every tag input or Cloud account state.

A read-only history audit inspected 2,568 existing events. It found 88 native
recurrence-linked creates and 88 completion events. In 64 cases, Things made
the next linked copy in a later history group. Completion and next-copy create
were never in one group. The orchestrator therefore verifies completion of the
current copy and lets native Things create the later copy.

## Safety boundaries

Permanent Project deletion is a descendant-first transaction through one
approved `things_commit` plan. A direct tombstone for a non-empty Project can
leave detached Tasks and is not used.

Things Project trees are flat. The live probe covers a native Project with
headings, a Task, and a checklist. A memory contract also injects a non-native
nested Project and proves that defensive cleanup walks deepest-first.

Repeat rule updates preserve every unknown rule field. Creation writes the
complete observed rule with version, anchor, end sentinel, count, and skip
metadata. The public interface uses semantic mode, unit, interval, and weekday
names. Cloud codes stay inside the recurrence module.

Rich notes are preserved by default. `replace_rich_note: true` is the explicit,
approval-bound full replacement path.

## Exact capability-to-probe mapping

The table uses the exact result keys declared by
`PROBE_CAPABILITY_KEYS` in `scripts/probe_cloud_capabilities.py`. A live run
must pass every key listed for a capability before that row can claim `Yes`.
`Partial` rows name the tested slice and do not claim full input coverage.
