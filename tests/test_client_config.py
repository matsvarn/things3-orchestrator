from __future__ import annotations

import json
import shlex
import tomllib

import pytest

from things_orchestrator.client_config import ClientKind, Endpoint, render_client_config
from things_orchestrator.config import ConfigError, McpBearer, normalize_mcp_url


@pytest.fixture
def endpoint() -> Endpoint:
    return Endpoint(
        url=normalize_mcp_url("https://tasks.example.com"),
        bearer=McpBearer("secret-bearer"),
    )


def test_codex_config_is_parseable_toml_and_redacted_by_default(
    endpoint: Endpoint,
) -> None:
    rendered = render_client_config(
        ClientKind.CODEX, endpoint, show_secrets=False
    )
    parsed = tomllib.loads(rendered.body)

    assert parsed["mcp_servers"]["things"] == {
        "url": "https://tasks.example.com/mcp",
        "http_headers": {"Authorization": "Bearer <mcp_token>"},
    }
    assert "secret-bearer" not in rendered.body


def test_hermes_config_uses_native_cli_without_putting_the_bearer_in_history(
    endpoint: Endpoint,
) -> None:
    rendered = render_client_config(
        ClientKind.HERMES, endpoint, show_secrets=False
    )
    commands = rendered.body.splitlines()

    assert shlex.split(commands[0]) == [
        "hermes",
        "mcp",
        "add",
        "things",
        "--url",
        "https://tasks.example.com/mcp",
        "--auth",
        "header",
    ]
    assert shlex.split(commands[1]) == [
        "hermes",
        "skills",
        "install",
        "https://raw.githubusercontent.com/matsvarn/things3-orchestrator/"
        "v0.10.0/plugin/skills/things-orchestrator/SKILL.md",
        "--yes",
    ]
    assert "secret-bearer" not in rendered.body
    assert "prompt" in rendered.guidance.lower()
    assert "pinned" in rendered.guidance.lower()


def test_hermes_renderer_never_puts_the_secret_in_commands(endpoint: Endpoint) -> None:
    rendered = render_client_config(ClientKind.HERMES, endpoint, show_secrets=True)

    assert "secret-bearer" not in rendered.body


def test_grok_config_exposes_public_mcp_url_and_required_bearer(
    endpoint: Endpoint,
) -> None:
    redacted = render_client_config(ClientKind.GROK, endpoint, show_secrets=False)
    revealed = render_client_config(ClientKind.GROK, endpoint, show_secrets=True)

    assert json.loads(redacted.body) == {
        "url": "https://tasks.example.com/mcp",
        "headers": {"Authorization": "Bearer <mcp_token>"},
    }
    assert json.loads(revealed.body) == {
        "url": "https://tasks.example.com/mcp",
        "headers": {"Authorization": "Bearer secret-bearer"},
    }
    assert "grok.com/connectors" in revealed.guidance
    assert "New Connector" in revealed.guidance
    assert "Custom" in revealed.guidance
    assert "exactly eight tools" in revealed.guidance
    assert "cannot verify DNS or reachability" in revealed.guidance


@pytest.mark.parametrize(
    "url",
    (
        "http://127.0.0.1:8787",
        "https://127.0.0.1:8787",
        "https://localhost:8787",
        "https://LOCALHOST.:8787",
        "https://printer",
        "https://nas.local",
        "https://service.internal",
        "https://machine.tailnet.TS.NET.",
        "https://192.168.1.2",
        "https://100.64.0.2",
        "https://127.1",
        "https://0x7f.0.0.1",
        "https://0177.0.0.1",
        "https://2130706433",
        "https://224.0.0.1",
        "https://[ff02::1]",
    ),
)
def test_grok_config_rejects_http_and_known_local_or_private_endpoints(
    url: str,
) -> None:
    endpoint = Endpoint(
        url=normalize_mcp_url(url),
        bearer=McpBearer("secret-bearer"),
    )

    with pytest.raises(ConfigError, match="known local or private"):
        render_client_config(ClientKind.GROK, endpoint, show_secrets=True)


def test_grok_config_accepts_public_ipv6_without_claiming_reachability() -> None:
    endpoint = Endpoint(
        url=normalize_mcp_url("https://[2606:4700:4700::1111]"),
        bearer=McpBearer("secret-bearer"),
    )

    rendered = render_client_config(ClientKind.GROK, endpoint, show_secrets=False)

    assert "https://[2606:4700:4700::1111]/mcp" in rendered.body
    assert "cannot verify DNS or reachability" in rendered.guidance


def test_cursor_configs_are_parseable_and_cloud_guidance_is_explicit(
    endpoint: Endpoint,
) -> None:
    desktop = render_client_config(
        ClientKind.CURSOR, endpoint, show_secrets=True
    )
    cloud = render_client_config(
        ClientKind.CURSOR_CLOUD, endpoint, show_secrets=True
    )

    assert json.loads(desktop.body)["mcpServers"]["things"]["headers"] == {
        "Authorization": "Bearer secret-bearer"
    }
    assert json.loads(cloud.body) == json.loads(desktop.body)
    assert "dashboard" in cloud.guidance.lower()
    assert "literal token" in cloud.guidance.lower()
    assert "cannot view" in cloud.guidance.lower()
    assert "rotation" in cloud.guidance.lower()


def test_claude_code_emits_a_finished_command_and_typed_http_json(
    endpoint: Endpoint,
) -> None:
    rendered = render_client_config(
        ClientKind.CLAUDE_CODE, endpoint, show_secrets=True
    )
    command = shlex.split(rendered.body)

    assert command == [
        "claude",
        "mcp",
        "add",
        "--transport",
        "http",
        "things",
        "https://tasks.example.com/mcp",
        "--header",
        "Authorization: Bearer secret-bearer",
    ]
    assert json.loads(rendered.secondary_body or "") == {
        "type": "http",
        "url": "https://tasks.example.com/mcp",
        "headers": {"Authorization": "Bearer secret-bearer"},
    }
    assert "claude mcp add-json things '<JSON>'" in rendered.guidance


def test_caddy_uses_the_saved_hostname_and_keeps_streaming_enabled(
    endpoint: Endpoint,
) -> None:
    rendered = render_client_config(
        ClientKind.CADDY, endpoint, show_secrets=False
    )

    assert rendered.body.startswith("tasks.example.com {")
    assert "reverse_proxy 127.0.0.1:8787" in rendered.body
    assert "flush_interval -1" in rendered.body
    assert "Bearer" not in rendered.body
