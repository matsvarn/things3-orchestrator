# Trust boundary

Self-hosted software for one owner. The project does not operate a
shared service or receive your Things Cloud credentials.

The server uses an unofficial, reverse-engineered Things Cloud protocol.
It impersonates a Things Mac client. Cultured Code can change or block
this protocol, or disable an account.

This does not mean that all data stays on one machine.

## Data path

1. `login` accepts the Cloud password only in a private terminal.
2. The CLI stores it as plaintext JSON with mode 0600 on the serving host.
   The same file stores the owner's non-secret IANA timezone.
3. The server sends it to Things Cloud with each Cloud request.
4. MCP tool results return task data to the configured chat client.
5. That client can send the result to its model provider.

Your trust boundary includes the serving host, Things Cloud, the chat
client, and its model provider. Check each provider's data controls
before you connect an account with sensitive task names or notes.

Do not describe this system as fully private, zero-knowledge, or as
keeping all data on one device.

## MCP access

The HTTP server binds only to `127.0.0.1`. Put TLS in front of it. Each
`/mcp` request needs the MCP bearer. `/health` contains no account data.

All authorized clients share one MCP bearer. There is no per-client
identity. Owner approval (`things_approve`) is a model workflow rule,
not a second authentication factor.

The bearer is not the Cloud password. Rotate it with
`login --rotate-token` if an HTTP configuration leaks.

This project does not provide MCP OAuth. Claude.ai, ChatGPT web, and
ChatGPT mobile cannot use this bearer-token setup.
