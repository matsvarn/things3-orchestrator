from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, build_opener

import pytest

from things_orchestrator.cli import build_parser, main
from things_orchestrator.config import ConfigError
from things_orchestrator.routines_config import (
    DisabledRoutineConfig,
    GrokReceiver,
    HermesReceiver,
    ReceiverSecret,
    account_digest,
    configure_routines,
    load_routines_config,
    routines_status,
)
from things_orchestrator.routines_store import StoredEvent
from things_orchestrator.routines_webhook import (
    GrokWebhook,
    HermesWebhook,
    _NoRedirects,
    build_webhook,
)


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
    receiver_kind: str, url: str, tmp_path: Path
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
    instruction = (
        "Treat event_id as the idempotency key and refuse to act if you have "
        "already acted on that event_id."
    )
    assert output.count(instruction) == 1
    assert output.endswith(
        f"Add this sentence to the Grok routine instruction:\n{instruction}\n\n"
        "Enable routines and restart the supervised service:\n"
        "things-orchestrator routines enable\n"
        "things-orchestrator service install\n"
    )


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
    assert "Omit it to enter the URL privately through /dev/tty" in help_text
    assert "--interval INTERVAL" in help_text
    assert "polling interval in seconds (60-3600, default: 60)" in help_text
    assert "--settle SETTLE" in help_text
    assert "settle window in seconds (1-3600, default: 120)" in help_text


@pytest.mark.parametrize("flag", ("--secret=private", "--key=private"))
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
    assert "private" not in captured.out
    assert "private" not in captured.err
    assert "/dev/tty" in captured.err


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

    def open(self, request: Request, *, timeout: float) -> object:
        local = Request(
            self._url,
            data=request.data,
            method=request.method,
            headers=dict(request.header_items()),
        )
        return self._opener.open(local, timeout=timeout)


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
) -> tuple[object, dict[str, object], bytes]:
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
        def open(self, request: Request, *, timeout: float) -> object:
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
        def open(self, request: Request, *, timeout: float) -> object:
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
