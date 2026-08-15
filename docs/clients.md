# Chat clients

Login wrote snippets under `~/.config/things-orchestrator/` (mode 0600).
Merge **one** transport. Lost the snippet?
`uv run things-orchestrator print-config`. HTTP reprint:
`print-config --http` (keeps the URL you already set).
Do not paste the Cloud password into chat. The HTTP Bearer is the MCP token.
Tool results can reach the client and its model provider. Read
[trust.md](trust.md) before you connect an account with sensitive task data.

Hermes is the default. Merge the `things` server into `~/.hermes/config.yaml`
under `mcp_servers` and append `plugin/skills` to `skills.external_dirs`. Do
not replace the whole file.

The three tool schemas contain request and recovery rules. The one model skill
adds Things-specific task quality guidance. It helps the model choose Tasks,
Projects, checklists, Markdown notes, dates, tags, and Areas. The server owns
write safety. Routine changes apply directly. Area changes, broad batches, and
closing a Project with open actions need approval. The model asks one natural
question and keeps approval IDs private.

Claude.ai, ChatGPT web, and ChatGPT mobile cannot use this bearer. This
project does not ship MCP OAuth. When tools are missing: [recovery.md](recovery.md).

## Same machine

The chat client starts the server. Login already printed Hermes YAML
(`mcp.hermes.yaml`).

**Hermes.** Merge `mcp.hermes.yaml` into `~/.hermes/config.yaml`. Append the
skills path; do not replace other servers or `external_dirs`. Reload MCP.

**Cursor desktop / Claude Desktop.** Merge the `things` key from
`mcp.stdio.json` into `~/.cursor/mcp.json` or `claude_desktop_config.json`.
Do not overwrite other servers. Cursor desktop can also load skills from
`plugin/skills` if that client catalogs the folder.

**Claude Code.** From the clone:

```console
claude mcp add things -- "$(pwd)/plugin/bin/things-orchestrator" serve
```

**Codex.** From the clone (plugin registers stdio MCP and skills; do not also
`mcp add`):

```console
codex plugin marketplace add .
codex plugin add things-orchestrator@things-orchestrator
```

Done when the client lists server `things` and tools `things_read`,
`things_commit`, and `things_approve`.

## Other machine (VPS)

The Mac can be off. Login and `serve-http` run on the host that stays up.
Leave the process on `127.0.0.1:8787`. Put TLS in front with
[deploy/Caddyfile](../deploy/Caddyfile). Do not expose port 8787. Keep the
process after SSH logout ([deploy/serve-http.service](../deploy/serve-http.service)
or `tmux`).

```console
uv run things-orchestrator serve-http
uv run things-orchestrator print-config --http --url https://YOUR-HOST
```

**Hermes.** Merge `mcp.hermes.http.yaml` into `~/.hermes/config.yaml` on the
chat-client machine. Point `skills.external_dirs` at a copy of `plugin/skills`
on that machine.

**Cursor Cloud Agents.** Paste `mcp.http.json` in the Cloud Agents MCP
settings (`https://cursor.com/agents`), not `~/.cursor/mcp.json`. The URL
must be public HTTPS.

**Claude Code.**

```console
claude mcp add --transport http things https://YOUR-HOST/mcp --header "Authorization: Bearer <mcp_token>"
```

**Codex / Cursor desktop (HTTP).** Merge `mcp.http.json` (`url` +
`headers.Authorization`).

`/health` returning `{"ok":true}` means the process is up. MCP 401:
[recovery.md](recovery.md).
