# Operate Things Orchestrator

Run these commands on the serving host.

## Inspect

```console
things-orchestrator service status
things-orchestrator doctor --wait
curl -sS http://127.0.0.1:8787/health
# {"ok":true}
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/mcp
# 401
```

On Linux, read service logs with
`journalctl -u things-orchestrator-http.service -e`. On macOS, inspect the
launchd agent in Console.

For a TLS origin, add it to doctor:

```console
things-orchestrator doctor --wait --url https://mcp.example.com
```

## Update and rollback

Install the new exact tag, refresh the service definition, and prove the running
artifact:

```console
uv tool install --force "git+https://github.com/matsvarn/things3-orchestrator.git@v0.9.1"
things-orchestrator service install
things-orchestrator doctor --wait
```

For rollback, repeat those commands with the previous tag. Doctor fails with
`service: stale - restart` if the running commit differs from the installed
commit.

## Rotate the MCP bearer

In a private terminal:

```console
things-orchestrator login --rotate-token
things-orchestrator service install
things-orchestrator doctor --wait
things-orchestrator print-config --client codex --show-secrets
```

Reconfigure every client. Old configurations receive 401 until they contain
the new bearer. Cursor Cloud Agents require another dashboard paste because the
stored value cannot be viewed.

## Back up

Back up these private paths, or their `XDG_CONFIG_HOME` and `XDG_STATE_HOME`
equivalents:

- `~/.config/things-orchestrator/credentials.json`
- `~/.config/things-orchestrator/preferences.json`
- `~/.local/state/things-orchestrator/launcher`
- `~/.local/state/things-orchestrator/state.json`
- `~/.local/state/things-orchestrator/journal-*.sqlite3`
- `~/.local/state/things-orchestrator/contexts-*.sqlite3`

Credentials contain the plaintext Things Cloud password. Store the backup as a
secret.

## Configure owner preferences

```console
things-orchestrator configure --note-style natural
things-orchestrator configure --timezone Europe/Berlin
things-orchestrator configure --url https://mcp.example.com
```

Preference changes apply to the next request. They do not require a server
restart.

## Uninstall

```console
things-orchestrator service uninstall
uv tool uninstall things-orchestrator
```

Disable Tailscale Serve or remove the Caddy site before deleting private state.
The service command removes only its exact launchd agent or systemd unit. It
does not delete credentials, journals, contexts, or Things data.
