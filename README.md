# Things Orchestrator

**Use Things 3 from chat, on a host you control.**

Unofficial self-hosted MCP for one Things Cloud account. Three tools:
`things_read`, `things_commit`, and `things_approve`. Things stays the
system of record. Hermes is the primary client. Cursor, Codex, and
Claude Code work too.

> **Unofficial.** Impersonates a Things Mac client. Cultured Code can
> change the protocol, block access, or disable an account. `login`
> stores the Cloud password on that host; the server sends it to Things
> Cloud. Task text can reach your chat client and its model provider.
> Never paste the password into chat.
> [Trust](docs/trust.md) · [Security](SECURITY.md) ·
> [Not the local Mac MCP, not a hosted login](docs/comparison.md)

MIT licensed. See [LICENSE](LICENSE).

## Pick a path

You need [uv](https://docs.astral.sh/uv/) and a **git clone**. This
project is not on PyPI. Use the clone because it contains the Codex
plugin, model skill, and server wrapper.

### This Mac

The chat client starts the server. Your Mac must stay on.

```console
scripts/setup
```

Merge `mcp.hermes.yaml` into `~/.hermes/config.yaml`. Do not replace
that file. Then ask: `What should I focus on in Things today?`

Other local clients: [docs/clients.md](docs/clients.md).

### A VPS

Your Mac can be off. Do not run `scripts/setup` on the laptop. Login
and the server live on the VPS. The personal default is a private
Tailscale VPS ([docs/host.md](docs/host.md)). Public HTTPS is for
off-tailnet clients such as Cursor Cloud Agents.

## Talk

[docs/owner.md](docs/owner.md) — what to say, what it will ask, what
needs your yes.

## Fix

[docs/recovery.md](docs/recovery.md) — tools missing, HTTP 401, remote
host down.

## What it can do

Create and update Tasks, Projects, Areas, tags, native checklists,
Markdown notes, dates, reminders, and list order. Complete or cancel
work.

Repeats (create, change, or stop). Headings. Nested tags. Trash,
restore, and permanent delete. Rich notes stay unless you approve a
full Markdown replace.

Weekly Review returns a compact exception index for Get Clear, Get Current,
Get Creative, and optional weekly planning. Named categories open only when a
decision needs their full list.

It asks instead of guessing a date kind, a reminder time, a Project
outcome, or a permanent-delete target.

What shipped in each tag: [CHANGELOG.md](CHANGELOG.md). Proof matrix:
[docs/capability-proof.md](docs/capability-proof.md).

## Develop

```console
uv sync --group dev
uv run pytest
uv run mypy
uv run ruff check .
```

Changing the tools: [docs/maintainer.md](docs/maintainer.md).

## Protocol acknowledgement

The Cloud adapter uses protocol research from the MIT-licensed
[`evanpurkhiser/things3-cloud`](https://github.com/evanpurkhiser/things3-cloud)
project. See the pinned [protocol notes](docs/research/things3-cloud.md).
