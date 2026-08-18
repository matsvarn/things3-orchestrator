from __future__ import annotations

import hmac
import json
import logging
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
from .interface import (
    APPROVE_DESC,
    APPROVE_IN,
    APPROVE_OUT,
    COMMIT_DESC,
    COMMIT_IN,
    COMMIT_OUT,
    READ_DESC,
    READ_IN,
    READ_OUT,
    ApproveCall,
    CommitCall,
    ReadCall,
    Result,
)
from .workspace import ThingsWorkspace

_LOGGER = logging.getLogger("things_orchestrator")

_REPAIR = {
    "things_read": 'Use {} or {"find":"passport"}',
    "things_commit": 'Use {"intent_id":"capture-001","create":[{"title":"Renew password"}]}',
    "things_approve": 'Copy the returned plan ID, for example {"plan_id":"plan_12345678"}',
}
_FIELD_REPAIR = {
    "scope_revision": (
        "Area and registry changes need the scope_revision from a fresh "
        "view=system read"
    ),
    "start": (
        "start accepts today, evening, someday, an ISO date, or null to clear "
        "scheduling while keeping the current Project or Area"
    ),
    "today_after": (
        "today_after needs a Today item, including one moved to Today earlier "
        "in this same commit"
    ),
    "within": (
        "view project needs within as project:<id>; view area needs within as "
        "area:<id>; within=trash needs find"
    ),
    "view": (
        "Use one of today, inbox, week, system, project, area, audit, "
        "diagnostics, logbook, trash, or tags"
    ),
    "ids": "ids is a review-only list of 1 to 10 unique exact item IDs",
    "include": (
        "include is only for purpose=change or organize, must be unique, "
        "and accepts up to 40 compact lookups"
    ),
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

_TOOLS = (
    Tool(
        name="things_read",
        description=READ_DESC,
        input_schema=READ_IN,
        output_schema=READ_OUT,
        annotations=_READ_ONLY,
    ),
    Tool(
        name="things_commit",
        description=COMMIT_DESC,
        input_schema=COMMIT_IN,
        output_schema=COMMIT_OUT,
        annotations=_IDEMPOTENT_WRITE,
    ),
    Tool(
        name="things_approve",
        description=APPROVE_DESC,
        input_schema=APPROVE_IN,
        output_schema=APPROVE_OUT,
        annotations=_IDEMPOTENT_WRITE,
    ),
)


class ThingsMCPServer:
    name = "things"

    def __init__(self, workspace: ThingsWorkspace) -> None:
        self._workspace = workspace
        self._lock = anyio.Lock()
        self._tools_only_server: Server[object] = Server(
            name=self.name,
            version=package_version(),
            on_list_tools=self._list_wire_tools,
            on_call_tool=self._call_wire_tool,
        )

    async def list_tools(self) -> list[Tool]:
        return [tool.model_copy(deep=True) for tool in _TOOLS]

    def _dispatch(self, name: str, arguments: dict[str, Any]) -> Result:
        if name == "things_read":
            return self._workspace.read(ReadCall.model_validate(arguments))
        if name == "things_commit":
            return self._workspace.commit(CommitCall.model_validate(arguments))
        return self._workspace.approve(ApproveCall.model_validate(arguments))

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        try:
            if name not in {tool.name for tool in _TOOLS}:
                raise ValidationError.from_exception_data(
                    "Tool",
                    [
                        {
                            "type": "literal_error",
                            "loc": ("name",),
                            "input": name,
                            "ctx": {"expected": json.dumps([tool.name for tool in _TOOLS])},
                        }
                    ],
                )
            if name == "things_read":
                ReadCall.model_validate(arguments)
            elif name == "things_commit":
                CommitCall.model_validate(arguments)
            else:
                ApproveCall.model_validate(arguments)
        except ValidationError as error:
            return _domain_result(
                Result(
                    next="ask",
                    status="rejected",
                    instruction=_safe_validation_error(
                        error, repair=_REPAIR.get(name)
                    ),
                ),
                full_items=name == "things_read",
            )
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
            return _domain_result(
                Result(
                    next="stop",
                    status="internal_error",
                    instruction=(
                        "The server stopped because of an internal error "
                        f"({correlation_id}). "
                        "Do not assume that a write started. "
                        "See server logs for this correlation ID."
                    ),
                ),
                full_items=name == "things_read",
                is_error=True,
            )
        return _domain_result(result, full_items=name == "things_read")

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


def _domain_result(
    result: Result, *, full_items: bool, is_error: bool = False
) -> CallToolResult:
    summary = f"Things result. status={result.status}; next={result.next}. {result.instruction}"
    if len(summary) > 300:
        summary = summary[:297] + "..."
    structured = result.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    if not full_items and "items" in structured:
        summary_fields = {
            "id",
            "revision",
            "kind",
            "title",
            "status",
            "into_id",
            "heading_id",
            "start",
            "signals",
        }
        structured["items"] = [
            {key: value for key, value in item.items() if key in summary_fields}
            for item in structured["items"]
        ]
    return CallToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=structured,
        is_error=is_error,
    )
