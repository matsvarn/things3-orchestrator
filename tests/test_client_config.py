from __future__ import annotations

import json
import shlex
import tomllib

import pytest

from things_orchestrator.client_config import ClientKind, Endpoint, render_client_config
from things_orchestrator.config import McpBearer, normalize_mcp_url


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
        "v0.9.1/plugin/skills/things-orchestrator/SKILL.md",
        "--yes",
    ]
    assert "secret-bearer" not in rendered.body
    assert "prompt" in rendered.guidance.lower()
    assert "pinned" in rendered.guidance.lower()


def test_hermes_renderer_never_puts_the_secret_in_commands(endpoint: Endpoint) -> None:
    rendered = render_client_config(ClientKind.HERMES, endpoint, show_secrets=True)

    assert "secret-bearer" not in rendered.body


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
