# Maintainer guide

Keep one production server and one stable model Interface.

## Module map

- `v2.py` owns the eight bounded public contracts, immutable operation drafts
  and manifests, and taint-preserving output. Schemas stay flat. Do not add
  `oneOf` or `anyOf` to discovery schemas. Discovery output schemas expose
  control flow and compact item summaries. Runtime facts stay strict through
  the `Result` model.
- `workspace.py` remains the one transaction engine. V2 shares private
  preparation, application, read-back, and reconciliation primitives. It never
  constructs the v1 `CommitCall` language.
- `consistency.py` owns native-state conflict detection for diagnostics
  and review signals.
- `config.py` owns credentials, owner preferences, normalized MCP endpoints,
  and the exact plugin launcher binding.
- `client_config.py` renders one client artifact from one endpoint and bearer.
- `deployment.py` owns package resources, version, installed Git identity,
  cache version, authenticated health detail, `tool_schema_hash`, and
  `tool_contract_hash`.
- `doctor.py` proves public health privacy, authenticated health, MCP
  initialization, the exact tool list, hashes, version, and commit.
- `service.py` owns launchd and systemd lifecycle planning and execution.
- `journal.py` creates immutable v2 operations, performs legal compare-and-set
  transitions, claims the account fence, and appends exact receipt rows. The
  journal path is namespaced by account. It rehashes the complete persisted
  manifest before review, authorization, reconciliation, and application.
  Retained v1 rows stay private for recovery only.
- `library.py` is the in-memory Things graph used by tests and Cloud.
- `cloud.py` syncs and commits through the unofficial Things Cloud API.
  It coalesces one envelope per UUID and verifies a forced Cloud pull. Its
  strict grouped-history interface preserves group positions for routines and
  raises a typed history-identity change instead of joining two histories.
  Maintainer protocol notes: `docs/research/things3-cloud.md` (not a user
  guide; account/ToS risk).
- `routines_config.py` owns the account-bound private configuration union, the
  fixed trigger and event constants, the complete receiver instruction, the
  `HermesReceiver | GrokReceiver` union, receiver-specific URL validation, and
  redacted rendering. Missing `receiver_kind` in version 1 means Hermes.
- `routines_store.py` owns the process lock, tag-only seed, bounded live task
  projection, canonical metadata body, and durable event ledger. Cursor
  advancement and event insertion share one SQLite transaction. It never
  reuses the mutation journal.
- `routines_webhook.py` builds the one-method adapter from `HermesReceiver` or
  `GrokReceiver`, sends the exact stored body over bounded redirect-free HTTP,
  ignores environment proxies, and keeps the acknowledgement classifiers
  separate.
- `routines.py` owns one polling and delivery loop, independent backoff, hot
  disablement, and the dedicated blocking-work limit.
- `owner_authority.py` is retained only for signed legacy recovery. It is not
  part of the normal v2 mutation path. `server.py` is the v2 MCP adapter.
- `server.py` exposes stdio and Streamable HTTP. The optional routine lifecycle
  is created only after the HTTP socket is ready and never receives the MCP
  request lock or request-path state.
- `live_acceptance.py` owns the restart-safe disposable write workflow used as
  a release gate. It persists request IDs before mutation and passes only after
  receipt and Trash read-back.
- `cli.py` defines the owner commands: `login`, `configure`, `routines`,
  `service`, `serve-http`, `print-config`, `cloud-check`, `support-bundle`, and
  `doctor`.
  Production installs use an exact
  Git tag through `uv tool install`; clone development uses the same commands
  through `uv run`. `login` keeps the existing bearer unless
  `--rotate-token`. `print-config --client` writes nothing and prints one
  client artifact. `doctor` performs authenticated Streamable HTTP
  initialization plus `tools/list` against loopback, the saved origin, and an
  optional one-time origin.
  Install paths are in `docs/install.md`, client targets in `docs/clients.md`,
  and lifecycle operations in `docs/operations.md`. Capability evidence is in
  `docs/capability-proof.md`; human workflow coverage is in `docs/dogfood.md`.

For public write changes, deploy the exact candidate before tagging it. Run the
live acceptance workflow from `docs/capability-proof.md` to a `cleaned` result.
An unresolved pending or partial outcome blocks the release. Tag and
build assets only from the commit that passed this gate.

Do not expose v1 tools or add advanced scopes during this cutover. Keep
discovery schemas flat because some model clients
reject union schemas. Keep results bounded. Batch Cloud writes. Coalesce each
UUID. Treat post timeouts as unknown until a Cloud read proves the state.
Debounce normal reads with the history cursor. Treat a write HTTP 409 as
definitive rejection without a commit. Do not replay the old write after a
pull. Use the configured owner
timezone for Today, Logbook, and reminder dates.

Keep routines outside the MCP tool contract. Disabled or ineligible HTTP paths
must not construct a routine client, store, lock, or background task. A cold
baseline validates all history but reduces only exact-`AI` tag UUIDs before the
fixed baseline head. Live candidates retain only kind, lifecycle, trash state,
direct tags, and settlement times. Unknown entity versions or malformed
relevant fields fail the complete batch without cursor movement.

The event key is the persisted account namespace, routine ID, and public task
ID. Do not add a history position, history key, observation time, or attempt
number. Webhook delivery is at-least-once. Hermes accepts the documented exact
`200 delivered` and `200 duplicate`, plus the earlier exact `202 accepted` as a
narrow compatibility case. Grok accepts only `200` with top-level `success=true` and
a nonempty string `runUuid`. Keep response bodies, receiver details, task
content, account values, and history identity out of logs and diagnostics. Do
not add a shared permissive 2xx classifier.

The MCP server does not care which chat client you use. The canonical skill is
packaged under `src/things_orchestrator/skills/`; CI requires the Codex plugin
copy to be byte-identical. `things-orchestrator skill-path` prints the installed
directory.

## Public source allowlist

[`public-files.txt`](../public-files.txt) is the exact public boundary. Update
it in the same review as any new public file. CI compares it with the visible
source tree. Keep product notes, design work, generated media, test evidence,
local logs, credentials, and private agent traces outside it.

## Create the public history

First, finish the private validation and commit the release tree. Then export
the allowlist from that commit into a new directory. Do not copy `.git`. Do not
push or mirror the private repository.

```console
public_paths=()
while IFS= read -r public_path; do
  public_paths+=("$public_path")
done < public-files.txt
public_dir=../things3-orchestrator-public
test ! -e "$public_dir"
mkdir "$public_dir"
git archive --format=tar HEAD -- "${public_paths[@]}" | tar -x -C "$public_dir"
cd "$public_dir"
git init -b main
git add --all
comm -3 \
  <(printf '%s\n' "${public_paths[@]}" | sort) \
  <(git ls-files | sort)
git diff --cached --stat
git commit -m "Initial public release"
```

The `comm` command must return no paths. Run the full test, build, link, and
secret checks in the new repository. Add only the new public remote. Push only
`main`.
