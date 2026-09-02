from __future__ import annotations

import json
from pathlib import Path

import pytest

from things_orchestrator.config import (
    ConfigError,
    McpBearer,
    load_credentials,
    load_mcp_url,
    load_timezone,
    normalize_mcp_url,
    save_credentials,
    save_launcher,
    save_preferences,
)


def _url(scheme: str, remainder: str) -> str:
    return f"{scheme}://{remainder}"


def test_credentials_and_owner_preferences_have_separate_authority(
    tmp_path: Path,
) -> None:
    credentials = tmp_path / "credentials.json"
    preferences = tmp_path / "preferences.json"

    save_credentials(
        "user@example.com", "secret", McpBearer("bearer"), path=credentials
    )
    save_preferences(
        timezone="Europe/Berlin",
        mcp_url=normalize_mcp_url(_url("https", "tasks.example.com")),
        path=preferences,
    )

    assert json.loads(credentials.read_text()) == {
        "email": "user@example.com",
        "password": "secret",
        "mcp_token": "bearer",
    }
    assert load_timezone(
        preferences_file=preferences, credentials_file=credentials
    ) == "Europe/Berlin"
    assert str(
        load_mcp_url(preferences_file=preferences, credentials_file=credentials)
    ) == _url("https", "tasks.example.com/mcp")
    assert str(load_credentials(path=credentials).bearer) == "<mcp_token>"


def test_legacy_credentials_timezone_is_a_read_only_fallback(tmp_path: Path) -> None:
    credentials = tmp_path / "credentials.json"
    preferences = tmp_path / "preferences.json"
    credentials.write_text(
        json.dumps(
            {
                "email": "user@example.com",
                "password": "secret",
                "mcp_token": "bearer",
                "timezone": "Europe/Berlin",
            }
        )
    )

    assert load_timezone(
        preferences_file=preferences, credentials_file=credentials
    ) == "Europe/Berlin"
    assert load_mcp_url(
        preferences_file=preferences, credentials_file=credentials
    ) is None
    assert not preferences.exists()


@pytest.mark.parametrize(
    "raw",
    (
        _url("https", "YOUR-HOST"),
        _url("ftp", "tasks.example.com"),
        _url("http", "tasks.example.com"),
        _url("https", "tasks.example.com/other"),
        _url("https", "user:secret@tasks.example.com"),
        _url("https", "tasks.example.com?token=x"),
    ),
)
def test_mcp_url_rejects_placeholders_and_unsafe_origins(raw: str) -> None:
    with pytest.raises(ConfigError):
        normalize_mcp_url(raw)


def test_mcp_url_accepts_https_and_loopback_http() -> None:
    assert str(normalize_mcp_url(_url("https", "tasks.example.com/"))) == (
        _url("https", "tasks.example.com/mcp")
    )
    assert str(normalize_mcp_url(_url("https", "tasks.example.com/mcp"))) == (
        _url("https", "tasks.example.com/mcp")
    )
    assert str(normalize_mcp_url(_url("http", "127.0.0.1:8787"))) == (
        _url("http", "127.0.0.1:8787/mcp")
    )


def test_launcher_binding_is_exact_private_and_executable(tmp_path: Path) -> None:
    executable = tmp_path / "bin/things-orchestrator"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o700)
    binding = tmp_path / "state/launcher"

    saved = save_launcher(executable, path=binding)

    assert saved.read_text() == f"{executable.resolve()}\n"
    assert saved.stat().st_mode & 0o777 == 0o600


def test_launcher_binding_rejects_a_non_executable(tmp_path: Path) -> None:
    executable = tmp_path / "things-orchestrator"
    executable.write_text("not executable")
    with pytest.raises(ConfigError, match="executable"):
        save_launcher(executable, path=tmp_path / "launcher")
