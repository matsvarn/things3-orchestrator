# Host on a VPS

Hermes (or Claude Code, Cursor, Codex) stays on your laptop. The server
and the Things Cloud password stay on a Linux host that is always on.
Your Mac can be off.

Do not run `scripts/setup` or `login` on the laptop. That would store
the Cloud password on the machine you want to close.

You need two copies of this repo: a full checkout on the VPS, and at
least `plugin/skills` on the laptop. Replace `mcp.example.com` with
your hostname everywhere. Client paste targets: [clients.md](clients.md).

## On the VPS

1. Point an A or AAAA record at the VPS. Open ports 80 and 443. Do not
   publish port 8787.
2. Install [uv](https://docs.astral.sh/uv/getting-started/installation/)
   and Python 3.12, 3.13, or 3.14.
3. Clone this repo and log in as the user who will run the service.
   Things Cloud must already be on in Things 3.

   ```console
   uv python install 3.12
   uv sync --locked
   uv run things-orchestrator login --url https://mcp.example.com
   ```

   Override the timezone with `--timezone Europe/Berlin` if the host
   clock is not your own. The password stays in
   `~/.config/things-orchestrator/credentials.json` on this machine.
   Never paste it into chat.
4. Fill [deploy/serve-http.service](../deploy/serve-http.service):
   `User` is the login user, `WorkingDirectory` is this clone, and
   `ExecStart` uses that user's `uv` (`command -v uv`, often
   `~/.local/bin/uv`, not `/usr/bin/uv`). Then:

   ```console
   sudo cp deploy/serve-http.service /etc/systemd/system/things-orchestrator-http.service
   sudo systemctl enable --now things-orchestrator-http
   ```

   Or leave `uv run things-orchestrator serve-http` running in `tmux`.
5. Install [Caddy](https://caddyserver.com/docs/install). In
   [deploy/Caddyfile](../deploy/Caddyfile) replace `mcp.example.com`
   with your hostname, then:

   ```console
   caddy run --config deploy/Caddyfile
   ```

   TLS terminates in front of `127.0.0.1:8787`.
6. From any machine, expect `{"ok":true}`:

   ```console
   curl https://mcp.example.com/health
   ```

   On the VPS, copy the printed Hermes YAML and bearer (they live here,
   not on the laptop):

   ```console
   uv run things-orchestrator print-config --http
   ```

   If you omitted `--url` at login, pass
   `--url https://mcp.example.com` here once. Later reprints keep that
   URL.

## On the laptop

7. Clone this repo (or copy `plugin/skills`). Do not run `login`.
8. Merge the printed `mcp.hermes.http.yaml` into
   `~/.hermes/config.yaml`. Do not replace that file. Do not use
   `mcp.hermes.yaml` (that file starts a local server). The printed
   `skills.external_dirs` path is the VPS checkout — change it:

   ```yaml
   mcp_servers:
     things:
       url: "https://mcp.example.com/mcp"
       headers:
         Authorization: "Bearer <mcp_token>"
       tools:
         resources: false
         prompts: false

   skills:
     external_dirs:
       - "/Users/you/things3-orchestrator/plugin/skills"
   ```

   Keep any other servers and `external_dirs` already in that file.
9. Reload Hermes. It should list server `things` and tools
    `things_read`, `things_commit`, and `things_approve`. Ask:
    `What should I focus on in Things today?`

## Add another client later

Do not run `login` on the laptop. On the VPS,
`uv run things-orchestrator print-config --http` reprints the same URL
and bearer.

**Claude Code**

```console
claude mcp add --transport http things https://mcp.example.com/mcp --header "Authorization: Bearer <mcp_token>"
```

`<mcp_token>` is the HTTP Bearer from `print-config`, not the Cloud
password. Tools work without the skill. For the same capture and ask
behavior as Hermes, copy `plugin/skills/things-orchestrator` into the
skills folder Claude Code already uses.

**Cursor desktop / Codex.** Merge `mcp.http.json` from the VPS
(`url` + `headers.Authorization`). Codex's plugin marketplace path is
stdio on one machine; do not use it here.

**Cursor Cloud Agents.** Paste that JSON at
[cursor.com/agents](https://cursor.com/agents), not
`~/.cursor/mcp.json`. The URL must be public HTTPS.

Do not pass `--rotate-token` unless you will update every client. A
401 is a bearer mismatch: reprint on the VPS and paste again.
[recovery.md](recovery.md).
