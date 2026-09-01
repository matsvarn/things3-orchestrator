from __future__ import annotations

import argparse
import json
import os
import re
from functools import partial
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

import anyio
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from things_orchestrator.cloud import load_credentials
from things_orchestrator.deployment import package_version
from things_orchestrator.live_acceptance import AcceptanceFailure, LiveAcceptanceRunner


class SessionToolClient:
    def __init__(self, session: ClientSession) -> None:
        self.session = session

    async def call_tool(
        self, name: str, arguments: dict[str, object]
    ) -> dict[str, Any]:
        result = await self.session.call_tool(name, arguments)
        structured = result.structured_content
        if not isinstance(structured, dict):
            raise RuntimeError(f"{name} returned no structured result")
        return cast(dict[str, Any], structured)


def acceptance_urls(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/mcp"
    ):
        raise ValueError("URL needs one uncredentialed exact /mcp endpoint")
    if parsed.scheme == "http" and parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("remote live acceptance requires HTTPS")
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("URL has an invalid port") from error
    mcp = parsed._replace(path="/mcp/")
    health = parsed._replace(path="/health")
    return urlunsplit(mcp), urlunsplit(health)


def summary_exit_code(summary: dict[str, object]) -> int:
    if summary.get("state") == "cleaned" and summary.get("passed") is True:
        return 0
    return 1


def acceptance_failure_message(error: BaseException) -> str | None:
    if isinstance(error, AcceptanceFailure):
        return str(error)
    if not isinstance(error, BaseExceptionGroup):
        return None
    matching, remainder = error.split(AcceptanceFailure)
    if matching is None or remainder is not None:
        return None
    messages: list[str] = []
    pending: list[BaseException] = list(reversed(matching.exceptions))
    while pending:
        current = pending.pop()
        if isinstance(current, AcceptanceFailure):
            if str(current) not in messages:
                messages.append(str(current))
        elif isinstance(current, BaseExceptionGroup):
            pending.extend(reversed(current.exceptions))
    return "; ".join(messages) if messages else None


async def run(
    *,
    url: str,
    health_url: str,
    state_path: Path,
    token: str,
    expect_commit: str,
) -> dict[str, object]:
    expected_version = package_version()
    async with httpx2.AsyncClient(timeout=10.0) as health_client:
        response = await health_client.get(health_url)
        response.raise_for_status()
        health = response.json()
    if not isinstance(health, dict):
        raise RuntimeError("health endpoint returned no object")
    if health.get("commit") != expect_commit:
        raise RuntimeError("health commit differs from --expect-commit")
    if health.get("version") != expected_version:
        raise RuntimeError("health version differs from the local candidate")

    headers = {"Authorization": f"Bearer {token}"}
    async with httpx2.AsyncClient(headers=headers, timeout=30.0) as http_client:
        async with streamable_http_client(url, http_client=http_client) as streams:
            async with ClientSession(*streams) as session:
                initialized = await session.initialize()
                if initialized.server_info.version != expected_version:
                    raise RuntimeError(
                        "MCP initialize version differs from the local candidate"
                    )
                result = await LiveAcceptanceRunner(
                    SessionToolClient(session),
                    state_path,
                    target={"url": url, "commit": expect_commit},
                ).run()
                return {
                    "server_version": initialized.server_info.version,
                    **result,
                }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="exact HTTPS or loopback /mcp URL")
    parser.add_argument(
        "--expect-commit",
        required=True,
        help="exact 40-character deployed Git commit from /health",
    )
    parser.add_argument(
        "--state",
        required=True,
        type=Path,
        help="private mode-0600 state file reused for every resume",
    )
    parser.add_argument(
        "--live-write-acceptance",
        action="store_true",
        help="acknowledge disposable live writes and recoverable Trash cleanup",
    )
    args = parser.parse_args()
    if not args.live_write_acceptance:
        parser.error("live writes need --live-write-acceptance")
    if re.fullmatch(r"[0-9a-f]{40}", args.expect_commit) is None:
        parser.error("--expect-commit needs one lowercase 40-character Git SHA")
    try:
        mcp_url, health_url = acceptance_urls(args.url)
    except ValueError as error:
        parser.error(str(error))
    token = os.environ.get("THINGS_MCP_TOKEN")
    if token is None:
        _email, _password, token = load_credentials()
    if not token:
        parser.error("live acceptance needs an MCP bearer")
    try:
        summary = anyio.run(
            partial(
                run,
                url=mcp_url,
                health_url=health_url,
                state_path=args.state,
                token=token,
                expect_commit=args.expect_commit,
            )
        )
    except (AcceptanceFailure, ExceptionGroup) as error:
        message = acceptance_failure_message(error)
        if message is None:
            raise
        print(
            json.dumps(
                {"error": message, "passed": False, "state": "failed"},
                indent=2,
                sort_keys=True,
            )
        )
        raise SystemExit(1) from None
    print(json.dumps(summary, indent=2, sort_keys=True))
    raise SystemExit(summary_exit_code(summary))


if __name__ == "__main__":
    main()
