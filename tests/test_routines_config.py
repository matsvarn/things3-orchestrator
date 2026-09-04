from __future__ import annotations

import json
import traceback
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr
from io import StringIO
from pathlib import Path

import pytest

from things_orchestrator.cli import main
from things_orchestrator.config import ConfigError
from things_orchestrator.routines_config import (
    DisabledRoutineConfig,
    EnabledRoutineConfig,
    ReceiverSecret,
    account_digest,
    configure_routines,
    load_routines_config,
    routines_status,
    set_routines_enabled,
)
from things_orchestrator.routines_store import RoutineStore


def _valid_config_payload() -> dict[str, object]:
    return {
        "version": 1,
        "state": "disabled",
        "profile": {
            "account_digest": account_digest("owner@example.com"),
            "host_profile": "always_on",
            "receiver_url": "https://agent.example/webhooks/task",
            "receiver_secret": "secret",
            "poll_interval_seconds": 60,
            "settle_seconds": 120,
            "routine_id": "things-ai-task-created-v1",
            "retry": {
                "initial_delay_seconds": 5,
                "max_delay_seconds": 900,
                "max_attempts": 10,
                "max_age_seconds": 604_800,
            },
        },
    }


def _replace_nested(
    payload: dict[str, object], dotted_path: str, value: object
) -> None:
    current = payload
    parts = dotted_path.split(".")
    for part in parts[:-1]:
        nested = current[part]
        assert isinstance(nested, dict)
        current = nested
    current[parts[-1]] = value


def test_private_config_is_account_bound_redacted_and_convergent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "routines.json"
    secret = "webhook-secret-value"
    configured = configure_routines(
        email="Owner@Example.com",
        receiver_url="https://agent.example/webhooks/things-ai",
        receiver_secret=ReceiverSecret(secret),
        poll_interval_seconds=60,
        path=path,
    )

    assert isinstance(configured, DisabledRoutineConfig)
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["profile"]["receiver_secret"] == secret
    assert secret not in repr(configured)
    assert secret not in json.dumps(
        routines_status(configured, email="owner@example.com")
    )
    assert "agent.example" not in json.dumps(
        routines_status(configured, email="owner@example.com")
    )

    path.chmod(0o644)
    enabled = set_routines_enabled(True, email="owner@example.com", path=path)
    assert path.stat().st_mode & 0o777 == 0o600
    first = path.read_bytes()
    enabled_again = set_routines_enabled(True, email="OWNER@example.com", path=path)
    assert isinstance(enabled, EnabledRoutineConfig)
    assert enabled_again == enabled
    assert path.read_bytes() == first

    disabled = set_routines_enabled(False, email="owner@example.com", path=path)
    assert isinstance(disabled, DisabledRoutineConfig)
    assert load_routines_config(path=path) == disabled


def test_account_mismatch_fails_without_rewriting_config(tmp_path: Path) -> None:
    path = tmp_path / "routines.json"
    configure_routines(
        email="owner@example.com",
        receiver_url="https://agent.example/webhooks/things-ai",
        receiver_secret=ReceiverSecret("secret"),
        poll_interval_seconds=60,
        path=path,
    )
    before = path.read_bytes()

    with pytest.raises(ConfigError, match="different Things account"):
        set_routines_enabled(True, email="other@example.com", path=path)

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "url",
    (
        "http://agent.example/webhooks/task",
        "https://" + "identity@" + "agent.example/webhooks/task",
        "https://agent.example/",
        "https://agent.example/hook/task",
        "https://agent.example/webhooks/",
        "https://agent.example/webhooks/task/nested",
        "https://agent.example/webhooks/task/",
        "https://agent.example/webhooks/task?secret=value",
    ),
)
def test_receiver_url_fails_closed(url: str, tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        configure_routines(
            email="owner@example.com",
            receiver_url=url,
            receiver_secret=ReceiverSecret("secret"),
            poll_interval_seconds=60,
            path=tmp_path / "routines.json",
        )


def test_config_rejects_unknown_version_and_profile(tmp_path: Path) -> None:
    path = tmp_path / "routines.json"
    path.write_text('{"version": 2, "state": "disabled", "profile": {}}')
    with pytest.raises(ConfigError, match="unsupported version"):
        load_routines_config(path=path)

    assert account_digest(" Owner@Example.com ") == account_digest("owner@example.com")


def test_config_rejects_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "routines.json"
    path.write_text('{"version":1,"state":"enabled","profile":')

    with pytest.raises(ConfigError, match="unreadable"):
        load_routines_config(path=path)


def test_config_error_exception_and_stderr_rendering_are_value_free(
    tmp_path: Path,
) -> None:
    private_values = (
        "private@example.com",
        "private-receiver-secret",
        "https://private.example/webhooks/private-route",
        "private-history-key",
        "private task title",
    )
    payload = _valid_config_payload()
    profile = payload["profile"]
    assert isinstance(profile, dict)
    profile.update(
        {
            "host_profile": "private-invalid-profile",
            "receiver_url": private_values[2],
            "receiver_secret": private_values[1],
            "account_email": private_values[0],
            "history_key": private_values[3],
            "task_title": private_values[4],
        }
    )
    path = tmp_path / "routines.json"
    path.write_text(json.dumps(payload))

    with pytest.raises(ConfigError) as caught:
        load_routines_config(path=path)

    stderr = StringIO()
    with redirect_stderr(stderr):
        traceback.print_exception(caught.value)
    rendered = stderr.getvalue() + str(caught.value) + repr(caught.value)
    for private in private_values:
        assert private not in rendered


@pytest.mark.parametrize(
    ("dotted_path", "value"),
    (
        ("state", "unconfigured"),
        ("profile", []),
        ("profile.host_profile", "personal"),
        ("profile.routine_id", "unknown-routine"),
        ("profile.retry", []),
        ("profile.retry.initial_delay_seconds", 0),
        ("profile.retry.max_delay_seconds", 4),
        ("profile.retry.max_attempts", True),
        ("profile.retry.max_age_seconds", 59),
    ),
)
def test_config_rejects_illegal_state_profile_and_retry_structures(
    dotted_path: str,
    value: object,
    tmp_path: Path,
) -> None:
    payload = _valid_config_payload()
    _replace_nested(payload, dotted_path, value)
    path = tmp_path / "routines.json"
    path.write_text(json.dumps(payload))

    with pytest.raises(ConfigError):
        load_routines_config(path=path)


@pytest.mark.parametrize(
    ("interval", "accepted"),
    ((59, False), (60, True), (3600, True), (3601, False), (True, False)),
)
def test_poll_interval_enforces_inclusive_sixty_to_3600_second_bounds(
    interval: int,
    accepted: bool,
    tmp_path: Path,
) -> None:
    path = tmp_path / "routines.json"
    if not accepted:
        with pytest.raises(ConfigError, match="Polling interval"):
            configure_routines(
                email="owner@example.com",
                receiver_url="https://agent.example/webhooks/task",
                receiver_secret=ReceiverSecret("secret"),
                poll_interval_seconds=interval,
                path=path,
            )
        assert not path.exists()
        return

    configured = configure_routines(
        email="owner@example.com",
        receiver_url="https://agent.example/webhooks/task",
        receiver_secret=ReceiverSecret("secret"),
        poll_interval_seconds=interval,
        path=path,
    )
    assert configured.profile.poll_interval_seconds == interval


def test_atomic_write_failure_preserves_existing_config_and_removes_temp_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "routines.json"
    configured = configure_routines(
        email="owner@example.com",
        receiver_url="https://agent.example/webhooks/task",
        receiver_secret=ReceiverSecret("secret"),
        poll_interval_seconds=60,
        path=path,
    )
    before = path.read_bytes()

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr("things_orchestrator.routines_config.os.replace", fail_replace)

    with pytest.raises(OSError, match="atomic replace failure"):
        set_routines_enabled(True, email="owner@example.com", path=path)

    assert path.read_bytes() == before
    assert load_routines_config(path=path) == configured
    assert list(path.parent.glob(f".{path.name}.*")) == []


def test_cli_reads_secret_only_from_private_tty_and_reports_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_root = tmp_path / "config"
    owner_dir = config_root / "things-orchestrator"
    owner_dir.mkdir(parents=True)
    (owner_dir / "credentials.json").write_text(
        json.dumps(
            {
                "email": "owner@example.com",
                "password": "cloud-secret",
                "mcp_token": "mcp-bearer",
            }
        )
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    @contextmanager
    def tty(_parser: object) -> Iterator[StringIO]:
        yield StringIO()

    prompts: list[str] = []
    answers = iter(("webhook-secret", "webhook-secret"))
    monkeypatch.setattr("things_orchestrator.cli._routine_secret_tty", tty)

    def get_private_value(prompt: str, stream: object = None) -> str:
        del stream
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("things_orchestrator.cli.getpass", get_private_value)

    main(
        [
            "routines",
            "configure",
            "--profile",
            "always_on",
            "--url",
            "https://agent.example/webhooks/task",
            "--interval",
            "60",
        ]
    )
    main(["routines", "enable"])
    enabled = load_routines_config()
    assert isinstance(enabled, EnabledRoutineConfig)
    store = RoutineStore(enabled.profile)
    store.open()
    store.close()
    main(["routines", "status"])
    main(["routines", "disable"])
    output = capsys.readouterr().out

    assert "webhook-secret" not in output
    assert "cloud-secret" not in output
    assert "agent.example" not in output
    assert prompts == [
        "Hermes webhook secret: ",
        "Confirm Hermes webhook secret: ",
    ]
    assert "Restart required: things-orchestrator service install" in output
    status = next(
        json.loads(line)
        for line in output.splitlines()
        if line.startswith("{") and '"phase"' in line
    )
    assert status == {
        "account_bound": True,
        "counts": {
            "candidates": 0,
            "dead": 0,
            "delivered": 0,
            "pending": 0,
        },
        "phase": "uninitialized",
        "receiver_kind": "hermes",
        "state": "enabled",
    }
    assert (owner_dir / "routines.json").stat().st_mode & 0o777 == 0o600


def test_cli_rejects_secret_argv_without_echoing_its_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "routines",
                "configure",
                "--profile",
                "always_on",
                "--url",
                "https://agent.example/webhooks/task",
                "--secret=must-not-render",
            ]
        )
    captured = capsys.readouterr()
    assert "must-not-render" not in captured.out
    assert "must-not-render" not in captured.err
    assert "/dev/tty" in captured.err
