from __future__ import annotations

import io
import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Literal, Never, cast
from urllib.error import URLError
from urllib.request import Request, build_opener

import pytest

from things_orchestrator.cli import build_parser, main
from things_orchestrator.config import ConfigError
from things_orchestrator.routines_config import (
    ROUTINE_RECEIVER_INSTRUCTION,
    DisabledRoutineConfig,
    EnabledRoutineConfig,
    GrokReceiver,
    HermesReceiver,
    ReceiverKind,
    ReceiverSecret,
    account_digest,
    configure_routines,
    load_routines_config,
    routines_status,
    set_routines_enabled,
)
from things_orchestrator.routines_store import StoredEvent
from things_orchestrator.routines_webhook import (
    DeliveryResult,
    GrokWebhook,
    HermesWebhook,
    _HTTPResponse,
    _NoRedirects,
    build_webhook,
)
from things_orchestrator.service import (
    ServiceApplyError,
    ServiceOperationResult,
    ServiceStatus,
)


def _write_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def _v1_payload() -> dict[str, object]:
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


def test_v1_config_without_receiver_kind_loads_as_hermes(tmp_path: Path) -> None:
    path = tmp_path / "routines.json"
    path.write_text(json.dumps(_v1_payload()))

    loaded = load_routines_config(path=path)

    assert isinstance(loaded, DisabledRoutineConfig)
    assert loaded.profile.receiver == HermesReceiver(
        "https://agent.example/webhooks/task", ReceiverSecret("secret")
    )


def test_grok_profile_persists_explicit_kind_and_redacts_values(
    tmp_path: Path,
) -> None:
    path = tmp_path / "routines.json"
    url = "https://api2.cursor.sh/automations/webhook/opaque-route_123"
    key = "private-grok-key"

    configured = configure_routines(
        email="owner@example.com",
        receiver_kind="grok",
        receiver_url=url,
        receiver_secret=ReceiverSecret(key),
        poll_interval_seconds=60,
        path=path,
    )

    saved = json.loads(path.read_text())
    assert saved["version"] == 1
    assert saved["profile"]["receiver_kind"] == "grok"
    assert saved["profile"]["receiver_url"] == url
    assert saved["profile"]["receiver_secret"] == key
    assert load_routines_config(path=path) == configured
    rendered = json.dumps(routines_status(configured, email="owner@example.com"))
    assert '"receiver_kind": "grok"' in rendered
    assert "api2.cursor.sh" not in rendered
    assert "opaque-route_123" not in rendered
    assert key not in rendered
    assert key not in repr(configured)


@pytest.mark.parametrize(
    "url",
    (
        "http://api2.cursor.sh/automations/webhook/route",
        "https://api.cursor.sh/automations/webhook/route",
        "https://api2.cursor.sh.evil.example/automations/webhook/route",
        "https://api2.cursor.sh./automations/webhook/route",
        "https://user@api2.cursor.sh/automations/webhook/route",
        "https://api2.cursor.sh:443/automations/webhook/route",
        "https://api2.cursor.sh/automations/webhook/",
        "https://api2.cursor.sh/automations/webhook/route/nested",
        "https://api2.cursor.sh/automations/webhooks/route",
        "https://api2.cursor.sh/automations/webhook/route?token=private",
        "https://api2.cursor.sh/automations/webhook/route#fragment",
        "https://api2.cursor.sh/automations/webhook/percent%2Fescape",
    ),
)
def test_grok_receiver_url_fails_closed(url: str, tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        configure_routines(
            email="owner@example.com",
            receiver_kind="grok",
            receiver_url=url,
            receiver_secret=ReceiverSecret("key"),
            poll_interval_seconds=60,
            path=tmp_path / "routines.json",
        )


@pytest.mark.parametrize(
    ("receiver_kind", "url"),
    (
        ("hermes", "https://api2.cursor.sh/automations/webhook/route"),
        ("grok", "https://agent.example/webhooks/route"),
    ),
)
def test_receiver_kind_is_never_inferred_from_url(
    receiver_kind: ReceiverKind, url: str, tmp_path: Path
) -> None:
    with pytest.raises(ConfigError):
        configure_routines(
            email="owner@example.com",
            receiver_kind=receiver_kind,
            receiver_url=url,
            receiver_secret=ReceiverSecret("key"),
            poll_interval_seconds=60,
            path=tmp_path / "routines.json",
        )


def test_cli_prompts_privately_for_grok_url_and_key(
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
    terminal = object()

    @contextmanager
    def tty(_parser: object) -> Iterator[object]:
        yield terminal

    prompts: list[str] = []
    answers = iter(
        (
            "https://api2.cursor.sh/automations/webhook/private-route",
            "private-grok-key",
            "private-grok-key",
        )
    )
    monkeypatch.setattr("things_orchestrator.cli._routine_secret_tty", tty)

    def get_private_value(prompt: str, stream: object = None) -> str:
        prompts.append(prompt)
        assert stream is terminal
        return next(answers)

    monkeypatch.setattr("things_orchestrator.cli.getpass", get_private_value)

    main(
        [
            "routines",
            "configure",
            "--profile",
            "always_on",
            "--receiver",
            "grok",
        ]
    )

    configured = load_routines_config()
    assert isinstance(configured, DisabledRoutineConfig)
    assert configured.profile.receiver.kind == "grok"
    assert prompts == [
        "Grok webhook URL: ",
        "Grok webhook key: ",
        "Confirm Grok webhook key: ",
    ]
    output = capsys.readouterr().out
    for private in (
        "api2.cursor.sh",
        "private-route",
        "private-grok-key",
        "cloud-secret",
    ):
        assert private not in output
    assert output.count(ROUTINE_RECEIVER_INSTRUCTION) == 1
    assert "things-orchestrator routines enable" in output
    assert "things-orchestrator service install" in output
    main(["routines", "status"])
    status = json.loads(capsys.readouterr().out)
    assert status["configuration_state"] == "disabled"
    assert status["account_binding"] == "bound"
    assert status["receiver_kind"] == "grok"
    assert status["worker_liveness"] in {"stopped", "unknown"}


def test_routines_configure_help_explains_private_defaults_and_each_option(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(["routines", "configure", "--help"])

    assert caught.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "Store a disabled, account-bound routines receiver profile" in help_text
    assert "--profile {always_on}" in help_text
    assert "supervised routines worker" in help_text
    assert "--receiver {hermes,grok}" in help_text
    assert "Hermes is the default" in help_text
    assert "--url URL" in help_text
    assert "Omit it to enter the URL privately in a private terminal" in help_text
    assert "--interval INTERVAL" in help_text
    assert "polling interval in seconds (60-3600, default: 60)" in help_text
    assert "--settle SETTLE" in help_text
    assert "settle window in seconds (1-3600, default: 120)" in help_text


def test_routines_setup_help_is_portable_private_happy_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(["routines", "setup", "--help"])

    assert caught.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "Configure, enable, and install the supervised routines service" in help_text
    assert "--profile {always_on}" in help_text
    assert "--receiver {hermes,grok}" in help_text
    assert "Hermes is the default" in help_text
    assert "--interval INTERVAL" in help_text
    assert "--settle SETTLE" in help_text
    assert "--url" not in help_text


@pytest.mark.parametrize(
    ("action", "phrase"),
    (
        ("enable", "does not start a worker until the service restarts"),
        ("disable", "without deleting configuration, candidates, or delivery history"),
        ("status", "authenticated loopback health probe"),
    ),
)
def test_routines_state_command_help_names_its_effect(
    capsys: pytest.CaptureFixture[str], action: str, phrase: str
) -> None:
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(["routines", action, "--help"])

    assert caught.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert phrase in help_text
    if action == "status":
        assert "When an MCP bearer is configured" in help_text


def test_routines_status_uses_one_authenticated_runtime_probe_without_leaking_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_credentials(tmp_path, monkeypatch)
    configure_routines(
        email="owner@example.com",
        receiver_kind="grok",
        receiver_url="https://api2.cursor.sh/automations/webhook/private-route",
        receiver_secret=ReceiverSecret("private-grok-key"),
        poll_interval_seconds=60,
    )
    set_routines_enabled(True, email="owner@example.com")
    probes: list[str | None] = []
    monkeypatch.setattr(
        "things_orchestrator.cli.collect_service_state", lambda: "active"
    )

    def probe(bearer: str | None) -> dict[str, object]:
        probes.append(bearer)
        return {
            "state": "running",
            "last_successful_poll_at": 80,
            "private": "must-not-render",
        }

    monkeypatch.setattr("things_orchestrator.cli.probe_routine_runtime", probe)

    main(["routines", "status"])

    assert probes == ["mcp-bearer"]
    output = capsys.readouterr().out
    status = json.loads(output)
    assert status["service_state"] == "active"
    assert status["worker_liveness"] == "running"
    assert status["last_successful_poll_at"] == 80
    for private in (
        "must-not-render",
        "mcp-bearer",
        "private-route",
        "private-grok-key",
        "owner@example.com",
    ):
        assert private not in output


def test_routines_status_without_mcp_bearer_attempts_no_runtime_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_credentials(tmp_path, monkeypatch)
    credentials_path = (
        tmp_path / "config/things-orchestrator/credentials.json"
    )
    credentials_path.write_text(
        json.dumps({"email": "owner@example.com", "password": "cloud-secret"})
    )
    probes: list[str | None] = []
    monkeypatch.setattr(
        "things_orchestrator.cli.collect_service_state", lambda: "active"
    )

    def probe(bearer: str | None) -> None:
        probes.append(bearer)

    monkeypatch.setattr("things_orchestrator.cli.probe_routine_runtime", probe)

    main(["routines", "status"])

    assert probes == []
    status = json.loads(capsys.readouterr().out)
    assert status["worker_liveness"] == "unknown"


@pytest.mark.parametrize(
    "arguments",
    (
        ["--url", "private-route-value"],
        ["--url=private-route-value"],
    ),
)
def test_routines_setup_rejects_url_argv_without_echoing_value(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as caught:
        main(
            [
                "routines",
                "setup",
                "--profile",
                "always_on",
                *arguments,
            ]
        )

    assert caught.value.code == 2
    captured = capsys.readouterr()
    assert "private-route-value" not in captured.out
    assert "private-route-value" not in captured.err
    assert "private terminal" in captured.err


def test_routines_setup_grok_guides_private_prompts_and_orders_actions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_credentials(tmp_path, monkeypatch)
    terminal = io.StringIO()

    @contextmanager
    def tty(_parser: object) -> Iterator[io.StringIO]:
        yield terminal

    prompts: list[str] = []
    answers = iter(
        (
            "https://api2.cursor.sh/automations/webhook/private-route",
            "private-grok-key",
            "private-grok-key",
        )
    )
    monkeypatch.setattr("things_orchestrator.cli._routine_secret_tty", tty)

    def get_private_value(prompt: str, stream: object = None) -> str:
        prompts.append(prompt)
        assert stream is terminal
        return next(answers)

    monkeypatch.setattr("things_orchestrator.cli.getpass", get_private_value)
    calls: list[str] = []
    original_configure = configure_routines

    def ordered_configure(
        *,
        email: str,
        receiver_kind: ReceiverKind = "hermes",
        receiver_url: str,
        receiver_secret: ReceiverSecret,
        poll_interval_seconds: int,
        settle_seconds: int = 120,
        path: Path | None = None,
    ) -> DisabledRoutineConfig:
        calls.append("configure")
        return original_configure(
            email=email,
            receiver_kind=receiver_kind,
            receiver_url=receiver_url,
            receiver_secret=receiver_secret,
            poll_interval_seconds=poll_interval_seconds,
            settle_seconds=settle_seconds,
            path=path,
        )

    original_enable = set_routines_enabled

    def ordered_enable(
        enabled: bool, *, email: str, path: Path | None = None
    ) -> DisabledRoutineConfig | EnabledRoutineConfig:
        calls.append("enable")
        return original_enable(enabled, email=email, path=path)

    def ordered_service(action: str, *, dry_run: bool) -> ServiceOperationResult:
        calls.append("service")
        assert (action, dry_run) == ("install", False)
        return ServiceOperationResult("install", ServiceStatus.ACTIVE, (), True)

    monkeypatch.setattr("things_orchestrator.cli.configure_routines", ordered_configure)
    monkeypatch.setattr("things_orchestrator.cli.set_routines_enabled", ordered_enable)
    monkeypatch.setattr("things_orchestrator.cli.service_action", ordered_service)

    main(
        [
            "routines",
            "setup",
            "--profile",
            "always_on",
            "--receiver",
            "grok",
        ]
    )

    assert calls == ["configure", "enable", "service"]
    assert prompts == [
        "Grok Bot webhook POST URL: ",
        "Grok Bot webhook key: ",
        "Confirm Grok Bot webhook key: ",
    ]
    guidance = terminal.getvalue()
    assert "print-config --client grok --show-secrets" in guidance
    assert "grok.com/connectors" in guidance
    assert "New Connector" in guidance
    assert "Custom" in guidance
    assert "exactly eight tools, including things_get" in guidance
    assert 'choose "When a webhook fires"' in guidance
    assert "save it before copying the generated POST URL and key" in guidance
    assert "Do not put the URL or key in argv or chat" in guidance
    assert "trigger_ready=true" in guidance
    assert ROUTINE_RECEIVER_INSTRUCTION in guidance
    output = capsys.readouterr().out
    for private in (
        "api2.cursor.sh",
        "private-route",
        "private-grok-key",
        "cloud-secret",
    ):
        assert private not in output
        assert private not in guidance
    assert '"configuration_state": "enabled"' in output
    assert "Next readiness check" in output
    assert "Then turn the saved Grok Routine Active" in output
    assert ROUTINE_RECEIVER_INSTRUCTION in output


def test_routines_setup_service_failure_leaves_enabled_for_direct_service_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_credentials(tmp_path, monkeypatch)
    terminal = io.StringIO()

    @contextmanager
    def tty(_parser: object) -> Iterator[io.StringIO]:
        yield terminal

    monkeypatch.setattr("things_orchestrator.cli._routine_secret_tty", tty)
    answers = iter(
        (
            "https://agent.example/webhooks/task",
            "hermes-secret",
            "hermes-secret",
        )
    )
    prompts: list[str] = []

    def get_private_value(prompt: str, stream: object = None) -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("things_orchestrator.cli.getpass", get_private_value)
    service_calls = 0

    def flaky_service(action: str, *, dry_run: bool) -> ServiceOperationResult:
        nonlocal service_calls
        service_calls += 1
        if service_calls == 1:
            raise ServiceApplyError("private service detail")
        return ServiceOperationResult("install", ServiceStatus.ACTIVE, (), True)

    monkeypatch.setattr("things_orchestrator.cli.service_action", flaky_service)
    command = ["routines", "setup", "--profile", "always_on"]

    with pytest.raises(SystemExit) as caught:
        main(command)

    assert caught.value.code == 2
    assert load_routines_config().state == "enabled"
    failure = capsys.readouterr()
    assert "private service detail" not in failure.err
    assert "things-orchestrator service install" in failure.err
    assert "saved receiver values do not need to be entered again" in failure.err
    assert "rerun this setup command" not in failure.err

    main(["service", "install"])
    assert service_calls == 2
    assert load_routines_config().state == "enabled"
    assert "Grok" not in terminal.getvalue()
    assert prompts == [
        "Hermes webhook URL: ",
        "Hermes webhook secret: ",
        "Confirm Hermes webhook secret: ",
    ]
    assert "hermes gateway setup" in terminal.getvalue()
    assert "hermes webhook subscribe things-ai-task-created" in terminal.getvalue()
    assert "webhook_subscriptions.json" in terminal.getvalue()
    assert '"toolsets": ["mcp-things"]' in terminal.getvalue()
    assert "hermes webhook test" not in terminal.getvalue()
    assert "positive selected-task smoke test" in terminal.getvalue()
    assert "Anyone with the route's HMAC secret" in terminal.getvalue()
    assert ROUTINE_RECEIVER_INSTRUCTION in terminal.getvalue()
    assert "service: active" in capsys.readouterr().out


def test_routines_setup_missing_credentials_precedes_prompt_and_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    def forbidden_tty(_parser: object) -> Never:
        pytest.fail("prompted before credentials")

    def forbidden_service(
        action: Literal["install", "uninstall", "status"], *, dry_run: bool
    ) -> Never:
        del action, dry_run
        pytest.fail("service called before credentials")

    monkeypatch.setattr("things_orchestrator.cli._routine_secret_tty", forbidden_tty)
    monkeypatch.setattr("things_orchestrator.cli.service_action", forbidden_service)

    with pytest.raises(SystemExit) as caught:
        main(["routines", "setup", "--profile", "always_on"])

    assert caught.value.code == 2
    captured = capsys.readouterr()
    assert "Run `things-orchestrator login`" in captured.err
    assert "webhook" not in captured.out


@pytest.mark.parametrize(
    "flag", ("--secret=must-not-render", "--key=must-not-render")
)
def test_cli_rejects_receiver_credentials_in_argv_without_echoing_value(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "routines",
                "configure",
                "--profile",
                "always_on",
                "--receiver",
                "grok",
                flag,
            ]
        )
    captured = capsys.readouterr()
    assert "must-not-render" not in captured.out
    assert "must-not-render" not in captured.err
    assert "private terminal" in captured.err


def test_webhook_factory_selects_exact_adapter() -> None:
    hermes = HermesReceiver(
        "https://agent.example/webhooks/task", ReceiverSecret("secret")
    )
    grok = GrokReceiver(
        "https://api2.cursor.sh/automations/webhook/route", ReceiverSecret("key")
    )

    assert isinstance(build_webhook(hermes), HermesWebhook)
    assert isinstance(build_webhook(grok), GrokWebhook)


class _RewritingOpener:
    def __init__(self, url: str) -> None:
        self._url = url
        self._opener = build_opener(_NoRedirects())

    def open(self, request: Request, *, timeout: float) -> _HTTPResponse:
        local = Request(
            self._url,
            data=request.data,
            method=request.method,
            headers=dict(request.header_items()),
        )
        return cast(_HTTPResponse, self._opener.open(local, timeout=timeout))


@contextmanager
def _grok_server(*, status: int, body: bytes) -> Iterator[tuple[str, dict[str, object]]]:
    captured: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            captured["body"] = self.rfile.read(length)
            captured["authorization"] = self.headers.get("Authorization")
            captured["request_id"] = self.headers.get("X-Request-ID")
            captured["timestamp"] = self.headers.get("X-Webhook-Timestamp")
            captured["signature"] = self.headers.get("X-Webhook-Signature-V2")
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/test", captured
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _deliver_to_local_grok(
    *, status: int, response_body: bytes, max_response_bytes: int = 65_536
) -> tuple[DeliveryResult, dict[str, object], bytes]:
    event_body = b'{"event_id":"evt_test","event_type":"task.created"}'
    event = StoredEvent("evt_test", "routine", "task", 1, event_body, 0)
    receiver = GrokReceiver(
        "https://api2.cursor.sh/automations/webhook/route",
        ReceiverSecret("grok-key"),
    )
    with _grok_server(status=status, body=response_body) as (url, captured):
        result = GrokWebhook(
            receiver,
            max_response_bytes=max_response_bytes,
            _opener=_RewritingOpener(url),
        ).deliver(event, timestamp=2)
    return result, captured, event_body


def test_local_fake_grok_server_receives_exact_authorization_and_body() -> None:
    result, captured, event_body = _deliver_to_local_grok(
        status=200,
        response_body=b'{"success":true,"runUuid":"run-123","private":"discard"}',
    )

    assert (result.kind, result.code, result.http_status) == (
        "delivered",
        "accepted",
        200,
    )
    assert captured == {
        "body": event_body,
        "authorization": "Bearer grok-key",
        "request_id": None,
        "timestamp": None,
        "signature": None,
    }
    assert "discard" not in repr(result)


@pytest.mark.parametrize(
    ("status", "body", "kind", "code"),
    (
        (200, b'{"success":true,"runUuid":"run"}', "delivered", "accepted"),
        (200, b'{"success":true}', "retry", "ambiguous_2xx"),
        (200, b'{"success":true,"runUuid":""}', "retry", "ambiguous_2xx"),
        (200, b'{"success":1,"runUuid":"run"}', "retry", "ambiguous_2xx"),
        (200, b"not-json", "retry", "ambiguous_2xx"),
        (201, b'{"success":true,"runUuid":"run"}', "retry", "ambiguous_2xx"),
        (202, b'{"success":true,"runUuid":"run"}', "retry", "ambiguous_2xx"),
        (204, b"", "retry", "ambiguous_2xx"),
        (302, b"private redirect body", "permanent", "redirect"),
        (400, b"private error body", "permanent", "client_error"),
        (408, b"", "retry", "retryable_http"),
        (425, b"", "retry", "retryable_http"),
        (429, b"private throttling body", "retry", "retryable_http"),
        (500, b"private server body", "retry", "retryable_http"),
    ),
)
def test_grok_exact_acknowledgement_and_http_matrix(
    status: int, body: bytes, kind: str, code: str
) -> None:
    result, _captured, _event_body = _deliver_to_local_grok(
        status=status, response_body=body
    )

    assert (result.kind, result.code) == (kind, code)
    if body:
        assert body.decode(errors="ignore") not in repr(result)


def test_grok_response_bound_and_network_failure_are_value_free() -> None:
    oversized, _captured, _event_body = _deliver_to_local_grok(
        status=200,
        response_body=b'{"success":true,"runUuid":"private-too-large"}',
        max_response_bytes=16,
    )
    receiver = GrokReceiver(
        "https://api2.cursor.sh/automations/webhook/route",
        ReceiverSecret("private-key"),
    )

    class FailingOpener:
        def open(self, request: Request, *, timeout: float) -> Never:
            del request, timeout
            raise URLError(socket.timeout("private network detail"))

    network = GrokWebhook(receiver, _opener=FailingOpener()).deliver(
        StoredEvent("evt", "routine", "task", 1, b"{}", 0), timestamp=2
    )

    assert (oversized.kind, oversized.code) == ("retry", "response_too_large")
    assert (network.kind, network.code) == ("retry", "network_failure")
    rendered = repr(oversized) + repr(network)
    for private in ("private-too-large", "private-key", "private network detail"):
        assert private not in rendered


def test_grok_transport_uses_ten_second_default_and_thirty_second_maximum() -> None:
    receiver = GrokReceiver(
        "https://api2.cursor.sh/automations/webhook/route",
        ReceiverSecret("key"),
    )
    observed_timeouts: list[float] = []

    class TimeoutOpener:
        def open(self, request: Request, *, timeout: float) -> Never:
            del request
            observed_timeouts.append(timeout)
            raise URLError("offline")

    event = StoredEvent("evt", "routine", "task", 1, b"{}", 0)
    GrokWebhook(receiver, _opener=TimeoutOpener()).deliver(event, timestamp=2)
    GrokWebhook(
        receiver, timeout_seconds=30, _opener=TimeoutOpener()
    ).deliver(event, timestamp=2)

    assert observed_timeouts == [10.0, 30]
    with pytest.raises(ValueError, match="timeout"):
        GrokWebhook(receiver, timeout_seconds=30.01)
