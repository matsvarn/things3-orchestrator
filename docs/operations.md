# Operate Things Orchestrator

Run these commands on the serving host.

## Inspect

```console
things-orchestrator service status
things-orchestrator doctor --wait
curl -sS http://127.0.0.1:8787/health
# {"ok":true}
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/mcp
# 401
```

On Linux, read service logs with
`journalctl -u things-orchestrator-http.service -e`. On macOS, inspect the
launchd agent in Console.

Inspect routines separately:

```console
things-orchestrator routines status
things-orchestrator support-bundle
```

See [Run the built-in AI task routine](routines.md) for setup, trigger
semantics, receiver instructions, and the two-part smoke test.

`routines status` reports saved configuration, account binding, service state,
durable history phase, trigger readiness, timing, and aggregate delivery
counts. When an MCP bearer is configured, it also makes one bounded
authenticated runtime probe for worker liveness. It does not print the account,
receiver URL, secret, task IDs, event IDs, or history identity. Public health
remains `{"ok":true}`. The authenticated health response retains the installed
version, tool hash, commit, and capabilities. It also adds value-free worker
state, failure counts, and safe poll and delivery times.

For routines, `support-bundle` reads configuration, the durable projection, and
service evidence. It does not make the authenticated runtime probe. Worker
liveness remains `unknown` when that evidence does not prove it.

`doctor` automatically checks the TLS origin saved by `login`. Use `--url` to
add a one-time endpoint that is not saved:

```console
things-orchestrator doctor --wait --url https://mcp.example.com
```

## Update and rollback

Install the new exact tag, refresh the service definition, and prove the running
artifact:

```console
uv tool install --force "git+https://github.com/matsvarn/things3-orchestrator.git@<new-tag>"
things-orchestrator service install
things-orchestrator doctor --wait
```

For rollback, replace `<new-tag>` with `<previous-tag>` and repeat the commands.
Doctor fails with
`service: stale - restart` if the running commit differs from the installed
commit.

A host rollback keeps the installed tag identity.

From v0.11.0, clients can run `client-sync` to follow the `/client/bundle`
served by the running host. They must not fetch GitHub latest. See
[Connect a client](clients.md).

## Create value-free diagnostics

Verify Cloud authentication and a fresh full-history fold without writing to
Things or reusing the normal state cache:

```console
things-orchestrator cloud-check
```

The command returns only a fixed status and aggregate counts. It exits with
status 1 when the Cloud read fails.

Create a support report before filing an issue:

```console
things-orchestrator support-bundle
```

The JSON contains the installed version and commit, platform name, Python
version, tool hashes, Cloud status and counts, endpoint class, service status,
operation-state counts, and value-free routine state when available. It omits
account values, network locations, Things content and IDs, credentials, raw
errors, receiver URL, receiver host, receiver credential, and database rows. It
does include the receiver kind. Its routine section is not a live readiness
probe. Inspect the report before sharing it.

## Operate routines

Use guided setup for a new installation or before receiver values have been
saved. Receiver setup, MCP grants, and the smoke test are in
[Run the built-in AI task routine](routines.md).

```console
things-orchestrator routines setup --profile always_on --receiver grok
```

`configure` remains compatible with `--url`, but receiver credentials are
always entered in a private terminal.

Setup enables the profile before it installs the service.
If service installation fails, the enabled configuration already contains the
receiver values. Fix the service problem, then run
`things-orchestrator service install`. Do not re-enter one-time receiver values.

The commands below are low-level recovery and automation controls.

Disable delivery and polling without deleting configuration or state:

```console
things-orchestrator routines disable
```

The running worker checks the saved enabled state at least once per configured
poll interval. It stops delivery as well as polling. Run `service install` when
you need an immediate restart into the disabled state.

Enabling is idempotent but does not hot-start a worker:

```console
things-orchestrator routines enable
things-orchestrator service install
```

Cloud failures and delivery failures back off independently. Routine polling
does not take the MCP request lock. Delivery uses a stable event ID and is
at-least-once: a receiver can see a retry after it accepted an earlier request
whose local acknowledgement was interrupted. Hermes-compatible receivers must
deduplicate `X-Request-ID`. Grok receives the same identity as `event_id` in the
JSON body. Both receivers must deduplicate before acting. An
accept-before-commit failure may otherwise start a duplicate run.

## Rotate the MCP bearer

In a private terminal:

```console
things-orchestrator login --rotate-token
things-orchestrator service install
things-orchestrator doctor --wait
things-orchestrator print-config --client codex --show-secrets
```

Reconfigure every client. Old configurations receive 401 until they contain
the new bearer. Cursor Cloud Agents require another dashboard paste because the
stored value cannot be viewed.

## Back up

Back up these private paths, or their `XDG_CONFIG_HOME` and `XDG_STATE_HOME`
equivalents:

- `~/.config/things-orchestrator/credentials.json`
- `~/.config/things-orchestrator/preferences.json`
- `~/.config/things-orchestrator/routines.json`
- `~/.local/state/things-orchestrator/launcher`
- `~/.local/state/things-orchestrator/state.json`
- `~/.local/state/things-orchestrator/journal-*.sqlite3`
- `~/.local/state/things-orchestrator/routines/*.sqlite3`

Older installations may also have `contexts-*.sqlite3` files. The current service
does not create or use them.

Credentials contain the plaintext Things Cloud password. `routines.json`
contains the Hermes webhook secret or Grok webhook key and its private URL.
Store the backup as a secret.

Do not copy an active routines database while its WAL may contain committed
rows. Disable routines, run `service install` to restart without the worker,
then copy the database. Restore the configuration and matching account-scoped
database together. Deleting the database discards delivered-event tombstones
and changes the event namespace; it is not a safe retry procedure.

## Owner-run routine acceptance

Automated tests use fake Things history and a local webhook server. Validate a
real installation with the two-part smoke test in
[Run the built-in AI task routine](routines.md).

## Configure owner preferences

```console
things-orchestrator configure --note-style natural
things-orchestrator configure --timezone Europe/Berlin
things-orchestrator configure --url https://mcp.example.com
```

Note style, source scheme, and URL changes apply to the next command. A timezone
change requires `things-orchestrator service install` because the running
server captures its timezone at startup.

## Uninstall

```console
things-orchestrator service uninstall
uv tool uninstall things-orchestrator
```

Disable Tailscale Serve or remove the Caddy site before deleting private state.
The service command removes only its exact launchd agent or systemd unit. It
does not delete credentials, journals, contexts, or Things data.
