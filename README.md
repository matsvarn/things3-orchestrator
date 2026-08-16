# Things Orchestrator

**Use Things 3 from chat, on a host you control.**

Things Orchestrator is an unofficial, self-hosted MCP server for one Things
Cloud account. It exposes three intent-level tools: `things_read`,
`things_commit`, and `things_approve`. Things remains the system of record.

Run it beside your chat client, or leave it on an always-on Linux host. Your
Mac can be off when that host runs the server. Hermes is the primary client.
Configuration for Cursor, Codex, and Claude Code is also included.

The stored Cloud password stays on the host where you run `login`. There is no
hosted Things Orchestrator service. The server still sends the password to
Things Cloud with each request. Tool results can reach your chat client and
its model provider. See [the trust boundary](docs/trust.md).

> **Unofficial software.** This is not a Cultured Code API. This project is
> not affiliated with Cultured Code. It impersonates a Things Mac client.
> Cultured Code can change the protocol, block access, or disable an account.

This project is not the local `things-mcp`. That project uses the Mac database
and URL scheme, so the Mac must be on. This project is also not the hosted
service at [thingscloudmcp.com](https://thingscloudmcp.com), which receives
users' Things Cloud credentials. See the dated, source-linked
[boundary comparison](docs/comparison.md).

Login is TTY-only. It stores the Cloud password as plaintext JSON with mode
0600. Never paste that password into chat or a GitHub issue. See
[SECURITY.md](SECURITY.md).

MIT licensed. See [LICENSE](LICENSE).

## First command

You need [uv](https://docs.astral.sh/uv/) and a **git clone**. This project is
not on PyPI. Use the clone because it contains the Codex plugin, model skill,
and server wrapper.

```console
scripts/setup
```

`login` records the owner's IANA timezone. Override automatic detection with
`uv run things-orchestrator login --timezone Europe/Berlin`.

Login writes `mcp.hermes.yaml`. Merge its `things` server into
`~/.hermes/config.yaml`. Do not replace that file. Login also writes JSON for
Cursor, Codex, and Claude. Reprint it with
`uv run things-orchestrator print-config`.

## Next

1. Wire **one** scenario: [docs/clients.md](docs/clients.md)
2. Talk to the model: [docs/owner.md](docs/owner.md)
3. Understand the data path: [docs/trust.md](docs/trust.md)
4. Compare the credential boundaries: [docs/comparison.md](docs/comparison.md)
5. Tools missing or a write stops: [docs/recovery.md](docs/recovery.md)

Changing the tools: [docs/maintainer.md](docs/maintainer.md).

## Current capability

The server can create and update Tasks, Projects, Areas, tags, native
checklists, Markdown notes, dates, reminders, and list order. It can complete
or cancel Tasks and Projects.

It can make a new or existing Task repeat. It keeps an existing Task as the
current generated copy and preserves its metadata in the future template. It
can batch schedule, placement, order, and checklist edits into that conversion.
The current checklist keeps its completion state. Future checklist rows start open. It
can change repeat mode, unit, interval, and weekly pattern. It can change a
template and its current copy in one batch. It can stop a repeat rule and keep
linked copies as ordinary Tasks. Rule changes keep unknown Cloud fields.

It can create, rename, reorder, assign, clear, and remove headings. It can
create nested tags, rename or reparent them, and remove their references before
deletion.

It can move Tasks and Projects to recoverable Trash, restore them, or delete
them permanently. Permanent Project deletion removes descendants first, so it
does not leave detached Tasks. Irreversible changes need confirmation.

It reads structured rich notes without overwriting them. An explicit approved
operation can replace the complete rich note with Markdown.

See the executable [capability proof](docs/capability-proof.md) for each memory,
Cloud, live, approval, and read-back gate.

## Develop

```console
uv sync --group dev
uv run pytest
uv run mypy
uv run ruff check .
```

## Protocol acknowledgement

The Cloud adapter uses protocol research from the MIT-licensed
[`evanpurkhiser/things3-cloud`](https://github.com/evanpurkhiser/things3-cloud)
project. See the pinned [protocol notes](docs/research/things3-cloud.md).
