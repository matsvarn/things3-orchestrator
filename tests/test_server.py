from __future__ import annotations

import asyncio

from jsonschema import validate
from mcp.types import ToolAnnotations

from things_orchestrator.cloud import _note, fold_events
from things_orchestrator.interface import (
    APPROVE_OUT,
    COMMIT_IN,
    COMMIT_OUT,
    READ_IN,
    READ_OUT,
    ReadCall,
)
from things_orchestrator.library import ChecklistLine, MemoryLibrary, Record, new_uuid
from things_orchestrator.server import ThingsMCPServer, bearer_matches
from things_orchestrator.workspace import ThingsWorkspace


def test_discovery_is_three_flat_tools() -> None:
    server = ThingsMCPServer(ThingsWorkspace(MemoryLibrary()))
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert set(tools) == {"things_read", "things_commit", "things_approve"}
    assert "oneOf" not in str(READ_IN)
    assert "oneOf" not in str(COMMIT_IN)
    assert "anyOf" not in str(READ_IN)
    assert "anyOf" not in str(COMMIT_IN)
    assert tools["things_read"].annotations == ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
    assert tools["things_commit"].annotations == ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=True,
        open_world_hint=False,
    )


def test_read_schema_accepts_empty_and_rejects_combined_looks() -> None:
    ReadCall.model_validate({})
    ReadCall.model_validate({"find": "passport"})
    try:
        ReadCall.model_validate({"find": "x", "id": "task:1"})
    except Exception as error:
        assert "use only one" in str(error)
    else:
        raise AssertionError("combined see should fail")


def test_fold_history_items_and_base58_ids() -> None:
    library = MemoryLibrary()
    fold_events(
        [
            {"uuid": "area1", "e": "Area2", "t": 0, "p": {"tt": "Work"}},
            {
                "uuid": "task1",
                "e": "Task6",
                "t": 0,
                "p": {
                    "tt": "Call bank",
                    "tp": 0,
                    "ss": 0,
                    "st": 0,
                    "tr": False,
                    "ar": ["area1"],
                    "nt": {"_t": "tx", "t": 1, "v": "pin", "ch": 1},
                },
            },
            {"uuid": "task1", "e": "Task6", "t": 2, "p": {}},
        ],
        library=library,
    )
    assert library.records["area1"].title == "Work"
    assert "task1" not in library.records
    uuid = new_uuid()
    assert 15 <= len(uuid) <= 22
    assert all(char not in uuid for char in "0OIl")
    note = _note("hello")
    assert note["t"] == 1
    assert note["v"] == "hello"


def test_bearer_compare_rejects_wrong_token() -> None:
    assert bearer_matches("Bearer secret-token-value", "secret-token-value") is True
    assert bearer_matches("Bearer other", "secret-token-value") is False
    assert bearer_matches(None, "secret-token-value") is False
    assert bearer_matches("Bearer secret-token-value", "") is False


def test_build_http_app_rejects_empty_token() -> None:
    server = ThingsMCPServer(ThingsWorkspace(MemoryLibrary()))
    try:
        server.build_http_app(token="")
    except ValueError as error:
        assert "bearer token" in str(error)
    else:
        raise AssertionError("empty token should fail")


def test_unexpected_commit_failure_stops_without_a_false_write_receipt() -> None:
    server = ThingsMCPServer(ThingsWorkspace(MemoryLibrary()))

    def fail(_name: str, _arguments: dict[str, object]) -> object:
        raise RuntimeError("hidden internal failure")

    server._dispatch = fail  # type: ignore[method-assign]  # noqa: SLF001
    result = asyncio.run(
        server.call_tool(
            "things_commit",
            {"intent_id": "capture-001", "create": [{"title": "Call bank"}]},
        )
    )

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["next"] == "stop"
    assert result.structured_content["status"] == "internal_error"
    assert "receipt" not in result.structured_content
    assert "Do not assume" in result.structured_content["instruction"]
    assert "err_" in result.structured_content["instruction"]


def test_validation_errors_prefer_field_specific_repair() -> None:
    server = ThingsMCPServer(ThingsWorkspace(MemoryLibrary()))
    result = asyncio.run(
        server.call_tool(
            "things_commit",
            {
                "intent_id": "area-no-scope",
                "create": [{"kind": "area", "title": "Health"}],
            },
        )
    )
    assert result.is_error is True
    text = result.content[0].text
    assert "scope_revision from a fresh view=system read" in text
    assert "Renew password" not in text

    start = asyncio.run(
        server.call_tool(
            "things_commit",
            {
                "intent_id": "bad-start",
                "create": [{"title": "Later", "start": "anytime"}],
            },
        )
    )
    assert start.is_error is True
    assert "today, evening, someday" in start.content[0].text


def test_mcp_server_version_matches_the_package() -> None:
    from things_orchestrator.deployment import package_version

    server = ThingsMCPServer(ThingsWorkspace(MemoryLibrary()))
    assert server._tools_only_server.version == package_version()  # noqa: SLF001


def test_each_tool_emits_only_fields_accepted_by_its_output_schema() -> None:
    task = Record(
        uuid="task",
        kind="task",
        title="Read detail",
        notes="# Context",
        checklists=[ChecklistLine("row", "Check")],
        sort_index=1024,
    )
    workspace = ThingsWorkspace(MemoryLibrary([task]))
    server = ThingsMCPServer(workspace)

    read_result = asyncio.run(server.call_tool("things_read", {"id": task.id}))
    assert read_result.structured_content is not None
    validate(instance=read_result.structured_content, schema=READ_OUT)
    assert read_result.structured_content["items"][0]["notes_markdown"] == "# Context"
    assert read_result.structured_content["items"][0]["order"] == 1024

    commit_result = asyncio.run(
        server.call_tool(
            "things_commit",
            {"intent_id": "wire-commit-001", "create": [{"title": "Created"}]},
        )
    )
    assert commit_result.structured_content is not None
    validate(instance=commit_result.structured_content, schema=COMMIT_OUT)
    assert set(commit_result.structured_content["items"][0]) == {
        "id",
        "revision",
        "kind",
        "title",
        "status",
    }

    scope = workspace.read(ReadCall(view="system")).scope_revision
    area_result = asyncio.run(
        server.call_tool(
            "things_commit",
            {
                "intent_id": "wire-area-001",
                "scope_revision": scope,
                "create": [{"kind": "area", "title": "Health"}],
            },
        )
    )
    assert area_result.structured_content is not None
    plan_id = area_result.structured_content["plan"]["id"]
    approve_result = asyncio.run(
        server.call_tool("things_approve", {"plan_id": plan_id})
    )
    assert approve_result.structured_content is not None
    validate(instance=approve_result.structured_content, schema=APPROVE_OUT)
    assert set(approve_result.structured_content["items"][0]) == {
        "id",
        "revision",
        "kind",
        "title",
        "status",
    }


def test_health_is_open_without_bearer() -> None:
    from starlette.testclient import TestClient

    server = ThingsMCPServer(ThingsWorkspace(MemoryLibrary()))
    app = server.build_http_app(token="good-token")
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["version"]
        assert payload["cache_version"] == 4
        assert payload["capabilities"]["clear_someday"] is True
        assert payload["capabilities"]["area_view"] is True


def test_mcp_returns_401_without_authorization() -> None:
    from starlette.testclient import TestClient

    server = ThingsMCPServer(ThingsWorkspace(MemoryLibrary()))
    app = server.build_http_app(token="good-token")
    with TestClient(app) as client:
        assert client.post("/mcp").status_code == 401


def test_mcp_returns_401_with_wrong_bearer() -> None:
    from starlette.testclient import TestClient

    server = ThingsMCPServer(ThingsWorkspace(MemoryLibrary()))
    app = server.build_http_app(token="good-token")
    with TestClient(app) as client:
        response = client.post("/mcp", headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401
