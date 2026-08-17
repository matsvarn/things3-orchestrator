# Changelog

## Unreleased

### Docs

- README picks a path: this Mac, or a VPS. Competitor and trust
  detail live in their own pages.
- Numbered host guide for Hermes, then Claude Code or another client,
  against that same hosted server.
- Owner guide is what to say, not how the tools work.

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
