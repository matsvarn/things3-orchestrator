# Dogfood register

This register tracks human runs against released Things Orchestrator versions.
Automated tests and isolated model replays do not count as human dogfood.

Use a fresh client session for each run. Keep the natural owner prompt, the
visible trace, the exact Things result, and any recovery path. Record a pass
only when the result remains useful without the chat.

## Status

- **Round 1 complete**: a human ran the workflow and recorded its failures.
- **Repeat required**: fixes landed after that run. Run the workflow again
  after all first-round fixes ship.
- **Queued**: no complete human run is recorded.

## First-round record

### Source-heavy Project capture — Round 1 complete

The Mats Mode request in
[`tests/fixtures/mats_mode_owner_prompt.txt`](../tests/fixtures/mats_mode_owner_prompt.txt)
ran several times. It covered source research, reply coverage, a finite Project,
later Tasks, native headings, checklists, Task-local sources, and both note
styles.

It found wrong scope, empty Tasks, pasted briefs, fake Read work, omitted later
Tasks, continual work inside a finite Project, weak notes, a second note pass,
excess trace narration, incomplete source coverage, unsafe native order, and
preference-persistence gaps. Releases 0.4.6 through 0.5.0 repaired that path.

Status: **Repeat required** after the first-round fix program is complete.

### Full reorganization — Round 1 complete

The request in
[`tests/fixtures/full_reorg_owner_prompt.txt`](../tests/fixtures/full_reorg_owner_prompt.txt)
ran against the owner's live system. The owner then answered the material Area,
tag, language, and home questions.

It found excess reads and narration, incomplete write context, stale retries,
unstable short refs, incompatible cursor syntax, a false rich-note rejection,
two confirmations for one manifest, wrong heading homes, missing native Project
order, a reorder retry, and unresolved Project quality.

Status: **Repeat required** after the first-round fix program is complete.

### Weekly review — old contract completed

A live weekly review covered Inbox, Areas, Projects, and heading cleanup. The
exact natural prompt was not recorded. It found copied revisions, shell-only
Area reads, hidden heading occupants, incomplete pages, stale approvals, and
misleading receipts.

The current review contract did not exist during that run.

Status: **Repeat required**. This is the next workflow after the current
release.

Use the recorded natural prompt in
[`tests/fixtures/weekly_review_owner_prompt.txt`](../tests/fixtures/weekly_review_owner_prompt.txt).
Do not add tool or form instructions before the run.

## First-round queue

Run these workflows once before the regression round. Keep each prompt natural.
Do not teach the agent its expected tool calls.

1. **Routine capture and refusal gate — Queued.** Capture `Renew passport`, two
   independent Inbox actions, and one mashed dump. Valid capture must stay fast.
   An invalid form must ask and write nothing.
2. **Named home and tag capture — Queued.** Add one Task to a named Project or
   Area with one named reusable tag. It must not invent a home or second tag.
3. **Ordinary Project form — Queued.** Use `Replace kitchen tap` with three known
   actions and useful context. Check the Task, checklist, heading, and note split.
4. **Inbox processing — Queued.** Process a mixed real Inbox to zero without
   duplicate creation or invented dates.
5. **Daily focus — Queued.** Review Today, postpone work, move one item to
   Evening, and reorder the remainder.
6. **Exact change and scheduling — Queued.** Rename, complete, and cancel exact
   items. Set Today, Evening, a start, a deadline, and a timed reminder.
7. **Recurrence lifecycle — Queued.** Create a repeat, edit its rule, change the
   current copy, complete it, and stop repetition while the Task remains.
8. **Tags and Waiting — Queued.** Rename, reparent, assign, and delete tags.
   Preserve direct assignments and delegated meaning.
9. **Project organization and merge — Queued.** Add, reorder, and delete native
   headings. Merge two Projects without losing hidden occupants.
10. **Trash, restore, permanent delete, and rich-note replacement — Queued.**
    Verify exact targets, recoverability, approval, and full replacement scope.
11. **Focused Area redesign — Queued.** Change a small Area map without forcing
    tag minimalism or removing a named responsibility.
12. **New-system setup — Queued.** Use an empty or nearly empty account. Create a
    small provisional basis from named responsibilities and workflows.
13. **Install, update, rollback, and recovery — Queued.** Verify client setup,
    preference preservation, health checks, rollback, and safe recovery.

## Regression round

After every queued workflow has one human run, repeat all workflows in this
register against the then-current release. Use changed owner data and natural
paraphrases. A workflow passes only when its trace is concise, its writes match
one accepted intent, and the result works without the chat. When the workflow
writes Things, Cloud read-back must prove the result. Operational workflows
instead need true health and configuration checks, preserved preferences, a
working rollback, and a verified recovery path.
