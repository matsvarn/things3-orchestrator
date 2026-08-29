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
- `deployment.py` owns package version, cache version, `/health`
  capabilities, `tool_schema_hash`, and `tool_contract_hash`.
- `journal.py` creates immutable v2 operations, performs legal compare-and-set
  transitions, claims the account fence, and appends exact receipt rows. The
  journal path is namespaced by account. Retained v1 rows stay private for
  recovery only.
- `library.py` is the in-memory Things graph used by tests and Cloud.
- `cloud.py` syncs and commits through the unofficial Things Cloud API.
  It coalesces one envelope per UUID and verifies a forced Cloud pull.
  Maintainer protocol notes: `docs/research/things3-cloud.md` (not a user
  guide; account/ToS risk).
- `owner_authority.py` is host-only. The MCP server must not import it or gain
  access to its passphrase verifier or encrypted signing key. The journal pins
  only the Ed25519 public key used to verify host signatures. `server.py` is
  the v2 MCP adapter.
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

Do not expose v1 tools or add advanced scopes during this cutover. Keep
discovery schemas flat because some model clients
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
