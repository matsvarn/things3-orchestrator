from __future__ import annotations

import json
import os
from pathlib import Path
from secrets import token_hex
from typing import Any, Protocol, cast
from uuid import uuid4


class ToolClient(Protocol):
    async def call_tool(
        self, name: str, arguments: dict[str, object]
    ) -> dict[str, Any]: ...


State = dict[str, Any]


class AcceptanceFailure(RuntimeError):
    pass


class LiveAcceptanceRunner:
    def __init__(
        self,
        client: ToolClient,
        state_path: Path,
        *,
        target: dict[str, str],
    ) -> None:
        self.client = client
        self.state_path = state_path
        self.target = target

    async def run(self) -> dict[str, object]:
        state = self._load_or_create()
        if state.get("phase") == "cleanup_staged":
            return await self._resume_cleanup(state)
        if state.get("phase") == "cleaned":
            return {"state": "cleaned", "passed": True, "next_action": "none"}

        await self._select_tags(state)
        await self._capture_scope(state)
        await self._prove_atomic_validation(state)
        await self._move_tag_and_add_checklist(state)
        await self._patch_checklist(state)
        await self._prove_within_paging(state)
        return await self._cleanup(state)

    def _load_or_create(self) -> State:
        if self.state_path.exists():
            loaded = cast(State, json.loads(self.state_path.read_text()))
            if loaded.get("schema_version") != 1:
                raise AcceptanceFailure("unsupported acceptance state schema")
            if loaded.get("target") != self.target:
                raise AcceptanceFailure(
                    "acceptance state belongs to a different target or commit"
                )
            self.state_path.chmod(0o600)
            return loaded

        nonce = token_hex(5)
        prefix = f"[things-orchestrator acceptance {nonce}]"
        state: State = {
            "schema_version": 1,
            "target": self.target,
            "phase": "new",
            "created_ids": [],
            "roles": {},
            "tag_ids": [],
            "request_ids": {
                key: str(uuid4())
                for key in (
                    "capture",
                    "invalid_batch",
                    "move_and_patch",
                    "checklist_patch",
                    "move_for_cursor",
                    "cleanup",
                )
            },
            "titles": {
                "primary_project": f"{prefix} Primary",
                "secondary_project": f"{prefix} Secondary",
                "atomic_original": f"{prefix} Atomic original",
                "atomic_forbidden": f"{prefix} Atomic MUST NOT APPLY",
                "cursor_task": f"{prefix} Cursor member",
                "spare_task": f"{prefix} Spare member",
                "inbox_task": f"{prefix} Inbox move",
                "checklist_first": f"{prefix} First row",
                "checklist_second": f"{prefix} Second row",
                "checklist_updated": f"{prefix} First row updated",
                "checklist_added": f"{prefix} Added row",
            },
        }
        self._save(state)
        return state

    def _save(self, state: State) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(state, stream, indent=2, sort_keys=True)
                stream.write("\n")
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        os.replace(temporary, self.state_path)
        self.state_path.chmod(0o600)

    async def _select_tags(self, state: State) -> None:
        if state["phase"] != "new":
            return
        tag_ids: list[str] = []
        arguments: dict[str, object] = {"view": "tags", "limit": 40}
        while True:
            result = await self.client.call_tool("things_view", arguments)
            self._expect(result, state="ok", label="tag catalog read")
            tag_ids.extend(str(tag["id"]) for tag in result.get("tags", []))
            cursor = result.get("cursor")
            if cursor is None or len(tag_ids) >= 2:
                break
            arguments = {"cursor": str(cursor), "limit": 40}
        if len(tag_ids) < 2:
            raise AcceptanceFailure("live acceptance needs two existing tag IDs")
        state["tag_ids"] = tag_ids[:2]
        state["phase"] = "tags_selected"
        self._save(state)

    async def _capture_scope(self, state: State) -> None:
        if state["phase"] != "tags_selected":
            return
        titles = cast(dict[str, str], state["titles"])
        payload: dict[str, object] = {
            "request_id": state["request_ids"]["capture"],
            "items": [
                {
                    "kind": "project",
                    "title": titles["primary_project"],
                    "tasks": [
                        {"title": titles["atomic_original"]},
                        {"title": titles["cursor_task"]},
                        {"title": titles["spare_task"]},
                    ],
                },
                {"kind": "project", "title": titles["secondary_project"]},
                {"kind": "task", "title": titles["inbox_task"]},
            ],
        }
        result = await self._applied_with_receipt("things_capture", payload)
        by_title = {
            self._title(item): str(item["id"])
            for item in cast(list[dict[str, object]], result.get("items", []))
        }
        role_titles = {
            "primary_project": "primary_project",
            "secondary_project": "secondary_project",
            "atomic_task": "atomic_original",
            "cursor_task": "cursor_task",
            "spare_task": "spare_task",
            "inbox_task": "inbox_task",
        }
        missing = [role for role, key in role_titles.items() if titles[key] not in by_title]
        if missing:
            raise AcceptanceFailure(f"capture omitted expected roles: {', '.join(missing)}")
        roles = {role: by_title[titles[key]] for role, key in role_titles.items()}
        state["roles"] = roles
        state["created_ids"] = sorted(roles.values())
        state["phase"] = "scope_captured"
        self._save(state)

    async def _prove_atomic_validation(self, state: State) -> None:
        if state["phase"] != "scope_captured":
            return
        roles = cast(dict[str, str], state["roles"])
        titles = cast(dict[str, str], state["titles"])
        result = await self.client.call_tool(
            "things_update",
            {
                "request_id": state["request_ids"]["invalid_batch"],
                "items": [
                    {
                        "id": roles["atomic_task"],
                        "set": {"title": titles["atomic_forbidden"]},
                    },
                    {
                        "id": roles["spare_task"],
                        "set": {"start": "not-a-start"},
                    },
                ],
            },
        )
        self._expect(result, state="rejected", code="validation_error", label="invalid batch")
        issues = cast(list[dict[str, object]], result.get("issues", []))
        if not any(
            issue.get("path") == "items.1.set.start" and issue.get("item_index") == 1
            for issue in issues
        ):
            raise AcceptanceFailure("invalid batch did not identify items.1.set.start")
        exact = await self._get_one(roles["atomic_task"])
        if self._title(exact) != titles["atomic_original"]:
            raise AcceptanceFailure("invalid batch partially changed its first item")
        state["phase"] = "atomic_validation_proved"
        self._save(state)

    async def _move_tag_and_add_checklist(self, state: State) -> None:
        if state["phase"] != "atomic_validation_proved":
            return
        roles = cast(dict[str, str], state["roles"])
        titles = cast(dict[str, str], state["titles"])
        inherited_tag, direct_tag = cast(list[str], state["tag_ids"])
        await self._applied_with_receipt(
            "things_update",
            {
                "request_id": state["request_ids"]["move_and_patch"],
                "items": [
                    {
                        "id": roles["primary_project"],
                        "set": {"tags": {"add": [inherited_tag]}},
                    },
                    {
                        "id": roles["inbox_task"],
                        "set": {
                            "into_id": roles["primary_project"],
                            "start": "anytime",
                            "tags": {"add": [direct_tag]},
                            "checklist": {
                                "add": [
                                    {"title": titles["checklist_first"]},
                                    {"title": titles["checklist_second"]},
                                ]
                            },
                        },
                    },
                ],
            },
        )
        moved = await self._get_one(roles["inbox_task"])
        if (
            moved.get("into_id") != roles["primary_project"]
            or moved.get("start") != "anytime"
        ):
            raise AcceptanceFailure("move plus Anytime did not preserve the new Project home")
        if moved.get("direct_tag_ids") != [direct_tag]:
            raise AcceptanceFailure("direct tag delta did not round-trip")
        if moved.get("inherited_tag_ids") != [inherited_tag]:
            raise AcceptanceFailure("Project tag inheritance did not round-trip")
        rows = cast(list[dict[str, object]], moved.get("checklist", []))
        if [self._title(row) for row in rows] != [
            titles["checklist_first"],
            titles["checklist_second"],
        ]:
            raise AcceptanceFailure("initial checklist rows did not round-trip in order")
        state["checklist_ids"] = [str(row["id"]) for row in rows]
        state["phase"] = "move_and_patch_proved"
        self._save(state)

    async def _patch_checklist(self, state: State) -> None:
        if state["phase"] != "move_and_patch_proved":
            return
        roles = cast(dict[str, str], state["roles"])
        titles = cast(dict[str, str], state["titles"])
        first, second = cast(list[str], state["checklist_ids"])
        await self._applied_with_receipt(
            "things_update",
            {
                "request_id": state["request_ids"]["checklist_patch"],
                "items": [
                    {
                        "id": roles["inbox_task"],
                        "set": {
                            "checklist": {
                                "update": [
                                    {
                                        "id": first,
                                        "set": {
                                            "title": titles["checklist_updated"],
                                            "status": "completed",
                                        },
                                    }
                                ],
                                "remove": [second],
                                "add": [{"title": titles["checklist_added"]}],
                            }
                        },
                    }
                ],
            },
        )
        moved = await self._get_one(roles["inbox_task"])
        rows = cast(list[dict[str, object]], moved.get("checklist", []))
        observed = [(self._title(row), row.get("status")) for row in rows]
        expected = [
            (titles["checklist_updated"], "completed"),
            (titles["checklist_added"], "open"),
        ]
        if observed != expected:
            raise AcceptanceFailure("checklist update/remove/add did not preserve order")
        state["phase"] = "checklist_patch_proved"
        self._save(state)

    async def _prove_within_paging(self, state: State) -> None:
        roles = cast(dict[str, str], state["roles"])
        if state["phase"] == "checklist_patch_proved":
            first = await self.client.call_tool(
                "things_find", {"within": roles["primary_project"], "limit": 1}
            )
            self._expect(first, state="ok", label="within-only first page")
            if first.get("next_action") != "continue_read" or first.get("cursor") is None:
                raise AcceptanceFailure("first within-only page did not expose continuation")
            state["stale_cursor"] = str(first["cursor"])
            state["phase"] = "cursor_saved"
            self._save(state)

        if state["phase"] == "cursor_saved":
            await self._applied_with_receipt(
                "things_update",
                {
                    "request_id": state["request_ids"]["move_for_cursor"],
                    "items": [
                        {
                            "id": roles["cursor_task"],
                            "set": {"into_id": roles["secondary_project"]},
                        }
                    ],
                },
            )
            stale = await self.client.call_tool(
                "things_find", {"cursor": state["stale_cursor"], "limit": 40}
            )
            self._expect(stale, state="rejected", code="cursor_invalid", label="stale cursor")
            expected = {
                roles["atomic_task"],
                roles["spare_task"],
                roles["inbox_task"],
            }
            observed = await self._read_within(roles["primary_project"])
            if observed != expected:
                raise AcceptanceFailure("fresh within-only paging returned the wrong membership")
            state["phase"] = "within_paging_proved"
            self._save(state)

    async def _cleanup(self, state: State) -> dict[str, object]:
        if state["phase"] != "within_paging_proved":
            raise AcceptanceFailure(f"cannot run cleanup from phase {state['phase']}")
        roles = cast(dict[str, str], state["roles"])
        result = await self.client.call_tool(
            "things_trash",
            {
                "request_id": state["request_ids"]["cleanup"],
                "ids": [roles["primary_project"], roles["secondary_project"]],
            },
        )
        self._expect(result, state="applied", code="applied", label="cleanup")
        operation_id = result.get("operation_id")
        if not isinstance(operation_id, str):
            raise AcceptanceFailure("cleanup omitted its operation ID")
        state["cleanup_operation_id"] = operation_id
        receipt = await self.client.call_tool(
            "things_receipt", {"operation_id": operation_id, "limit": 100}
        )
        self._expect(
            receipt,
            state="applied",
            code="applied",
            label="cleanup receipt",
        )
        rows = cast(list[dict[str, object]], receipt.get("rows", []))
        if not receipt.get("receipt_hash") or not rows:
            raise AcceptanceFailure("applied cleanup has no immutable receipt evidence")
        created = set(cast(list[str], state["created_ids"]))
        receipt_ids = {str(row.get("target_id")) for row in rows}
        if not created.issubset(receipt_ids):
            raise AcceptanceFailure("cleanup receipt omitted a disposable acceptance item")
        trash_ids = await self._read_view_ids("trash")
        if not created.issubset(trash_ids):
            raise AcceptanceFailure("not every disposable acceptance item reached Trash")
        state["phase"] = "cleaned"
        self._save(state)
        return {"state": "cleaned", "passed": True, "next_action": "none"}

    async def _resume_cleanup(self, state: State) -> dict[str, object]:
        operation_id = str(state["cleanup_operation_id"])
        receipt = await self.client.call_tool(
            "things_receipt", {"operation_id": operation_id, "limit": 100}
        )
        outcome = str(receipt.get("state"))
        if outcome == "pending":
            roles = cast(dict[str, str], state["roles"])
            request_ids = cast(dict[str, str], state["request_ids"])
            retried = await self.client.call_tool(
                "things_trash",
                {
                    "request_id": request_ids["cleanup"],
                    "ids": [roles["primary_project"], roles["secondary_project"]],
                },
            )
            if retried.get("operation_id") != operation_id:
                raise AcceptanceFailure("cleanup retry changed operation identity")
            outcome = str(retried.get("state"))
            if outcome == "pending":
                return {
                    "state": outcome,
                    "passed": False,
                    "next_action": "retry_same",
                }
            receipt = await self.client.call_tool(
                "things_receipt", {"operation_id": operation_id, "limit": 100}
            )
            outcome = str(receipt.get("state"))
        if outcome == "partial":
            return {
                "state": outcome,
                "passed": False,
                "next_action": "read_receipt",
            }
        if outcome == "stale":
            raise AcceptanceFailure(
                "legacy cleanup was retired without Cloud I/O; use a fresh acceptance "
                "state and request after reading current Trash"
            )
        if outcome != "applied":
            raise AcceptanceFailure(f"cleanup settled as {outcome}; inspect its receipt")
        if not receipt.get("receipt_hash") or not receipt.get("rows"):
            raise AcceptanceFailure("applied cleanup has no immutable receipt evidence")
        trash_ids = await self._read_view_ids("trash")
        created = set(cast(list[str], state["created_ids"]))
        if not created.issubset(trash_ids):
            raise AcceptanceFailure("not every disposable acceptance item reached Trash")
        state["phase"] = "cleaned"
        self._save(state)
        return {"state": "cleaned", "passed": True, "next_action": "none"}

    async def _applied_with_receipt(
        self, tool: str, arguments: dict[str, object]
    ) -> dict[str, Any]:
        result = await self.client.call_tool(tool, arguments)
        self._expect(result, state="applied", code="applied", label=tool)
        operation_id = result.get("operation_id")
        if not isinstance(operation_id, str):
            raise AcceptanceFailure(f"{tool} omitted its operation ID")
        receipt = await self.client.call_tool(
            "things_receipt", {"operation_id": operation_id, "limit": 100}
        )
        self._expect(receipt, state="applied", code="applied", label=f"{tool} receipt")
        if not receipt.get("receipt_hash") or not receipt.get("rows"):
            raise AcceptanceFailure(f"{tool} receipt is missing immutable evidence")
        return result

    async def _get_one(self, item_id: str) -> dict[str, object]:
        result = await self.client.call_tool("things_get", {"ids": [item_id]})
        self._expect(result, state="ok", label=f"get {item_id.partition(':')[0]}")
        items = cast(list[dict[str, object]], result.get("items", []))
        if len(items) != 1 or items[0].get("id") != item_id:
            raise AcceptanceFailure("exact get did not return exactly its requested item")
        return items[0]

    async def _read_within(self, within: str) -> set[str]:
        ids: set[str] = set()
        arguments: dict[str, object] = {"within": within, "limit": 1}
        while True:
            result = await self.client.call_tool("things_find", arguments)
            self._expect(result, state="ok", label="within-only page")
            ids.update(str(item["id"]) for item in result.get("items", []))
            cursor = result.get("cursor")
            if cursor is None:
                return ids
            arguments = {"cursor": str(cursor), "limit": 1}

    async def _read_view_ids(self, view: str) -> set[str]:
        ids: set[str] = set()
        arguments: dict[str, object] = {"view": view, "limit": 40}
        while True:
            result = await self.client.call_tool("things_view", arguments)
            self._expect(result, state="ok", label=f"{view} view")
            ids.update(str(item["id"]) for item in result.get("items", []))
            cursor = result.get("cursor")
            if cursor is None:
                return ids
            arguments = {"cursor": str(cursor), "limit": 40}

    @staticmethod
    def _title(item: dict[str, object]) -> str:
        title = item.get("title")
        if not isinstance(title, dict) or not isinstance(title.get("value"), str):
            raise AcceptanceFailure("public item omitted tainted title metadata")
        return str(title["value"])

    @staticmethod
    def _expect(
        result: dict[str, Any],
        *,
        state: str,
        label: str,
        code: str | None = None,
    ) -> None:
        if result.get("state") != state or (
            code is not None and result.get("code") != code
        ):
            raise AcceptanceFailure(
                f"{label} returned state={result.get('state')} code={result.get('code')}"
            )
