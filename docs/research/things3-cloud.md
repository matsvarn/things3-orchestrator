# Things Cloud protocol research

**Unofficial. Not a user guide.** This note describes another project's
reverse-engineered Cloud protocol. This repo is not affiliated with Cultured
Code. Using it may conflict with their terms; they can block access or
disable an account. Do not hammer Cloud. Owners start at the README.

Reviewed: 2026-08-13. Task7 update reviewed: 2026-08-30.

Primary source:
[`evanpurkhiser/things3-cloud` at `1281f43`](https://github.com/evanpurkhiser/things3-cloud/tree/1281f43bc677325968a6fdea242a5c39bb04d208)
(package `0.8.3`). All source links below use this commit. This note describes
that Rust CLI's Cloud protocol, not this project's MCP tools.

Scope: auth, incremental sync, local cache, commit envelope, IDs, rate limits, checklists/headings/recurrence, crash-adjacent writes, and performance tricks. Extra command files (`src/commands/*.rs`, `src/common.rs`, `src/cmd_ctx.rs`, `src/app.rs`) are cited only where the listed protocol files do not show write behavior.

## Task7 update

Upstream commit
[`04b6ff6`](https://github.com/evanpurkhiser/things3-cloud/commit/04b6ff6c04cd2d0a96ca0828cc27231c038ba073)
added `Task7` on 2026-08-22. Current task, Project, and heading creates emit
`Task7`. Supported Task6 and Task7 mutations also emit `Task7`. The payload
keeps the Task6 fields and adds opaque repeater bookkeeping in `rp`. This
project accepts Task7 history, emits Task7 task-family writes, and rejects an
unknown future numbered entity before it folds any event from that page.

## What it is

A Rust CLI (`things3`) that talks to the Things Cloud API, with incremental disk cache and mutations for tasks, projects, areas, tags, checklists, schedule, and reorder. Headings and recurrence writes are still open. ([README.md](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/README.md), [ROADMAP.md](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/ROADMAP.md), [Cargo.toml](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/Cargo.toml))

## Auth and storage

Credentials are a JSON `{email, password}` file at `{XDG state}/things3/auth.json`, with a one-time rename from the legacy `{XDG_STATE_HOME or ~/.local/state}/things-cli` directory. Unix mode is `0o600`. Writes are tmp-file then rename. `THINGS3_EMAIL` / `THINGS3_PASSWORD` override the file. ([src/auth.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/auth.rs), [src/dirs.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/dirs.rs), [README.md](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/README.md))

HTTP base is `https://cloud.culturedcode.com/version/1`. Every request impersonates Things Mac:

- `User-Agent: ThingsMac/32209501`
- `things-client-info`: base64 of `{"dm":"Mac14,2","lr":"US","nf":true,"nk":true,"nn":"ThingsMac","nv":"32209501","on":"macOS","ov":"26.3.0","pl":"en-US","ul":"en-Latn-US"}`
- `App-Id: com.culturedcode.ThingsMac`
- `Schema: 301`
- `App-Instance-Id`: `THINGS_APP_INSTANCE_ID` or `"things3-cloud"`
- `Accept: application/json`, `Accept-Charset: UTF-8`

JSON bodies also set `Content-Type: application/json; charset=UTF-8` and `Content-Encoding: UTF-8`. ([src/client.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/client.rs))

Verify is `GET /account/{urlencoded email}` with `Authorization: Password {urlencoded password}`. The response field `history-key` is stored on the client. History GET and commit POST do **not** attach `Authorization`; they use the history-key in the path. A failed history page fetch re-authenticates and retries once. ([src/client.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/client.rs), [src/log_cache.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/log_cache.rs))

Live writes seed `head_index` from the on-disk cursor, not from a fresh `/account` call. ([src/cloud_writer.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/cloud_writer.rs))

## Incremental sync and local cache

### History pages

`GET /history/{history_key}/items?start-index=N` returns JSON with `items`, `current-item-index`, `end-total-content-size`, and `latest-total-content-size`. ([src/client.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/client.rs))

Pagination is **not** GROUP COUNT vs `current-item-index`. Both the uncached full pull and the append-log sync do:

1. Fold every element of `items` (each element is a UUID→wire-object map).
2. Advance `start_index` by `items.len()`.
3. Set `head_index` from `current-item-index`.
4. Stop when `items` is empty **or** `end-total-content-size >= latest-total-content-size`.

([src/client.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/client.rs), [src/log_cache.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/log_cache.rs))

Observed item shape: `{ uuid: { "t": operation, "e": entity, "p": properties } }`. Replaying in order yields current state. ([src/wire/mod.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/wire/mod.rs))

### Disk cache format

Cache directory: `{app_state_dir}/append-log`. ([src/dirs.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/dirs.rs))

| File | Role |
| --- | --- |
| `things.log` | Append-only NDJSON. One history `items[]` element per line (a JSON object keyed by UUID). |
| `cursor.json` | `{next_start_index, history_key, head_index, updated_at}` |
| `state_cache.json` | `{version: 2, log_offset, state}` — folded `RawState` plus the byte offset already consumed from `things.log` |

Cursor and state-cache writes are tmp + rename. If `state_cache.json` version ≠ `2`, fold restarts from empty. A trailing line without `\n` is ignored until the next complete write. After each non-empty page, the log is flushed and the cursor is persisted. If the on-disk cursor already has a `history_key`, authenticate is skipped and that key is reused. A changed history-key is written into the cursor; the log is **not** wiped. ([src/log_cache.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/log_cache.rs))

`--no-sync` / `--no-cloud` fold the log only. `--load-journal` loads a JSON array of wire items instead of Cloud. ([src/app.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/app.rs), [README.md](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/README.md))

### Fold

`t=0` Create replaces the UUID. `t=1` Update merges known patches; if the UUID is missing, the update is inserted as a create. `t=2` Delete removes the UUID. Unknown `t` is ignored. Unparseable UUID keys are skipped. Settings entities are ignored. ([src/store/state.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/store/state.rs), [src/wire/wire_object.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/wire/wire_object.rs))

Materialized store indexes Task3/4/6/7, Area2/3, Tag3/4, and ChecklistItem/2/3. Checklist items attach to the parent task and sort by `ix`. Headings (`tp=2`) are dropped from most listings. Recurrence templates (`rr` set and `rt` empty) are dropped from Someday. Tombstone entities are excluded from short-ID matching. ([Task7 compatibility](https://github.com/evanpurkhiser/things3-cloud/commit/04b6ff6c04cd2d0a96ca0828cc27231c038ba073), [src/store/entities.rs](https://github.com/evanpurkhiser/things3-cloud/blob/04b6ff6c04cd2d0a96ca0828cc27231c038ba073/src/store/entities.rs))

## Commit / write envelope

`POST /history/{history_key}/commit?ancestor-index={idx}&_cnt=1` with JSON object `{uuid: WireObject, ...}` and header `Push-Priority: 10`. `idx` defaults to `head_index`. Response `server-head-index` becomes the new head. Several UUID changes go in one POST (`BTreeMap`). ([src/client.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/client.rs), [src/cloud_writer.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/cloud_writer.rs))

Serialized wire object is always `{t, e, p}`:

| `t` | Meaning |
| --- | --- |
| 0 | Create / full snapshot |
| 1 | Sparse update |
| 2 | Delete (`p` empty) |

Current write entity names are `Task7`, `Area3`, `Tag4`, and `ChecklistItem3`. Task6 and Task7 mutations emit `Task7`. Area writes remain `Area3`. ([Task7 compatibility](https://github.com/evanpurkhiser/things3-cloud/commit/04b6ff6c04cd2d0a96ca0828cc27231c038ba073))

`TaskProps` (create) serializes the full field set (no `skip_serializing_if`). `TaskPatch` (update) omits unset keys and uses `Option<Option<T>>` so `sr`/`tir`/`dd`/`sp`/`rr` can be sent as JSON `null` to clear. ([src/wire/task.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/wire/task.rs))

### Task7 create

New tasks are `t=0` `e=Task7` with `ThingsId::random()`, `cd` and `md` set to now, `xx: {"_t":"oo","sn":{}}`, and notes only if non-empty. Empty notes stay `nt: null`. Inbox is `st=0`. A Project or Area container forces `st=1` plus `pr` or `ar`. Today is `st=1` plus `sr` and `tir`. This adapter uses UTC midnight for `sr`, `tir`, `rmd`, and `dd`. Deadline is `dd`. Sibling insertion uses `ix` gap math or a stride-1024 rebalance. ([src/commands/new.rs](https://github.com/evanpurkhiser/things3-cloud/blob/04b6ff6c04cd2d0a96ca0828cc27231c038ba073/src/commands/new.rs), [src/wire/task.rs](https://github.com/evanpurkhiser/things3-cloud/blob/04b6ff6c04cd2d0a96ca0828cc27231c038ba073/src/wire/task.rs))

Task field map used on the wire ([src/wire/task.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/wire/task.rs)):

| Key | Meaning | Enums |
| --- | --- | --- |
| `tt` | title | |
| `nt` | notes | |
| `tp` | type | 0 todo, 1 project, 2 heading |
| `ss` | status | 0 incomplete, 2 canceled, 3 completed |
| `st` | list | 0 inbox, 1 anytime, 2 someday |
| `sr` | start/scheduled day | |
| `tir` | today-index reference day | |
| `dd` | deadline | |
| `pr`/`ar`/`agr`/`tg` | project / area / heading / tag ID lists | |
| `ix` / `ti` | structural / Today sort index | |
| `sb` | evening bit | 1 evening |
| `rr` / `rt` | recurrence rule / template IDs | |
| `tr` | trashed | |
| `cd` / `md` | created / modified | |

Schedule updates: anytime clears `sr`/`tir` and `sb`; today sets `st=1` + `sr`/`tir` = today; evening is today plus `sb=1`; someday clears dates; a future date uses `st=2`. ([src/commands/schedule.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/commands/schedule.rs))

Projects are also `Task7` with `tp=1`. Areas are `Area3` `{tt, tg, ix, xx}`. Tags are `Tag4` `{tt, sh, ix, pn, xx}`. Delete is `t=2` with the current entity name and empty `p`, not a `Tombstone2` create. ([src/commands/projects.rs](https://github.com/evanpurkhiser/things3-cloud/blob/04b6ff6c04cd2d0a96ca0828cc27231c038ba073/src/commands/projects.rs), [src/commands/delete.rs](https://github.com/evanpurkhiser/things3-cloud/blob/04b6ff6c04cd2d0a96ca0828cc27231c038ba073/src/commands/delete.rs))

### Notes

Writes use structured notes `{_t: "tx", t: 1, ch: crc32(utf8), v: text}` via `crc32fast`. Read accepts legacy plain string, `t=1` (`v`), or `t=2` (`ps[].r` joined by newline). Unicode line/paragraph separators are normalized to `\n`. ([src/common.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/common.rs), [src/wire/notes.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/wire/notes.rs), [Cargo.toml](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/Cargo.toml))

## IDs

`ThingsId` is 16 bytes. Display/serialize is Bitcoin-style Base58, alphabet `123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz` (no `0OIl`), length 1..=22. Compact IDs decode directly. Hyphenated UUIDs are accepted at parse time by SHA1 of the **uppercase** canonical UUID string, truncated to 16 bytes. `ThingsId::random()` generates random UUID bytes, then that SHA1 truncation, then Base58 — so new IDs are never hyphenated UUID strings. `"0OIl"` is rejected. CLI matching uses shortest unique Base58 prefixes. ([src/ids/things_id.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/ids/things_id.rs), [src/ids/matching.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/ids/matching.rs), [src/cmd_ctx.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/cmd_ctx.rs))

This repo does **not** document that standard UUIDs crash Things.app. The write path simply never emits them.

## Rate limits

No sleep, throttle, or 1-request-per-second limiter exists in `client.rs` or `log_cache.rs`. Sync can issue many GETs back-to-back, and a background thread fetches pages while the main thread folds stale cache. There is no 2s debounce. Batching is “all planned `WireObject`s in one commit,” not delayed HTTP. ([src/client.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/client.rs), [src/log_cache.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/log_cache.rs))

## Checklists, headings, recurrence

**Checklists — implemented.** Separate `ChecklistItem3` entities. Parent link is `ts` (task UUID list; deserializer also accepts a single UUID). Status uses the same `ss` enum as tasks. Add/rename/remove and check/uncheck/check-cancel are first-class. Checklist creates include `tt`, `ts`, `ss=0`, `ix`, `cd`, `md`. Deletes are `t=2` `ChecklistItem3`. They can share the same commit as a task patch. ([src/wire/checklist.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/wire/checklist.rs), [src/commands/edit.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/commands/edit.rs), [src/commands/mark.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/commands/mark.rs), [README.md](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/README.md))

**Headings — read only.** `tp=2` is `TaskType::Heading`. Views filter them. Parent heading is `agr`. ROADMAP item “Add/remove/rename headers for projects” is unchecked. ([src/wire/task.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/wire/task.rs), [src/store/mod.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/store/mod.rs), [ROADMAP.md](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/ROADMAP.md))

**Recurrence — read model only.** `rr` is a structured rule (`tp` 0 fixed / 1 after-completion; `fu` 8 daily / 16 monthly / 256 weekly; `fa`, `of`, `sr`, `ia`, `ed` default `64092211200`, `rc`, `ts`, `rrv` default 4). Template vs instance: template has `rr` and empty `rt`; instance has no `rr` and nonempty `rt`. ROADMAP items “Resolve recurring items” and “Mark items as recurring” are unchecked. No command writes `rr`. ([src/wire/recurrence.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/wire/recurrence.rs), [src/store/entities.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/store/entities.rs), [ROADMAP.md](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/ROADMAP.md))

**Reorder — implemented.** Updates `ix` (and Today `ti`) with stride 1024. ([src/commands/reorder.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/commands/reorder.rs), [README.md](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/README.md))

## Crash-adjacent / safety notes

Documented in this repo (behavior, not a crash warning):

- Never send `Task7` for an Area UUID. Area updates remain `Area3`. ([src/commands/edit.rs](https://github.com/evanpurkhiser/things3-cloud/blob/04b6ff6c04cd2d0a96ca0828cc27231c038ba073/src/commands/edit.rs))
- New IDs are compact Base58, not hyphenated UUIDs. ([src/ids/things_id.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/ids/things_id.rs))
- Do not write headings or recurrence (`rr`). ([ROADMAP.md](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/ROADMAP.md))
- Unknown entity/`t` values are preserved or ignored rather than dropped from the log. ([src/wire/wire_object.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/wire/wire_object.rs), [src/store/state.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/store/state.rs))
- Creates include `xx: {"_t":"oo","sn":{}}` (conflict-override object). ([src/commands/new.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/commands/new.rs), [src/wire/task.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/wire/task.rs))
- Index collisions trigger a full stride-1024 rebalance of siblings in the same commit. ([src/commands/new.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/commands/new.rs))
- Auth and cache files use atomic rename; auth is mode `600`. ([src/auth.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/auth.rs), [src/log_cache.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/log_cache.rs))

Risks this repo does **not** mitigate:

- No HTTP rate limit (can hammer Cloud). ([src/client.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/client.rs))
- Password stored in plaintext JSON. ([src/auth.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/auth.rs))
- `LoggingCloudWriter` debug-logs the full commit JSON. ([src/cloud_writer.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/cloud_writer.rs))
- Default `App-Instance-Id` is the literal `"things3-cloud"`, not a Mac-style instance id. ([src/client.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/client.rs))
- History-key change does not truncate `things.log`. ([src/log_cache.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/log_cache.rs))
- SHA1 mapping of hyphenated UUID keys can diverge from compact keys if both forms appear for one object. ([src/ids/things_id.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/ids/things_id.rs))

## Performance tricks

- Append-only NDJSON + folded `state_cache.json` keyed by log byte offset, so startup does not replay the whole history. ([src/log_cache.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/log_cache.rs), [README.md](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/README.md))
- Overlap: fold stale cache on the main thread while a worker pulls new pages; then fold again. ([src/log_cache.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/log_cache.rs))
- `--no-sync` for cache-only reads. ([src/app.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/app.rs))
- Cursor `next_start_index` resumes pagination. ([src/log_cache.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/log_cache.rs))
- Stack `[u8; 22]` Base58 encode; shortest-unique-prefix scan on those buffers. ([src/ids/things_id.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/ids/things_id.rs), [src/ids/matching.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/ids/matching.rs))
- `crc32fast` for notes. ([src/common.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/common.rs))
- One commit per command plan; sibling `ix` updates ride along. ([src/commands/new.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/commands/new.rs), [src/client.rs](https://github.com/evanpurkhiser/things3-cloud/blob/1281f43bc677325968a6fdea242a5c39bb04d208/src/client.rs))

## Steal vs skip (our Cloud adapter)

Steal:

- Disk cache: NDJSON history + versioned folded state + byte offset. We currently re-pull from `start-index=0` after process restart.
- Overlap fetch with a stale fold, then fold again.
- Pagination stop condition `end-total-content-size >= latest-total-content-size` — verify against our GROUP COUNT vs `current-item-index` loop; keep whichever matches live Cloud.
- Mac headers on **all** methods (`Schema`, `App-Id`, `things-client-info`), not only POST.
- `ancestor-index` from `current-item-index` / `server-head-index`; batch every planned UUID into one commit.
- Full `Task7` create (`xx` conflict object, empty arrays, `nt` null when empty). Keep Base58 IDs.
- Notes `{_t:"tx", ch:crc32, v, t:1}` (we already do this).
- Emit `Task7` for Task6 and Task7 mutations. Keep `Area3` for Area writes. Filter `tp=2` and `rr` templates from listings.
- Sparse patches that can send JSON `null` to clear `sr`/`tir`/`dd`.
- Atomic tmp+rename for any cache we add; version the snapshot so old files are discarded.
- If we add checklists later: `ChecklistItem3` as sibling UUIDs in the same commit, parent `ts`.
- If we add reorder later: `ix` gap / stride-1024 rebalance, not fractional indexes.

Skip / do not copy:

- Missing 1 req/s limiter and 2s sync debounce. Keep ours.
- Auth only on `/account`. Keep `Authorization: Password …` on every request unless we prove history-key-only GETs are stable.
- Default `App-Instance-Id = "things3-cloud"` and `Push-Priority: 10`. Keep a Mac-shaped instance id; treat priority 5 vs 10 as unknown.
- Setting `md` on create (they send now; we send `md: null` on create). Do not change create `md` without a Things.app round-trip.
- SHA1-truncating hyphenated UUIDs on parse. Keep one identity form (compact Base58) end to end.
- Plaintext `auth.json` and logging full commit bodies.
- Heading writes, recurrence writes, and `agr` placement. They have not shipped these either.
- Wiping nothing when `history-key` changes. If we cache, invalidate on key mismatch.
- Cache-only `--no-sync` as the MCP default. Fine as an optional fast path, not as the only refresh.
