# Routines architecture review brief

## Project

Things Orchestrator is a Python 3.12+ self-hosted MCP server for one Things Cloud account. It exposes exactly eight bounded tools. The normal transport is an authenticated loopback Streamable HTTP server supervised by launchd or systemd. Stdio exists only for the same-host Codex plugin. The Things Cloud protocol is unofficial.

The baseline is clean at `0323fd561357c57fdcd72277ef5e7df95867aa93`. The existing suite has 1,475 passing tests. Strict mypy and Ruff pass.

## Feature

Add an opt-in routines worker inside the `serve-http` process. The first routine observes a newly created task, waits for sparse updates, requires an exact directly assigned `AI` tag, atomically enqueues one durable `task.created` event, and delivers metadata to an external agent. The worker never mutates Things. The receiver uses the existing MCP tools.

Routines are disabled by default. They may run only when `serve-http` is supervised and the saved host profile is `always_on`. They must never run for stdio, the same-host plugin path, unconfigured installations, or a `personal` host profile. Disabled startup constructs no worker, performs no Cloud request, opens no routines database, and creates no background task.

## Existing runtime facts

- `cli._workspace()` owns the request-path `CloudClient`, `CloudLibrary`, global `state.json`, account-scoped mutation journal, and context store.
- `ThingsMCPServer.call_tool()` holds one `anyio.Lock` across every valid MCP read or write, then dispatches blocking work in a worker thread.
- `serve-http` currently starts only the MCP session manager from the Starlette lifespan. Public health is exactly `{"ok": true}` and does no I/O.
- `CloudClient.items(start_index)` returns flattened events plus `groups = len(raw_items)`. Each raw group is a mapping of event UUID to `{t, e, p}`. `loaded_index` counts groups, not events.
- `t=0` creates an entity. Sparse `t=1` updates preserve omitted task fields. `t=2` permanently deletes the entity. Unknown versioned entities such as `Task8` fail closed. Unknown unversioned entities are skipped today.
- A task record stores its own tag UUIDs in `p.tg`. Inherited tags are computed later from project and area ancestry. The watcher therefore can test direct tags without building ancestry.
- Tag titles come from Tag history entities. The rule needs the UUID for the exact title `AI`, then requires that UUID in the task record's own current `tg` list.
- A create group can be followed by sparse updates in later groups. Settlement must be based on observation time, not on an assumed complete create payload.
- History GET 404 triggers one account re-verification. A changed history key resets to a new baseline. An unchanged key retries the requested index once.
- The request-path cache is not account-scoped. The mutation journal is account-scoped and has a separate cross-process apply fence. Routines must share neither.

## Performance and durability contracts

- Poll interval is 60 to 3600 seconds, default and minimum 60.
- Once caught up, one interval performs at most one history GET.
- Cloud failures use exponential backoff with jitter capped at 15 minutes.
- Delivery retries have independent bounded backoff and eventually dead-letter.
- Run blocking Cloud, SQLite, and webhook work off the async event loop.
- One worker loop and one routines-store writer. Drain at most 25 due deliveries per pass.
- First startup and a changed history identity establish a no-backfill baseline at the current head.
- Cursor advancement and derived event insertion must commit in one SQLite transaction.
- Delivery is at least once. Event identity is deterministic and opaque. The private Things history key must not appear in the event ID or payload.
- A blocked 30-second Cloud GET must not block public health, MCP reads, receipts, or mutations.

## Candidate history representations

1. Share the request `CloudLibrary` under the MCP lock. Reject because every poll can block every MCP call and the worker would share cache and mutable records.
2. Create an isolated second `CloudLibrary`. Correctness is simpler, but it duplicates the full task library, writes another cache, and does more folding than this rule needs.
3. Add a stable-group `CloudClient.history_groups(start_index)` interface and keep a bounded routines projection in the routines database. The projection holds only tag UUID to title and unsettled task candidates with creation position, direct tag UUIDs, completion or deletion state, and settle deadline. This is preferred if it can handle sparse updates, pagination, restarts, duplicate polling, tag renames/deletes, and account history changes without replay.

## Current Hermes contract, checked 2026-09-04

- POST JSON to `/webhooks/<route-name>` or the configured full receiver URL.
- Put `event_type: "task.created"` in the JSON because generic event selection reads `event_type` or `type`.
- Put the opaque event ID in `X-Request-ID` for Hermes deduplication.
- Put Unix seconds in `X-Webhook-Timestamp`.
- Put the raw lowercase hex HMAC-SHA256 of `<timestamp>.<raw body>` in `X-Webhook-Signature-V2`.
- Hermes accepts an agent-mode run with HTTP 202 and `status: accepted`. A duplicate returns HTTP 200 and `status: duplicate`.
- Metadata-only body: schema version, event ID, event type, routine ID, public task ID, and observed timestamp. No title, notes, checklist, project content, account email, or history key.

## Proposed public caller usage

```text
things-orchestrator routines configure --profile always_on --url https://agent.example/webhooks/things-ai --interval 60
# CLI prompts for the webhook secret through /dev/tty and never accepts it in argv.
things-orchestrator routines enable
things-orchestrator routines status
things-orchestrator routines disable
```

`configure` saves a valid disabled profile. `enable` requires a complete `always_on` profile. `disable` is idempotent. Configuration changes require a supervised HTTP service restart, except a running worker must observe disablement and stop polling within one poll interval.

## Proposed module seams

- `routines_config.py`: parse and atomically store a versioned discriminated union. Own URL, profile, interval, settle window, retry policy, and redacted rendering. Secret is a redacted value object and is stored only in mode-0600 `routines.json`.
- `routines.py`: domain types, projection reducer, worker state machine, backoff, and one `RoutineWorker.run(stop_event)` interface.
- `routines_store.py`: account-scoped SQLite cursor, tag projection, candidates, outbox, attempts, and dead letters. One transactional `apply_groups()` interface advances the cursor and enqueues events.
- `routines_webhook.py`: canonical JSON, Hermes V2 signature, bounded HTTP adapter, and response classification.
- `cloud.py`: preserve raw stable group boundaries through a new read-only history method. Existing `items()` uses that method and keeps existing folding/cache behavior.
- `cli.py`: compose the worker only for eligible `serve-http`; configuration commands never construct Cloud or the routines store unless the command needs them.
- `server.py`: accept an optional worker lifecycle and start it after HTTP readiness from lifespan. The worker has no reference to `ThingsMCPServer._lock`.
- diagnostics and support: expose only value-free routine status and counts. Public health stays unchanged.

## Questions for review

1. Can the minimal projection correctly implement settled direct-tag matching without a second full `CloudLibrary`? Identify any history shape that breaks it.
2. What exact SQLite schema and transaction boundary best prove crash convergence and deterministic logical event identity?
3. How should the projection remain bounded while still settling sparse updates after long downtime or tag rename/delete events?
4. Should the event ID derive from routine ID and public task ID only, or also the stable creation group index? The result must not reveal the history key and must not duplicate a logical event after restart.
5. What response classification and retry/dead-letter policy should the Hermes adapter use? Treat a 202 accepted and 200 duplicate as delivered. Challenge any unsafe interpretation of other 2xx responses.
6. How should the Starlette lifespan start after readiness and stop cleanly without ever constructing or opening routines resources in the disabled path?
7. Identify shallow interfaces, leaked storage or wire details, hidden shared state, or test gaps in the proposed module map.

Return a concrete architecture verdict. Prefer a smaller design when it meets the contracts. Name blocking flaws and the tests that would catch them. Do not implement or edit files.
