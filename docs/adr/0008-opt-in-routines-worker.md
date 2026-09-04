# ADR 0008: Opt-in routines worker

## Caller contract

```text
things-orchestrator routines setup --profile always_on --receiver hermes
things-orchestrator routines status
things-orchestrator routines disable
```

`setup` prints receiver-specific upstream instructions, prompts for the URL and
credential in a private terminal, writes an account-bound profile, enables it,
and converges the supervised service. `configure` writes a complete, disabled
profile for recovery and scripted administration. `enable` and `disable` are
idempotent. Enabling requires a profile bound to the current Things account and
does not start routines until the supervised HTTP service restarts. A running
worker observes disablement at least once per polling interval and then stops
polling and delivery. Configuration changes other than disablement require the
same supervised restart.

The CLI never accepts a receiver secret or key in arguments. `--url` remains
available for both receiver kinds, but omitting it keeps the URL in the private
terminal interaction. Status includes the receiver kind. It never prints the
URL or host.

The service-generated launchd and systemd commands include a hidden
`--service-managed` marker. Stdio and manually started HTTP processes never
compose a routines lifecycle. The marker records operational provenance; it is
not a security boundary against the local owner.

## Decision

Run one optional routines lifecycle inside the supervised `serve-http` process.
It owns a read-only `CloudClient`, an account-scoped process lock and SQLite
database, a bounded history projection, and webhook delivery state. It shares
no mutable object, cache, state file, request lock, or mutation journal with the
MCP request path. Blocking routines work uses a routines-only capacity limiter
and never runs on the event loop.

Use strict stable history groups and a tag-only seed followed by a live minimal
projection. Reject sharing the request `CloudLibrary`: a slow poll would couple
Cloud latency to all MCP calls. Reject a second `CloudLibrary`: it would retain
and cache an unnecessary full task model. The selected feed stores only the
current exact-`AI` tag UUID set, unsettled task candidates, and compact durable
event rows.

## Domain model

Configuration is a versioned discriminated union:

- `UnconfiguredRoutineConfig`
- `DisabledRoutineConfig(profile)`
- `EnabledRoutineConfig(profile)`

`RoutineProfile` contains the account binding, `always_on` host profile,
polling and settlement policy, routine ID, delivery retry policy, and one
receiver. The receiver is `HermesReceiver(url, secret)` or
`GrokReceiver(url, key)`. Each variant validates and normalizes only its own URL
contract. The only initial routine ID is stable and built in.

History uses immutable `HistoryEvent`, `HistoryGroup`, and `HistoryBatch`
values. A batch includes a private SHA-256 history fingerprint, requested start,
current head, stable group positions, and catch-up state. A changed history key
raises `HistoryIdentityChanged`; malformed groups or known-family future entity
versions fail the whole batch.

Candidates contain task UUID, creation group, domain kind, lifecycle, trash
state, direct tag UUIDs, first and last observation times, and settle deadline.
Sparse updates preserve omitted fields. Every update to an active candidate
resets the quiet window, including updates to fields the projection does not
otherwise retain.

Events contain schema version, opaque event ID, `task.created`, routine ID,
public task ID, and observation timestamp. The deterministic identity input is
the persisted random account event namespace, routine ID, and task ID. It never
contains the history key, creation group, task content, or delivery attempt.
Delivery is at least once, not exactly once.

Combined status keeps saved configuration, account binding, launchd or systemd
state, authenticated worker liveness, durable history phase, fixed trigger,
tag discovery, timing, and aggregate delivery counts separate. Worker liveness
is `initializing`, `running`, `backing_off`, `stopped`, or `unknown`. Neither an
enabled profile nor a live SQLite history phase proves a running worker. Public
health remains exactly `{"ok": true}`.

## Interfaces and modules

- `routines_config.py` owns the receiver union, fixed trigger constants, complete
  receiver instruction, validates each URL at the config
  boundary, parses and atomically stores private mode-0600 configuration, binds
  it to the current account, and renders endpoint-free, value-free status with
  the receiver kind only. Version-1 profiles without `receiver_kind` load as
  Hermes. New profiles store the kind explicitly.
- `cloud.py` adds strict grouped history reading without changing the public
  behavior of the existing flattened `items()` caller.
- `routines_store.py` owns the account-scoped lock, five-table SQLite schema,
  seed/live reducer, cursor transaction, canonical event body, event ledger, and
  read-only counts.
- `routines_webhook.py` exposes one `Webhook.deliver` interface and
  `build_webhook(receiver)` factory. The Hermes adapter owns V2 signing. The
  Grok adapter owns Bearer authentication. Both use proxyless, redirect-free,
  bounded HTTP, but each keeps its acknowledgement classifier separate.
- `routines.py` owns scheduling, independent Cloud and delivery backoff,
  stop/disable checks, and the single lifecycle entry point.
- `cli.py` is the composition root. It reads one credential snapshot, builds the
  request workspace, and supplies a zero-resource routines lifecycle factory
  only after bearer, service marker, enabled state, `always_on`, and account
  binding checks pass.
- `server.py` accepts the optional lifecycle factory and an HTTP-readiness gate.
  Disabled callers take the existing lifespan path and construct no worker,
  Cloud client, process lock, database, or task.
- `diagnostics.py` combines configuration, service status, one bounded
  authenticated loopback health snapshot when an MCP bearer exists, and
  read-only value-free counts. Its health request is proxyless and
  redirect-free. It never creates a routines database.

The lifecycle factory is the only interface between the HTTP host and the
worker. The worker receives the one-method webhook interface, so the host,
store, and scheduler do not branch on receiver kind.

`build_webhook(receiver)` selects one adapter before it constructs the worker.
Each adapter has its own acknowledgement rules. A shared "any successful 2xx"
classifier would accept responses that neither protocol defines as success.
Putting the receiver branch in the worker would mix HTTP policy with retry and
durability code. The config never infers the receiver kind from the URL. The
existing version-1 keys can store either receiver, and `receiver_kind` removes
the ambiguity. An older binary rejects the Grok path under the unchanged Hermes
rules.

## State machines

The durable history phase is `uninitialized -> seeding -> live`. On first
activation, the first index-0 response fixes `baseline_head`. Every page before
that head is fully validated, but only Tag entities reduce into the set of all
UUIDs whose exact current title is `AI`. Historical task creates never become
candidates. Once the cursor reaches the fixed head, later positions are live;
tasks created while a multi-page seed runs are therefore not lost.

A history-identity change clears tag seed and candidates, preserves all event
rows, and repeats the tag-only seed without task backfill. A gap-shaped empty
page fails closed. A valid empty group still advances one stable group position.

In live mode, `t=0` replaces the active candidate for its task UUID. `t=1`
preserves omitted state and resets settlement. Explicit empty direct tags clear
them. `t=2` removes the candidate. Settlement runs only on a batch that proves
catch-up. A candidate emits only when it is a normal open, untrashed task whose
direct tags intersect the current exact-`AI` set; every due candidate is then
removed.

Delivery state is `pending -> delivered | dead`. Network failures, timeouts,
408, 425, 429, 5xx, and ambiguous 2xx responses retry the same event ID with
full-jitter exponential backoff. Hermes delivers on the documented exact `200`
with `status=delivered` or `status=duplicate`. It also retains the earlier exact
`202` with `status=accepted` as a compatibility case. Grok delivers only exact
`200` with top-level `success=true` and a nonempty string `runUuid`. Redirects
and other 4xx responses are permanent. Attempt and age bounds move retryable
events to dead letter.

## Durability and ownership

The SQLite schema has `meta`, `ai_tags`, `candidates`, `candidate_tags`, and
`events`. Delivered rows retain a compact tombstone and discard the body. Dead
rows retain the metadata-only body for explicit host recovery. A unique
constraint on `(routine_id, task_uuid)` enforces one logical event.

Each validated history batch is applied in one `BEGIN IMMEDIATE` transaction:
verify account, fingerprint, phase, and cursor; reduce every stable group;
advance by group count; settle only at catch-up; insert matching events and
remove due candidates; then commit. Webhook I/O never occurs inside the
transaction. A crash leaves either the old cursor without the event or the new
cursor with the event.

A nonblocking account-scoped routines file lock is acquired before SQLite is
opened. Synchronous store operations open, configure, use, and close their own
connection. The worker serializes them through a routines-only capacity
limiter. Diagnostics use SQLite read-only mode and do not create missing files.

## Performance contract

The poll interval is 60 to 3600 seconds. When caught up, an interval performs at
most one history GET. Cloud backoff has jitter and caps at 15 minutes, but the
configuration disable check still runs at every ordinary poll interval.
Delivery backoff is independent. A drain handles at most 25 due events and
checks stop, disablement, and poll priority between requests.

The enabled task may be scheduled from the lifespan, but it waits for explicit
HTTP socket readiness before invoking the resource factory. Initialization
therefore does not delay public health. Shutdown stops new work, waits for the
bounded in-flight Cloud or webhook request, closes the store, and releases the
process lock.

## Failure policy

- Unknown or malformed Things history fails closed without cursor movement.
- Account mismatch opens neither the old database nor the receiver path.
- Cloud failures change only the polling backoff; they do not block HTTP or
  delivery disablement.
- A crash after receiver acceptance but before delivery commit retries the same
  event ID. Hermes duplicate acknowledgement converges it to delivered. Grok
  Bot may start a duplicate run. The complete receiver instruction requires
  deduplication by `event_id` before any action.
- The event deliberately contains an opaque `event_id` and public `task_id`.
  It excludes task content, secrets, private account and history data, and
  receiver data. Logs, status, diagnostics, support output, and exceptions omit
  event and task ID values as well as all private values and response bodies.
- Things Cloud remains unsupported. History family versions and field meanings
  are verified by fixtures, not by a provider contract.

## Acceptance boundary

Automated tests use fake clocks, fake grouped Cloud pages, injected crashes,
and local webhook servers. The owner must separately restart the supervised
service, configure the intended receiver and MCP connection, and validate a
fresh directly tagged task. No live account is used by the automated suite.

PRs #60 through #63 and the original ADR text directly record why the worker is
isolated, disabled by default, metadata-only, account-bound, no-backfill, and
service-only. The v0.10.0 onboarding and trust changes are an inference from
that design plus the owner-run acceptance. They do not change the durable
worker contract.

The current official Grok Bot guide documents routines, testing, history,
approvals, and retries. It does not document the observed beta host
`api2.cursor.sh`, `/automations/webhook/<route>` path, Bearer header, or exact
`success` and `runUuid` acknowledgement. Those details remain observed beta
compatibility and may change.

The current official Hermes guide documents gateway setup, dynamic webhook
subscriptions, V2 HMAC signing, one-hour `X-Request-ID` deduplication, and exact
`200` `delivered` or `duplicate` acknowledgements. The adapter retains exact
`202` `accepted` only as a legacy compatibility case. It does not accept a
generic 2xx response.
