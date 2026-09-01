import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from things_orchestrator.journal import MemoryJournal
from things_orchestrator.live_acceptance import AcceptanceFailure, LiveAcceptanceRunner
from things_orchestrator.server import ThingsMCPServer
from things_orchestrator.v2 import ThingsV2
from things_orchestrator.workspace import MemoryLibrary, ThingsWorkspace


class LocalMCPClient:
    def __init__(self, server: ThingsMCPServer) -> None:
        self.server = server
        self.calls: list[str] = []

    async def call_tool(
        self, name: str, arguments: dict[str, object]
    ) -> dict[str, Any]:
        self.calls.append(name)
        result = await self.server.call_tool(name, arguments)
        assert result.structured_content is not None
        return result.structured_content


def test_live_acceptance_exercises_dogfood_workflow_and_verifies_cleanup_in_one_run(
    tmp_path: Path,
) -> None:
    library = MemoryLibrary([])
    library.tags = {"inherited": "Inherited", "direct": "Direct"}
    journal = MemoryJournal()
    server = ThingsMCPServer(
        ThingsV2(
            ThingsWorkspace(
                library,
                journal=journal,
                account_id="acceptance@example.com",
            )
        )
    )
    client = LocalMCPClient(server)
    state_path = tmp_path / "acceptance.json"

    first = asyncio.run(
        LiveAcceptanceRunner(client, state_path, target={"url": "memory://test", "commit": "abc"}).run()
    )

    assert first == {"state": "cleaned", "passed": True, "next_action": "none"}
    assert state_path.stat().st_mode & 0o777 == 0o600
    state = json.loads(state_path.read_text())
    assert state["phase"] == "cleaned"
    assert state["cleanup_operation_id"].startswith("op_")
    assert len(state["created_ids"]) == 6

    moved = library.records[state["roles"]["inbox_task"].partition(":")[2]]
    primary = library.records[state["roles"]["primary_project"].partition(":")[2]]
    assert moved.parent_uuid == primary.uuid
    assert moved.inbox is False
    assert moved.start is None
    assert moved.tag_uuids == [state["tag_ids"][1].partition(":")[2]]
    assert [row.title for row in moved.checklists] == [
        state["titles"]["checklist_updated"],
        state["titles"]["checklist_added"],
    ]
    assert primary.tag_uuids == [state["tag_ids"][0].partition(":")[2]]
    atomic_id = state["roles"]["atomic_task"].partition(":")[2]
    assert library.records[atomic_id].title == state["titles"]["atomic_original"]
    assert all(
        library.records[item_id.partition(":")[2]].trashed
        for item_id in state["created_ids"]
    )

    trash_calls = client.calls.count("things_trash")
    second = asyncio.run(
        LiveAcceptanceRunner(client, state_path, target={"url": "memory://test", "commit": "abc"}).run()
    )

    assert second == {"state": "cleaned", "passed": True, "next_action": "none"}
    assert client.calls.count("things_trash") == trash_calls
    operation = journal.get_v2_operation(state["cleanup_operation_id"])
    assert operation is not None and operation.state == "applied"


def test_live_acceptance_refuses_to_replay_after_partial_cleanup(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "acceptance.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target": {"url": "memory://test", "commit": "abc"},
                "phase": "cleanup_staged",
                "cleanup_operation_id": "op_partial123",
                "created_ids": [],
                "roles": {},
                "titles": {},
                "request_ids": {},
            }
        )
    )
    state_path.chmod(0o600)

    class PartialClient:
        async def call_tool(
            self, name: str, arguments: dict[str, object]
        ) -> dict[str, Any]:
            assert name == "things_receipt"
            return {
                "state": "partial",
                "code": "partial",
                "next_action": "read_receipt",
                "operation_id": "op_partial123",
                "instruction": "Read the immutable receipt.",
            }

    result = asyncio.run(
        LiveAcceptanceRunner(
            PartialClient(),
            state_path,
            target={"url": "memory://test", "commit": "abc"},
        ).run()
    )

    assert result == {
        "state": "partial",
        "passed": False,
        "next_action": "read_receipt",
    }


def test_live_acceptance_retries_exact_pending_cleanup_for_read_back(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "acceptance.json"
    created = ["project:one", "project:two"]
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target": {"url": "memory://test", "commit": "abc"},
                "phase": "cleanup_staged",
                "cleanup_operation_id": "op_cleanup123",
                "created_ids": created,
                "roles": {
                    "primary_project": created[0],
                    "secondary_project": created[1],
                },
                "titles": {},
                "request_ids": {"cleanup": "0198f0ee-98d4-7bd5-91ba-8e76019b2735"},
            }
        )
    )
    state_path.chmod(0o600)

    class PendingThenAppliedClient:
        calls: list[tuple[str, dict[str, object]]] = []
        receipt_count = 0

        async def call_tool(
            self, name: str, arguments: dict[str, object]
        ) -> dict[str, Any]:
            self.calls.append((name, arguments))
            if name == "things_receipt":
                self.receipt_count += 1
                if self.receipt_count == 1:
                    return {"state": "pending"}
                return {
                    "state": "applied",
                    "receipt_hash": "sha256:test",
                    "rows": [{"target_id": item_id} for item_id in created],
                }
            if name == "things_trash":
                return {"state": "applied", "operation_id": "op_cleanup123"}
            assert name == "things_view"
            return {"state": "ok", "items": [{"id": item_id} for item_id in created]}

    client = PendingThenAppliedClient()
    result = asyncio.run(
        LiveAcceptanceRunner(
            client,
            state_path,
            target={"url": "memory://test", "commit": "abc"},
        ).run()
    )

    assert result == {"state": "cleaned", "passed": True, "next_action": "none"}
    assert client.calls[1] == (
        "things_trash",
        {
            "request_id": "0198f0ee-98d4-7bd5-91ba-8e76019b2735",
            "ids": created,
        },
    )


def test_live_acceptance_recovers_after_capture_response_before_state_save(
    tmp_path: Path,
) -> None:
    library = MemoryLibrary([])
    library.tags = {"one": "One", "two": "Two"}
    journal = MemoryJournal()
    client = LocalMCPClient(
        ThingsMCPServer(
            ThingsV2(
                ThingsWorkspace(
                    library,
                    journal=journal,
                    account_id="acceptance@example.com",
                )
            )
        )
    )
    state_path = tmp_path / "acceptance.json"

    class CrashAfterCapture(LiveAcceptanceRunner):
        crashed = False

        def _save(self, state: dict[str, Any]) -> None:
            if state.get("phase") == "scope_captured" and not self.crashed:
                self.crashed = True
                raise RuntimeError("simulated crash after applied capture")
            super()._save(state)

    with pytest.raises(RuntimeError, match="simulated crash"):
        asyncio.run(
            CrashAfterCapture(
                client,
                state_path,
                target={"url": "memory://test", "commit": "abc"},
            ).run()
        )
    assert len(library.records) == 6
    assert json.loads(state_path.read_text())["phase"] == "tags_selected"

    resumed = asyncio.run(
        LiveAcceptanceRunner(
            client,
            state_path,
            target={"url": "memory://test", "commit": "abc"},
        ).run()
    )

    assert resumed == {"state": "cleaned", "passed": True, "next_action": "none"}
    assert len(library.records) == 6
    capture_operations = [
        operation
        for operation in journal._v2_operations.values()
        if operation.tool == "things_capture"
    ]
    assert len(capture_operations) == 1


def test_live_acceptance_refuses_state_from_another_target(tmp_path: Path) -> None:
    state_path = tmp_path / "acceptance.json"

    class NoCalls:
        async def call_tool(
            self, name: str, arguments: dict[str, object]
        ) -> dict[str, Any]:
            raise AssertionError(f"unexpected call to {name}")

    first = LiveAcceptanceRunner(
        NoCalls(),
        state_path,
        target={"url": "https://first.example/mcp", "commit": "abc"},
    )
    first._load_or_create()

    second = LiveAcceptanceRunner(
        NoCalls(),
        state_path,
        target={"url": "https://second.example/mcp", "commit": "def"},
    )
    with pytest.raises(RuntimeError, match="different target"):
        asyncio.run(second.run())


@pytest.mark.parametrize("omit_created_id", [False, True])
def test_live_acceptance_requires_every_created_id_in_trash_after_approval(
    tmp_path: Path, omit_created_id: bool
) -> None:
    state_path = tmp_path / "acceptance.json"
    created = ["project:one", "task:two"]
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target": {"url": "memory://test", "commit": "abc"},
                "phase": "cleanup_staged",
                "cleanup_operation_id": "op_cleanup123",
                "created_ids": created,
                "roles": {},
                "titles": {},
                "request_ids": {},
            }
        )
    )
    state_path.chmod(0o600)

    class AppliedCleanupClient:
        calls: list[str] = []

        async def call_tool(
            self, name: str, arguments: dict[str, object]
        ) -> dict[str, Any]:
            self.calls.append(name)
            if name == "things_receipt":
                return {
                    "state": "applied",
                    "code": "applied",
                    "next_action": "read_receipt",
                    "operation_id": "op_cleanup123",
                    "instruction": "Immutable receipt rows.",
                    "receipt_hash": "sha256:test",
                    "rows": [{"target_id": item_id} for item_id in created],
                }
            assert name == "things_view"
            visible = created[:-1] if omit_created_id else created
            return {
                "state": "ok",
                "code": "ok",
                "next_action": "none",
                "instruction": "Current Things facts.",
                "items": [{"id": item_id} for item_id in visible],
            }

    client = AppliedCleanupClient()
    runner = LiveAcceptanceRunner(
        client,
        state_path,
        target={"url": "memory://test", "commit": "abc"},
    )
    if omit_created_id:
        with pytest.raises(AcceptanceFailure, match="not every"):
            asyncio.run(runner.run())
        assert json.loads(state_path.read_text())["phase"] == "cleanup_staged"
    else:
        assert asyncio.run(runner.run()) == {
            "state": "cleaned",
            "passed": True,
            "next_action": "none",
        }
        assert json.loads(state_path.read_text())["phase"] == "cleaned"
    assert client.calls == ["things_receipt", "things_view"]
