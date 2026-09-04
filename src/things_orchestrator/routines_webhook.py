from __future__ import annotations

import hashlib
import hmac
import json
import socket
from dataclasses import dataclass
from types import TracebackType
from typing import Literal, Protocol, assert_never
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .routines_config import GrokReceiver, HermesReceiver, Receiver
from .routines_store import StoredEvent

DeliveryKind = Literal["delivered", "retry", "permanent"]


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    kind: DeliveryKind
    code: str
    http_status: int | None = None
    retry_after_seconds: int | None = None


class Webhook(Protocol):
    def deliver(
        self, event: StoredEvent, *, timestamp: int
    ) -> DeliveryResult: ...


class _HTTPResponse(Protocol):
    status: int

    def read(self, amount: int = -1) -> bytes: ...

    def __enter__(self) -> _HTTPResponse: ...

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class RoutineHTTPOpener(Protocol):
    def open(
        self, request: Request, *, timeout: float
    ) -> _HTTPResponse: ...


@dataclass(frozen=True, slots=True)
class _HTTPOutcome:
    status: int
    body: bytes
    retry_after: str | None


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def proxyless_no_redirect_opener() -> RoutineHTTPOpener:
    return build_opener(ProxyHandler({}), _NoRedirects())


def hermes_signature(secret: bytes, timestamp: int, body: bytes) -> str:
    message = str(timestamp).encode("ascii") + b"." + body
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


class HermesWebhook:
    def __init__(
        self,
        receiver: HermesReceiver,
        *,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 65_536,
        _opener: RoutineHTTPOpener | None = None,
    ) -> None:
        _validate_transport_bounds(timeout_seconds, max_response_bytes)
        self._receiver = receiver
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._opener = _opener or proxyless_no_redirect_opener()

    def deliver(self, event: StoredEvent, *, timestamp: int) -> DeliveryResult:
        secret = self._receiver.secret.reveal().encode("utf-8")
        request = Request(
            self._receiver.url,
            data=event.body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Request-ID": event.event_id,
                "X-Webhook-Timestamp": str(timestamp),
                "X-Webhook-Signature-V2": hermes_signature(
                    secret, timestamp, event.body
                ),
            },
        )
        outcome = _send_bounded(
            self._opener,
            request,
            timeout_seconds=self._timeout_seconds,
            max_response_bytes=self._max_response_bytes,
        )
        if isinstance(outcome, DeliveryResult):
            return outcome
        return _classify_http(outcome.status, outcome.body, outcome.retry_after)


class GrokWebhook:
    def __init__(
        self,
        receiver: GrokReceiver,
        *,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 65_536,
        _opener: RoutineHTTPOpener | None = None,
    ) -> None:
        _validate_transport_bounds(timeout_seconds, max_response_bytes)
        self._receiver = receiver
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._opener = _opener or proxyless_no_redirect_opener()

    def deliver(self, event: StoredEvent, *, timestamp: int) -> DeliveryResult:
        del timestamp
        request = Request(
            self._receiver.url,
            data=event.body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._receiver.key.reveal()}",
                "Content-Type": "application/json",
            },
        )
        outcome = _send_bounded(
            self._opener,
            request,
            timeout_seconds=self._timeout_seconds,
            max_response_bytes=self._max_response_bytes,
        )
        if isinstance(outcome, DeliveryResult):
            return outcome
        return _classify_grok_http(
            outcome.status, outcome.body, outcome.retry_after
        )


def build_webhook(receiver: Receiver) -> Webhook:
    if isinstance(receiver, HermesReceiver):
        return HermesWebhook(receiver)
    if isinstance(receiver, GrokReceiver):
        return GrokWebhook(receiver)
    assert_never(receiver)


def _validate_transport_bounds(
    timeout_seconds: float, max_response_bytes: int
) -> None:
    if not 0 < timeout_seconds <= 30:
        raise ValueError("Webhook timeout must be between 0 and 30 seconds")
    if not 1 <= max_response_bytes <= 1_048_576:
        raise ValueError("Webhook response bound is invalid")


def _send_bounded(
    opener: RoutineHTTPOpener,
    request: Request,
    *,
    timeout_seconds: float,
    max_response_bytes: int,
) -> _HTTPOutcome | DeliveryResult:
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            status = int(response.status)
            body = response.read(max_response_bytes + 1)
    except HTTPError as error:
        return _HTTPOutcome(
            error.code,
            b"",
            error.headers.get("Retry-After"),
        )
    except (TimeoutError, socket.timeout, URLError, OSError):
        return DeliveryResult("retry", "network_failure")
    if len(body) > max_response_bytes:
        return DeliveryResult("retry", "response_too_large", status)
    return _HTTPOutcome(status, body, None)


def _classify_http(status: int, body: bytes, retry_after: str | None) -> DeliveryResult:
    acknowledgement: object = None
    if status in {200, 202}:
        try:
            acknowledgement = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            acknowledgement = None
    if (
        status == 202
        and isinstance(acknowledgement, dict)
        and acknowledgement.get("status") == "accepted"
    ):
        return DeliveryResult("delivered", "accepted", status)
    if status == 200 and isinstance(acknowledgement, dict):
        acknowledgement_status = acknowledgement.get("status")
        if acknowledgement_status == "delivered":
            return DeliveryResult("delivered", "delivered", status)
        if acknowledgement_status == "duplicate":
            return DeliveryResult("delivered", "duplicate", status)
    return _classify_non_success(status, retry_after)


def _classify_grok_http(
    status: int, body: bytes, retry_after: str | None
) -> DeliveryResult:
    acknowledgement: object = None
    if status == 200:
        try:
            acknowledgement = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            acknowledgement = None
    if isinstance(acknowledgement, dict):
        run_uuid = acknowledgement.get("runUuid")
        if (
            acknowledgement.get("success") is True
            and isinstance(run_uuid, str)
            and bool(run_uuid)
        ):
            return DeliveryResult("delivered", "accepted", status)
    return _classify_non_success(status, retry_after)


def _classify_non_success(
    status: int, retry_after: str | None
) -> DeliveryResult:
    if 200 <= status < 300:
        return DeliveryResult("retry", "ambiguous_2xx", status)
    if status in {408, 425, 429} or 500 <= status < 600:
        return DeliveryResult(
            "retry",
            "retryable_http",
            status,
            _bounded_retry_after(retry_after),
        )
    if 300 <= status < 400:
        return DeliveryResult("permanent", "redirect", status)
    if 400 <= status < 500:
        return DeliveryResult("permanent", "client_error", status)
    return DeliveryResult("retry", "unexpected_http", status)


def _bounded_retry_after(value: str | None) -> int | None:
    if value is None or not value.isascii() or not value.isdigit():
        return None
    seconds = int(value)
    return min(seconds, 900)
