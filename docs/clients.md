# Connect a client

Every client uses the same MCP URL and bearer. The serving host keeps the
Things Cloud email and password. Run `print-config` there in a private terminal,
then transfer only its output to the client.

| Client | Selector | Put the output here |
|---|---|---|
| Codex | `codex` | Merge the TOML block into `~/.codex/config.toml`. |
| Hermes | `hermes` | Merge the YAML into the active Hermes profile. The generated skill path belongs on the agent-runtime host. |
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

Do not paste the Things Cloud password into any client. Possession of the MCP
bearer authorizes every bounded v2 mutation, including recoverable Trash and
repeat Stop. There is no per-client identity.

## Client-specific facts

- Hermes configuration is profile-specific. Merge the generated block; do not
  replace the profile or its other MCP servers. Start a new session after MCP
  or skill changes.
- Cursor Cloud Agents accept MCP configuration only through the dashboard.
  Paste the literal token; environment interpolation is unavailable. The saved
  value cannot be viewed, so bearer rotation requires another paste.
- Codex uses the generated `http_headers` TOML form. Do not add a second
  `bearer_token_env_var` configuration for the same server.
- The Codex plugin remains a stdio packaging detail. Normal installed-tool
  onboarding uses the HTTP configuration above.

The connection is ready when the client lists `things_view`, `things_find`,
`things_get`, `things_capture`, `things_update`, `things_complete`,
`things_trash`, and `things_receipt`. If it does not, use
[recovery.md](recovery.md).
