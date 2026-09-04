# ADR 0008: Opt-in routines worker

## Caller contract

```text
things-orchestrator routines configure \
  --profile always_on \
  --receiver hermes \
  --url https://agent.example/webhooks/things-ai \
  --interval 60
# Hermes is the default. The command reads and confirms its webhook secret
# through /dev/tty.

things-orchestrator routines configure --profile always_on --receiver grok
# With no --url, the command reads the Grok Bot webhook URL and key through
# /dev/tty. It stores the profile disabled.

things-orchestrator routines enable
# Restart required: things-orchestrator service install

things-orchestrator routines status
things-orchestrator routines disable
```

`configure` writes a complete, disabled profile. `enable` and `disable` are
idempotent. Enabling requires a profile bound to the current Things account and
does not start routines until the supervised HTTP service restarts. A running
worker observes disablement at least once per polling interval and then stops
polling and delivery. Configuration changes other than disablement require the
same supervised restart.

The CLI never accepts a receiver secret or key in arguments. `--url` remains
available for both receiver kinds, but omitting it keeps the URL in the private
terminal interaction. Status includes only the receiver kind and a redacted
scheme. It never prints the URL or host.

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

Runtime status is one of `disabled`, `initializing`, `running`, `backing_off`,
or `stopped`. Authenticated diagnostics read only an in-memory value-free
snapshot. Public health remains exactly `{"ok": true}`.

## Interfaces and modules

- `routines_config.py` owns the receiver union, validates each URL at the config
  boundary, parses and atomically stores private mode-0600 configuration, binds
  it to the current account, and renders redacted status. Version-1 profiles
  without `receiver_kind` load as Hermes. New profiles store the kind
  explicitly.
- `cloud.py` adds strict grouped history reading without changing the public
  behavior of the existing flattened `items()` caller.
- `routines_store.py` owns the account-scoped lock, five-table SQLite schema,
  seed/live reducer, cursor transaction, canonical event body, event ledger, and
  read-only counts.
- `routines_webhook.py` exposes one `Webhook.deliver` interface and
  `build_webhook(receiver)` factory. The Hermes adapter owns V2 signing. The
  Grok adapter owns Bearer authentication. Both use redirect-free bounded HTTP,
  but each keeps its acknowledgement classifier separate.
- `routines.py` owns scheduling, independent Cloud and delivery backoff,
  stop/disable checks, and the single lifecycle entry point.
- `cli.py` is the composition root. It reads one credential snapshot, builds the
  request workspace, and supplies a zero-resource routines lifecycle factory
  only after bearer, service marker, enabled state, `always_on`, and account
  binding checks pass.
- `server.py` accepts the optional lifecycle factory and an HTTP-readiness gate.
  Disabled callers take the existing lifespan path and construct no worker,
  Cloud client, process lock, database, or task.
- `diagnostics.py` exposes configuration state and read-only value-free counts;
  it never creates a routines database.

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
full-jitter exponential backoff. Hermes delivers only `202` with
`status=accepted` or `200` with `status=duplicate`. Grok delivers only exact
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
  Bot may start a duplicate run. Add this sentence to its instruction: "Treat
  event_id as the idempotency key and refuse to act if you have already acted on
  that event_id."
- Secrets, URLs, history keys, account email, task content, and response bodies
  are absent from events, logs, diagnostics, support output, and exceptions.
- Things Cloud remains unsupported. History family versions and field meanings
  are verified by fixtures, not by a provider contract.

## Acceptance boundary

Automated tests use fake clocks, fake grouped Cloud pages, injected crashes,
and local webhook servers. The owner must separately restart the supervised
service, configure the intended receiver and MCP connection, and validate a
fresh directly tagged task. No live account is used by the automated suite.

On 2026-09-04, a safe synthetic request to the Grok Bot desktop beta webhook
confirmed the request and acknowledgement shape in this ADR. No provider
documentation says that this integration is supported. A desktop beta update
may change the contract.
