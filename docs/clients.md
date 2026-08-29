# Connect a client

Login wrote snippets under `~/.config/things-orchestrator/` (mode 0600)
on the machine that ran `login`. Merge **one** transport. Lost the
snippet? `uv run things-orchestrator print-config` reprints the paths.
`print-config --http` reprints HTTP paths without wiping a URL you
already set. `print-config --show-secrets` (or `login --show-secrets`)
prints the bodies, including the MCP bearer.

The HTTP Bearer is the MCP token, not the Cloud password. Do not paste
the Cloud password into chat.

All clients share that one bearer. There is no per-client identity. MCP cannot
approve an operation. Host approval uses a separate owner factor that the MCP
server and agent runtime cannot access.

Claude.ai, ChatGPT web, and ChatGPT mobile cannot use this bearer. This
project does not ship MCP OAuth. When tools are missing:
[recovery.md](recovery.md).

## This Mac

The chat client starts the server. Login already wrote Hermes YAML
(`mcp.hermes.yaml`).

**Hermes.** Configuration is profile-specific. Default profile:
`~/.hermes/config.yaml`. Named profiles:
`~/.hermes/profiles/<name>/`. When a named profile is active, Hermes
uses `$HERMES_HOME` — do not hardcode `~/.hermes`. Merge
`mcp.hermes.yaml` into that profile's `config.yaml`. Append the skills
path; do not replace other servers or `external_dirs`. Start a **new
session** after MCP or skills changes.

**Cursor desktop / Claude Desktop.** Merge the `things` key from
`mcp.stdio.json` into `~/.cursor/mcp.json` or
`claude_desktop_config.json`. Do not overwrite other servers. Cursor
desktop can also load skills from `plugin/skills` if that client
catalogs the folder.

**Claude Code.** From the clone:

```console
# Run on this Mac
claude mcp add things -- "$(pwd)/plugin/bin/things-orchestrator" serve
```

**Codex.** From the clone (plugin registers stdio MCP and skills; do not
also `mcp add`):

```console
# Run on this Mac
codex plugin marketplace add .
codex plugin add things-orchestrator@things-orchestrator
```

Done when the client lists server `things` and all eight tools:
`things_view`, `things_find`, `things_get`, `things_capture`, `things_update`,
`things_complete`, `things_trash`, and `things_receipt`. Ask:
`What should I focus on in Things today?`

## Already hosted

If the server is not up yet, follow [host.md](host.md) first. Do not
run `login` on the laptop.

On the VPS, `print-config --http` prints snippet paths. To see the
bearer in a private terminal on the VPS:

```console
# Run on VPS
uv run things-orchestrator print-config --http --show-secrets
```

Merge on the client as below.

**Hermes.** Merge `mcp.hermes.http.yaml` into the active profile
`config.yaml` on the **gateway host** (default
`~/.hermes/config.yaml`; named profile: `$HERMES_HOME` or
`~/.hermes/profiles/<name>/`). Do not replace that file. Do not use
`mcp.hermes.yaml` (stdio). Set `skills.external_dirs` to a checkout the
*agent runtime* can read: the VPS checkout if the gateway is on the
VPS, or a Mac copy of `plugin/skills` if the agent runs on the Mac.
Start a **new session** after MCP or skills changes. Hermes Desktop on
the Mac is only the remote UI when the gateway is on the VPS.

**Claude Code.**

```console
# Run on the Claude Code host
claude mcp add --transport http things https://YOUR-HOST/mcp --header "Authorization: Bearer <mcp_token>"
```

`<mcp_token>` is the HTTP Bearer from `print-config --show-secrets`,
not the Cloud password. Tools work without the skill. For the same
capture and ask behavior as Hermes, copy
`plugin/skills/things-orchestrator` into the skills folder Claude Code
already uses.

**Cursor desktop.** Merge the `things` key from `mcp.http.json` (`url`
+ `headers.Authorization`). Do not overwrite other servers.

**Cursor Cloud Agents.** The URL must be public HTTPS (the public VPS
path in [host.md](host.md), not Tailscale-only). Paste `mcp.http.json`
at [cursor.com/agents](https://cursor.com/agents), not
`~/.cursor/mcp.json`.

**Codex.** Official Codex does not merge `mcp.http.json`. Edit
`~/.codex/config.toml` (Streamable HTTP). See
[Codex MCP](https://developers.openai.com/codex/mcp/). Put the bearer
in the file if you want it to survive new sessions:

```toml
# Run on this Mac (~/.codex/config.toml)
[mcp_servers.things]
url = "https://YOUR-HOST/mcp"
http_headers = { Authorization = "Bearer <mcp_token>" }
```

`bearer_token_env_var = "THINGS_MCP_TOKEN"` keeps the token out of the
file. That variable must already be set in the environment Codex
starts with (shell profile, launchd, or the desktop app). A one-shot
`export` in another terminal does not apply. Then:

```console
# Run on this Mac
codex mcp add things --url https://YOUR-HOST/mcp --bearer-token-env-var THINGS_MCP_TOKEN
```

Keep the skill (`plugin/skills/things-orchestrator`). Do not also use
the local plugin marketplace path — that starts stdio on one machine.

Do not pass `--rotate-token` unless you will update every client. A
401 is a bearer mismatch: reprint on the VPS with `--show-secrets` and
update the client. [recovery.md](recovery.md).

**Leave stdio.** Remove the old stdio `things` server from the client
config. Keep the installed skill. Point MCP at the hosted HTTP URL plus
the shared bearer. Do not run `login` on the laptop.
