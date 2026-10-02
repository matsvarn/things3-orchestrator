# Things Orchestrator

Use Things 3 from chat on a host you control.

Things Orchestrator is an unofficial, self-hosted MCP for one Things Cloud
account. Things remains the system of record. The default v2 interface has
eight bounded tools: `things_view`, `things_find`, `things_get`,
`things_capture`, `things_update`, `things_complete`, `things_trash`, and
`things_receipt`.

> Things Orchestrator impersonates a Things Mac client. Cultured Code can
> change the protocol, block access, or disable an account. `login` stores the
> Cloud password on the serving host. Never paste that password into chat.

Read [Trust](docs/trust.md) and [Security](SECURITY.md) before deployment.

## Install

Install an exact Git tag, log in on the serving host, and start the supervised
HTTP service. A single Mac uses loopback. A remote host adds Tailscale Serve or
Caddy for TLS.

```console
uv tool install "git+https://github.com/matsvarn/things3-orchestrator.git@v0.12.0"
things-orchestrator login
things-orchestrator service install
things-orchestrator doctor --wait
things-orchestrator print-config --client codex --show-secrets
```

See [Install](docs/install.md) for the Mac, private VPS, tailnet, and public
HTTPS paths. Then use the compact [client table](docs/clients.md). The serving
host keeps the Things Cloud password; clients receive only the MCP URL and
bearer.

To run the optional built-in `AI` task routine, follow
[Run the built-in AI task routine](docs/routines.md). Routines are disabled by
default and do not add MCP tools. Choose from the
[named routines and copyable prompts](docs/routine-examples.md), including
task enrichment and scheduled planning reports.

For same-host Codex, the repository also contains a self-distributed plugin.
It is not an official marketplace listing. For Hermes, `print-config` emits
native setup commands and Hermes prompts for the bearer. See
[Connect a client](docs/clients.md) for both paths.

## Use the v2 tools

Mutation rules live in the
[Things skill](plugin/skills/things-orchestrator/SKILL.md) for agents and in
[PRODUCT.md](PRODUCT.md) for the release contract. Four short examples are in
[Workflow recipes](docs/workflows.md).

## Develop

```console
uv sync --group dev
uv run pytest -q
uv run mypy --strict src
uv run ruff check .
```

See [maintainer notes](docs/maintainer.md) and the
[authenticated-write ADR](docs/adr/0007-authenticated-bounded-v2-writes.md).

## Protocol acknowledgement

The Cloud adapter uses protocol research from the MIT-licensed
[`evanpurkhiser/things3-cloud`](https://github.com/evanpurkhiser/things3-cloud)
project. See the pinned [protocol notes](docs/research/things3-cloud.md).
