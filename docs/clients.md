# Connect a client

If this client will receive routine events, finish the MCP connection first,
then follow [Run the built-in AI task routine](routines.md).

HTTP clients use the same MCP URL and bearer. The serving host keeps the Things
Cloud email and password. Run `print-config` there in a private terminal, then
transfer only its output to the client. Same-host Codex can instead use the
self-distributed stdio plugin below.

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
  or skill changes. The generated skill URL is pinned to the installed release.
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
codex plugin marketplace add matsvarn/things3-orchestrator --ref v0.10.4
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

The connection is ready when the client lists `things_view`, `things_find`,
`things_get`, `things_capture`, `things_update`, `things_complete`,
`things_trash`, and `things_receipt`. If it does not, use
[recovery.md](recovery.md).
