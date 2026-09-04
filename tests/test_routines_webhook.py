from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from things_orchestrator.routines_config import (
    ReceiverSecret,
    RetryPolicy,
    RoutineProfile,
)
from things_orchestrator.routines_store import StoredEvent
from things_orchestrator.routines_webhook import (
    HermesWebhook,
    _classify_http,
    hermes_signature,
)


def _profile(url: str, secret: str = "receiver-secret") -> RoutineProfile:
    return RoutineProfile(
        account_digest="a" * 64,
        host_profile="always_on",
        receiver_url=url,
        receiver_secret=ReceiverSecret(secret),
        poll_interval_seconds=60,
        settle_seconds=120,
        retry=RetryPolicy(),
    )


@pytest.mark.parametrize(
    "status, body, expected_kind, expected_code",
    (
        (202, b'{"status":"accepted"}', "delivered", "accepted"),
        (200, b'{"status":"duplicate"}', "delivered", "duplicate"),
        (202, b'{"status":"duplicate"}', "retry", "ambiguous_2xx"),
        (200, b'{"status":"accepted"}', "retry", "ambiguous_2xx"),
        (204, b"", "retry", "ambiguous_2xx"),
        (202, b"not-json", "retry", "ambiguous_2xx"),
        (302, b"", "permanent", "redirect"),
        (409, b"", "permanent", "client_error"),
        (408, b"", "retry", "retryable_http"),
        (425, b"", "retry", "retryable_http"),
        (429, b"", "retry", "retryable_http"),
        (500, b"", "retry", "retryable_http"),
    ),
)
def test_exact_hermes_response_matrix(
    status: int, body: bytes, expected_kind: str, expected_code: str
) -> None:
    result = _classify_http(status, body, "99999" if status == 429 else None)
    assert (result.kind, result.code) == (expected_kind, expected_code)
    if status == 429:
        assert result.retry_after_seconds == 900


def test_adapter_posts_exact_stored_body_with_fresh_v2_signature() -> None:
    captured: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            captured["body"] = self.rfile.read(length)
            captured["request_id"] = self.headers["X-Request-ID"]
            captured["timestamp"] = self.headers["X-Webhook-Timestamp"]
            captured["signature"] = self.headers["X-Webhook-Signature-V2"]
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"accepted","private":"discard-me"}')

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        body = json.dumps(
            {"event_id": "evt_test", "event_type": "task.created"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        event = StoredEvent("evt_test", "routine", "task", 123, body, attempt_count=0)
        result = HermesWebhook(
            _profile(f"http://127.0.0.1:{server.server_port}/webhooks/task")
        ).deliver(event, timestamp=456)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert result.kind == "delivered"
    assert captured == {
        "body": body,
        "request_id": "evt_test",
        "timestamp": "456",
        "signature": hermes_signature(b"receiver-secret", 456, body),
    }
    assert result.code == "accepted"
    assert "discard-me" not in repr(result)


def test_adapter_never_follows_redirect() -> None:
    requests = 0

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            nonlocal requests
            requests += 1
            self.send_response(302)
            self.send_header("Location", "/acted")
            self.end_headers()

        def do_GET(self) -> None:
            nonlocal requests
            requests += 1
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        event = StoredEvent("evt", "routine", "task", 1, b"{}", 0)
        result = HermesWebhook(
            _profile(f"http://127.0.0.1:{server.server_port}/webhooks/task")
        ).deliver(event, timestamp=2)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert requests == 1
    assert (result.kind, result.code, result.http_status) == (
        "permanent",
        "redirect",
        302,
    )


def test_adapter_bounds_acknowledgement_body() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(202)
            self.end_headers()
            self.wfile.write(b'{"status":"accepted","padding":"too-large"}')

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        event = StoredEvent("evt", "routine", "task", 1, b"{}", 0)
        result = HermesWebhook(
            _profile(f"http://127.0.0.1:{server.server_port}/webhooks/task"),
            max_response_bytes=16,
        ).deliver(event, timestamp=2)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert (result.kind, result.code) == ("retry", "response_too_large")


def test_signature_has_stable_lowercase_hex_vector() -> None:
    signature = hermes_signature(b"secret", 123, b'{"a":1}')
    assert (
        signature == "979e3c2c30ebc0b46dd7165b75ee282921dd508ff4a0b4a4e072ba27b16970ae"
    )
    assert len(signature) == 64
    assert signature == signature.lower()
