# Security

Treat the host that ran `login` as the trust boundary. See
[docs/trust.md](docs/trust.md) for the data path.

This project impersonates a Things Mac client against an unofficial API.
That may conflict with Cultured Code's terms. Cultured Code can change
the protocol, block access, or disable an account. Use at your own risk.

## Do

- Run `things-orchestrator login` in a private terminal. Confirm
  the password prompt is this project's CLI, not chat.
- Keep `~/.config/things-orchestrator/credentials.json` mode 0600. Do
  not commit it, paste it, or put it in issues, screenshots, or chat.
- Treat output from `print-config --show-secrets` as secret. It contains the
  MCP bearer. Do not save it in a repository, screenshot it, or paste it into
  an issue.
- Put TLS in front of `serve-http` before any non-loopback client hop. Leave the
  process on `127.0.0.1`; a client on the same host may use loopback HTTP. Do
  not publish port 8787. Do not run `/mcp` without a bearer.
  Unauthenticated `/health` is liveness only (`{"ok":true}`). A request with
  the correct bearer receives deployment diagnostics; a wrong bearer gets 401.
- Rotate the MCP bearer with `login --rotate-token` if a client configuration
  leaked. That token is not the Cloud password; still treat it as a
  secret.

## Do not

- Paste the Cloud password into chat, tickets, or a hosted login we do
  not run on your own machine.
- File a GitHub issue that contains `credentials.json`, a generated client
  configuration, or a Bearer header.
- Open `/mcp` with no auth so ChatGPT will connect.

## Report a vulnerability

Open a private GitHub security advisory on this repository. Do not
attach Cloud credentials. If the unofficial Cloud protocol is involved,
say so without a live password or history-key.

Security fixes target the latest tagged release. This project does not
promise security fixes for older releases before version 1.0.
