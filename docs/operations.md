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

These commands report saved configuration, account binding, service state,
authenticated worker liveness, durable history phase, trigger readiness,
timing, and aggregate delivery counts. They
do not print the account, receiver URL, secret, task IDs, event IDs, or history
identity. Public health remains `{"ok":true}`. Authenticated health adds only
value-free worker state, failure counts, and safe poll and delivery times.

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
errors, receiver details, and database rows. Inspect the report before sharing
it.

## Operate routines

Use guided setup for a new installation or to converge after a partial setup:

```console
things-orchestrator routines setup --profile always_on --receiver grok
```

For Grok Bot, create or edit a Routine, choose "When a webhook fires", save it,
and leave it inactive before running setup. The command prompts for the
generated POST URL and key in a private terminal. Do not put either value in
argv or chat. Paste the complete receiver instruction printed by setup into
the Routine. Before setup, run this command and configure a Custom connector at
`grok.com/connectors`:

```console
things-orchestrator print-config --client grok --show-secrets
```

Confirm that it exposes exactly eight tools, including `things_get`.

For Hermes, setup prints the subscription command. Before you enter the
returned URL and secret, add `"toolsets": ["mcp-things"]` to the
`things-ai-task-created` entry in `~/.hermes/webhook_subscriptions.json`. Run
`hermes webhook test things-ai-task-created`. Keep the HMAC secret private
because a valid sender gains the eight bounded Things tools on that route.

Run `things-orchestrator routines status`. Turn Active on only after the status
reports `trigger_ready=true`. Setup enables the profile before it installs the
service.
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
- `~/.local/state/things-orchestrator/contexts-*.sqlite3`
- `~/.local/state/things-orchestrator/routines/*.sqlite3`

Credentials contain the plaintext Things Cloud password. `routines.json`
contains the Hermes webhook secret or Grok webhook key and its private URL.
Store the backup as a secret.

Do not copy an active routines database while its WAL may contain committed
rows. Disable routines, run `service install` to restart without the worker,
then copy the database. Restore the configuration and matching account-scoped
database together. Deleting the database discards delivered-event tombstones
and changes the event namespace; it is not a safe retry procedure.

## Owner-run routine acceptance

Automated tests use fake Things history and a local webhook server. To validate
a real installation, the owner must:

1. Configure the receiver's MCP connection through the matching client setup in
   a private terminal. Hermes can use `print-config --client hermes
   --show-secrets`. Grok can use `print-config --client grok --show-secrets`.
2. Configure and enable routines, then restart the supervised service.
3. Record the delivered count. Create a fresh untagged task, stop editing it,
   and wait through settlement and one poll. Confirm no new delivery.
4. Create another fresh normal task and assign the exact `AI` tag directly.
5. Confirm exactly one new metadata-only webhook reaches the receiver.
6. Confirm the receiver can call `things_get` for the public task ID through
   MCP, then perform any intended change through the existing bounded tools.
7. Remove or trash the disposable tasks through the normal owner workflow.

Do not use an existing task for this check; baseline startup intentionally does
not replay historical tasks. The official Grok Bot guide documents routines,
testing, history, approvals, and retries. It does not document the observed
beta webhook host, route, Bearer header, or acknowledgement body. On
2026-09-04, a safe synthetic request confirmed that exact observed shape. Treat
it as beta compatibility, and do not claim live compatibility until this check
passes on the intended installation.

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
