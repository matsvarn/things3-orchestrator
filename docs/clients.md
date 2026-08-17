# Connect a client

Login wrote snippets under `~/.config/things-orchestrator/` (mode 0600)
on the machine that ran `login`. Merge **one** transport. Lost the
snippet? `uv run things-orchestrator print-config`. HTTP reprint:
`print-config --http` (keeps the URL you already set).

The HTTP Bearer is the MCP token, not the Cloud password. Do not paste
the Cloud password into chat.

Claude.ai, ChatGPT web, and ChatGPT mobile cannot use this bearer. This
project does not ship MCP OAuth. When tools are missing:
[recovery.md](recovery.md).

## This Mac

The chat client starts the server. Login already printed Hermes YAML
(`mcp.hermes.yaml`).

**Hermes.** Merge `mcp.hermes.yaml` into `~/.hermes/config.yaml`. Append
the skills path; do not replace other servers or `external_dirs`. Reload
MCP.

**Cursor desktop / Claude Desktop.** Merge the `things` key from
`mcp.stdio.json` into `~/.cursor/mcp.json` or
`claude_desktop_config.json`. Do not overwrite other servers. Cursor
desktop can also load skills from `plugin/skills` if that client
catalogs the folder.

**Claude Code.** From the clone:

```console
claude mcp add things -- "$(pwd)/plugin/bin/things-orchestrator" serve
```

**Codex.** From the clone (plugin registers stdio MCP and skills; do not
also `mcp add`):

```console
codex plugin marketplace add .
codex plugin add things-orchestrator@things-orchestrator
```

Done when the client lists server `things` and tools `things_read`,
`things_commit`, and `things_approve`. Ask:
`What should I focus on in Things today?`

## Already hosted

If the server is not up yet, follow [host.md](host.md) first. Copy the
HTTP snippet off the VPS (`print-config --http`) and merge it on the
laptop. Do not run `login` there.

**Hermes.** Merge `mcp.hermes.http.yaml` into `~/.hermes/config.yaml`.
Change `skills.external_dirs` to a local copy of `plugin/skills`.

**Claude Code.**

```console
claude mcp add --transport http things https://YOUR-HOST/mcp --header "Authorization: Bearer <mcp_token>"
```

**Cursor Cloud Agents.** Paste `mcp.http.json` in the Cloud Agents MCP
settings (`https://cursor.com/agents`), not `~/.cursor/mcp.json`. The URL
must be public HTTPS.

**Codex / Cursor desktop (HTTP).** Merge `mcp.http.json` (`url` +
`headers.Authorization`).
