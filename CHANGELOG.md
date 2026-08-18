# Changelog

## Unreleased

### Fixed

- `start: null` cannot combine with `remind_at`. The previous pair
  could schedule a date from the reminder while claiming to clear
  start.
- Duplicate change `include` lookups are a validation error, not
  `internal_error`.
- An explicit empty `ids` list is rejected instead of becoming Today.
- Tag-only diagnostics no longer claim "no conflicts" while returning
  tag signals. Extra tag conflicts set `truncated`.
- Unexpected internal exceptions set MCP `is_error`.

### Added

- Diagnostics cover heading-without-project, wrong parent/area kinds,
  malformed reminders, and tag-parent cycles, with repair hints.
- Partial `ids` reads return the found items and name the missing IDs.
- Audit pages group items by home title.
- `view=area` expands one Area: the Area, loose tasks, and Projects.
- `view=audit` lists every active item once. Compact rows include
  `has_notes` and `has_checklist`.
- `view=diagnostics` lists native-state conflicts.
- `ids` reads up to 10 exact items at full fidelity.
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
