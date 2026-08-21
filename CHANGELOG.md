# Changelog

## Unreleased

### Added

- A public dogfood register now records completed human runs, found failures,
  the first-round workflow queue, and the later regression round.

### Fixed

- One complete audit now supplies the write context for Area, Inbox, Task, and
  Project-layout changes in one full reorganization. Its final page returns
  every complete Project layout in native order.
- Context refs stay stable when a fresh read adds items. Refs remain bound to
  the context that returned them.
- Audit continuations accept the repeated audit view. A different view still
  fails before data is returned.
- `replace_rich_note` is harmless for plain Markdown and empty notes. Rich
  notes still require explicit replacement approval.
- Full reorganizations stage the server manifest before the owner confirms it.
  One clear yes now leads to approval, not a second commit and question.
- Tag changes in a full reorganization now use one complete tag-catalog read
  and its registry revision without replacing the audit write context.
- Created headings and Tasks now show their Project as the manifest home.

## 0.5.1 — 2026-08-21

Full Things reorganizations now use one reviewable transaction and verify the
result before the agent reports success.

### Changed

- Full reorganizations use one complete audit, batched Project reads, material
  owner questions, one exact before-and-after manifest, and one commit. Risky
  work uses at most one approval.
- Audit rows expose direct tag assignments. Native Project children no longer
  report their inherited Project Area as a second-home conflict.
- Approval manifests name final Area and item order, title and home changes,
  full note replacements, schedule changes, tag assignments, lifecycle
  changes, and permanent tag deletion.
- Applied receipts report Task and Project mutation counts, final Area order,
  and the final tag catalog. A pending or mismatched read-back cannot become a
  success message.
- Chained reordering now projects every prior move in the same batch. This
  fixes dense Area, Project, Task, Inbox, and Project-child order changes.
- Agent-proposed tags must enable a repeated useful filter. Empty or thin Areas
  can stay when the owner names the ongoing responsibility.
- Tag cleanup now keeps the final assignment when the same item is also moved,
  renamed, or cleaned after more than one tag deletion.
- Waiting stays a tag. Replacing a localized or canonical Waiting tag in the
  same batch now creates and assigns a valid `Waiting` tag before deletion.

## 0.5.0 — 2026-08-20

Project notes now store clear meaning with a consistent owner-selected style.
Agents no longer write presentation Markdown for source-backed Projects.

### Changed

- Source Projects send an outcome, completion checks, shared constraints, Task
  finishes, useful starting facts, approach notes, and labeled source locations.
  One renderer turns that meaning into native Things Markdown.
- `natural` is the default style. The optional `visual` style uses a fixed,
  accessible emoji vocabulary in notes only. Titles and native headings stay
  plain.
- `configure --note-style natural|visual` saves the owner default outside the
  checkout and credentials. The workspace reloads it before each Project, so a
  restart is not required. A one-Project override does not change the default.
- Third-party app source links require an owner allowlist. Web, local file,
  absolute path, and read-only Things links remain built in.
- Every Task and Project Cloud create now uses a positive order index. This
  prevents Things from trapping while it applies incremental `Task6` history.
- Preferences use a separate versioned, atomic `preferences.json`. Updates,
  login, token rotation, snippet generation, rollback, and package replacement
  do not change it.
- Existing Things items stay unchanged. The old source-note payload grammar is
  intentionally not accepted by v0.5.0.
- Source documents enforce the taught 20-row limit and one checklist of at
  most three rows. Semantic prose cannot carry Markdown or hidden source URIs.

## 0.4.9 — 2026-08-20

Source-backed Projects now land as one complete Things document. The server
rejects a stripped skeleton before it writes owner data.

### Changed

- Project creates can declare `document=source`. Every nested Task supplies a
  concrete `finish`, which the server renders as a native Markdown
  `## Leave with` block.
- Source documents validate the Project result, done state, guardrails,
  heading structure, and every Task note before the first write. Incomplete
  payloads return `next=revise` and tell the agent to repair the call without
  asking the owner.
- The source-document gate limits each Markdown section to 800 prose
  characters. A rich structured note can be longer, and labeled full URLs do
  not consume that limit. Ordinary capture keeps its existing 800-character
  note gate.
- Applied receipts report the Project home, heading and Task counts, Task-note
  read-back, and first Task. They tell the agent to report the result and stop.
- The release behavior gate now replays isolated Codex and Cursor payloads
  through the public workspace. Model predictions alone do not pass.

## 0.4.8 — 2026-08-20

Source-backed capture now writes a human-readable Things document. Compact
Project Tasks can use native checklists without sibling-create plumbing.

### Changed

- Source capture is one quiet transaction before any Things call. It separates
  research needed to shape the Project from work the Project must still finish.
- Project and Task notes use selective Markdown structure, first-person text,
  natural action titles, and collapsed-view and opened-Task checks.
- Compact Project `tasks` accept native checklist rows alongside notes and
  headings. The rows share the Task finish and stay in the same atomic commit.
- The Mats Mode capability fixture now proves the full human-readable path,
  including owner review, draft review, cross-client tests, real use, and
  Cursor setup.

## 0.4.7 — 2026-08-20

Source-heavy Projects keep their full evidence-to-delivery path. Same three
tools.

### Changed

- A Project's compact `tasks` can set `heading_title`. The server creates
  native, contiguous Things headings while it keeps Task array order.
- Source-backed creation can research and write in one turn when the owner
  authorized one clear result. It asks only when two supported readings change
  the result or Things form.
- Project planning now checks evidence order, required intermediate results,
  owner review, verification, delivery, source labels, and note-local work.
- New creates do not browse or infer an Area. Work traces omit routine tool
  narration and stop after one fallback for an unavailable source.

## 0.4.6 — 2026-08-20

Accepted Project plans keep ordered Tasks visible. Same three tools.

### Changed

- Project `tasks` replaces `next_actions`. The array holds every accepted,
  committed Project Task in dependency order. The first Task is available now;
  later Tasks stay visible. One new Project cannot mix compact Tasks with
  sibling or moved items.
- Research names the durable artifact before it splits finishes. It preserves
  owner-named evidence scope, relevant thread replies, and actual use across
  named harnesses.
- Project Task titles and notes pass the same dump checks as their Project.
  A mashed child title or note over 800 characters asks and writes nothing.
- New Task, Project, and Area titles must be unique within one commit.

## 0.4.5 — 2026-08-20

Project next actions can carry notes. Same three tools.

### Changed

- `next_actions` is title plus optional notes. Title-only strings still
  coerce. A sitting that needs a path or URL keeps that packet on the
  Task, not a five-link dump on the Project.
- Form and research: a Task is startable without opening the Project.
  Do not file Read after sources were already gathered. Optional or
  continual work is not this Project.

## 0.4.4 — 2026-08-20

Named Project create with distilled notes applies. Same three tools.

### Fixed

- A new Project with one to three next actions and distilled notes is
  not a dump. Dump is a mashed title, Decide / Think about / Work on /
  Assess, or a pasted brief. The form note template is not a brief.
- Form notes stay short and plain. A source URL that one Task must
  open lives on that Task.

## 0.4.3 — 2026-08-20

Applied receipts name unique homes. Exact `into` wins. Same three tools.

### Fixed

- Applied copy names unique homes from the written record, including
  Anytime. Inbox-only still uses the Cloud sentence.
- Exact `into` is the home when `into_title` is also sent. Heading
  create and heading moves resolve `into_title` through the same home
  match.
- Capture uses the named tag title. It does not invent a second name.

## 0.4.2 — 2026-08-20

Named homes and tags in one capture. Leaner review bytes. Same three tools.

### Changed

- Disclosed skill files are `research.md`, `form.md`, and `review.md`.
  Capture, Today, and repeats live in `SKILL.md`. A dump loads research,
  not form. `things_commit` asks and writes nothing when a new Task or
  Project is still a dump.
- Create and change may send `into_title` for a unique Area or Project.
  Open Project and Area title twins ask, like Tasks already did.
- Compact items name `into_title` and `heading_title`. They omit sibling
  `order`, empty recurrence, and section `item_ids` that replay the list.
- Applied receipts bind assigned `direct_tag_ids` and name the home.
  Tag pages are a catalog. Capture may `ensure_tags` in the same commit.

## 0.4.1 — 2026-08-19

Trash list copy and trashed Project layout. Same three tools.

### Fixed

- Trash list copy says read the item, not `purpose=change`.
- A trashed Project read includes layout for the contained tree.

## 0.4.0 — 2026-08-19

One Project read. Same three tools.

### Changed

- A Project id, a Project change, and `purpose=organize` return the
  writable neighborhood: Area, layout, hidden occupants, and if it is
  in Trash, the contained records. Organize drafts compile from that
  complete Project scope. They do not require `purpose=organize` on
  the read.
- Create entries may name a heading defined later in the same array.
  The commit interface orders local dependencies.
- The main skill names teardown: do not use `view=trash` alone.

## 0.3.3 — 2026-08-19

Tell the truth about truncated-page copy and trashed Project change.
Same three tools.

### Fixed

- Truncated review copy keeps the period: `in one commit. Continue the cursor`.
- `purpose=change` on a trashed Project lists the contained Trash
  records that restore or purge will write.

## 0.3.2 — 2026-08-19

Tell the truth about complete, teardown receipts, and diagnostics copy.
Same three tools.

### Fixed

- Incomplete review pages include `context.complete: false` on the wire.
  Compact dumps cannot omit a required control field.
- Permanently deleting a Project with `delete_contents` names contained
  headings as `heading:<id>` in `missing_ids`.
- Diagnostics instruction mentions `test_residue` only when that signal
  is on the page.

## 0.3.1 — 2026-08-19

Pagination honesty after v0.3.0 dogfood. Same three tools.

### Changed

- A truncated review page marks its context incomplete. Continue the
  cursor to add the rest to that same context, up to 120 items.
- Applied receipts name every permanently deleted id, not only the
  first ten. Heading deletes appear as `heading:<id>`.
- Already-trashed harness leftovers leave diagnostics. Living
  `__TO_PROBE__` and `Things Orchestrator scoped` titles still show
  `test_residue`.
- Organize and change includes no longer leak `revision`. The ref is
  the write token.
- `/health` capabilities include `review_context` and
  `native_heading_delete`.

## 0.3.0 — 2026-08-19

A weekly review can finish without fighting the tool. Same three tools.

### Changed

- Review pages mint a context and short refs. Inbox, Today, Week, Area,
  Project, audit, logbook, trash, and diagnostics can commit listed
  work with `context_id` and refs. No `if_revision` scavenger hunt.
- An Area or Project `id` lists its children. `view=area` and
  `view=project` accept `id` or `within`.
- Heading delete is one native `permanent_delete`. Assigned work stays
  in the Project, including completed and trashed occupants. Approve no
  longer goes stale because Today order or a logbook row ticked.
- Organize layouts report `hidden_count` and `hidden_signals` when a
  heading still has completed, canceled, or trashed occupants.
- `delete_headings` can stand alone. Include another Project on an
  organize read to organize both in one commit.
- `view=logbook` defaults to the last 14 days.
- Approval summaries use owner titles (`Inbox → Kitchen`), not
  `area:<uuid>`.
- Exact `change` still needs `id` and `if_revision` unless a context
  binds the item. Diagnostics label `__TO_PROBE__` and
  `Things Orchestrator scoped` leftovers as `test_residue`.

## 0.2.3 — 2026-08-19

Last honesty edges after v0.2.2 dogfood. Same three tools.

### Fixed

- Permanently deleting a heading whose Project is already in Trash no
  longer runs the merge-destination check. Same-home heading cleanup is
  not a merge.
- Deleted tags appear in applied `missing_ids` as `tag:<id>`.
- `within=trash` searches Trash by title so a living notes-hit cannot
  hide a short heading name.

## 0.2.2 — 2026-08-19

The first release that can finish work. Neighborhood reads, Project
teardown, and honest control fields from the unreleased 0.2.1 follow-up,
plus honesty at the edges so a session can clean up after itself.

### Changed

- Change and organize reads return the local neighborhood. Include a
  destination to move or merge. Unresolved includes no longer abort the
  target context.
- `lifecycle=trash` on a Project moves remaining descendants to Trash
  with it. Restore of that Project restores the same parent-linked
  subtree. Heading trash is no longer a silent no-op.
- Today lists overdue, Evening, Today, and Waiting. Inbox stays on
  `view=inbox`. Evening is grouped before Today so tonight work does
  not land in Today.
- A cursor now returns `next=read`. Commit receipts echo `into_id`,
  `heading_id`, `start`, and `signals`. Instance recurrence facts
  inherit the template rule.
- MCP tool descriptions are the short call contract. Teardown, repeat,
  and organize details live in the skill references.
- Invalid tool requests return a `rejected` Result on the MCP channel
  instead of `is_error`. Expected schema misses no longer look like an
  outage.
- Review `find` that matches only Trash or completed work returns those
  items instead of an empty living search.
- Applied receipts prove checklist parents, assigned tags, and
  permanently deleted ids. Organize recovery for a trashed Project
  points at `purpose=change`, not the same dead organize id.
- Live children of a trashed heading now carry `orphaned_heading`.

### Fixed

- A heading `into` a different Project is rejected unless the batch is
  an atomic merge that also trashes the heading's source Project.
- Pending Cloud read-back no longer returns `retry_same` forever.
  After three unsettled attempts, `things_commit` and `things_approve`
  stop with `unavailable` so the model can read current facts.
- Setting only `remind_at` on an Evening task keeps Evening. A reminder
  is a clock time, not a move out of Evening. Send `start=today` to
  leave Evening, or `start=evening` to set it.

## 0.2.1 — 2026-08-18

Same three tools. Production dogfood of 0.2.0 hardened review completeness,
diagnostics, and bulk reads so agents can keep using the server without
crashing, hiding truncation, or overflowing context.

### Added

- Bulk `ids` accepts `fields` to request only `notes`, `checklist`,
  `tags`, and/or `recurrence`. An explicit empty list returns core
  facts only. Items carry `direct_tag_ids` and `inherited_tag_ids`;
  titles and parents live on the top-level `tags` registry.

- `/health` publishes `tool_contract_hash` so description and runtime
  `Result` changes are visible even when discovery schemas stay
  compact. Compare it after a deploy as well as `tool_schema_hash`.
- Bulk `ids` reads hoist unique tag facts to `tags` so parent graphs
  are not repeated on every item.
- Exact item facts include `truncated_fields` so omitted notes,
  checklist, or tags cannot disappear behind the 20-signal cap.
- Diagnostics rows include a `repairs` list aligned to each conflict.
- `/health` publishes `tool_schema_hash` so clients can detect a stale
  cached schema.
- Missing bulk IDs are also returned as `missing_ids`.
- `view=diagnostics` pages item and tag conflicts in `diagnostics`
  with `repair_kind`. Continue the cursor for the rest.
- `view=audit` accepts `signals_any` to keep one GTD state.
- Audit sections can list up to 40 homes on a page.

### Fixed

- Links-only recurrence instances now resolve their repeat type from
  the template, matching `template_uuid` instances.

- Recurrence templates list every valid instance, whether the
  relationship is stored as `template_uuid`, `recurrence.links`, or
  both. Exact-item cursors bind that set, so adding or removing an
  instance stales continuation. Instances are ordered by sort index
  then UUID.

- Bulk `ids` reads reserve a 400-character note prefix for every
  item before spending the shared remaining budget. Earlier items
  can no longer consume later items' promised prefixes.
- The 256 KB structured-result ceiling is enforced on the complete
  `Result`, including `scope_revision`, cursor, and `missing_ids`.
  Tag parent graphs are stripped before task-tag membership. If
  core metadata still overflows, the page returns fewer items and a
  cursor. `truncated_fields` can include `recurrence`.

- Missing and trashed Area or Project parent/home relations recommend
  a kind-valid repair. An Area never gets "place in a Project"; a
  Project never gets `rehome_item` into another Project.
- Bulk `ids` reads hoist unique tags, then allocate a shared
  100,000-character budget in global passes. The complete structured
  result stays under 256 KB.
- Discovery `READ_OUT` items now declare `signals` and
  `truncated_fields`.

- Diagnostics omit combined repair prose when it would exceed 400
  characters. `repairs` still lists every conflict.
- Bulk completeness uses `truncated_fields` and keeps truncation
  signals even when an item already has 20 state signals.
- A pre-existing per-item `tags_truncated` signal no longer drops
  already-bounded inherited tags from a multi-id read.
- Area parent/home relations that point at the wrong kind recommend
  clearing that relation instead of an invalid Project or Area home.
- Diagnostics cursors include the rendered title, so a title-only
  change makes continuation stale.
- Singular `repair_kind` is set only when one repair exists.
- The bulk detail budget applies to every `ids` read, including one
  item.
- Bulk exact reads keep a 100,000-character budget across notes,
  checklist titles, and tag titles. Overflow sets `truncated_fields`
  and the matching truncation signal.
- All-missing `ids` name every missing ID and use `next=read`.
- Diagnostics detect nested Projects and Areas.
- `start: null` cannot combine with `remind_at`. The previous pair
  could schedule a date from the reminder while claiming to clear
  start.
- Duplicate change `include` lookups are a validation error, not
  `internal_error`.
- An explicit empty `ids` list is rejected instead of becoming Today.
- Tag-only diagnostics no longer claim "no conflicts" while returning
  tag signals.
- Unexpected internal exceptions set MCP `is_error`.
- Diagnostics cover heading-without-project, wrong parent/area kinds,
  malformed reminders, and tag-parent cycles, with repair hints.
- Partial `ids` reads return the found items and name the missing IDs.
- Audit pages group items by home title.
- `view=area` expands one Area: the Area, loose tasks, and Projects.
- `view=audit` lists every active item once. Compact rows include
  `has_notes` and `has_checklist`.
- `view=diagnostics` lists native-state conflicts.
- `ids` returns bounded full-detail facts for up to 10 exact items.
- Change contexts accept 40 include lookups.
- Approval plans add grouped counts and ID sections.
- `/health` reports version, cache version, capabilities, and optional
  commit. The MCP initialize version matches the package.

### Fixed

- `start: null` now clears Someday on an ordinary Task and keeps its
  Project or Area. The previous desired-state check treated Someday as
  already unscheduled.
- `today_after` can follow a sibling moved to Today in the same commit.
- Repeating the same Project home no longer stale-locks a sibling repair
  batch. List revisions bind only when membership, heading, or order
  changes.
- Trash review serializes untitled and malformed records instead of
  returning `internal_error`.
- Area-only Project reads are rejected at the schema. Use `view=area`.
- Unexpected exceptions log a correlation ID. Validation errors lead
  with the field that failed, not a capture example.
- Moving a task from Inbox into a Project or Area now leaves Inbox.
  Native Things used to keep it there after a successful-looking
  move. Repeating the same move repairs already-stuck items.

### Docs

- Hosting rewrite: three topologies. Private Tailscale is the personal
  VPS default. Public Caddy is for off-tailnet clients. Command blocks
  say where they run.
- Client guide: Codex hosted MCP is `~/.codex/config.toml` Streamable
  HTTP, not a merge of `mcp.http.json`.
- README still picks this Mac or a VPS. Capability proof is linked.
  Owner guide is what to say, not how the tools work.

### CLI

- `login` / `print-config` print snippet paths only. `--show-secrets`
  prints bodies including the MCP bearer.
- `doctor` checks credentials, snippets, timezone, and loopback
  `http://127.0.0.1:8787/health`. Loopback is required after a hosted
  URL is set, or with `--wait`. `--url` checks remote `/health`.

## 0.2.0 — 2026-08-17

Same three tools. Shorter first-action skill, clearer owner questions, and
several write-contract fixes. Also the first tagged release of the Cloud
operations and contextual workflows that landed after 0.1.0.

### Skill

- Keep always-needed first actions in `SKILL.md`: capture, tags, change,
  schedule, and when to ask.
- Ask before guessing whether a date is a start or a deadline.
- Ask before inventing a reminder clock time.
- Ask when a new Project has no stated outcome, or a permanent delete does
  not name exact Trash items.
- Keep owner-supplied Project next-action titles.
- Find with one title token. A packing checklist finds `Pack`.

### Write contracts

- Tag changes accept the tags-read revision.
- Organize can open the unique parent Project of a heading find.
- An organize miss that only finds Inbox tasks tells the model to create a
  Project and move them.
- `after` may follow a sibling moved into the same new Project in one commit.
- `repeat.remove` on the current generated copy applies to its template.
- Repeat rule edits on a generated copy apply to its template.
- Creating a Task whose exact title already exists asks instead of
  duplicating.

### Capability first released here

- Create, inspect, change, convert, and stop repeating work.
- Create, rename, reorder, assign, clear, and delete headings.
- Trash, restore, and permanently delete Tasks and Projects.
- Nested tags, Markdown notes, and approval-bound rich-note replacement.
- Bounded change and organize contexts with short refs.

## 0.1.0 — 2026-08-15

First public release. Three intent-level tools for revisioned reads, verified
writes, and owner-approved high-impact changes.
