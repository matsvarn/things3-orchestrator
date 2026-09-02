from __future__ import annotations

import json
from contextlib import contextmanager
from hashlib import sha256
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from things_orchestrator.cli import (
    _legacy_resolution_command,
    _private_tty,
    _server,
    build_parser,
    main,
)
from things_orchestrator.config import ConfigError, Credentials, McpBearer
from things_orchestrator.doctor import DoctorFailure
from things_orchestrator.journal import IntentRecord, SQLiteJournal, V2Operation

ROOT = Path(__file__).parents[1]


class _TTYBuffer(StringIO):
    def isatty(self) -> bool:
        return True


def test_private_tty_uses_inherited_terminal_when_dev_tty_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdin = _TTYBuffer()
    stderr = _TTYBuffer()

    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise OSError("service account cannot reopen the terminal")

    monkeypatch.setattr("things_orchestrator.cli.open", unavailable, raising=False)
    monkeypatch.setattr("things_orchestrator.cli.sys.stdin", stdin)
    monkeypatch.setattr("things_orchestrator.cli.sys.stderr", stderr)

    with _private_tty(build_parser()) as terminal:
        assert terminal is stderr

    assert not stderr.closed


def test_private_tty_rejects_redirected_inherited_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise OSError("no controlling terminal")

    monkeypatch.setattr("things_orchestrator.cli.open", unavailable, raising=False)
    monkeypatch.setattr("things_orchestrator.cli.sys.stdin", StringIO())
    monkeypatch.setattr("things_orchestrator.cli.sys.stderr", StringIO())

    with pytest.raises(SystemExit) as caught:
        with _private_tty(build_parser()):
            pass

    assert caught.value.code == 2


def _stdout_without_secret_flag(out: str) -> str:
    return out.replace("--show-secrets", "").replace("show-secrets", "")


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


def _seed_credentials(
    tmp_path: Path,
    *,
    token: str = "keep-me",
    timezone: str = "Europe/Berlin",
    password: str = "secret",
) -> Path:
    creds = tmp_path / "credentials.json"
    creds.write_text(
        json.dumps(
            {
                "email": "user@example.com",
                "password": password,
                "mcp_token": token,
                "timezone": timezone,
            }
        )
        + "\n"
    )
    return creds


def _seed_preferences(
    tmp_path: Path, *, url: str = "http://127.0.0.1:8787/mcp"
) -> None:
    (tmp_path / "preferences.json").write_text(
        json.dumps(
            {
                "version": 2,
                "note_style": "natural",
                "timezone": "Europe/Berlin",
                "mcp_url": url,
            }
        )
        + "\n"
    )


def test_login_stores_credentials_and_preferences_without_snippets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds = tmp_path / "credentials.json"
    _fake_cloud(monkeypatch)
    monkeypatch.setattr("things_orchestrator.cli.credentials_path", lambda: creds)
    monkeypatch.setattr("things_orchestrator.cli.state_cache_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr("things_orchestrator.cli.token_urlsafe", lambda _n: "fixed-token")
    main(["login", "--timezone", "Europe/Berlin"])
    out = capsys.readouterr().out
    assert "fixed-token" not in out
    assert "Bearer fixed-token" not in out
    assert "Authorization" not in out
    assert "mcp_servers" not in out
    assert "mcpServers" not in out
    assert "install the HTTP service" in out
    assert "doctor --wait" in out
    assert "Do not paste the Cloud password into chat." in out
    assert "The HTTP Bearer is the MCP token, not the Cloud password." in out
    assert "codex plugin marketplace add ." not in out
    assert "secret" not in _stdout_without_secret_flag(out)
    assert not list(tmp_path.glob("mcp.*"))
    stored = json.loads(creds.read_text())
    assert stored["mcp_token"] == "fixed-token"
    assert stored["password"] == "secret"
    assert "timezone" not in stored
    assert json.loads((tmp_path / "preferences.json").read_text()) == {
        "version": 2,
        "note_style": "natural",
        "timezone": "Europe/Berlin",
        "mcp_url": "http://127.0.0.1:8787/mcp",
    }
    checkout = (tmp_path / "checkout").read_text().strip()
    assert Path(checkout) == ROOT.resolve()
    for name in ("credentials.json", "preferences.json"):
        assert (tmp_path / name).stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700


def test_login_show_secrets_never_prints_bearer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds = tmp_path / "credentials.json"
    _fake_cloud(monkeypatch)
    monkeypatch.setattr("things_orchestrator.cli.credentials_path", lambda: creds)
    monkeypatch.setattr("things_orchestrator.cli.state_cache_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr("things_orchestrator.cli.token_urlsafe", lambda _n: "fixed-token")
    main(
        [
            "login",
            "--timezone",
            "Europe/Berlin",
            "--show-secrets",
            "--url",
            "https://tasks.example.com",
        ]
    )
    out = capsys.readouterr().out
    assert "fixed-token" not in out
    assert "Bearer fixed-token" not in out
    assert "mcp_servers" not in out
    assert "mcpServers" not in out
    assert "--show-secrets moved to print-config --client CLIENT" in out
    assert '"secret"' not in out


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
    main(["login", "--timezone", "Europe/Berlin"])
    assert json.loads(creds.read_text())["mcp_token"] == "keep-me"
    main(["login", "--rotate-token", "--timezone", "Europe/Berlin"])
    assert json.loads(creds.read_text())["mcp_token"] == "new-token"


def test_login_updates_only_host_preferences_and_preserves_other_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creds = tmp_path / "credentials.json"
    preferences = tmp_path / "preferences.json"
    original = '{"version":1,"note_style":"visual","future":"keep"}\n'
    preferences.write_text(original)
    _fake_cloud(monkeypatch)
    monkeypatch.setattr("things_orchestrator.cli.credentials_path", lambda: creds)
    monkeypatch.setattr("things_orchestrator.cli.state_cache_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr("things_orchestrator.cli.token_urlsafe", lambda _n: "first-token")

    main(["login", "--timezone", "Europe/Berlin"])
    main(["login", "--rotate-token", "--timezone", "Europe/Berlin"])
    main(["print-config"])

    assert json.loads(preferences.read_text()) == {
        "version": 2,
        "note_style": "visual",
        "future": "keep",
        "timezone": "Europe/Berlin",
        "mcp_url": "http://127.0.0.1:8787/mcp",
    }


def test_configure_changes_only_note_style_and_preserves_unknown_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "things-orchestrator/preferences.json"
    path.parent.mkdir()
    path.write_text(
        '{"version":1,"note_style":"natural","future":{"keep":true}}\n'
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    main(["configure", "--note-style", "visual"])

    payload = json.loads(path.read_text())
    assert payload == {
        "version": 2,
        "note_style": "visual",
        "future": {"keep": True},
    }
    out = capsys.readouterr().out
    assert "Note style: visual" in out
    assert str(path) in out


def test_configure_refuses_invalid_saved_preferences_without_overwrite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "things-orchestrator/preferences.json"
    path.parent.mkdir()
    path.write_text("broken")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    with pytest.raises(SystemExit) as caught:
        main(["configure", "--note-style", "visual"])

    assert caught.value.code == 2
    assert "Preferences file is unreadable" in capsys.readouterr().err
    assert path.read_text() == "broken"


def test_configure_sets_and_clears_source_schemes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "things-orchestrator/preferences.json"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    main(
        [
            "configure",
            "--source-schemes",
            "Obsidian",
            "x-devonthink-item",
        ]
    )

    assert json.loads(path.read_text()) == {
        "version": 2,
        "note_style": "natural",
        "source_schemes": ["obsidian", "x-devonthink-item"],
    }
    assert "Source schemes: obsidian, x-devonthink-item" in capsys.readouterr().out

    main(["configure", "--source-schemes"])
    assert json.loads(path.read_text())["source_schemes"] == []
    assert "Source schemes: none" in capsys.readouterr().out


def test_configure_requires_at_least_one_preference(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["configure"])

    assert caught.value.code == 2
    assert "needs --note-style, --source-schemes, --timezone, or --url" in (
        capsys.readouterr().err
    )


def test_migration_report_quarantines_and_reads_disposable_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "journal.sqlite3"
    journal = SQLiteJournal(path)
    journal.save(IntentRecord("old-approval", "a", "needs_approval"))
    journal.save(IntentRecord("old-pending", "b", "pending"))
    monkeypatch.setattr(
        "things_orchestrator.cli.load_credentials",
        lambda: Credentials("owner@example.com", "unused", None),
    )
    monkeypatch.setattr("things_orchestrator.cli.journal_path", lambda _email: path)

    main(["migration-report"])

    report = json.loads(capsys.readouterr().out)
    assert report["quarantined"] == ["old-approval"]
    assert report["unresolved"] == ["old-pending"]
    assert SQLiteJournal(path).get("old-approval").state == "stale"  # type: ignore[union-attr]


def test_configure_rejects_scheme_and_keeps_note_style_change_atomic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "things-orchestrator/preferences.json"
    path.parent.mkdir()
    path.write_text('{"version":1,"note_style":"natural"}\n')
    original = path.read_text()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    with pytest.raises(SystemExit):
        main(
            [
                "configure",
                "--note-style",
                "visual",
                "--source-schemes",
                "javascript",
            ]
        )

    assert path.read_text() == original


def test_print_config_renders_without_writing_and_hides_token(
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
    main(["print-config", "--url", "https://tasks.example.com"])
    out = capsys.readouterr().out
    assert "secret" not in _stdout_without_secret_flag(out)
    assert "keep-me" not in out
    assert "Bearer" in out
    assert "https://tasks.example.com/mcp" in out
    assert "Bearer <mcp_token>" in out
    assert "deprecated default" in out
    assert not list(tmp_path.glob("mcp.*"))


def test_print_config_show_secrets_prints_bearer(
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
    main(["print-config", "--client", "codex", "--show-secrets", "--url", "https://tasks.example.com"])
    out = capsys.readouterr().out
    assert '"secret"' not in out
    assert "keep-me" in out
    assert "Bearer keep-me" in out
    assert "~/.codex/config.toml" in out
    assert "mcp_servers.things" in out


def test_print_config_uses_saved_url_without_mutating_preferences(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds = tmp_path / "credentials.json"
    creds.write_text(
        json.dumps(
            {"email": "user@example.com", "password": "secret", "mcp_token": "keep-me"}
        )
        + "\n"
    )
    preferences = tmp_path / "preferences.json"
    preferences.write_text(
        '{"version":2,"note_style":"natural","mcp_url":"https://tasks.example.com/mcp"}\n'
    )
    before = preferences.read_text()
    monkeypatch.setattr("things_orchestrator.cli.credentials_path", lambda: creds)
    monkeypatch.setattr("things_orchestrator.cli.state_cache_path", lambda: tmp_path / "state.json")
    main(["print-config", "--client", "cursor"])
    assert "https://tasks.example.com/mcp" in capsys.readouterr().out
    assert preferences.read_text() == before


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


def test_doctor_without_credentials_points_at_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "things_orchestrator.cli.credentials_path", lambda: tmp_path / "missing.json"
    )
    with pytest.raises(SystemExit) as caught:
        main(["doctor"])
    assert caught.value.code == 2
    assert "uv run things-orchestrator login" in capsys.readouterr().err


def test_doctor_without_server_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds = _seed_credentials(tmp_path)
    _seed_preferences(tmp_path)
    monkeypatch.setattr("things_orchestrator.cli.credentials_path", lambda: creds)
    monkeypatch.setattr("things_orchestrator.cli.state_cache_path", lambda: tmp_path / "state.json")

    async def refused(*_args: object, **_kwargs: object) -> object:
        raise DoctorFailure("connection refused")

    monkeypatch.setattr("things_orchestrator.cli.run_doctor", refused)
    with pytest.raises(SystemExit) as caught:
        main(["doctor"])
    assert caught.value.code == 1
    out = capsys.readouterr().out
    assert "keep-me" not in out
    assert "secret" not in out
    assert "credentials: ok" in out
    assert "timezone: ok (Europe/Berlin)" in out
    assert "doctor: fail (connection refused)" in out


def test_doctor_without_url_checks_loopback_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds = _seed_credentials(tmp_path)
    _seed_preferences(tmp_path, url="https://tasks.example.com/mcp")
    monkeypatch.setattr("things_orchestrator.cli.credentials_path", lambda: creds)
    monkeypatch.setattr("things_orchestrator.cli.state_cache_path", lambda: tmp_path / "state.json")

    seen: list[str] = []

    async def healthy(targets: list[object], *_args: object, **_kwargs: object) -> object:
        seen.extend(str(target) for target in targets)
        return _doctor_report(targets)

    monkeypatch.setattr("things_orchestrator.cli.run_doctor", healthy)
    main(["doctor"])
    out = capsys.readouterr().out
    assert seen == ["http://127.0.0.1:8787/mcp"]
    assert "mcp: ok (http://127.0.0.1:8787/mcp; 8 tools" in out


def test_doctor_passes_wait_to_authenticated_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds = _seed_credentials(tmp_path)
    _seed_preferences(tmp_path, url="https://tasks.example.com/mcp")
    monkeypatch.setattr("things_orchestrator.cli.credentials_path", lambda: creds)
    monkeypatch.setattr("things_orchestrator.cli.state_cache_path", lambda: tmp_path / "state.json")
    seen_wait: list[bool] = []

    async def healthy(targets: list[object], *_args: object, wait: bool) -> object:
        seen_wait.append(wait)
        return _doctor_report(targets)

    monkeypatch.setattr("things_orchestrator.cli.run_doctor", healthy)
    main(["doctor", "--wait"])
    out = capsys.readouterr().out
    assert seen_wait == [True]
    assert "service: current" in out
    assert "keep-me" not in out
    assert "secret" not in out


def test_doctor_url_probes_loopback_and_remote_mcp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds = _seed_credentials(tmp_path)
    _seed_preferences(tmp_path, url="https://tasks.example.com/mcp")
    monkeypatch.setattr("things_orchestrator.cli.credentials_path", lambda: creds)
    monkeypatch.setattr("things_orchestrator.cli.state_cache_path", lambda: tmp_path / "state.json")
    seen: list[str] = []

    async def healthy(targets: list[object], *_args: object, **_kwargs: object) -> object:
        seen.extend(str(target) for target in targets)
        return _doctor_report(targets)

    monkeypatch.setattr("things_orchestrator.cli.run_doctor", healthy)
    main(["doctor", "--url", "https://tasks.example.com/mcp"])
    out = capsys.readouterr().out
    assert seen == [
        "http://127.0.0.1:8787/mcp",
        "https://tasks.example.com/mcp",
    ]
    assert "mcp: ok (https://tasks.example.com/mcp; 8 tools" in out
    assert "$THINGS_MCP_TOKEN" in out
    assert "keep-me" not in out


def _doctor_report(targets: list[object]) -> object:
    receipts = tuple(
        SimpleNamespace(
            url=target,
            detailed_health={"commit": "a" * 40},
            tool_names=tuple(range(8)),
        )
        for target in targets
    )
    return SimpleNamespace(targets=receipts)


def test_serve_without_credentials_points_at_login(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(**_kwargs: object) -> Credentials:
        raise ConfigError("unused")

    monkeypatch.setattr("things_orchestrator.cli.load_credentials", missing)
    with pytest.raises(SystemExit) as caught:
        main(["serve"])
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert "uv run things-orchestrator login" in err
    assert "THINGS_PASSWORD" not in err


def test_server_binds_a_persistent_context_store_to_the_cloud_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    account_journal = tmp_path / "journal-accountdigest.sqlite3"
    captured: dict[str, object] = {}
    class FakeJournal:
        def cutover_v1(self) -> dict[str, object]:
            return {"unresolved": []}

        def prune_v2(self, *, now: str, retention_days: int) -> int:
            assert now
            assert retention_days == 7
            return 0

    journal = FakeJournal()

    class FakeClient:
        def __init__(self, email: str, password: str) -> None:
            assert (email, password) == ("owner@example.com", "cloud-secret")

    class FakeWorkspace:
        def __init__(self, library: object, **kwargs: object) -> None:
            captured["library"] = library
            captured.update(kwargs)

    class FakeContextStore:
        def __init__(
            self,
            path: Path,
            *,
            clock: object,
            token_factory: object,
        ) -> None:
            captured["context_path"] = path
            captured["context_clock"] = clock
            captured["token_factory"] = token_factory
            captured["context_store_instance"] = self

    monkeypatch.setattr(
        "things_orchestrator.cli.load_credentials",
        lambda: Credentials("owner@example.com", "cloud-secret", None),
    )
    monkeypatch.setattr(
        "things_orchestrator.cli.load_timezone", lambda: "Europe/Berlin"
    )
    monkeypatch.setattr("things_orchestrator.cli.CloudClient", FakeClient)
    monkeypatch.setattr("things_orchestrator.cli.CloudLibrary", lambda client: client)
    monkeypatch.setattr(
        "things_orchestrator.cli.journal_path", lambda email: account_journal
    )
    monkeypatch.setattr("things_orchestrator.cli.SQLiteJournal", lambda path: journal)
    monkeypatch.setattr("things_orchestrator.cli.SQLiteContextStore", FakeContextStore)
    monkeypatch.setattr("things_orchestrator.cli.ThingsWorkspace", FakeWorkspace)
    monkeypatch.setattr(
        "things_orchestrator.cli.ThingsMCPServer", lambda workspace: workspace
    )
    monkeypatch.setattr(
        "things_orchestrator.cli.token_urlsafe",
        lambda size: "secure-context-token" if size == 24 else "wrong-size",
    )

    workspace = _server(build_parser())

    assert workspace is not None
    assert captured["journal"] is journal
    assert captured["account_id"] == "owner@example.com"
    assert callable(captured["preferences"])
    assert captured["context_store"] is captured["context_store_instance"]
    assert captured["context_path"] == tmp_path / "contexts-accountdigest.sqlite3"
    context_clock = captured["context_clock"]
    assert callable(context_clock)
    assert context_clock().utcoffset() is not None  # type: ignore[operator]
    token_factory = captured["token_factory"]
    assert callable(token_factory)
    assert token_factory() == "secure-context-token"  # type: ignore[operator]


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
    assert "## This Mac" in clients
    assert "## Already hosted" in clients
    assert "~/.hermes/config.yaml" in clients
    assert "cursor.com/agents" in clients
    assert "cannot use this bearer" in clients
    assert "does not ship MCP OAuth" in clients
    assert "config.toml" in clients
    assert "bearer_token_env_var" in clients or "http_headers" in clients
    host = (ROOT / "docs/host.md").read_text()
    assert "login --url" in host
    assert "plugin/skills" in host
    assert "do not run `login`" in host.lower()
    assert "serve-http.service" in host
    assert "claude mcp add --transport http" not in host
    assert "Tailscale" in host
    assert "topolog" in host.lower()
    assert "Things Cloud password" in host
    assert "VPS" in host
    assert "Hermes Desktop" in host or "Hermes desktop" in host
    assert "docs/host.md" in stages


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
    assert "Mac can be off" in readme
    assert "docs/trust.md" in readme
    assert "docs/comparison.md" in readme
    assert "docs/host.md" in readme
    assert "docs/capability-proof.md" in readme
    trust = (ROOT / "docs/trust.md").read_text()
    assert "model provider" in trust
    assert "fully private" in trust
    comparison = (ROOT / "docs/comparison.md").read_text()
    assert "Reviewed on 2026-08-14" in comparison
    assert "hald/things-mcp" in comparison
    assert "thingscloudmcp.com" in comparison
    assert "wbopan/things-cloud-mcp" in comparison
    assert not (ROOT / "docs/public-launch.md").exists()


def test_parser_names_the_owner_commands() -> None:
    help_text = build_parser().format_help()
    assert "login" in help_text
    assert "serve" in help_text
    assert "serve-http" in help_text
    assert "print-config" in help_text
    assert "doctor" in help_text
    assert "configure" in help_text
    assert "skill-path" in help_text
    assert "uv run things-orchestrator login" in help_text
    compact = help_text.replace("-\n", "-").replace("\n", " ")
    assert "things-orchestrator doctor" in compact
    with pytest.raises(SystemExit):
        build_parser().parse_args(["serve-http", "--host", "0.0.0.0"])


def test_skill_path_prints_the_packaged_skill(capsys: pytest.CaptureFixture[str]) -> None:
    main(["skill-path"])

    output = Path(capsys.readouterr().out.strip())
    assert output.name == "things-orchestrator"
    assert (output / "SKILL.md").is_file()


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


def test_plugin_wrapper_runs_configure_from_the_checkout(tmp_path: Path) -> None:
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
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(state),
    }

    result = subprocess.run(
        [str(script), "configure", "--note-style", "visual"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    saved = json.loads(
        (tmp_path / "config/things-orchestrator/preferences.json").read_text()
    )
    assert saved["note_style"] == "visual"


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


def test_plugin_wrapper_routes_every_recovery_command() -> None:
    script = (ROOT / "plugin/bin/things-orchestrator").read_text()
    commands = ("legacy-reconcile", "legacy-resolve", "operation-reconcile")
    for command in commands:
        assert command in script
    usage = next(line for line in script.splitlines() if line.startswith('    echo "Usage:'))
    for command in commands:
        assert command in usage
    for removed in (
        "operation-settle-not-applied",
        "operation-approve",
        "operation-decline",
        "operation-accept-partial",
    ):
        assert removed not in script


def test_legacy_resolution_renders_before_reading_passphrase(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    title = "\x1b[31mOwner\n|\u202e"
    plan = {
        "writes": [
            {"action": "update", "uuid": "a", "kind": "task", "title": title}
        ]
    }
    digest = "sha256:v1:" + sha256(
        json.dumps(
            plan, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
    ).hexdigest()
    operation = V2Operation(
        account_id="owner@example.com",
        api_version="legacy-v1",
        request_id="fp",
        request_hash=digest,
        operation_id="legacy_render",
        tool="legacy_pending_resolution",
        state="pending",
        manifest={
            "intent_id_hash": "sha256:v1:" + "0" * 64,
            "writes": plan["writes"],
            "display_titles": [title],
            "legacy_plan": plan,
        },
        manifest_hash=digest,
        safety_policy_digest="sha256:v1:legacy-no-replay-resolution",
    )

    class Workspace:
        def host_get_legacy_resolution_v1(self, _intent_id: str) -> V2Operation:
            return operation

        def host_resolve_legacy_v1(self, *_args: object) -> bool:
            return True

    @contextmanager
    def tty(_parser: object) -> object:
        yield object()

    monkeypatch.setattr("things_orchestrator.cli._workspace", lambda _parser: Workspace())
    monkeypatch.setattr("things_orchestrator.cli._private_tty", tty)

    def getpass_after_render(_prompt: str, *, stream: object) -> str:
        assert stream is not None
        rendered = capsys.readouterr().out
        assert "legacy_plan |" in rendered
        assert "\x1b" not in rendered and "\\u000a" in rendered and "\\u202e" in rendered
        return "passphrase"

    monkeypatch.setattr("things_orchestrator.cli.getpass", getpass_after_render)
    monkeypatch.setattr("things_orchestrator.owner_authority.verified_authorization", lambda *_args, **_kwargs: object())
    _legacy_resolution_command(build_parser(), "legacy", "accepted_as_is")


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
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "things_orchestrator.cli.load_credentials",
        lambda: Credentials("user@example.com", "secret", None),
    )
    monkeypatch.setattr("things_orchestrator.cli._server", lambda _parser: object())

    with pytest.raises(SystemExit) as caught:
        main(["serve-http"])
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert "mcp_token" in err


def test_serve_http_uses_only_the_stored_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class Server:
        def run_http(self, *, port: int, token: str) -> None:
            seen.update(port=port, token=token)

    monkeypatch.setattr("things_orchestrator.cli._server", lambda _parser: Server())
    monkeypatch.setattr(
        "things_orchestrator.cli.load_credentials",
        lambda: Credentials(
            "user@example.com", "secret", McpBearer("stored-bearer")
        ),
    )
    monkeypatch.setenv("THINGS_MCP_TOKEN", "stale-environment-bearer")

    main(["serve-http"])

    assert seen == {"port": 8787, "token": "stored-bearer"}


def test_login_prompts_for_timezone_on_a_utc_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creds = tmp_path / "credentials.json"
    _fake_cloud(monkeypatch)
    answers = iter(("user@example.com", "Europe/Berlin"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("things_orchestrator.cli._local_timezone_name", lambda: "UTC")
    monkeypatch.setattr("things_orchestrator.cli.credentials_path", lambda: creds)
    monkeypatch.setattr(
        "things_orchestrator.cli.state_cache_path", lambda: tmp_path / "state.json"
    )
    monkeypatch.setattr("things_orchestrator.cli.token_urlsafe", lambda _n: "token")

    main(["login"])

    assert json.loads((tmp_path / "preferences.json").read_text())["timezone"] == (
        "Europe/Berlin"
    )
