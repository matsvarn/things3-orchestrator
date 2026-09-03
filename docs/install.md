# Install Things Orchestrator

Install the server on the host that will keep running. That host stores the
Things Cloud password and the MCP bearer. Other machines receive only the MCP
URL and bearer.

You need [uv](https://docs.astral.sh/uv/) and Things Cloud enabled in Things 3.
The optional client-side curl acceptance line also needs `jq` on that client.
Install an exact Git tag:

```console
uv tool install "git+https://github.com/matsvarn/things3-orchestrator.git@v0.9.0"
```

Run the next commands in a private terminal. `login` verifies the Things Cloud
credentials, stores the owner timezone and endpoint, and creates one MCP
bearer. Do not paste the Cloud password into chat.

## One Mac

The server and client share one Mac. Loopback HTTP needs no TLS.

```console
things-orchestrator login
things-orchestrator service install
things-orchestrator doctor --wait
things-orchestrator print-config --client codex --show-secrets
```

Replace `codex` with another selector from [clients.md](clients.md).

## Private Linux host

Use this path when the agent runtime also runs on the server. The service stays
on loopback.

```console
things-orchestrator login --timezone Europe/Berlin
things-orchestrator service install
things-orchestrator doctor --wait
things-orchestrator print-config --client hermes --show-secrets
```

`service install` writes a systemd system unit for the current user and starts
it. It may ask for `sudo`. The unit executes the resolved console script,
contains no credentials, and restarts after failure.

## Private tailnet client

Add Tailscale Serve when a client on another tailnet device needs the server.
Use the machine's MagicDNS HTTPS origin during login.

```console
things-orchestrator login --url https://MACHINE.TAILNET.ts.net --timezone Europe/Berlin
things-orchestrator service install
sudo tailscale serve --bg 8787
things-orchestrator doctor --wait --url https://MACHINE.TAILNET.ts.net
things-orchestrator print-config --client claude-code --show-secrets
```

The first `tailscale serve` may show a one-time prompt to enable HTTPS for the
tailnet. Approve it only for the intended tailnet. Do not use Funnel: this path
is private to tailnet members.

## Public HTTPS client

Use public HTTPS only for a client that cannot join the tailnet, such as Cursor
Cloud Agents. Point DNS at the host and allow inbound TCP 80 and 443. Keep port
8787 private.

```console
things-orchestrator login --url https://mcp.example.com --timezone Europe/Berlin
things-orchestrator service install
sudo apt update
sudo apt install caddy
things-orchestrator print-config --client caddy | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo systemctl reload caddy
things-orchestrator doctor --wait --url https://mcp.example.com
things-orchestrator print-config --client cursor-cloud --show-secrets
```

The Caddy package owns the long-running systemd service. Do not run `caddy run`
in an SSH session.

## Acceptance

`doctor` verifies public health privacy, authenticated health, installed version
and commit, schema hash, MCP initialization, and the exact eight-tool list. It
checks loopback and the saved public origin. Use `--url` to add a one-time
endpoint that is not saved. Its last block prints a curl command for a client
machine; set `THINGS_MCP_TOKEN` in that terminal and expect the command to print
`8`.

Then connect one client with [clients.md](clients.md). Operational commands,
updates, rollback, backup, and removal are in [operations.md](operations.md).
