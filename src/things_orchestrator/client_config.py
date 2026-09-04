"""Pure client configuration rendering from one validated MCP endpoint."""

from __future__ import annotations

import ipaddress
import json
import shlex
import socket
from dataclasses import dataclass
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from urllib.parse import urlsplit

from .config import ConfigError, McpBearer, McpUrl


class ClientKind(str, Enum):
    CODEX = "codex"
    GROK = "grok"
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
    show_secrets: bool,
) -> RenderedClientConfig:
    if client is ClientKind.HERMES:
        try:
            release = version("things-orchestrator")
        except PackageNotFoundError:
            release = "unknown"
        add_server = " ".join(
            shlex.quote(part)
            for part in (
                "hermes",
                "mcp",
                "add",
                "things",
                "--url",
                str(endpoint.url),
                "--auth",
                "header",
            )
        )
        install_skill = " ".join(
            shlex.quote(part)
            for part in (
                "hermes",
                "skills",
                "install",
                "https://raw.githubusercontent.com/matsvarn/"
                f"things3-orchestrator/v{release}/plugin/skills/"
                "things-orchestrator/SKILL.md",
                "--yes",
            )
        )
        return RenderedClientConfig(
            client,
            f"{add_server}\n{install_skill}\n",
            "Run both commands one at a time. Hermes prompts for the MCP bearer "
            "privately and tests the MCP connection. The skill URL is pinned to "
            f"v{release}.",
        )

    token = endpoint.bearer.reveal() if show_secrets else "<mcp_token>"
    authorization = f"Bearer {token}"
    if client is ClientKind.GROK:
        if not _is_https_without_known_local_host(endpoint.url):
            raise ConfigError(
                "Grok configuration rejects HTTP and known local or private MCP "
                "endpoints. Verify that the HTTPS endpoint is publicly reachable"
            )
        body = json.dumps(
            {
                "url": str(endpoint.url),
                "headers": {"Authorization": authorization},
            },
            indent=2,
        ) + "\n"
        return RenderedClientConfig(
            client,
            body,
            "At grok.com/connectors, choose New Connector, then Custom. "
            "xAI requires an HTTPS MCP URL that the public internet can reach. "
            "This command rejects known local or private addresses, but it cannot "
            "verify DNS or reachability. Provide the URL and required authentication "
            "from this output. Confirm that Grok discovers exactly eight tools "
            "before you activate a routine.",
        )
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
            raise ConfigError("Caddy configuration needs an HTTPS MCP URL")
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


def _is_https_without_known_local_host(url: McpUrl) -> bool:
    if not url.origin.startswith("https://"):
        return False
    raw_hostname = urlsplit(str(url)).hostname
    if raw_hostname is None:
        return False
    hostname = raw_hostname.casefold().rstrip(".")
    if not hostname:
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            address = ipaddress.IPv4Address(socket.inet_aton(hostname))
        except OSError:
            return not (
                "." not in hostname
                or hostname == "localhost"
                or hostname.endswith(
                    (".localhost", ".local", ".lan", ".home", ".internal")
                )
                or hostname.endswith(".ts.net")
            )
    shared = ipaddress.ip_network("100.64.0.0/10")
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or isinstance(address, ipaddress.IPv4Address) and address in shared
    )
