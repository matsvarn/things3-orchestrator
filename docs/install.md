# Install Things Orchestrator

Install the server on the host that will keep running. That host stores the
Things Cloud password and the MCP bearer. Other machines receive only the MCP
URL and bearer.

You need [uv](https://docs.astral.sh/uv/) and Things Cloud enabled in Things 3.
The optional client-side curl acceptance line also needs `jq` on that client.
This project uses an unsupported Things Cloud protocol and impersonates a
Things Mac client. Cultured Code can change the protocol, block access, or
disable an account. Read [Security](../SECURITY.md) before login.

Install an exact Git tag:

```console
uv tool install "git+https://github.com/matsvarn/things3-orchestrator.git@v0.10.4"
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

Run the two commands printed by `print-config` one at a time. Paste the
separately printed bearer at Hermes's private prompt, never into a shell.
Hermes adds the authorization scheme; the command lines never contain the
secret.

## Optional routines worker

Use [Run the built-in AI task routine](routines.md) for the complete setup,
trust contract, timing model, and smoke test.

Routines are off by default. Enable them only on a host that should poll Things
Cloud continuously. The worker runs only inside the generated launchd or
systemd `serve-http` service and only with the explicit `always_on` profile. It
does not run in stdio, a manually started HTTP process, or an unconfigured
installation.

For Hermes, run the guided setup in a private terminal:

```console
things-orchestrator routines setup --profile always_on --receiver hermes
things-orchestrator routines status
```

Before the prompt, setup prints the portable upstream `hermes gateway setup`
and `hermes webhook subscribe` commands. The subscribe command returns the URL
and HMAC secret. Before you enter those values, edit
`~/.hermes/webhook_subscriptions.json`. Add
`"toolsets": ["mcp-things"]` to the `things-ai-task-created` entry. Inspect
that entry and verify the exact value. Dynamic subscription commands cannot set
route toolsets. Without the manual grant, webhook runs use a restricted default
and cannot call Things. This file check does not prove MCP access. The positive
selected-task smoke test proves whether the real route can use `things_get`.

The configured MCP server named `things` creates `mcp-things`. Anyone who can
send a valid HMAC request to this route gains its eight bounded Things tools.
Keep the URL and HMAC secret private. `setup` saves the values, enables the
account-bound profile, and installs or restarts the supervised service. Hermes
is the default, so `--receiver hermes` is optional. Use the positive smoke test
before relying on the route. No owner-run Hermes acceptance is recorded for
v0.10.4.

For Grok Bot, first connect the MCP server. Run this command in a private
terminal:

```console
things-orchestrator print-config --client grok --show-secrets
```

At `grok.com/connectors`, choose **New Connector**, then choose **Custom**.
Provide the HTTPS MCP URL and required authentication from the output. Grok
requires a server that the public internet can reach. The command rejects known
local and private addresses but cannot verify DNS or public reachability.
Verify reachability, then confirm that the connector exposes exactly eight
tools, including `things_get`.
The [official xAI connector guide](https://docs.x.ai/grok/connectors) documents
this path.

Create or edit a Routine. Choose **When a webhook fires**, save the Routine,
and leave it inactive. Then run:

```console
things-orchestrator routines setup --profile always_on --receiver grok
```

The command explains where to copy the generated POST URL and key, then prompts
for both in the private terminal. Do not put either value in argv or chat. The URL
must use HTTPS on `api2.cursor.sh` with an `/automations/webhook/<route>` path.

Copy the complete receiver instruction printed by setup into the Grok Routine.

Check startup without assuming that the worker is immediately live:

```console
things-orchestrator routines status
```

Keep the Grok Routine inactive until the command reports
`trigger_ready=true`. Then turn Active on and complete the positive smoke test.
Official xAI documentation establishes Custom MCP connectors, but not that the
observed webhook-triggered Grok Bot execution always receives that connector.

Use `routines configure`, `routines enable`, and `service install` separately
only for recovery or scripted administration. `configure` remains compatible
with `--url`, but receiver credentials are always entered in a private
terminal.

The polling interval is 60 to 3600 seconds.

The first startup scans historical tag changes to learn every current tag named
exactly `AI`. It does not emit events for historical tasks. A new, open task
must carry that tag directly; project or area inheritance does not qualify.
The worker waits through sparse follow-up changes, then sends only the event
schema, opaque event ID, event type, routine ID, public task ID, and observation
time. It never changes Things. The receiver uses its separately configured MCP
connection to read or change the task.

The default 120-second settlement window and separate 60-second polling
interval normally deliver two to three minutes after the final edit. See the
canonical routines guide for the direct-tag and no-backfill edge cases.

Enabling routines increases Things Cloud read traffic and sends event metadata
to the configured receiver. Local HTTP tests exercise both transport contracts
without a live account. The official Grok Bot guide documents routines,
testing, history, approvals, and retries. It does not document the observed
beta webhook host, route, Bearer header, or acknowledgement body. On
2026-09-04, a safe synthetic request confirmed that exact observed shape. Treat
it as beta compatibility, and complete the owner-run check in
[operations.md](operations.md) before relying on it.

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
