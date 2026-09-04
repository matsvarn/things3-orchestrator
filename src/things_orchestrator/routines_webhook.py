"""Bounded Hermes V2 webhook transport with value-free outcomes."""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
from dataclasses import dataclass
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .routines_config import RoutineProfile
from .routines_store import StoredEvent

DeliveryKind = Literal["delivered", "retry", "permanent"]


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    kind: DeliveryKind
    code: str
    http_status: int | None = None
    retry_after_seconds: int | None = None


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


def hermes_signature(secret: bytes, timestamp: int, body: bytes) -> str:
    message = str(timestamp).encode("ascii") + b"." + body
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


class HermesWebhook:
    def __init__(
        self,
        profile: RoutineProfile,
        *,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 65_536,
    ) -> None:
        if not 0 < timeout_seconds <= 30:
            raise ValueError("Webhook timeout must be between 0 and 30 seconds")
        if not 1 <= max_response_bytes <= 1_048_576:
            raise ValueError("Webhook response bound is invalid")
        self._profile = profile
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._opener = build_opener(_NoRedirects())

    def deliver(self, event: StoredEvent, *, timestamp: int) -> DeliveryResult:
        secret = self._profile.receiver_secret.reveal().encode("utf-8")
        request = Request(
            self._profile.receiver_url,
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
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                status = int(response.status)
                body = response.read(self._max_response_bytes + 1)
        except HTTPError as error:
            return _classify_http(error.code, b"", error.headers.get("Retry-After"))
        except (TimeoutError, socket.timeout, URLError, OSError):
            return DeliveryResult("retry", "network_failure")
        if len(body) > self._max_response_bytes:
            return DeliveryResult("retry", "response_too_large", status)
        return _classify_http(status, body, None)


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
    if (
        status == 200
        and isinstance(acknowledgement, dict)
        and acknowledgement.get("status") == "duplicate"
    ):
        return DeliveryResult("delivered", "duplicate", status)
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
