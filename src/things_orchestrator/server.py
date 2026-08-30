from __future__ import annotations

import hmac
import logging
import re
from contextlib import asynccontextmanager
from secrets import token_urlsafe
from typing import Any

import anyio
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

from .deployment import health_payload, package_version
from .v2 import (
    DESCRIPTIONS,
    DISCOVERY_MODELS,
    MODELS,
    PublicIssue,
    PublicResult,
    ThingsV2,
    flat_schema,
)
from .workspace import ThingsWorkspace

_LOGGER = logging.getLogger("things_orchestrator")

_FIELD_REPAIR = {
    "request_id": "request_id needs one opaque UUID or ULID",
    "start": (
        "start accepts today, evening, tomorrow, someday, an ISO date, or null"
    ),
    "deadline": "deadline needs an ISO date or null",
    "remind_at": "remind_at needs an ISO date-time with an explicit offset or null",
    "into_id": "into_id needs one exact project:<id> or area:<id>",
}

_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_IDEMPOTENT_WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)

_TOOL_NAMES = tuple(MODELS)
_READ_NAMES = frozenset(("things_view", "things_find", "things_get", "things_receipt"))
_TOOLS = tuple(
    Tool(
        name=name,
        description=DESCRIPTIONS[name],
        input_schema=flat_schema(DISCOVERY_MODELS[name]),
        output_schema=flat_schema(PublicResult),
        annotations=_READ_ONLY if name in _READ_NAMES else _IDEMPOTENT_WRITE,
    )
    for name in _TOOL_NAMES
)


class ThingsMCPServer:
    name = "things"

    def __init__(self, workspace: ThingsWorkspace | ThingsV2) -> None:
        self._interface = workspace if isinstance(workspace, ThingsV2) else ThingsV2(workspace)
        self._lock = anyio.Lock()
        self._tools_only_server: Server[object] = Server(
            name=self.name,
            version=package_version(),
            on_list_tools=self._list_wire_tools,
            on_call_tool=self._call_wire_tool,
        )

    async def list_tools(self) -> list[Tool]:
        return [tool.model_copy(deep=True) for tool in _TOOLS]

    def _dispatch(self, name: str, arguments: dict[str, Any]) -> PublicResult:
        return self._interface.dispatch(name, arguments)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        try:
            if name not in {tool.name for tool in _TOOLS}:
                return _domain_result(PublicResult(
                    state="rejected", code="unknown_tool", next_action="correct_request",
                    instruction="That tool is not part of the bounded v2 interface.",
                ))
            MODELS[name].model_validate(arguments)
        except ValidationError as error:
            instruction = _safe_validation_error(error)
            return _domain_result(PublicResult(
                state="rejected", code="validation_error", next_action="correct_request",
                instruction=instruction, issues=_public_issues(error, arguments),
            ))
        try:
            async with self._lock:
                result = await anyio.to_thread.run_sync(self._dispatch, name, arguments)
        except Exception as error:
            correlation_id = f"err_{token_urlsafe(9)}"
            _LOGGER.exception(
                "internal_error tool=%s correlation_id=%s version=%s error_type=%s",
                name,
                correlation_id,
                package_version(),
                type(error).__name__,
            )
            return _domain_result(PublicResult(state="rejected", code="internal_error", next_action="contact_operator", instruction=(
                "The server stopped because of an internal error "
                f"({correlation_id}). A mutation outcome may be unknown; do not "
                "repost it as new work. See server logs and the receipt for this correlation ID."
            )), is_error=True)
        return _domain_result(result)

    async def _list_wire_tools(
        self,
        _context: ServerRequestContext[object],
        _params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        return ListToolsResult(tools=await self.list_tools())

    async def _call_wire_tool(
        self,
        _context: ServerRequestContext[object],
        params: CallToolRequestParams,
    ) -> CallToolResult:
        return await self.call_tool(params.name, params.arguments or {})

    async def run_stdio_async(self) -> None:
        async with stdio_server() as (read_stream, write_stream):
            await self._tools_only_server.run(
                read_stream,
                write_stream,
                self._tools_only_server.create_initialization_options(),
            )

    def run(self) -> None:
        anyio.run(self.run_stdio_async)

    def build_http_app(
        self,
        *,
        token: str,
        security_settings: TransportSecuritySettings | None = None,
    ) -> Starlette:
        if not token:
            raise ValueError("serve-http needs a bearer token")
        settings = security_settings or TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        )
        manager = StreamableHTTPSessionManager(
            app=self._tools_only_server,
            stateless=True,
            security_settings=settings,
        )

        async def mcp_app(scope: Scope, receive: Receive, send: Send) -> None:
            header = {
                key.decode("latin-1"): value.decode("latin-1")
                for key, value in scope.get("headers") or []
            }
            if not bearer_matches(header.get("authorization"), token):
                response = Response("unauthorized", status_code=401)
                await response(scope, receive, send)
                return
            await manager.handle_request(scope, receive, send)

        async def health(_request: object) -> JSONResponse:
            return JSONResponse(health_payload())

        @asynccontextmanager
        async def lifespan(_app: Starlette) -> Any:
            async with manager.run():
                yield

        return Starlette(
            routes=[
                Route("/health", health),
                Mount("/mcp", app=mcp_app),
            ],
            lifespan=lifespan,
        )

    def run_http(self, *, port: int, token: str) -> None:
        app = self.build_http_app(token=token)
        import uvicorn

        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def bearer_matches(authorization: str | None, token: str) -> bool:
    if not token:
        return False
    return hmac.compare_digest(authorization or "", f"Bearer {token}")


def _declares_source_document(arguments: dict[str, Any]) -> bool:
    creates = arguments.get("create")
    return isinstance(creates, list) and any(
        isinstance(entry, dict) and entry.get("document") == "source"
        for entry in creates
    )


def _safe_validation_error(error: ValidationError, *, repair: str | None = None) -> str:
    items = error.errors(include_input=False, include_url=False)
    details: list[str] = []
    field_repair: str | None = None
    for item in items[:4]:
        location = ".".join(str(part) for part in item["loc"])
        message = str(item["msg"])
        details.append(f"{location}: {message}" if location else message)
        if field_repair is None:
            for part in item["loc"]:
                key = str(part)
                if key in _FIELD_REPAIR:
                    field_repair = _FIELD_REPAIR[key]
                    break
            if field_repair is None:
                for key, text in _FIELD_REPAIR.items():
                    if key in location or key in message:
                        field_repair = text
                        break
    if field_repair is not None:
        message = (
            f"Invalid tool request. {field_repair}. Details: " + "; ".join(details)
        )
    elif details:
        message = "Invalid tool request: " + "; ".join(details)
    elif repair is not None:
        message = f"Invalid tool request. {repair}."
    else:
        message = "Invalid tool request."
    return message if len(message) <= 997 else message[:997] + "..."


def _public_issues(
    error: ValidationError, arguments: dict[str, Any]
) -> list[PublicIssue]:
    issues: list[PublicIssue] = []
    raw_items = arguments.get("items")
    for entry in error.errors(include_input=False, include_url=False)[:20]:
        location = tuple(entry["loc"])
        path = ".".join(str(part) for part in location)
        item_index = next(
            (
                int(location[index + 1])
                for index, part in enumerate(location[:-1])
                if part == "items" and isinstance(location[index + 1], int)
            ),
            None,
        )
        item_id: str | None = None
        if (
            item_index is not None
            and isinstance(raw_items, list)
            and item_index < len(raw_items)
            and isinstance(raw_items[item_index], dict)
            and isinstance(raw_items[item_index].get("id"), str)
        ):
            candidate = raw_items[item_index]["id"]
            if len(candidate) <= 512 and re.fullmatch(
                r"(?:task|project|area|heading):[^\s:]+", candidate
            ):
                item_id = candidate
        issues.append(
            PublicIssue(
                path=path,
                code=str(entry["type"]),
                hint=str(entry["msg"]),
                item_index=item_index,
                item_id=item_id,
            )
        )
    return issues


def _domain_result(result: PublicResult, *, is_error: bool = False) -> CallToolResult:
    structured = result.model_dump(mode="json", exclude_none=True)
    return CallToolResult(
        content=[TextContent(type="text", text=result.instruction)],
        structured_content=structured,
        is_error=is_error,
    )
