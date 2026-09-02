from __future__ import annotations

import json
import shlex
import tomllib
from pathlib import Path

import pytest
import yaml

from things_orchestrator.client_config import ClientKind, Endpoint, render_client_config
from things_orchestrator.config import McpBearer, normalize_mcp_url


@pytest.fixture
def endpoint() -> Endpoint:
    return Endpoint(
        url=normalize_mcp_url("https://tasks.example.com"),
        bearer=McpBearer("secret-bearer"),
    )


def test_codex_config_is_parseable_toml_and_redacted_by_default(
    endpoint: Endpoint, tmp_path: Path
) -> None:
    rendered = render_client_config(
        ClientKind.CODEX, endpoint, skill_dir=tmp_path, show_secrets=False
    )
    parsed = tomllib.loads(rendered.body)

    assert parsed["mcp_servers"]["things"] == {
        "url": "https://tasks.example.com/mcp",
        "http_headers": {"Authorization": "Bearer <mcp_token>"},
    }
    assert "secret-bearer" not in rendered.body


def test_hermes_config_is_parseable_http_yaml_with_the_installed_skill(
    endpoint: Endpoint, tmp_path: Path
) -> None:
    rendered = render_client_config(
        ClientKind.HERMES, endpoint, skill_dir=tmp_path, show_secrets=True
    )
    parsed = yaml.safe_load(rendered.body)

    assert parsed["mcp_servers"]["things"]["url"] == (
        "https://tasks.example.com/mcp"
    )
    assert parsed["mcp_servers"]["things"]["headers"] == {
        "Authorization": "Bearer secret-bearer"
    }
    assert parsed["skills"]["external_dirs"] == [str(tmp_path)]


def test_cursor_configs_are_parseable_and_cloud_guidance_is_explicit(
    endpoint: Endpoint, tmp_path: Path
) -> None:
    desktop = render_client_config(
        ClientKind.CURSOR, endpoint, skill_dir=tmp_path, show_secrets=True
    )
    cloud = render_client_config(
        ClientKind.CURSOR_CLOUD, endpoint, skill_dir=tmp_path, show_secrets=True
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
    endpoint: Endpoint, tmp_path: Path
) -> None:
    rendered = render_client_config(
        ClientKind.CLAUDE_CODE, endpoint, skill_dir=tmp_path, show_secrets=True
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


def test_caddy_uses_the_saved_hostname_and_keeps_streaming_enabled(
    endpoint: Endpoint, tmp_path: Path
) -> None:
    rendered = render_client_config(
        ClientKind.CADDY, endpoint, skill_dir=tmp_path, show_secrets=False
    )

    assert rendered.body.startswith("tasks.example.com {")
    assert "reverse_proxy 127.0.0.1:8787" in rendered.body
    assert "flush_interval -1" in rendered.body
    assert "Bearer" not in rendered.body
