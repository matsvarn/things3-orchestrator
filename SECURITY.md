# Security

Treat the host that ran `login` as the trust boundary. See
[docs/trust.md](docs/trust.md) for the data path.

This project impersonates a Things Mac client against an unofficial API.
That may conflict with Cultured Code's terms. Cultured Code can change
the protocol, block access, or disable an account. Use at your own risk.

## Do

- Run `uv run things-orchestrator login` in a private terminal. Confirm
  the password prompt is this project's CLI, not chat.
- Keep `~/.config/things-orchestrator/credentials.json` mode 0600. Do
  not commit it, paste it, or put it in issues, screenshots, or chat.
- Treat snippet files as secret (`mcp.http.json`,
  `mcp.hermes.http.yaml`, `mcp.hermes.yaml`, `mcp.stdio.json`). They are
  mode 0600. Default `login` / `print-config` print paths only.
  `--show-secrets` prints the MCP Bearer. Do not screenshot that
  terminal into an issue.
- Put TLS in front of `serve-http`. Leave the process on `127.0.0.1`.
  Do not publish port 8787. Do not run `/mcp` without a bearer.
  `/health` is liveness only (`{"ok":true}`).
- Rotate the MCP bearer with `login --rotate-token` if an HTTP snippet
  leaked. That token is not the Cloud password; still treat it as a
  secret.
- In systemd, do not set `THINGS_MCP_TOKEN`. `login` already wrote the
  token and Cloud credentials to `credentials.json`.

## Do not

- Paste the Cloud password into chat, tickets, or a hosted login we do
  not run on your own machine.
- File a GitHub issue that contains `credentials.json`, `mcp.http.json`,
  `mcp.hermes.http.yaml`, or a Bearer header.
- Open `/mcp` with no auth so ChatGPT will connect.

## Report a vulnerability

Open a private GitHub security advisory on this repository. Do not
attach Cloud credentials. If the unofficial Cloud protocol is involved,
say so without a live password or history-key.

Security fixes target the latest tagged release. This project does not
promise security fixes for older releases before version 1.0.
