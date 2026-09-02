"""Pure client configuration rendering from one validated MCP endpoint."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .config import ConfigError, McpBearer, McpUrl


class ClientKind(str, Enum):
    CODEX = "codex"
    HERMES = "hermes"
    CLAUDE_CODE = "claude-code"
    CURSOR = "cursor"
    CURSOR_CLOUD = "cursor-cloud"
    CADDY = "caddy"


@dataclass(frozen=True, repr=False)
class Endpoint:
    url: McpUrl
    bearer: McpBearer


@dataclass(frozen=True)
class RenderedClientConfig:
    client: ClientKind
    body: str
    guidance: str
    secondary_body: str | None = None


def render_client_config(
    client: ClientKind,
    endpoint: Endpoint,
    *,
    skill_dir: Path,
    show_secrets: bool,
) -> RenderedClientConfig:
    token = endpoint.bearer.reveal() if show_secrets else "<mcp_token>"
    authorization = f"Bearer {token}"
    if client is ClientKind.CODEX:
        body = (
            "[mcp_servers.things]\n"
            f"url = {json.dumps(str(endpoint.url))}\n"
            "http_headers = { Authorization = "
            f"{json.dumps(authorization)} }}\n"
        )
        return RenderedClientConfig(
            client,
            body,
            "Merge this block into ~/.codex/config.toml.",
        )
    if client is ClientKind.HERMES:
        body = (
            "mcp_servers:\n"
            "  things:\n"
            f"    url: {json.dumps(str(endpoint.url))}\n"
            "    headers:\n"
            f"      Authorization: {json.dumps(authorization)}\n"
            "    tools:\n"
            "      resources: false\n"
            "      prompts: false\n"
            "skills:\n"
            "  external_dirs:\n"
            f"    - {json.dumps(str(skill_dir))}\n"
        )
        return RenderedClientConfig(
            client,
            body,
            "Merge this YAML into the active Hermes profile.",
        )
    if client is ClientKind.CLAUDE_CODE:
        command = " ".join(
            shlex.quote(part)
            for part in (
                "claude",
                "mcp",
                "add",
                "--transport",
                "http",
                "things",
                str(endpoint.url),
                "--header",
                f"Authorization: {authorization}",
            )
        )
        secondary = json.dumps(
            {
                "type": "http",
                "url": str(endpoint.url),
                "headers": {"Authorization": authorization},
            },
            indent=2,
        ) + "\n"
        return RenderedClientConfig(
            client,
            command + "\n",
            "Run the command. For the alternative JSON block, run "
            "claude mcp add-json things '<JSON>', replacing <JSON> with that block.",
            secondary,
        )
    if client in {ClientKind.CURSOR, ClientKind.CURSOR_CLOUD}:
        body = json.dumps(
            {
                "mcpServers": {
                    "things": {
                        "url": str(endpoint.url),
                        "headers": {"Authorization": authorization},
                    }
                }
            },
            indent=2,
        ) + "\n"
        if client is ClientKind.CURSOR_CLOUD:
            guidance = (
                "Paste this in the Cursor Cloud Agents dashboard, not .cursor/mcp.json. "
                "Paste the literal token because environment interpolation is unavailable. "
                "You cannot view the stored value after save. Token rotation requires a new paste."
            )
        else:
            guidance = "Merge the things entry into ~/.cursor/mcp.json."
        return RenderedClientConfig(client, body, guidance)
    if client is ClientKind.CADDY:
        if endpoint.url.origin.startswith("https://") is False:
            raise ConfigError("Caddy configuration needs a public HTTPS MCP URL")
        hostname = endpoint.url.origin.removeprefix("https://")
        body = (
            f"{hostname} {{\n"
            "\treverse_proxy 127.0.0.1:8787 {\n"
            "\t\tflush_interval -1\n"
            "\t}\n"
            "}\n"
        )
        return RenderedClientConfig(
            client,
            body,
            "Install this as /etc/caddy/Caddyfile, then reload Caddy through systemd.",
        )
    raise AssertionError(f"Unhandled client: {client}")
