# Capability proof

Date: 2026-08-16

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

The content-free owner record in [dogfood.md](dogfood.md) proves one private VPS
topology: history reached `live`; an untagged negative control did not deliver;
directly assigning exact `AI` to the fresh candidate delivered one event; and
Grok read and updated that selected task through MCP. This is not general Grok
or Hermes compatibility proof.

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

- `Yes` means the layer has a passing proof.
- `Partial` means the live probe covers only the named slice.
- `Exercised` means the live probe used the approval plan and approval call.
- `Defined` means the approval contract is tested but this live row did not
  need, or did not exercise, an approval call.
- `Not required` means the operation is safe without approval.

## Historical v1 private-engine proof matrix

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

Before a release changes the skill or tool schema, use an isolated copy of the
skill in two supported clients. Give each client a realistic source-heavy
create request. Keep external research and owner Things data unavailable.
Replay each derived transaction through the public memory workspace. A model
prediction without this server replay is not evidence. After deployment, use
a fresh session for the live dogfood run.
The v0.5.0 run passes only when it has:

- one opening update at most, then one concise result;
- no Things or Area read for a clearly new Project;
- no owner question when one supported durable result remains;
- one material fact, uncertainty, or exclusion for every named source and
  relevant reply when research tools are available;
- one Project whose collapsed view explains the complete path;
- natural titles and semantic Project and Task fields that work without the
  chat;
- owner review before drafting, draft review, cross-client testing, real use,
  then delivery;
- native headings, useful checklists, structured Task-local sources, and no
  inferred Area; and
- one complete source Project commit, unless the server requires revision or
  approval;
- a concrete finish and stored note for every Task in the first accepted
  transaction; and
- a receipt that proves note read-back, names the first Task, and tells the
  agent to stop without offering a later note pass.

Replay the same source-heavy request through two supported agents with the
exact public schema. Each derived transaction must pass the real memory
workspace. Then render the accepted Project once with the saved `natural`
style and once with `visual`. Capture the collapsed Project and opened Task
notes in Things. A human must verify that both styles remain clear without the
chat. Record the two-agent replay and human review below. Do not commit the
live screenshots: even disposable Projects can expose private owner lists in
the Things sidebar. The renderer goldens and Cloud read-back tests are the
durable public artifacts.

Start a new client session after the server and skill update. This refreshes
the tool schema before the run.

### Weekly Review behavior gate

Run the natural request in `tests/fixtures/weekly_review_owner_prompt.txt`
against a realistic isolated library. It must contain Inbox work, stale and
future starts, Waiting, possible duplicates, Someday work, one healthy active
Project, and one active Project without an available next action.

One `view=weekly_review` read must return Get Clear, Get Current, Get Creative,
and optional weekly planning in one revision-bound context. Its default result
contains at most 40 exception rows. Someday and planning actions stay closed.
`category` opens one named list and pages its complete exact result
without creating another write context. The result reports the active Project
count. A focused `project_review` category exposes each active Project's first
Task in native heading order for semantic next-action review.

The agent asks for uncaptured work. It scans the past and upcoming calendars
before Waiting and Project choices. Weekly planning first shows the Things load
for each day and asks for calendar capacity. It keeps subjective priorities
neutral. It does not translate "next week" into Monday. A write uses one exact
server manifest and one owner confirmation. Its receipt identifies changed
items and exact requested no-ops. It states any bounded omission and excludes
unrelated Areas and tags.

The public memory tests prove the bounded index, category continuation, Project
coverage, inherited Waiting, checklist exceptions, stable pagination, date
semantics, mixed Someday state, duplicate signals, and bounded receipts.
A human rerun remains required in `docs/dogfood.md`.

### Full reorganization behavior gate

Run the natural request in `tests/fixtures/full_reorg_owner_prompt.txt` against
a realistic isolated library. The library must include several Areas, active
and Someday Projects, a thin Area, an empty Area, assigned tags, rich notes,
and at least one known Project inconsistency.

The release passes when the agent performs one complete audit. Its final page
returns every complete Project layout in native order. The agent reads affected
details in batches and asks only material owner questions. It then
stages one exact server manifest with before-and-after values. That call writes
nothing. One clear yes leads to one approval, with no second commit or question,
unless structured recovery requires a split.

The applied result must prove the final Area order, final tag catalog, mutation
counts, and read-back. The agent must not claim success while a requested state
differs or a known incoherent Project remains. The trace must omit tool-loading,
lookup, and retry narration. Measure duplicate work, not a fixed call count.

This gate is separate from the two-client source-Project gate above. A full
reorganization needs one isolated model replay plus the public memory and Cloud
integration tests.

#### v0.5.1 isolated replay

On 2026-08-21, Codex received the natural request and the owner's material Area,
Someday, Project-result, duplicate, and tag choices. The isolated library had
six Areas, five Projects, sixteen Tasks, six tags, rich notes, one mixed
Project, one Inbox duplicate, and one active Task inside a Someday Project.
Codex gave the required one-sentence opening, performed no writes, and returned
one exact manifest with no new owner question. It read diagnostics and audit
once each with `limit=40`, read the tag catalog once, opened all five Projects
in one root-plus-include batch, and used one bounded exact-item batch for the
remaining notes. No read was rejected or repeated.

The manifest named every direct tag effect, permanent tag deletion, note and
date preservation rule, recoverable Trash move, Someday decision, and final
Area and Project order. Every Project title named a finished result. The Task
paths added the missing IOC approval, AI-environment test, and Cursor pin.

A second run supplied that exact accepted manifest. It read diagnostics,
audit, and tags once, then sent one commit and one approval. No call was
rejected. The final read-back proved six Area changes, five tag changes,
twenty-one Task or Project changes, the final `Arbeit`, `KI & Systeme`,
`Studium`, `Finanzen`, `Gesundheit`, `Privat` order, the final `Besorgung` and
`Waiting` tag catalog, and no mismatch. The server tests replay the same
ordering, manifest, Waiting replacement, approval, and receipt seams through
memory and Cloud adapters.

### v0.4.8 derivation run

On 2026-08-20, Cursor Agent and Codex each ran three paraphrases against an
isolated copy of the final skill. The directory contained no repository tests
or golden Project fixture. Things, browser, web, and write tools were disabled.

All six predictions had no owner question, no Things read, at most one progress
update, and one predicted commit. The run did not replay those payloads through
the server. Live dogfood then found the missing proof: one rich Task note crossed
the raw 800-character dump gate. The agent removed all Task notes, created a
skeleton, and offered a later repair pass. The v0.4.8 derivation run therefore
did not prove the released capture path.

### v0.4.9 derivation and server replay

On 2026-08-20, Cursor Agent and Codex each received the same natural Mats Mode
owner request in a fresh isolated directory. The directory contained the final
skill, its disclosed references, the public commit schema, and the owner
prompt. It did not contain the repository, tests, golden Project fixture, or
either client's result. External research and Things owner data were
unavailable.

Codex and Cursor each derived one source Project with 12 Tasks under three
headings. Each used one three-row native checklist. Both payloads
passed the public `CommitCall` schema and the real memory workspace. Every Task
had a concrete finish. The source compiler stored `## Leave with` plus useful
context on all 24 Tasks. Each applied receipt reported complete Task-note
read-back, named the first Task, and told the agent to stop. Neither result
needed a later change call or owner question.

This isolated run proves transaction derivation, compilation, persistence, and
receipt behavior. It does not prove authenticated X research or full source
coverage because both clients lacked external research tools. The post-release
live dogfood run remains the evidence gate for reply coverage and source facts.

The run also caught and fixed one pre-release defect. Codex produced a natural
1,063-character Project note. The first replay rejected it because the legacy
gate counted the whole note. Source documents now limit each Markdown section
to 800 prose characters. A complete structured note can be longer. Labeled
full URLs do not count toward that section limit. The same Codex payload then
applied at that development stage. The final fresh runs above then passed the
stricter one-finish and native-row limits.

### v0.5.0 semantic replay and visual proof

Status: passed on 2026-08-20.

Fresh isolated Codex and Cursor runs each derived one semantic source Project
with 12 Tasks, three headings, and one three-row checklist. Both used
`outcome`, `finished_when`, `keep_in_mind`, Task `finish`, `start_here`,
`approach`, and structured `{label, location}` sources. Neither payload sent
presentation Markdown. Both passed the public schema, applied through the
memory workspace, stored every Project and Task note, and returned the rich
stop receipt.

A live Cloud proof then rendered disposable eight-Task Projects in `natural`
and `visual`. A human reviewed the collapsed Projects and an opened source
Task in Things 3. The visible `##` labels gave both note levels clear hierarchy.
Natural stayed quiet. Visual used only the six fixed markers. Optional sections
were absent, and each source label stayed beside its full link. Both Projects
and all Task notes passed Cloud read-back and were moved to Trash after review.
The public renderer goldens are in `tests/test_source_document.py`; the exact
Cloud structure and note read-back are covered in `tests/test_workspace.py` and
`tests/test_cloud.py`. The live screenshots were discarded to avoid publishing
the owner's Things sidebar.

The first live proof also exposed a Cloud safety defect before release. A first
heading or Task used `ix=0`. Things 3 trapped in `LegacySCHistoryPerformSync`
while it applied the incremental `Task6` history. v0.5.0 now starts native
Task, Project, and heading order at a positive index and normalizes every
Task6 envelope before commit. A restored 09:45 local backup replaced the bad
Cloud history. Things remained open after the reset, and both final live style
Projects synced with positive indexes and no new crash report.

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
`V2_CAPABILITY_KEYS` in `scripts/probe_cloud_capabilities.py`. A live run
must pass every key listed for a capability before that row can claim `Yes`.
`Partial` rows name the tested slice and do not claim full input coverage.
