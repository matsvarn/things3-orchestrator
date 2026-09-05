# Product contract

Release contract: v0.11.0

This file is the concise product reference for the current release. Public
claims must also remain consistent with the executable tool schemas, tests,
README, and trust documentation.

## Promise

Things Orchestrator lets one owner use Things 3 from an MCP client while the
Things Cloud password stays on a host they control. It is unofficial and is not
affiliated with or endorsed by Cultured Code.

The intended user is technically comfortable operating a Mac or Linux service,
connecting an MCP client, and accepting the risks of an unofficial Things Cloud
protocol. This is not a hosted service and it is not an official Things
integration.

## Public interface

The release exposes exactly 8 bounded tools:

- `things_view` reads a named Things view.
- `things_find` searches by owner text or reads direct membership within one
  exact Project or Area.
- `things_get` reads exact item IDs.
- `things_capture` creates Tasks or Projects, including nested Project Tasks.
- `things_update` changes explicit fields, direct tags, checklist rows, homes,
  dates, reminders, or supported RT1 recurrence rules.
- `things_complete` completes exact items atomically.
- `things_trash` moves exact items to recoverable Trash atomically.
- `things_receipt` reads immutable, content-minimized operation receipts.

Mutations require a caller-supplied UUID or ULID. The server refreshes current
Cloud state before a write, freezes the requested operation, records dispatch
before network I/O, and uses Cloud read-back to classify the outcome. An
uncertain write is not reported as successful. Retrying the same request ID
reconciles the frozen operation instead of posting it again.

## Supported product paths

- One Things Cloud account per server.
- macOS launchd or Linux systemd hosting.
- Loopback MCP transport, or owner-configured TLS through Tailscale Serve or
  Caddy.
- Codex, Claude Code, Cursor, Cursor Cloud, Grok, and Hermes client
  configuration.
- A repository-distributed Codex plugin and a release-pinned Hermes skill.
- Exact-tag installation and version-, commit-, schema-, contract-, and
  tool-list verification through `doctor`.
- One optional, disabled-by-default routine for a new normal, open, untrashed
  task with an exact directly assigned tag titled `AI`. The routine runs only
  in the supervised `always_on` HTTP service, sends metadata only, and adds no
  MCP tool. See [Run the built-in AI task routine](docs/routines.md).

Client configuration support means the project can render and document the
connection. It does not imply endorsement, marketplace acceptance, or official
support from the client vendor.

## Trust boundary

The serving host stores the Things Cloud password in a mode-0600 file. Never
paste that password into chat. HTTP clients receive an MCP URL and one shared
bearer; possession of that bearer, or access to the stdio server, grants all
eight tools and therefore every bounded write. There is no read-only bearer or
per-client authorization in v0.11.0.

Task data returned to a client can reach its configured model provider. Owners
must trust the serving host, client, and model provider. TLS termination and
remote-host security remain owner responsibilities.

## Deliberate limits

The current interface does not provide advanced scope redesign, heading
deletion, Project merge, restore from Trash, permanent deletion, arbitrary
replacement of unreadable notes, registry mutation, RT2 recurrence writes, or full-system
setup. It also does not provide mutation coaching through a staged approval
workflow.

Those capabilities remain deferred until they have a bounded public contract.
Marketing and dogfood plans must not present them as currently available.

## Product evidence

A healthy service and an eight-tool listing prove deployment and discovery,
not user activation. The primary human activation event is a fresh real client
returning one correct current read from a natural prompt without mutation. A
first useful write is a separate event and must have a receipt and Cloud
read-back evidence.

Human dogfood records belong in `docs/dogfood.md`. Automated tests, synthetic
model replays, the live acceptance runner, and the website simulation do not
count as human activation evidence.

## Claim discipline

- Say self-hosted, unofficial, and not affiliated with Cultured Code.
- Say recoverable Trash, not deletion or cleanup.
- Say which host, client, and topology were actually exercised.
- Do not infer adoption from releases, downloads, tool discovery, or CI.
- Do not claim official client support or marketplace availability unless that
  status is independently verified.
