# Recovery guide

- **Tools missing.** That is not an empty list. From the clone, in a private
  terminal: `uv run things-orchestrator login`. Then merge the snippet again
  in [clients.md](clients.md). If serve cannot find this clone, run `login`
  from it.
- **Cloud credentials were rejected.** Run `login` again. Never paste the
  password into chat.
- **Login says no history key.** Turn on Things Cloud in Things 3, then
  `login` again.
- **Lost the MCP snippet.** `uv run things-orchestrator print-config`.
  `print-config --http` reprints HTTP without wiping a URL you already set.
  `login` keeps `mcp_token` unless you pass `--rotate-token`.
- **MCP HTTP 401.** Bearer mismatch: `print-config --http`, update the
  client header, restart `serve-http`. Only `--rotate-token` replaces
  `mcp_token`.
- **Remote client cannot connect.** `serve-http` is still running after SSH
  logout, TLS reaches `/mcp` ([deploy/Caddyfile](../deploy/Caddyfile)),
  `/health` returns `{"ok":true}`, and the bearer matches. Do not expose
  port 8787 without TLS.
- **Claude.ai / ChatGPT.** This server has no MCP OAuth. Use Hermes, Cursor,
  Codex, or Claude Code.
- **A write returns `retry_same`.** Repeat the exact same call. Keep its
  `intent_id` and payload. The server reads Cloud state before it can post
  again.
- **A write returns `read`.** Read fresh facts. Use their revisions in a new
  intent.
- **A write returns `approve`.** Ask one natural confirmation from the visible
  change and its important consequence. Keep `plan_id` private. Send it to
  `things_approve` only after the owner accepts the change.
- **A write returns `stop`.** Report its `instruction`. Keep the work
  unchanged until the owner gives a new decision.
- **The requested write is unsupported.** Keep the item unchanged. Check the
  [current limits](../README.md#current-limits). Do not use raw Cloud fields as
  a bypass.
