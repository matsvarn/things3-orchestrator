# Maintainer guide

Keep one production server and one stable model Interface.

## Module map

- `interface.py` owns the three tool contracts. Schemas stay flat. Do not add
  `oneOf` or `anyOf` to discovery schemas. Discovery output schemas expose
  control flow and compact item summaries. Runtime facts stay strict through
  the `Result` model.
- `workspace.py` is the deep Module behind Read, Commit, and Approve. It owns
  revisions, task meaning, plans, idempotency, and verified outcomes.
- `consistency.py` owns native-state conflict detection for diagnostics
  and review signals.
- `deployment.py` owns package version, cache version, `/health`
  capabilities, `tool_schema_hash`, and `tool_contract_hash`.
- `journal.py` stores intent receipts and approval plans in SQLite. It makes
  retries safe across process restarts. Its compare-and-set claims an intent
  before Cloud I/O. The journal path is namespaced by account.
- `library.py` is the in-memory Things graph used by tests and Cloud.
- `cloud.py` syncs and commits through the unofficial Things Cloud API.
  It coalesces one envelope per UUID and verifies a forced Cloud pull.
  Maintainer protocol notes: `docs/research/things3-cloud.md` (not a user
  guide; account/ToS risk).
- `server.py` exposes stdio and Streamable HTTP.
- `cli.py` is the owner-facing seam: `login`, `serve`, `serve-http`,
  `print-config`, and `doctor`. Callers run `uv run things-orchestrator`
  from the checkout. `plugin/bin` locates that checkout when a client
  copies the plugin. Do not `pip install` this package as the install
  path: the wheel has no `plugin/` (skills or wrapper). `login` writes
  Hermes YAML plus `mcp.stdio.json` / `mcp.http.json`, keeps `mcp_token`
  unless `--rotate-token`, and keeps an HTTP URL already set. Default
  `login` / `print-config` print snippet paths. `--show-secrets` prints
  bodies including the MCP bearer. `doctor` checks credentials,
  snippets, timezone, and loopback `http://127.0.0.1:8787/health`.
  Loopback is required after a hosted URL is set, or with `--wait`.
  `doctor --url` checks remote `/health`.
  Per-client wiring lives in `docs/clients.md`. A VPS plus a client is
  `docs/host.md`. Hermes is the default paste. Capability evidence is
  `docs/capability-proof.md`. Human workflow coverage and rerun status are in
  `docs/dogfood.md`.

Do not add CRUD tools. Keep discovery schemas flat because some model clients
reject union schemas. Keep results bounded. Batch Cloud writes. Coalesce each
UUID. Treat post timeouts as unknown until a Cloud read proves the state.
Debounce normal reads with the history cursor. Treat HTTP 409 as stale
evidence. Do not replay the old write after a pull. Use the configured owner
timezone for Today, Logbook, and reminder dates.

The MCP server does not care which chat client you use. Skills live under
`plugin/skills/` and stay out of the Python wheel. Each client catalogs that
folder itself — `docs/clients.md`.

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
