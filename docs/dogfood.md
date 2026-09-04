# Dogfood register

This register tracks human runs against released Things Orchestrator versions.
Automated tests and isolated model replays do not count as human dogfood.

The pre-release runner in `scripts/run_live_acceptance.py` is also not human
dogfood. It creates several disposable records to exercise transport, mutation,
receipt, paging, and recovery contracts. It is a release gate, not an onboarding
flow or evidence that a person can get value from a real client.

## What a current run must record

Use a fresh client session and a natural owner prompt. Do not teach the agent
tool names, schemas, or expected calls before the run. Record:

- release version and commit;
- host topology and client version;
- whether the Things skill was installed or the client saw only MCP schemas;
- elapsed time to the first correct read and, separately, the first useful
  write;
- the stopping point and every owner intervention;
- whether any Things Cloud password or MCP bearer entered chat or shell history;
- the visible trace and exact Things result;
- result correctness after the chat is gone; and
- for writes, the operation ID, receipt result, Cloud read-back, and any
  recovery path.

Do not aggregate skill-installed and schema-only clients into one success rate.
The skill contains retry and trust guidance that bare MCP schemas do not.

## v0.9.1 activation events

The primary event is one correct current read from a natural prompt in a fresh
real client, without a mutation. Service health, `doctor`, `cloud-check`, and an
eight-tool listing are prerequisites, not this event.

The optional second event is one useful capture that the owner genuinely wants.
It passes only when Cloud read-back and the immutable receipt agree. If a test
item is moved to `things_trash`, record that it remains recoverable in Things
Trash; do not call that deletion or cleanup.

## Status vocabulary

- **Round 1 complete**: a human ran the workflow and recorded its failures.
- **Repeat required**: fixes landed after that run, so it needs another human
  run against the current contract.
- **Queued for v0.9.1**: the workflow is supported by the current eight tools
  but has no complete current human run.
- **Deferred**: the workflow requires a capability outside the v0.9.1 public
  contract and must not be counted as a release failure.

## Historical first-round record

These runs predate the bounded eight-tool v0.9 interface. They remain useful
history, but they do not prove current-client activation.

### Source-heavy Project capture — Round 1 complete

The Mats Mode request in
[`tests/fixtures/mats_mode_owner_prompt.txt`](../tests/fixtures/mats_mode_owner_prompt.txt)
ran several times. It covered source research, a finite Project, later Tasks,
native headings, checklists, Task-local sources, and both note styles.

It exposed wrong scope, empty Tasks, pasted briefs, fake Read work, omitted later
Tasks, weak notes, incomplete source coverage, unsafe native order, and
preference-persistence gaps. Releases 0.4.6 through 0.5.0 repaired that path.

Status: **Repeat required**, using only the parts supported by the current
contract.

### Full reorganization — Round 1 complete

The request in
[`tests/fixtures/full_reorg_owner_prompt.txt`](../tests/fixtures/full_reorg_owner_prompt.txt)
ran against the owner's live system. It exposed excess reads and narration,
incomplete write context, stale retries, unstable references, incompatible
cursor syntax, wrong heading homes, missing native Project order, and unresolved
Project quality.

Status: **Repeat required**, after unsupported broad reorganization actions are
removed from the prompt.

### Weekly review — Round 1 complete

A live run used the recorded natural prompt against version 0.5.2. It found
overlapping reads, skipped an empty-head check, mixed cleanup with planning,
invented priority judgments, converted next-week intent into Monday start dates,
and asked approval for unnamed changes.

Status: **Repeat required** against v0.9.1. Use the natural prompt in
[`tests/fixtures/weekly_review_owner_prompt.txt`](../tests/fixtures/weekly_review_owner_prompt.txt)
without tool or form instructions.

## Supported v0.9.1 queue

Run these workflows with natural prompts and the evidence fields above:

1. **First correct read — Queued for v0.9.1.** Ask a fresh client what needs
   attention today. Verify that the answer reflects current Things state and
   performs no mutation.
2. **Useful Inbox capture and refusal gate — Queued for v0.9.1.** Capture one
   wanted Task. Then give one ambiguous mashed request; it must ask a concise
   question and write nothing.
3. **Named home and tag capture — Queued for v0.9.1.** Add one Task to an exact
   named Project or Area with one existing named tag. It must not invent a home
   or second tag.
4. **Ordinary Project capture — Queued for v0.9.1.** Create a Project with known
   actions and useful context. Check the Project fields, nested Task fields,
   checklists, and notes.
5. **Inbox processing — Queued for v0.9.1.** Process a bounded mixed Inbox
   without duplicate creation or invented dates.
6. **Daily focus — Queued for v0.9.1.** Review Today, postpone one exact item,
   move one item to Evening, and re-read the resulting view.
7. **Exact changes and scheduling — Queued for v0.9.1.** Rename, complete, or
   trash exact items; set supported dates and reminders; move a Task between
   homes; verify unmentioned fields remain unchanged.
8. **Recurrence lifecycle — Queued for v0.9.1.** Create and update a supported
   recurrence, modify its current copy, complete it, and stop repetition.
9. **Tags, checklist, and Waiting — Queued for v0.9.1.** Patch exact direct tags
   and checklist rows while preserving unmentioned and inherited state.
10. **Recoverable Trash — Queued for v0.9.1.** Move one exact disposable item to
    recoverable Trash and verify its receipt and Cloud state.
11. **Install, update, rollback, and recovery — Queued for v0.9.1.** Verify
    client setup, health checks, preference preservation, rollback, and a safe
    recovery path. Record host and client topology precisely.

## Deferred until a bounded public contract exists

These are not queued for v0.9.1 and must not block its dogfood program:

- native heading deletion or Project merge;
- restore from Trash or permanent deletion;
- arbitrary rich-note replacement;
- tag or other registry mutation;
- focused Area redesign or other advanced scope editing; and
- empty-account or full-system setup.

When one of these capabilities ships with a public schema, tests, documentation,
and recovery behavior, move only its bounded workflow into the supported queue.

## Regression round

After each supported v0.9.1 workflow has one human run, repeat the supported
set against the then-current release using changed owner data and natural
paraphrases. A workflow passes only when the trace is concise, the result matches
one accepted intent, and the result remains correct without the chat. Writes
need Cloud read-back and receipt evidence. Operational workflows need true
health and configuration checks, preserved preferences, a working rollback, and
a verified recovery path.
