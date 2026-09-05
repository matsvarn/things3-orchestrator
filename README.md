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
uv tool install "git+https://github.com/matsvarn/things3-orchestrator.git@v0.11.0"
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

Read with `things_view`, `things_find`, or `things_get`. Every mutation needs a
fresh opaque UUID or ULID `request_id`. Reuse it only to retry the exact same
transport request.

`things_capture` creates Tasks and Projects. A new Project may contain nested
new Tasks. Add `repeat` to create a fixed or after-completion rule for either
kind. Rules support day, week, month, and year intervals, selected dates, an
optional end date, and a paused state.

`things_update` changes only explicit fields. It can change the title, notes,
start, deadline, reminder, home, direct tags, exact checklist rows, or an RT1
repeat rule. `into_id` moves the same Task to a Project or Area, or the same
Project to an Area. `start: "anytime"` preserves a Project/Area home and moves
an Inbox Task to top-level Anytime; `start: null` only clears its schedule.
Tag deltas preserve unmentioned direct tags and never mutate inherited tags.
Checklist patches preserve unmentioned rows and order and append new rows. Use
`{repeat: {paused: true}}`, `{repeat: {paused: false}}`,
`{repeat: {create_next: true}}`, or `{repeat: {remove: true}}` for repeat
lifecycle actions. Stopping keeps generated copies, materializes the hidden
template as a fresh ordinary item on its next date, then removes its old graph.
For a Project, the ordinary replacement includes its headings, Tasks, and
checklist rows with fresh IDs. Stop applies through the same authenticated,
bounded mutation path as other updates.
Create Next and Stop require Things' native next date; if it is absent, the
server returns `read_fresh` and writes nothing. Create Next also returns
`read_fresh` when that native date already has a generated copy.
Do not send `repeat: null`.

Use `things_complete` and `things_trash` for item lifecycle changes. Completing
a Project also completes its open action descendants in the same frozen
operation, excluding structural headings and hidden repeat templates.
RT2 repeat facts are read-only. The server rejects RT2 schedule, repeat, and
lifecycle writes instead of guessing at an unproven Cloud payload.

Four short examples are in [Workflow recipes](docs/workflows.md).

The server force-refreshes Things Cloud on the first mutation request and
freezes a private manifest. It never rebases that operation onto newer state.
An uncertain outcome returns `pending`. Retry the exact same request ID and
arguments to force read-back reconciliation; the retry never reposts the frozen
writes. Once dispatch starts, seeing only the frozen before-state is not proof
that the write cannot still land. A provider response that proves rejection
(currently HTTP 409) can settle `not_applied`; timeouts, unreachable responses,
and server errors remain fenced until positive read-back evidence appears. A
fully classified mixed outcome returns terminal `partial` with an exact receipt.
Corrective work always uses a fresh request ID.

`things_find` accepts owner text with an optional exact container, or a
`within`-only Project/Area membership read. When a page returns
`next_action: "continue_read"`, continue with only its cursor.

Recoverable Trash and repeat Stop apply directly for an authenticated MCP or
stdio client. The shared bearer is write authority; keep it private. There is
no per-client identity or separate owner flow in the normal v2 path.

Rows left in legacy `awaiting_owner` state by an older build are retired as
`stale` without Cloud I/O. Never replay their stored batch. Read current Things
state and send a fresh request if the change is still wanted.

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
[authenticated-write ADR](docs/adr/0007-authenticated-bounded-v2-writes.md).

## Protocol acknowledgement

The Cloud adapter uses protocol research from the MIT-licensed
[`evanpurkhiser/things3-cloud`](https://github.com/evanpurkhiser/things3-cloud)
project. See the pinned [protocol notes](docs/research/things3-cloud.md).
