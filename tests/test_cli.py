from __future__ import annotations

import json
from pathlib import Path

import pytest

from things_orchestrator.cli import build_parser, main
from things_orchestrator.cloud import CloudError

ROOT = Path(__file__).parents[1]


def test_mcp_plugin_launches_the_checkout_wrapper() -> None:
    payload = json.loads((ROOT / "plugin/.mcp.json").read_text())
    things = payload["mcpServers"]["things"]
    assert things["command"] == "./bin/things-orchestrator"
    assert things["args"] == ["serve"]
    assert things["cwd"] == "./"


def test_login_refuses_a_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("things_orchestrator.cli.sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit) as caught:
        main(["login"])
    assert caught.value.code == 2


def _fake_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, email: str, password: str) -> None:
            assert email == "user@example.com"
            assert password == "secret"

        def verify(self) -> None:
            return None

    monkeypatch.setattr("things_orchestrator.cli.CloudClient", FakeClient)
    monkeypatch.setattr("things_orchestrator.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "user@example.com")
    monkeypatch.setattr("things_orchestrator.cli.getpass", lambda _: "secret")


def test_login_prints_stdio_and_http_snippets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds = tmp_path / "credentials.json"
    _fake_cloud(monkeypatch)
    monkeypatch.setattr("things_orchestrator.cli.credentials_path", lambda: creds)
    monkeypatch.setattr("things_orchestrator.cli.state_cache_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr("things_orchestrator.cli.token_urlsafe", lambda _n: "fixed-token")
    main(["login", "--timezone", "Europe/Berlin"])
    out = capsys.readouterr().out
    wrapper = str((ROOT / "plugin/bin/things-orchestrator").resolve())
    skills = str((ROOT / "plugin/skills").resolve())
    assert wrapper in out
    assert "mcp_servers" in out
    assert "~/.hermes/config.yaml" in out
    assert "mcpServers" in out
    assert "docs/clients.md" in out
    assert "Do not paste the Cloud password into chat." in out
    assert "codex plugin marketplace add ." not in out
    assert "secret" not in out
    stdio = json.loads((tmp_path / "mcp.stdio.json").read_text())
    assert stdio["mcpServers"]["things"] == {"command": wrapper, "args": ["serve"]}
    http = json.loads((tmp_path / "mcp.http.json").read_text())
    assert http["mcpServers"]["things"] == {
        "url": "https://YOUR-HOST/mcp",
        "headers": {"Authorization": "Bearer fixed-token"},
    }
    hermes = (tmp_path / "mcp.hermes.yaml").read_text()
    assert "mcp_servers:" in hermes
    assert wrapper in hermes
    assert skills in hermes
    hermes_http = (tmp_path / "mcp.hermes.http.yaml").read_text()
    assert "Bearer fixed-token" in hermes_http
    assert "https://YOUR-HOST/mcp" in hermes_http
    stored = json.loads(creds.read_text())
    assert stored["mcp_token"] == "fixed-token"
    assert stored["password"] == "secret"
    assert stored["timezone"] == "Europe/Berlin"
    checkout = (tmp_path / "checkout").read_text().strip()
    assert Path(checkout) == ROOT.resolve()
    for name in (
        "credentials.json",
        "mcp.stdio.json",
        "mcp.http.json",
        "mcp.hermes.yaml",
        "mcp.hermes.http.yaml",
    ):
        assert (tmp_path / name).stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700


def test_login_keeps_mcp_token_unless_rotated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creds = tmp_path / "credentials.json"
    creds.write_text(
        json.dumps({"email": "old@example.com", "password": "old", "mcp_token": "keep-me"}) + "\n"
    )
    _fake_cloud(monkeypatch)
    monkeypatch.setattr("things_orchestrator.cli.credentials_path", lambda: creds)
    monkeypatch.setattr("things_orchestrator.cli.state_cache_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr("things_orchestrator.cli.token_urlsafe", lambda _n: "new-token")
    main(["login"])
    assert json.loads(creds.read_text())["mcp_token"] == "keep-me"
    main(["login", "--rotate-token"])
    assert json.loads(creds.read_text())["mcp_token"] == "new-token"


def test_print_config_reprints_without_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds = tmp_path / "credentials.json"
    creds.write_text(
        json.dumps(
            {"email": "user@example.com", "password": "secret", "mcp_token": "keep-me"}
        )
        + "\n"
    )
    monkeypatch.setattr("things_orchestrator.cli.credentials_path", lambda: creds)
    monkeypatch.setattr("things_orchestrator.cli.state_cache_path", lambda: tmp_path / "state.json")
    main(["print-config", "--http", "--url", "https://tasks.example.com"])
    out = capsys.readouterr().out
    assert "secret" not in out
    assert "keep-me" in out
    http = json.loads((tmp_path / "mcp.http.json").read_text())
    assert http["mcpServers"]["things"]["url"] == "https://tasks.example.com/mcp"
    assert http["mcpServers"]["things"]["headers"]["Authorization"] == "Bearer keep-me"
    hermes_http = (tmp_path / "mcp.hermes.http.yaml").read_text()
    assert "https://tasks.example.com/mcp" in hermes_http
    assert "Bearer keep-me" in hermes_http


def test_print_config_keeps_existing_http_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creds = tmp_path / "credentials.json"
    creds.write_text(
        json.dumps(
            {"email": "user@example.com", "password": "secret", "mcp_token": "keep-me"}
        )
        + "\n"
    )
    (tmp_path / "mcp.http.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "things": {
                        "url": "https://tasks.example.com/mcp",
                        "headers": {"Authorization": "Bearer keep-me"},
                    }
                }
            }
        )
        + "\n"
    )
    monkeypatch.setattr("things_orchestrator.cli.credentials_path", lambda: creds)
    monkeypatch.setattr("things_orchestrator.cli.state_cache_path", lambda: tmp_path / "state.json")
    main(["print-config", "--http"])
    http = json.loads((tmp_path / "mcp.http.json").read_text())
    assert http["mcpServers"]["things"]["url"] == "https://tasks.example.com/mcp"


def test_print_config_without_credentials_points_at_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "things_orchestrator.cli.credentials_path", lambda: tmp_path / "missing.json"
    )
    with pytest.raises(SystemExit) as caught:
        main(["print-config"])
    assert caught.value.code == 2
    assert "uv run things-orchestrator login" in capsys.readouterr().err


def test_serve_without_credentials_points_at_login(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(**_kwargs: object) -> tuple[str, str, str | None]:
        raise CloudError("unused")

    monkeypatch.setattr("things_orchestrator.cli.load_credentials", missing)
    with pytest.raises(SystemExit) as caught:
        main(["serve"])
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert "uv run things-orchestrator login" in err
    assert "THINGS_PASSWORD" not in err


def test_setup_wizard_is_linked_and_does_not_store_cloud_secrets() -> None:
    import subprocess

    script = ROOT / "scripts/setup"
    subprocess.run(["bash", "-n", str(script)], check=True)
    stages = script.read_text().split("# STAGES —", 1)[1]
    assert "write_env" not in stages
    assert "ask_secret" not in stages
    assert "scripts/setup" in (ROOT / "README.md").read_text()
    assert "scripts/setup" in (ROOT / "docs/owner.md").read_text()
    assert "docs/clients.md" in stages
    assert "mcp.stdio.json" in stages
    assert "mcp.http.json" in stages
    assert "mcp.hermes.yaml" in stages
    assert "~/.hermes/config.yaml" in stages
    assert "codex plugin marketplace add ." not in stages
    assert "CURSOR_SKILLS" not in stages
    assert "~/.cursor/skills" not in stages
    assert "the next screen will clear" not in stages
    assert "Do not replace the whole file." in stages
    assert (ROOT / "docs/clients.md").is_file()
    assert (ROOT / "deploy/Caddyfile").is_file()
    assert (ROOT / "deploy/serve-http.service").is_file()
    clients = (ROOT / "docs/clients.md").read_text()
    assert "## Same machine" in clients
    assert "## Other machine" in clients
    assert "~/.hermes/config.yaml" in clients
    assert "cursor.com/agents" in clients
    assert "cannot use this bearer" in clients
    assert "does not ship MCP OAuth" in clients


def test_readme_is_safe_to_publish() -> None:
    readme = (ROOT / "README.md").read_text()
    assert "<this-repo>" not in readme
    assert "unofficial" in readme.lower()
    assert "not on PyPI" in readme
    assert "Use the clone" in readme
    assert "disable an account" in readme
    assert "SECURITY.md" in readme
    assert "MIT" in (ROOT / "LICENSE").read_text()
    security = (ROOT / "SECURITY.md").read_text()
    assert "credentials.json" in security
    assert "disable an account" in security
    assert "private terminal" in security
    assert "THINGS_PASSWORD" not in security
    assert (ROOT / ".github/workflows/ci.yml").is_file()
    assert not (ROOT / "CONTEXT.md").exists()
    assert not (ROOT / "docs/research/hosted-cloud-mcp.md").exists()
    gitignore = (ROOT / ".gitignore").read_text()
    for name in (
        "credentials.json",
        "mcp.http.json",
        "mcp.hermes.http.yaml",
        "state.json",
        "journal.sqlite3",
    ):
        assert name in gitignore
    research = (ROOT / "docs/research/things3-cloud.md").read_text()
    assert "Unofficial" in research
    assert "disable an account" in research
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert "Python :: 3.13" in pyproject
    assert "Python :: 3.14" in pyproject
    assert "things-mcp" in readme
    assert "thingscloudmcp.com" in readme
    assert "Mac can be off" in readme
    assert "docs/trust.md" in readme
    assert "docs/comparison.md" in readme
    trust = (ROOT / "docs/trust.md").read_text()
    assert "model provider" in trust
    assert "fully private" in trust
    comparison = (ROOT / "docs/comparison.md").read_text()
    assert "Reviewed on 2026-08-14" in comparison
    assert "hald/things-mcp" in comparison
    assert "wbopan/things-cloud-mcp" in comparison
    assert not (ROOT / "docs/public-launch.md").exists()


def test_parser_names_the_owner_commands() -> None:
    help_text = build_parser().format_help()
    assert "login" in help_text
    assert "serve" in help_text
    assert "serve-http" in help_text
    assert "print-config" in help_text
    assert "uv run things-orchestrator login" in help_text
    with pytest.raises(SystemExit):
        build_parser().parse_args(["serve-http", "--host", "0.0.0.0"])


def test_plugin_wrapper_reads_the_checkout_file(tmp_path: Path) -> None:
    import os
    import stat
    import subprocess

    plugin = tmp_path / "cache" / "plugin"
    (plugin / "bin").mkdir(parents=True)
    script = plugin / "bin" / "things-orchestrator"
    script.write_text((ROOT / "plugin/bin/things-orchestrator").read_text())
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state"
    (state / "things-orchestrator").mkdir(parents=True)
    (state / "things-orchestrator" / "checkout").write_text(f"{ROOT.resolve()}\n")
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = str(state)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["HOME"] = str(tmp_path / "home")
    env.pop("THINGS_EMAIL", None)
    env.pop("THINGS_PASSWORD", None)
    result = subprocess.run(
        [str(script), "serve"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "uv run things-orchestrator login" in result.stderr


def test_plugin_wrapper_without_checkout_explains_login(tmp_path: Path) -> None:
    import os
    import stat
    import subprocess

    plugin = tmp_path / "cache" / "plugin"
    (plugin / "bin").mkdir(parents=True)
    script = plugin / "bin" / "things-orchestrator"
    script.write_text((ROOT / "plugin/bin/things-orchestrator").read_text())
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    result = subprocess.run(
        [str(script), "serve"],
        cwd=str(plugin),
        env={**os.environ, "HOME": str(tmp_path / "home"), "XDG_STATE_HOME": str(tmp_path / "state")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "uv run things-orchestrator login" in result.stderr
    assert "No module named" not in result.stderr


def test_login_password_confirm_mismatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("things_orchestrator.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "user@example.com")
    passwords = iter(["secret", "nope"])
    monkeypatch.setattr("things_orchestrator.cli.getpass", lambda _: next(passwords))
    with pytest.raises(SystemExit) as caught:
        main(["login"])
    assert caught.value.code == 2
    assert "password confirmation did not match" in capsys.readouterr().err


def test_serve_http_without_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds = tmp_path / "credentials.json"
    creds.write_text(
        json.dumps({"email": "user@example.com", "password": "secret"}) + "\n"
    )
    monkeypatch.setattr("things_orchestrator.cli.credentials_path", lambda: creds)
    monkeypatch.setattr("things_orchestrator.cloud.credentials_path", lambda: creds)
    monkeypatch.setattr("things_orchestrator.cli.state_cache_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr("things_orchestrator.cloud.state_cache_path", lambda: tmp_path / "state.json")
    monkeypatch.delenv("THINGS_MCP_TOKEN", raising=False)
    monkeypatch.delenv("THINGS_EMAIL", raising=False)
    monkeypatch.delenv("THINGS_PASSWORD", raising=False)

    def fail_run_http(**_kwargs: object) -> None:
        raise AssertionError("serve-http must not start without a token")

    monkeypatch.setattr("things_orchestrator.server.ThingsMCPServer.run_http", fail_run_http)
    with pytest.raises(SystemExit) as caught:
        main(["serve-http"])
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert "THINGS_MCP_TOKEN" in err
    assert "mcp_token" in err
