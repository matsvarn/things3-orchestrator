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

Read [Trust](docs/trust.md), [Security](SECURITY.md), and the
[product comparison](docs/comparison.md) before deployment. The
[capability proof](docs/capability-proof.md) records the tested Cloud boundary.

## Install on this Mac

You need [uv](https://docs.astral.sh/uv/) and a Git clone. The package is not on PyPI.
Use the clone because it contains the server, plugin, and CLI-only commands.

```console
scripts/setup
```

Merge `mcp.hermes.yaml` into `~/.hermes/config.yaml`. Do not replace that
file. See [client setup](docs/clients.md) for other clients.

## Install on a host

Run login and the server on the host. A private Tailscale VPS is the personal
default. The Mac can be off while that host runs. Use public HTTPS only for clients that cannot reach the tailnet. See
[host setup](docs/host.md).

## Use the v2 tools

Read with `things_view`, `things_find`, or `things_get`. Every mutation needs a
fresh opaque UUID or ULID `request_id`. Reuse it only to retry the exact same
transport request.

`things_capture` creates Tasks and Projects. A new Project may contain nested
new Tasks. Add `repeat` to create a fixed or after-completion rule for either
kind. Rules support day, week, month, and year intervals, selected dates, an
optional end date, and a paused state.

`things_update` changes only explicit item-local fields. It can change the
title, notes, start, deadline, reminder, or an RT1 repeat rule. Use
`{repeat: {paused: true}}`, `{repeat: {paused: false}}`,
`{repeat: {create_next: true}}`, or `{repeat: {remove: true}}` for repeat
lifecycle actions. Do not send `repeat: null`.

Use `things_complete` and `things_trash` for item lifecycle changes. Completing
a Project also completes its open descendants in the same frozen operation.
RT2 repeat facts are read-only. The server rejects RT2 schedule, repeat, and
lifecycle writes instead of guessing at an unproven Cloud payload.

The server force-refreshes Things Cloud on the first mutation request and
freezes a private manifest. It never rebases that operation onto newer state.
Pending and partial outcomes block every write path until CLI-only read-back
reconciliation settles them or the owner resolves the partial.

Recoverable Trash requires CLI approval. MCP has no approval tool. Enroll the
owner factor and use the CLI-only commands from a private local or SSH
terminal:

```console
uv run things-orchestrator owner-factor
uv run things-orchestrator operation-show op_EXAMPLE
uv run things-orchestrator operation-reconcile op_EXAMPLE
uv run things-orchestrator operation-settle-not-applied op_EXAMPLE
uv run things-orchestrator operation-approve op_EXAMPLE
uv run things-orchestrator operation-decline op_EXAMPLE
uv run things-orchestrator operation-accept-partial op_EXAMPLE accepted_as_is
```

Restart the server after enrolling or rotating the owner factor so it pins the
new public verification key.

The approval passphrase is read from `/dev/tty`, not from arguments,
environment variables, or ordinary stdin pipes. This is a CLI routing control,
not proof that a human is present. The MCP server does not load the encrypted
owner signing key. The server loads only its pinned public key. Code running as
the same OS user can still replace these local files or control the process;
use a separate OS identity or host for a stronger boundary.

Advanced scope editing, mutation coaching, and permanent deletion are not
available in this release.

## Develop

```console
uv sync --group dev
uv run pytest -q
uv run mypy --strict src
uv run ruff check .
```

See [maintainer notes](docs/maintainer.md) and the
[v2 ADR](docs/adr/0006-common-caller-owner-safe-v2.md).

## Protocol acknowledgement

The Cloud adapter uses protocol research from the MIT-licensed
[`evanpurkhiser/things3-cloud`](https://github.com/evanpurkhiser/things3-cloud)
project. See the pinned [protocol notes](docs/research/things3-cloud.md).
