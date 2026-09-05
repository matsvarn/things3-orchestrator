# Connect a client

If this client will receive routine events, finish the MCP connection first,
then follow [Run the built-in AI task routine](routines.md).

HTTP clients use the same MCP URL and bearer. The serving host keeps the Things
Cloud email and password. Run `print-config` there in a private terminal, then
transfer only its output to the client. Prefer this HTTP path. Same-host Codex
stdio is a separate server, documented below.

Connect once. After a host upgrade, reconnect the client if its catalog is
stale. Do not install Things Cloud credentials on a client-only machine.

| Client | Selector | Put the output here |
|---|---|---|
| Codex | `codex` | Merge the TOML block into `~/.codex/config.toml`. |
| Grok | `grok` | Add a Custom connector at `grok.com/connectors` with an HTTPS URL that the public internet can reach and with required authentication. |
| Hermes | `hermes` | Run the two native Hermes commands. Hermes prompts for the bearer and tests the connection. |
| Claude Code | `claude-code` | Run the printed command. For the alternative block, run `claude mcp add-json things '<JSON>'` with the printed JSON as the argument. |
| Cursor desktop | `cursor` | Merge the `things` entry into `~/.cursor/mcp.json`. |
| Cursor Cloud Agents | `cursor-cloud` | Paste in the Cloud Agents dashboard at `cursor.com/agents`, not in `.cursor/mcp.json`. |
| Caddy | `caddy` | Install as `/etc/caddy/Caddyfile`; this is infrastructure configuration, not an MCP client. |

Render one configuration:

```console
things-orchestrator print-config --client codex --show-secrets
```

Replace `codex` with the selector from the table. Without `--show-secrets`, the
output contains `<mcp_token>` instead of the bearer. Use that safe form for
examples and review. Use the secret form only when configuring a real client.
For Hermes, the secret form prints the bearer separately from the two native
commands and warns not to paste it into a shell. Paste only that bearer at the
private Hermes prompt.

For Grok, use an HTTPS endpoint that the public internet can reach. Run this
command in a private terminal:

```console
things-orchestrator print-config --client grok --show-secrets
```

The [official xAI connector guide](https://docs.x.ai/grok/connectors) says to
open `grok.com/connectors`, choose **New Connector**, then choose **Custom**.
Provide the URL and required authentication from the command output. The guide
does not document exact form-field names. The command rejects known local and
private addresses but cannot verify DNS or public reachability. Verify the
endpoint, then confirm that Grok discovers exactly these eight tools:
`things_view`, `things_find`, `things_get`, `things_capture`, `things_update`,
`things_complete`, `things_trash`, and `things_receipt`.

Do not paste the Things Cloud password into any client. Possession of the MCP
bearer authorizes every bounded v2 mutation, including recoverable Trash and
repeat Stop. There is no per-client identity.

## Client-specific facts

- Grok Custom connectors require an MCP server that the public internet can
  reach over HTTPS. The official connector guide documents connector discovery
  in Grok conversations. It does not prove that every webhook-triggered Grok Bot
  execution receives that connector. Complete the positive routine smoke test.
- Hermes stores the MCP configuration in its active profile. Run
  `things-orchestrator print-config --client hermes --show-secrets` in a private
  terminal, then run the two printed commands one at a time on the Hermes host.
  At the private prompt, enter only the separately printed MCP bearer.
  Hermes adds the authorization scheme. Start a new Hermes session after MCP
  or skill changes. The generated skill URL is pinned to the installed release
  and fetches one `SKILL.md`. That file is not the complete skill tree. Do not
  treat it as the complete Hermes install. The `client-sync` path
  below copies the complete directory from the running host.
- Cursor Cloud Agents accept MCP configuration only through the dashboard.
  Paste the literal token; environment interpolation is unavailable. The saved
  value cannot be viewed, so bearer rotation requires another paste.
- Remote Codex uses the generated `http_headers` TOML form. Do not add a second
  `bearer_token_env_var` configuration for the same server.

## Install the same-host Codex plugin

Use this path only when Codex and Things Orchestrator run as the same OS user
on one host. Install and log in to Things Orchestrator first. Then add this Git
repository as a marketplace and install its plugin:

```console
codex plugin marketplace add matsvarn/things3-orchestrator --ref v0.11.0
codex plugin add things-orchestrator@things-orchestrator
```

This repository marketplace is self-distributed. It is not an official Codex
marketplace listing or an endorsement by OpenAI or Cultured Code. The plugin
starts `things-orchestrator serve` over stdio and bundles the Things skill. It
does not connect to a remote Things Orchestrator host.

To connect Codex to a remote host, use the HTTP renderer instead:

```console
things-orchestrator print-config --client codex --show-secrets
```

## Sync instruction files from the host

A client-only machine needs the CLI, but no Things Cloud login or local service.
Install the CLI using the [package installation step](install.md). Skip host
login and service setup on a client-only machine. The host must also support
client bundles, available from v0.11.0:

```console
things-orchestrator client-sync --url https://mcp.example.com --directory ~/things-orchestrator-skill
```

`client-sync` reads the MCP URL and bearer only. Set `THINGS_MCP_TOKEN` to the
MCP bearer, or omit it and enter the bearer at the private prompt. Do not put
the bearer on the command line.

The CLI need not match the host's exact version when both understand the
bundle format.

For a loopback host on the same machine, use `http://127.0.0.1:8787` as `--url`.
The command fetches `/client/bundle` and a fresh `tools/list` from that host.
It writes the complete managed skill tree into the directory:

- `SKILL.md`
- `agents/openai.yaml`
- `references/` including the named routine templates
- `routines/receiver-instruction.txt`

Use that directory as the complete Things skill. Do not install the GitHub
`SKILL.md` URL from `print-config --client hermes` as the complete install.
Downloaded files do not activate a provider saved prompt or an application
skill. Hermes still needs a new session after you change which skill directory
it uses. A long-lived routine receiver needs a review of the saved prompt when
the receiver instruction hash changes. Each named routine template is reported
on its own when its file changes.

The same bundle is idempotent. If the host rolls back to a bundle-capable
release, rerun `client-sync` against that host. The directory follows the
running host, not GitHub latest. Releases without `/client/bundle` require their
release-specific manual installation instructions.
Unchanged managed files that left the new bundle are removed. The command
refuses to overwrite files you edited, to delete edited files that the new
bundle dropped, unmanaged files, directory symlinks, or a tree owned by
another product.

To compare the catalog your client still has, export that client's `tools/list`
JSON and pass `--observed-tools PATH`. A match is a snapshot comparison. It is
not proof of the current connection. Without that file, the report says the
client cache is unknown and tells you to export `tools/list`. A closed output
snapshot against an additive-output host is a required catalog refresh.
Description-only catalog drift is a recommendation. A version-only host update
with identical files does not ask you to reapply prompts.

`--read-id` runs one bounded `things_get` on the fresh `client-sync`
connection. A failure or non-ok result is not activation success.
An exit status of zero means file sync succeeded. The JSON status remains
`files_synced_client_unverified`: neither a fresh read nor a matching exported
catalog proves the application's current connection or saved prompt is active.

## Refresh a closed output catalog once

Advertised tool output tolerates extra properties on documented result objects.
Input validation and the server's constructed results stay strict. Unknown
outcome enums stay closed. Incomplete-note write protection is unchanged.

Clients that cached the older closed output schema reject new fields, including
`notes_state`. Reconnect that HTTP session so the client repeats `tools/list`.
Do this once after moving to a host that advertises additive output. Later
additive fields do not need a catalog refresh unless this client's cache is
still the closed schema. Catalog metadata names policy `additive_output_v1`.
A discovery hash change is classified from that policy and bounded schema
checks. It is not a breaking-change flag by itself.

## Connection arrangements

- Desktop client and HTTP service on one Mac. Prefer the supervised HTTP
  service. Update that service once. Refresh the client catalog after that
  host change. The `client-sync` path can refresh the synced
  directory when its report says so.
- Client on another Mac, Windows, or Linux host. Use the serving host's HTTPS
  URL and MCP bearer. The client does not need Things Cloud credentials.
- Agent and service on one always-on Linux host. Use HTTP to that service.
  Agent restarts and service updates are separate. A container has its own
  loopback, so give the client an address that reaches the service.
- Hosted Grok Custom connector. Use the Grok renderer and the public HTTPS
  connector workflow. Provider reconnect and prompt steps stay manual. A
  successful interactive chat does not prove webhook Bot access.
- Grogbot or another local agent with a stdio-to-HTTP bridge. Inspect that
  executable, transport, and process owner before applying Grok web steps.
  Prefer native HTTP if the client supports it. `mcp-remote` is one bridge for
  clients that need local stdio. Its presence is not evidence that this agent
  uses that package.
- Same-host Codex plugin. This launches local `serve` over stdio and bundles
  a skill. It does not proxy the HTTP host. Updating the remote service does
  not update this process. Treat it as an advanced path, or connect Codex to
  HTTP instead.

The connection is ready when the client lists `things_view`, `things_find`,
`things_get`, `things_capture`, `things_update`, `things_complete`,
`things_trash`, and `things_receipt`. If it does not, use
[recovery.md](recovery.md).
