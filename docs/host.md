# Host the server

The Things Cloud password and `serve-http` live on the serving host.
The Mac can be off. Clients talk to that host over MCP HTTP.

Do not run `scripts/setup` or `login` on the laptop when hosting. That
stores the Cloud password on the machine you want to close.

Client paste targets: [clients.md](clients.md). Do not paste the Cloud
password into chat.

## Choose a topology

**Local server and local agent.** The chat client starts the server.
The Mac stays on. Run [`scripts/setup`](../scripts/setup), then
[This Mac](clients.md#this-mac).

**Private VPS (recommended personal path).** Things Cloud ← Orchestrator
on the VPS ← private Tailscale HTTPS ← Hermes or Codex on approved
devices. No public DNS. No public ports. `serve-http` stays on
`127.0.0.1:8787`. Use `tailscale serve` (or equivalent) for HTTPS on the
tailnet only. Do not publish port 8787.

**Public VPS.** Caddy plus DNS plus ports 80 and 443. Required for
Cursor Cloud Agents and other off-tailnet clients. Still do not publish
port 8787.

The Hermes *gateway* may run on the VPS. Hermes Desktop on a Mac is
then only the remote interface. You need two checkouts only when the
agent runtime that loads `plugin/skills` is not on the VPS. Gateway plus
skills on the VPS is one checkout.

## Where each step runs

| Item | Location |
| --- | --- |
| Things Cloud password | VPS only |
| Server checkout | VPS |
| MCP bearer | VPS and each authorized client |
| Hermes profile configuration | Hermes gateway host |
| Hermes skill files | Agent runtime host |
| Hermes Desktop | Mac, as remote interface |
| Local Codex configuration | Mac |

## Shared facts

All clients share one MCP bearer. There is no per-client identity. The bearer
cannot approve operations. Enroll and use the separate host-only owner factor
from a private local or SSH terminal.

`login` and `print-config` print snippet *paths* only. Snippet files
are mode 0600. To print bodies, including the MCP bearer, use
`--show-secrets` in a private terminal on the VPS.

Override the timezone with `--timezone Europe/Berlin` if the host clock
is not your own. The password stays in
`~/.config/things-orchestrator/credentials.json` on the VPS.

## Private VPS

Replace `YOUR-MAGICDNS` with the Tailscale HTTPS origin
(`https://<machine>.<tailnet>.ts.net`).

### 1. Install and log in

Things Cloud must already be on in Things 3.

```console
# Run on VPS
# uv: https://docs.astral.sh/uv/getting-started/installation/
uv python install 3.12
uv sync --locked
uv run things-orchestrator login --url https://YOUR-MAGICDNS
```

Python 3.13 or 3.14 is fine. If you omit `--url`, set it later with
`print-config --http --url https://YOUR-MAGICDNS`. Later reprints keep
that URL.

### 2. Keep serve-http running after SSH logout

Fill [deploy/serve-http.service](../deploy/serve-http.service): `User`
is the login user, `WorkingDirectory` is this checkout, and `ExecStart`
uses that user's `uv` (`command -v uv`, often `~/.local/bin/uv`, not
`/usr/bin/uv`). Set `THINGS_ORCHESTRATOR_COMMIT` to
`git rev-parse HEAD` from that checkout so `/health` can tell builds
apart. After a schema-changing deploy, reconnect the MCP client and
start a fresh agent session so it picks up the new tool schema. Compare
`/health` `commit`, `tool_schema_hash`, and `tool_contract_hash` with
the previous values. `tool_schema_hash` covers the eight discovery
schemas. `tool_contract_hash` also covers tool descriptions and the
strict runtime `Result`.

```console
# Run on VPS
sudo cp deploy/serve-http.service /etc/systemd/system/things-orchestrator-http.service
sudo systemctl enable --now things-orchestrator-http
uv run things-orchestrator doctor --wait
```

`systemctl enable --now` can finish before port 8787 accepts
connections. `doctor --wait` retries loopback health for about 15s.

Or leave `uv run things-orchestrator serve-http` running in `tmux`,
then run `doctor --wait`.

### 3. Tailscale Serve

`serve-http` stays on `127.0.0.1:8787`. Publish HTTPS on the tailnet
only. Confirm flags with `tailscale serve --help` and
`tailscale serve status`.

```console
# Run on VPS
# Enable HTTPS certificates for the tailnet first, then:
tailscale serve --bg 8787
tailscale serve status
```

Do not publish port 8787. Stay on the tailnet unless you meant to
follow [Public VPS](#public-vps).

If the Hermes gateway is on this VPS, that Hermes profile may use
`http://127.0.0.1:8787/mcp`. Other devices still use
`https://YOUR-MAGICDNS/mcp`.

### 4. Copy snippet paths to clients

```console
# Run on VPS
uv run things-orchestrator print-config --http
```

That prints paths. To see the bearer in this private terminal:

```console
# Run on VPS
uv run things-orchestrator print-config --http --show-secrets
```

Merge on each client per [clients.md](clients.md). Do not run `login`
on the laptop.

```console
# Run on VPS
uv run things-orchestrator doctor --url https://YOUR-MAGICDNS
```

```console
# Run on Mac after leaving SSH
curl -sS https://YOUR-MAGICDNS/health
# → {"ok":true}
```

## Public VPS

Use this when a client is off the tailnet (Cursor Cloud Agents).

1. Point an A or AAAA record at the VPS. Open ports 80 and 443. Do not
   publish port 8787.
2. Install uv and Python 3.12, 3.13, or 3.14. Clone this repo as the
   user who will run the service. Things Cloud must already be on.

   ```console
   # Run on VPS
   uv python install 3.12
   uv sync --locked
   uv run things-orchestrator login --url https://YOUR-HOST
   ```

3. Install the systemd unit as in [Private VPS](#2-keep-serve-http-running-after-ssh-logout).
   Run `doctor --wait` immediately after `systemctl enable --now`.
4. Install [Caddy](https://caddyserver.com/docs/install). In
   [deploy/Caddyfile](../deploy/Caddyfile) replace `mcp.example.com`
   with `YOUR-HOST`, then:

   ```console
   # Run on VPS
   caddy run --config deploy/Caddyfile
   ```

   TLS terminates in front of `127.0.0.1:8787`.
5. Expect `{"ok":true}`:

   ```console
   # Run on VPS
   curl -sS https://YOUR-HOST/health
   uv run things-orchestrator doctor --url https://YOUR-HOST
   ```

6. Copy snippet paths with `print-config --http`. Bodies:
   `print-config --http --show-secrets`. Merge per
   [clients.md](clients.md). Do not run `login` on the laptop.

## Hermes on a hosted server

Configuration is profile-specific. Default profile:
`~/.hermes/config.yaml`. Named profiles live under
`~/.hermes/profiles/<name>/`. When a named profile is active, Hermes
uses `$HERMES_HOME` — do not hardcode `~/.hermes`.

Merge the generated `mcp.hermes.http.yaml` into that profile's
`config.yaml` on the **gateway host**. Do not replace the file. Keep
other servers and `skills.external_dirs`. Do not merge
`mcp.hermes.yaml`; that file starts a local stdio server.

Set `skills.external_dirs` to a checkout the *agent runtime* can read:
the VPS checkout if the gateway is on the VPS, or a Mac copy of
`plugin/skills` if the agent runs on the Mac.

Start a **new session** after MCP or skills changes. Do not assume a
reload is enough.

Hermes Desktop on the Mac is the remote UI when the gateway is on the
VPS.

Exact merge targets: [clients.md](clients.md).

## Migrate from local stdio to hosted HTTP

1. Remove the old stdio `things` server from the client config.
2. Keep the installed skill (`plugin/skills/things-orchestrator`).
3. Point MCP at the hosted HTTP URL plus the shared bearer.
4. Do not run `login` on the laptop.

Client commands: [clients.md](clients.md).

## Operate

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

**Update.** On the VPS: `git pull`, `uv sync --locked`,
`sudo systemctl restart things-orchestrator-http`, then
`uv run things-orchestrator doctor --wait`.

**Rollback.** Check out the previous git revision on the VPS, then
restart and run `doctor --wait`.

**Backup.** On the VPS, copy (or the `XDG_CONFIG_HOME` /
`XDG_STATE_HOME` equivalents):

- `~/.config/things-orchestrator/credentials.json`
- `~/.config/things-orchestrator/preferences.json` (if configured)
- `~/.local/state/things-orchestrator/state.json`
- `~/.local/state/things-orchestrator/journal-*.sqlite3`
- `~/.local/state/things-orchestrator/contexts-*.sqlite3`

**Rotate the MCP bearer.** On the VPS, in a private terminal:
`uv run things-orchestrator login --rotate-token --show-secrets`.
Then update every client with the new bearer. A 401 is a bearer
mismatch, not a Cloud-password problem. [recovery.md](recovery.md).

**Choose the note style.** Run
`uv run things-orchestrator configure --note-style natural`.
Replace `natural` with `visual` to use visual notes. Updates,
rollbacks, login, token rotation, and snippet generation do not change
this preference.

**After reboot.** On the VPS: `uv run things-orchestrator doctor --wait`.

**Uninstall.**

```console
# Run on VPS
sudo systemctl disable --now things-orchestrator-http
sudo rm /etc/systemd/system/things-orchestrator-http.service
sudo systemctl daemon-reload
```

Turn off Tailscale Serve or Caddy so port 8787 is not exposed.
Confirm Serve with `tailscale serve --help` / `tailscale serve status`.
Optionally delete `~/.config/things-orchestrator/` and
`~/.local/state/things-orchestrator/`.
