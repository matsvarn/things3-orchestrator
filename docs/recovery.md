# Fix

- **Tools missing on this Mac.** That is not an empty list. From the
  clone, in a private terminal: `uv run things-orchestrator login`.
  Then merge the snippet again in [clients.md](clients.md). If serve
  cannot find this clone, run `login` from it.
- **Tools missing against a VPS.** Do not run `login` on the laptop.
  On the host: `uv run things-orchestrator print-config --http`. Merge
  that snippet. Numbered steps: [host.md](host.md).
- **Cloud credentials were rejected.** Run `login` again on the
  serving host. Never paste the password into chat.
- **Login says no history key.** Turn on Things Cloud in Things 3,
  then `login` again.
- **Lost the MCP snippet.** `uv run things-orchestrator print-config`.
  `print-config --http` reprints HTTP without wiping a URL you already
  set. `login` keeps `mcp_token` unless you pass `--rotate-token`.
- **MCP HTTP 401.** Bearer mismatch: `print-config --http` on the
  serving host, update the client header, restart `serve-http`. Only
  `--rotate-token` replaces `mcp_token`. Then update every client.
- **Remote client cannot connect.** `serve-http` is still running after
  SSH logout, TLS reaches `/mcp`
  ([deploy/Caddyfile](../deploy/Caddyfile)), `/health` returns
  `{"ok":true}`, and the bearer matches. Do not expose port 8787
  without TLS. [host.md](host.md).
- **Claude.ai / ChatGPT.** This server has no MCP OAuth. Use Hermes,
  Cursor, Codex, or Claude Code.
- **It asked you to confirm.** Answer yes or no in words. Nothing
  writes until you accept.
- **It stopped and told you why.** Leave the work unchanged until you
  give a new decision.
- **It said it cannot do that safely.** Leave the item unchanged. This
  is not a prompt to work around.
