from __future__ import annotations

import asyncio

from jsonschema import validate
from mcp.types import ToolAnnotations

from things_orchestrator.library import MemoryLibrary, Record
from things_orchestrator.server import ThingsMCPServer, bearer_matches
from things_orchestrator.v2 import PublicResult
from things_orchestrator.workspace import ThingsWorkspace


def _server(*records: Record) -> ThingsMCPServer:
    return ThingsMCPServer(ThingsWorkspace(MemoryLibrary(list(records))))


def test_discovery_is_exactly_eight_bounded_v2_tools() -> None:
    tools = {tool.name: tool for tool in asyncio.run(_server().list_tools())}
    assert set(tools) == {
        "things_view", "things_find", "things_get", "things_capture",
        "things_update", "things_complete", "things_trash", "things_receipt",
    }
    for name in ("things_view", "things_find", "things_get", "things_receipt"):
        assert tools[name].annotations == ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    for name in ("things_capture", "things_update", "things_complete", "things_trash"):
        assert tools[name].annotations == ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=False,
        )


def test_discovery_exposes_repeat_contract_without_adding_tools() -> None:
    tools = {tool.name: tool for tool in asyncio.run(_server().list_tools())}
    assert len(tools) == 8
    capture_schema = str(tools["things_capture"].input_schema)
    update_schema = str(tools["things_update"].input_schema)
    assert "repeat" in capture_schema
    assert "repeat" in update_schema
    assert "semantic repeat" in tools["things_capture"].description
    assert "repeat rule" in tools["things_update"].description


def test_validation_errors_are_domain_results() -> None:
    result = asyncio.run(
        _server().call_tool(
            "things_capture",
            {"request_id": "semantic-id", "items": [{"kind": "task", "title": "A"}]},
        )
    )
    assert result.is_error is False
    assert result.structured_content["state"] == "rejected"
    assert "request_id" in result.structured_content["instruction"]


def test_unexpected_failure_is_error_without_operation_receipt() -> None:
    server = _server()

    def fail(_name: str, _arguments: dict[str, object]) -> object:
        raise RuntimeError("hidden")

    server._dispatch = fail  # type: ignore[method-assign]  # noqa: SLF001
    result = asyncio.run(server.call_tool("things_view", {}))
    assert result.is_error is True
    assert result.structured_content["state"] == "rejected"
    assert "operation_id" not in result.structured_content


def test_each_tool_result_matches_the_v2_output_schema() -> None:
    server = _server(Record(uuid="task", kind="task", title="Read detail", inbox=True))
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    calls = {
        "things_view": {},
        "things_find": {"text": "Read"},
        "things_get": {"ids": ["task:task"]},
        "things_capture": {"request_id": "0198f0ee-98d4-7bd5-91ba-8e76019b2735", "items": [{"kind": "task", "title": "A"}]},
        "things_update": {"request_id": "0198f0ef-3923-79b6-96a8-2bf28eac0d67", "items": [{"id": "task:task", "set": {"title": "New"}}]},
        "things_complete": {"request_id": "0198f0ef-3923-79b6-96a8-2bf28eac0d68", "ids": ["task:task"]},
        "things_trash": {"request_id": "0198f0ef-3923-79b6-96a8-2bf28eac0d69", "ids": ["task:task"]},
        "things_receipt": {"operation_id": "op_missing000"},
    }
    for name, arguments in calls.items():
        result = asyncio.run(server.call_tool(name, arguments))
        assert result.structured_content is not None
        validate(result.structured_content, tools[name].output_schema)


def test_mcp_server_version_matches_package() -> None:
    from things_orchestrator.deployment import package_version

    server = _server()
    assert server._tools_only_server.version == package_version()  # noqa: SLF001


def test_bearer_comparison_requires_exact_token() -> None:
    assert bearer_matches("Bearer secret", "secret") is True
    assert bearer_matches("Bearer other", "secret") is False
    assert bearer_matches(None, "secret") is False
    assert bearer_matches("Bearer secret", "") is False


def test_public_result_schema_contains_no_private_operation_controls() -> None:
    schema = str(PublicResult.model_json_schema())
    for forbidden in (
        "manifest_hash", "safety_policy_digest", "preconditions", "authorization",
    ):
        assert forbidden not in schema
