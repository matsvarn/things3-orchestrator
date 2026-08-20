# Fix

Check the serving host first:

```console
# Run on VPS
sudo systemctl status things-orchestrator-http
journalctl -u things-orchestrator-http -e
curl -sS http://127.0.0.1:8787/health
# → {"ok":true}
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/mcp
# → 401
uv run things-orchestrator doctor
```

```console
# Run on VPS
uv run things-orchestrator doctor --url https://YOUR-HOST
```

```console
# Run on Mac after leaving SSH
curl -sS https://YOUR-HOST/health
# → {"ok":true}
```

Host steps: [host.md](host.md).

- **Tools missing on this Mac.** That is not an empty list. From the
  clone, in a private terminal: `uv run things-orchestrator login`.
  Then merge the snippet again in [clients.md](clients.md). If serve
  cannot find this clone, run `login` from it.
- **Tools missing against a VPS.** Do not run `login` on the laptop.
  On the host: `uv run things-orchestrator print-config --http` (paths).
  To see the bearer in a private terminal:
  `print-config --http --show-secrets`. Merge that snippet.
- **Cloud credentials were rejected.** Run `login` again on the
  serving host. Never paste the password into chat.
- **Login says no history key.** Turn on Things Cloud in Things 3,
  then `login` again.
- **Lost the MCP snippet.** `uv run things-orchestrator print-config`
  reprints paths. `print-config --http` reprints HTTP paths without
  wiping a URL you already set. `--show-secrets` prints the bodies.
  `login` keeps `mcp_token` unless you pass `--rotate-token`.
- **Preferences are unreadable or invalid.** Move
  `~/.config/things-orchestrator/preferences.json` aside. Then run
  `uv run things-orchestrator configure --note-style natural` or use
  `visual`. The server refuses the affected Project before any Things
  write. It does not reset the file silently.
- **MCP HTTP 401.** Bearer mismatch: `print-config --http --show-secrets`
  on the serving host, update the client header, restart `serve-http`.
  Only `--rotate-token` replaces `mcp_token`. Then update every client.
- **Remote client cannot connect.** `serve-http` is still running after
  SSH logout, TLS reaches `/mcp` (Tailscale Serve or
  [deploy/Caddyfile](../deploy/Caddyfile)), `/health` returns
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
