"""Authenticated deployment and MCP protocol diagnostics."""

from __future__ import annotations

import shlex
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

import anyio
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

from .config import McpBearer, McpUrl
from .deployment import (
    DeploymentIdentity,
    installed_identity,
    tool_contract_hash,
    tool_schema_hash,
)
from .v2 import MODELS


class DoctorFailure(RuntimeError):
    """A target is reachable but does not match the installed server contract."""


class DoctorUnavailable(DoctorFailure):
    """A target is not listening yet and may become ready during --wait."""


@dataclass(frozen=True)
class TargetReceipt:
    url: McpUrl
    public_health: dict[str, object]
    detailed_health: dict[str, object]
    server_info: Implementation
    tool_names: tuple[str, ...]


@dataclass(frozen=True)
class DoctorReport:
    identity: DeploymentIdentity
    targets: tuple[TargetReceipt, ...]


TargetProbe = Callable[[McpUrl, McpBearer], Awaitable[TargetReceipt]]


def validate_target(
    receipt: TargetReceipt,
    identity: DeploymentIdentity,
) -> None:
    if receipt.public_health != {"ok": True}:
        raise DoctorFailure("public /health disclosed more than liveness")
    if receipt.detailed_health.get("ok") is not True:
        raise DoctorFailure("authenticated /health did not report ok")
    if receipt.server_info.version != identity.version:
        raise DoctorFailure("MCP initialize version differs from the installed version")
    if receipt.detailed_health.get("version") != identity.version:
        raise DoctorFailure("health version differs from the installed version")
    if receipt.tool_names != tuple(MODELS):
        raise DoctorFailure("MCP tools/list did not return the exact eight tools")
    if receipt.detailed_health.get("tool_schema_hash") != tool_schema_hash():
        raise DoctorFailure("health schema hash differs from the local schema hash")
    if receipt.detailed_health.get("tool_contract_hash") != tool_contract_hash():
        raise DoctorFailure("health contract hash differs from the local contract hash")
    if identity.commit is None:
        raise DoctorFailure("installed commit is unknown; reinstall from an exact Git tag")
    if receipt.detailed_health.get("commit") != identity.commit:
        raise DoctorFailure("service: stale - restart")


async def probe_target(url: McpUrl, bearer: McpBearer) -> TargetReceipt:
    try:
        async with httpx2.AsyncClient(timeout=5.0) as public_client:
            public_response = await public_client.get(url.health)
            public_response.raise_for_status()
            public_payload = public_response.json()
        headers = {"Authorization": f"Bearer {bearer.reveal()}"}
        async with httpx2.AsyncClient(headers=headers, timeout=10.0) as client:
            detailed_response = await client.get(url.health)
            if detailed_response.status_code == 401:
                raise DoctorFailure(f"{url}: stored bearer was rejected")
            detailed_response.raise_for_status()
            detailed_payload = detailed_response.json()
            async with streamable_http_client(
                str(url), http_client=client
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    initialized = await session.initialize()
                    tools = await session.list_tools()
    except DoctorFailure:
        raise
    except Exception as error:
        failure = DoctorUnavailable if _is_unavailable(error) else DoctorFailure
        raise failure(f"{url}: authenticated MCP round trip failed: {_message(error)}") from None
    if not isinstance(public_payload, dict) or not isinstance(detailed_payload, dict):
        raise DoctorFailure(f"{url}: health endpoint returned no JSON object")
    return TargetReceipt(
        url=url,
        public_health=dict(public_payload),
        detailed_health=dict(detailed_payload),
        server_info=initialized.server_info,
        tool_names=tuple(tool.name for tool in tools.tools),
    )


async def run_doctor(
    targets: Sequence[McpUrl],
    bearer: McpBearer,
    *,
    wait: bool,
    probe: TargetProbe = probe_target,
) -> DoctorReport:
    identity = installed_identity()
    receipts: list[TargetReceipt] = []
    for target in targets:
        deadline = time.monotonic() + 15 if wait and _is_loopback(target) else 0
        while True:
            try:
                receipt = await probe(target, bearer)
                break
            except DoctorUnavailable:
                if time.monotonic() >= deadline:
                    raise
                await anyio.sleep(1)
        validate_target(receipt, identity)
        receipts.append(receipt)
    return DoctorReport(identity=identity, targets=tuple(receipts))


def curl_tool_count_command(url: McpUrl) -> str:
    return (
        f"curl -fsS -X POST {shlex.quote(str(url))} "
        "-H \"Authorization: Bearer $THINGS_MCP_TOKEN\" "
        "-H 'Content-Type: application/json' "
        "-H 'Accept: application/json, text/event-stream' "
        "--data '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{}}' "
        "| sed -n 's/^data: //p' | jq '.result.tools | length'"
    )


def _is_loopback(url: McpUrl) -> bool:
    return url.origin.startswith(("http://127.0.0.1:", "http://localhost:", "http://[::1]:"))


def _message(error: BaseException) -> str:
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(_message(item) for item in error.exceptions)
    return str(error) or type(error).__name__


def _is_unavailable(error: BaseException) -> bool:
    if isinstance(
        error,
        (httpx2.ConnectError, httpx2.ConnectTimeout, ConnectionError),
    ):
        return True
    if isinstance(error, BaseExceptionGroup):
        return any(_is_unavailable(item) for item in error.exceptions)
    return False
