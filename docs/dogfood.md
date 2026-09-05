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

## 2026-09-05 daily-planning failure

The owner sent "Help me plan today" in a fresh Hermes Desktop 0.17.0 session
using `openai-codex: gpt-5.6-sol`, connected to the private VPS. The backend was
Hermes Agent v0.21.0, upstream `f159e581`, local `63279301`. Things Orchestrator
was v0.10.2 at `2b9ad32847e4dd52e0d45e394e84af9db825069e`.

The client attempted Today, Inbox, and This Week reads, but received only
"Current Things facts." as model-visible text. It then made repeated terminal
and fallback-tool attempts and asked the owner to paste the Today list or send
a screenshot. The owner reported the failure with a screenshot. The session
later showed both questions skipped and the operation interrupted. No correct
current read or useful write was established. No mutation receipt was recorded.

The server returned the actual facts in `structuredContent`, but its text block
contained only the instruction. v0.10.3 returns the same complete bounded JSON
in both fields. This preserves trust labels and control-flow fields for clients
that consume text. The public tool count remains eight.

Status: **Repeated successfully** below. The failed run made 18 tool calls
over approximately 115 seconds, including seven terminal calls. It loaded the
Things skill. Private screenshots and session identifiers remain outside the
public repository.

## 2026-09-05 daily-planning acceptance

The owner sent "Help me plan today using Things" in a fresh Hermes Desktop
0.17.0 session using `openai-codex: gpt-5.6-sol`. The private VPS backend was
Hermes Agent v0.21.0, upstream `f159e581`, local `63279301`, with the Things
skill installed. Things Orchestrator was released v0.10.3 at
`3943c18b58051926149b0c32d43f42913e58e0f6`.

The client read the 15-item Today list and related Things state, proposed a
smaller plan, and asked for available time and the main focus. The owner chose
two to three focused hours and an AI coding setup focus. The client then moved
ten tasks to Anytime in one update, fetched the immutable receipt, and read
Today again. It left five tasks in Today and reported leaving Inbox untouched.
The owner confirmed being comfortable with the changes and that the selection
generally made sense. No task names, dates, or mutation payloads were supplied
by the evaluating agent.

Independent verification found an applied receipt with ten rows and an
immutable hash. A separate uncached Things Cloud read confirmed every changed
start field and exact agreement with the five-item MCP Today result. The
verifier performed no mutation. Operation IDs, receipt details, and task
identifiers are retained privately. The visible run used 17 tool calls and took
approximately 141 seconds from the initial message to the final answer,
including the owner's response time. The trace included two skill reads,
three tool-description calls, and two terminal calls; the exact latency to
the first correct read and first settled write was not separately measured.

Status: **Owner accepted** for this release, client, and Today-to-Anytime
workflow. No further product friction was reported after the text-result fix.
This does not establish Evening placement, arbitrary scheduling, other
clients, or every journey below. The credential audit found no configured MCP
bearer in the stored trace; a complete shell-history audit was not performed.

## 2026-09-04 owner-run routine acceptance

An owner ran the built-in routine on a private VPS with a supervised worker.
The durable history phase reached `live`. A fresh untagged task was the negative
control and produced no webhook. Assigning the exact `AI` tag directly to that
fresh candidate during its settlement window produced one event. Grok fetched
the selected task through MCP and updated that task's notes.

The final routine counts were zero candidates, zero pending events, one
delivered event, and zero dead letters. This content-free record reports owner
acceptance for that topology only. It does not prove general Grok or Hermes
compatibility. The record does not include the exact deployed commit SHA, Grok
client version, installed skill state, or owner intervention details. It also
excludes task content, account identity, task and event IDs, receiver
credentials, webhook details, and host identity.

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
- **Queued for v0.10.4**: the workflow is supported by the current eight tools
  but has no complete current human run.
- **Deferred**: the workflow requires a capability outside the v0.10.4 public
  contract and must not be counted as a release failure.

## Historical first-round record

These runs predate the bounded eight-tool v0.9 interface. They remain useful
history, but they do not prove current-client activation.

### Source-heavy Project capture: round 1 complete

The Mats Mode request in
[`tests/fixtures/mats_mode_owner_prompt.txt`](../tests/fixtures/mats_mode_owner_prompt.txt)
ran several times. It covered source research, a finite Project, later Tasks,
native headings, checklists, Task-local sources, and both note styles.

It exposed wrong scope, empty Tasks, pasted briefs, fake Read work, omitted later
Tasks, weak notes, incomplete source coverage, unsafe native order, and
preference-persistence gaps. Releases 0.4.6 through 0.5.0 repaired that path.

Status: **Repeat required**, using only the parts supported by the current
contract.

### Full reorganization: round 1 complete

The request in
[`tests/fixtures/full_reorg_owner_prompt.txt`](../tests/fixtures/full_reorg_owner_prompt.txt)
ran against the owner's live system. It exposed excess reads and narration,
incomplete write context, stale retries, unstable references, incompatible
cursor syntax, wrong heading homes, missing native Project order, and unresolved
Project quality.

Status: **Repeat required**, after unsupported broad reorganization actions are
removed from the prompt.

### Weekly review: round 1 complete

A live run used the recorded natural prompt against version 0.5.2. It found
overlapping reads, skipped an empty-head check, mixed cleanup with planning,
invented priority judgments, converted next-week intent into Monday start dates,
and asked approval for unnamed changes.

Status: **Repeat required** against v0.10.4. Use the natural prompt in
[`tests/fixtures/weekly_review_owner_prompt.txt`](../tests/fixtures/weekly_review_owner_prompt.txt)
without tool or form instructions.

## Supported v0.10.4 queue

Run these workflows with natural prompts and the evidence fields above:

1. **First correct read. Accepted on v0.10.3.** The fresh Hermes run above
   read current state before asking the owner for planning choices.
2. **Useful Inbox capture and refusal gate. Queued for v0.10.4.** Capture one
   wanted Task. Then give one ambiguous mashed request; it must ask a concise
   question and write nothing.
3. **Named home and tag capture. Queued for v0.10.4.** Add one Task to an exact
   named Project or Area with one existing named tag. It must not invent a home
   or second tag.
4. **Ordinary Project capture. Queued for v0.10.4.** Create a Project with known
   actions and useful context. Check the Project fields, nested Task fields,
   checklists, and notes.
5. **Inbox processing. Queued for v0.10.4.** Process a bounded mixed Inbox
   without duplicate creation or invented dates.
6. **Daily focus. Partially accepted on v0.10.3.** Today-to-Anytime planning
   passed with owner acceptance, receipt and Cloud verification. Evening
   placement remains queued; the run did not require it.
7. **Exact changes and scheduling. Queued for v0.10.4.** Rename, complete, or
   trash exact items; set supported dates and reminders; move a Task between
   homes; verify unmentioned fields remain unchanged.
8. **Recurrence lifecycle. Queued for v0.10.4.** Create and update a supported
   recurrence, modify its current copy, complete it, and stop repetition.
9. **Tags, checklist, and Waiting. Queued for v0.10.4.** Patch exact direct tags
   and checklist rows while preserving unmentioned and inherited state.
10. **Recoverable Trash. Queued for v0.10.4.** Move one exact disposable item to
    recoverable Trash and verify its receipt and Cloud state.
11. **Install, update, rollback, and recovery. Queued for v0.10.4.** Verify
    client setup, health checks, preference preservation, rollback, and a safe
    recovery path. Record host and client topology precisely.

## Deferred until a bounded public contract exists

These are not queued for v0.10.4 and must not block its dogfood program:

- native heading deletion or Project merge;
- restore from Trash or permanent deletion;
- arbitrary rich-note replacement;
- tag or other registry mutation;
- focused Area redesign or other advanced scope editing; and
- empty-account or full-system setup.

When one of these capabilities ships with a public schema, tests, documentation,
and recovery behavior, move only its bounded workflow into the supported queue.

## Regression round

After each supported v0.10.4 workflow has one human run, repeat the supported
set against the then-current release using changed owner data and natural
paraphrases. A workflow passes only when the trace is concise, the result matches
one accepted intent, and the result remains correct without the chat. Writes
need Cloud read-back and receipt evidence. Operational workflows need true
health and configuration checks, preserved preferences, a working rollback, and
a verified recovery path.

## 2026-09-05 native note reconstruction acceptance

A disposable task was created in native Things on macOS with paragraphs,
blank lines, a heading, bold and italic Markdown, list text, two web links,
an emoji, and an accented word. A native edit from `café` to `coffee` emitted
an incremental Cloud note patch: UTF-8 byte position 141, removal length 4,
replacement `offee`, and CRC32 of the complete resulting text. This disproves
the previous interpretation of type-2 notes as rich-text paragraph arrays.

The corrected reader reconstructed the exact note. The public `things_update`
path then rewrote the first paragraph and added a short test sentence. It
returned `applied`; exact Cloud read-back matched all characters and blank
lines. Native Things displayed the rewritten text, Markdown styling, and
clickable URLs. A subsequent edit in native Things was reconstructed exactly
by another cold Cloud history replay.

Automated coverage checks failed checksums, malformed patch metadata, invalid
positions, missing bases, recovery through a new snapshot, sequential edits,
and a native note edit observed between planning and commit. The latter
rejects the stale agent replacement and preserves the newer native text.
The old cache version is invalidated so fragments are replayed from history.

This proves the note transport and public mutation path for the tested text.
It does not prove Grok or Hermes routine execution, file or email link behavior,
or atomic protection against edits arriving after the final precondition
refresh. Unknown or unverified note data remains unavailable and cannot be
replaced through this path.
