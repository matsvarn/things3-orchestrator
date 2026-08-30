from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone

import pytest

from things_orchestrator.cloud import CloudError
from things_orchestrator.journal import MemoryJournal
from things_orchestrator.library import ChecklistLine, MemoryLibrary, Record
from things_orchestrator.server import ThingsMCPServer
from things_orchestrator.v2 import ThingsV2
from things_orchestrator.workspace import ThingsWorkspace

NOW = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)


def _stack(*records: Record) -> tuple[ThingsMCPServer, MemoryLibrary, MemoryJournal]:
    library = MemoryLibrary(list(records))
    journal = MemoryJournal()
    workspace = ThingsWorkspace(
        library, journal=journal, clock=lambda: NOW, account_id="owner@example.com"
    )
    return ThingsMCPServer(ThingsV2(workspace)), library, journal


def _call(server: ThingsMCPServer, name: str, arguments: dict[str, object]) -> dict[str, object]:
    result = asyncio.run(server.call_tool(name, arguments))
    assert result.structured_content is not None
    return result.structured_content


def test_update_moves_same_task_in_place_and_preserves_omitted_content() -> None:
    area = Record(uuid="area", kind="area", title="Area")
    source = Record(uuid="source", kind="project", title="Source", area_uuid="area")
    destination = Record(uuid="destination", kind="project", title="Destination", area_uuid="area")
    task = Record(
        uuid="task",
        kind="task",
        title="Task",
        notes="Notes",
        parent_uuid="source",
        heading_uuid="old-heading",
        start=date(2026, 9, 1),
        remind="09:00",
        tag_uuids=["direct"],
        checklists=[ChecklistLine(uuid="row", title="Row")],
    )
    server, library, journal = _stack(area, source, destination, task)
    library.tags = {"direct": "Direct"}

    result = _call(server, "things_update", {
        "request_id": "0198f0ef-3923-79b6-96a8-2bf28eac0d71",
        "items": [{"id": "task:task", "set": {"into_id": "project:destination"}}],
    })

    assert result["state"] == "applied"
    moved = library.records["task"]
    assert moved.parent_uuid == "destination"
    assert moved.heading_uuid is None
    assert (moved.title, moved.notes, moved.start, moved.remind, moved.tag_uuids) == (
        "Task", "Notes", date(2026, 9, 1), "09:00", ["direct"]
    )
    assert [row.uuid for row in moved.checklists] == ["row"]
    assert set(library.records) == {"area", "source", "destination", "task"}
    operation = journal.get_v2_operation(str(result["operation_id"]))
    assert operation is not None
    assert [write["action"] for write in operation.manifest["writes"]] == ["move"]
    assert operation.manifest["requires_owner"] is False


def test_anytime_preserves_project_home_but_moves_inbox_to_top_level() -> None:
    project = Record(uuid="project", kind="project", title="Project")
    project_task = Record(
        uuid="project-task", kind="task", title="Project task", parent_uuid="project",
        start=date(2026, 9, 1), remind="09:00", tonight=True,
    )
    inbox_task = Record(uuid="inbox-task", kind="task", title="Inbox task", inbox=True)
    server, library, _ = _stack(project, project_task, inbox_task)

    first = _call(server, "things_update", {
        "request_id": "0198f0ef-3923-79b6-96a8-2bf28eac0d72",
        "items": [{"id": "task:project-task", "set": {"start": "anytime", "remind_at": None}}],
    })
    second = _call(server, "things_update", {
        "request_id": "0198f0ef-3923-79b6-96a8-2bf28eac0d73",
        "items": [{"id": "task:inbox-task", "set": {"start": "anytime"}}],
    })

    assert first["state"] == second["state"] == "applied"
    assert library.records["project-task"].parent_uuid == "project"
    assert library.records["project-task"].start is None
    assert library.records["project-task"].remind is None
    assert library.records["inbox-task"].inbox is False
    assert library.records["inbox-task"].parent_uuid is None
    assert first["items"][0]["start"] == "anytime"
    assert second["items"][0]["start"] == "anytime"
    for result in (first, second):
        receipt = _call(
            server, "things_receipt", {"operation_id": result["operation_id"]}
        )
        row = receipt["rows"][0]
        assert row["desired"]["start"] == "anytime"
        assert row["observed"]["start"] == "anytime"


def test_move_and_anytime_in_one_update_preserve_the_new_project_home() -> None:
    project = Record(uuid="project", kind="project", title="Project")
    task = Record(uuid="task", kind="task", title="Task", inbox=True)
    server, library, _ = _stack(project, task)

    result = _call(server, "things_update", {
        "request_id": "0198f0ef-3923-79b6-96a8-2bf28eac0d81",
        "items": [{
            "id": "task:task",
            "set": {"into_id": "project:project", "start": "anytime"},
        }],
    })

    assert result["state"] == "applied"
    assert library.records["task"].parent_uuid == "project"
    assert library.records["task"].inbox is False


@pytest.mark.parametrize(
    "set_fields",
    [
        {"into_id": None},
        {"tags": None},
        {"checklist": None},
        {"checklist": {"update": [{"id": "check:row", "set": {"title": None}}]}},
        {"checklist": {"update": [{"id": "check:row", "set": {"status": None}}]}},
    ],
)
def test_update_rejects_explicit_null_for_non_clearable_patch_fields(
    set_fields: dict[str, object],
) -> None:
    server, _, journal = _stack(Record(
        uuid="task",
        kind="task",
        title="Task",
        checklists=[ChecklistLine(uuid="row", title="Row")],
    ))

    result = _call(server, "things_update", {
        "request_id": "0198f0ef-3923-79b6-96a8-2bf28eac0d82",
        "items": [{"id": "task:task", "set": set_fields}],
    })

    assert result["state"] == "rejected"
    assert result["code"] == "validation_error"
    assert journal.blocking_v2_operations("owner@example.com") == []


def test_start_null_clears_schedule_without_changing_home() -> None:
    project = Record(uuid="project", kind="project", title="Project")
    task = Record(
        uuid="task", kind="task", title="Task", parent_uuid="project",
        start=date(2026, 9, 1), remind="09:00", tonight=True,
    )
    server, library, _ = _stack(project, task)

    result = _call(server, "things_update", {
        "request_id": "0198f0ef-3923-79b6-96a8-2bf28eac0d80",
        "items": [{"id": "task:task", "set": {"start": None, "remind_at": None}}],
    })

    assert result["state"] == "applied"
    assert library.records["task"].parent_uuid == "project"
    assert library.records["task"].start is None
    assert library.records["task"].remind is None
    assert result["items"][0]["start"] == "anytime"
    receipt = _call(
        server, "things_receipt", {"operation_id": result["operation_id"]}
    )
    assert receipt["rows"][0]["desired"]["start"] == "anytime"
    assert receipt["rows"][0]["observed"]["start"] == "anytime"


def test_direct_tag_delta_preserves_unmentioned_and_inherited_tags() -> None:
    area = Record(uuid="area", kind="area", title="Area", tag_uuids=["inherited"])
    task = Record(uuid="task", kind="task", title="Task", area_uuid="area", tag_uuids=["keep", "remove"])
    server, library, _ = _stack(area, task)
    library.tags = {"keep": "Keep", "remove": "Remove", "add": "Add", "inherited": "Inherited"}

    result = _call(server, "things_update", {
        "request_id": "0198f0ef-3923-79b6-96a8-2bf28eac0d74",
        "items": [{"id": "task:task", "set": {"tags": {"add": ["tag:add"], "remove": ["tag:remove"]}}}],
    })

    assert result["state"] == "applied"
    assert library.records["task"].tag_uuids == ["keep", "add"]
    item = _call(server, "things_get", {"ids": ["task:task"]})["items"][0]
    assert item["direct_tag_ids"] == ["tag:keep", "tag:add"]
    assert item["inherited_tag_ids"] == ["tag:inherited"]
    receipt = _call(server, "things_receipt", {"operation_id": result["operation_id"]})
    assert receipt["rows"][0]["desired"]["direct_tag_ids"] == ["tag:keep", "tag:add"]
    assert receipt["rows"][0]["observed"]["direct_tag_ids"] == ["tag:keep", "tag:add"]


def test_checklist_patch_updates_removes_and_appends_without_reordering() -> None:
    task = Record(
        uuid="task", kind="task", title="Task",
        checklists=[
            ChecklistLine(uuid="first", title="First", sort_index=10),
            ChecklistLine(uuid="second", title="Second", sort_index=20),
            ChecklistLine(uuid="third", title="Third", sort_index=30),
        ],
    )
    server, library, _ = _stack(task)

    result = _call(server, "things_update", {
        "request_id": "0198f0ef-3923-79b6-96a8-2bf28eac0d75",
        "items": [{"id": "task:task", "set": {"checklist": {
            "add": [{"title": "Fourth"}],
            "update": [{"id": "check:second", "set": {"title": "Second changed", "status": "completed"}}],
            "remove": ["check:first"],
        }}}],
    })

    assert result["state"] == "applied"
    rows = library.records["task"].checklists
    assert [(row.uuid, row.title, row.status) for row in rows[0:2]] == [
        ("second", "Second changed", "done"), ("third", "Third", "open")
    ]
    assert rows[-1].title == "Fourth" and rows[-1].status == "open"
    receipt = _call(server, "things_receipt", {"operation_id": result["operation_id"]})
    changed = next(row for row in receipt["rows"] if row["target_id"] == "check:second")
    assert changed["desired"]["status"] == "completed"
    assert changed["observed"]["status"] == "completed"


def test_invalid_late_item_rejects_entire_batch_with_issue_before_journaling() -> None:
    records = [Record(uuid=f"task-{index}", kind="task", title=str(index)) for index in range(40)]
    server, library, journal = _stack(*records)
    items = [
        {"id": f"task:task-{index}", "set": {"title": f"Changed {index}"}}
        for index in range(39)
    ] + [{"id": "task:task-39", "set": {"start": "not-a-start"}}]

    result = _call(server, "things_update", {
        "request_id": "0198f0ef-3923-79b6-96a8-2bf28eac0d76", "items": items,
    })

    assert result["state"] == "rejected"
    assert result["issues"][0]["path"] == "items.39.set.start"
    assert result["issues"][0]["item_index"] == 39
    assert [library.records[f"task-{index}"].title for index in range(40)] == [str(index) for index in range(40)]
    assert journal.blocking_v2_operations("owner@example.com") == []


def test_structured_issue_does_not_echo_an_oversized_invalid_item_id() -> None:
    server, _, _ = _stack(Record(uuid="task", kind="task", title="Task"))

    result = _call(server, "things_update", {
        "request_id": "0198f0ef-3923-79b6-96a8-2bf28eac0d85",
        "items": [{
            "id": "task:" + "x" * 10_000,
            "set": {"start": "not-a-start"},
        }],
    })

    assert result["state"] == "rejected"
    assert all("item_id" not in issue for issue in result["issues"])
    assert len(json.dumps(result)) < 4_000


@pytest.mark.parametrize(
    "set_fields",
    [
        {"tags": {"add": ["tag:" + "x" * 10_000]}},
        {"checklist": {"remove": ["check:" + "x" * 10_000]}},
    ],
)
def test_nested_exact_ids_are_bounded_without_response_amplification(
    set_fields: dict[str, object],
) -> None:
    server, _, _ = _stack(Record(uuid="task", kind="task", title="Task"))

    result = _call(server, "things_update", {
        "request_id": "0198f0ef-3923-79b6-96a8-2bf28eac0d86",
        "items": [{"id": "task:task", "set": set_fields}],
    })

    assert result["state"] == "rejected"
    assert result["code"] == "validation_error"
    assert len(json.dumps(result)) < 4_000


def test_within_only_find_pages_and_directs_continuation() -> None:
    project = Record(uuid="project", kind="project", title="Project")
    server, _, _ = _stack(
        project,
        Record(uuid="one", kind="task", title="One", parent_uuid="project", sort_index=1),
        Record(uuid="two", kind="task", title="Two", parent_uuid="project", sort_index=2),
    )

    first = _call(server, "things_find", {"within": "project:project", "limit": 1})
    assert first["next_action"] == "continue_read"
    assert "more results remain" in str(first["instruction"]).lower()
    second = _call(server, "things_find", {"cursor": first["cursor"], "limit": 40})
    assert second["next_action"] == "none"
    assert "cursor" not in second


def test_within_only_cursor_rejects_changed_membership_snapshot() -> None:
    project = Record(uuid="project", kind="project", title="Project")
    other = Record(uuid="other", kind="project", title="Other")
    server, _, _ = _stack(
        project,
        other,
        Record(uuid="one", kind="task", title="One", parent_uuid="project", sort_index=1),
        Record(uuid="two", kind="task", title="Two", parent_uuid="project", sort_index=2),
    )

    first = _call(server, "things_find", {"within": "project:project", "limit": 1})
    moved = _call(server, "things_update", {
        "request_id": "0198f0ef-3923-79b6-96a8-2bf28eac0d83",
        "items": [{"id": "task:two", "set": {"into_id": "project:other"}}],
    })
    continued = _call(server, "things_find", {"cursor": first["cursor"], "limit": 40})

    assert moved["state"] == "applied"
    assert continued["state"] == "rejected"
    assert continued["code"] == "cursor_invalid"


def test_within_only_cursor_survives_a_retryable_cloud_outage() -> None:
    class FlakyLibrary(MemoryLibrary):
        fail_next = False

        def refresh(self, *, force: bool = False) -> None:
            if self.fail_next:
                self.fail_next = False
                raise CloudError("unavailable")

    library = FlakyLibrary([
        Record(uuid="project", kind="project", title="Project"),
        Record(uuid="one", kind="task", title="One", parent_uuid="project"),
        Record(uuid="two", kind="task", title="Two", parent_uuid="project"),
    ])
    server = ThingsMCPServer(ThingsV2(ThingsWorkspace(
        library,
        journal=MemoryJournal(),
        clock=lambda: NOW,
        account_id="owner@example.com",
    )))

    first = _call(server, "things_find", {"within": "project:project", "limit": 1})
    library.fail_next = True
    unavailable = _call(server, "things_find", {"cursor": first["cursor"], "limit": 40})
    retried = _call(server, "things_find", {"cursor": first["cursor"], "limit": 40})

    assert unavailable["code"] == "read_unavailable"
    assert unavailable["next_action"] == "retry_same"
    assert retried["state"] == "ok"
    assert retried["next_action"] == "none"


def test_within_only_cursor_retry_returns_the_same_page_and_continuation() -> None:
    server, _, _ = _stack(
        Record(uuid="project", kind="project", title="Project"),
        Record(uuid="one", kind="task", title="One", parent_uuid="project", sort_index=1),
        Record(uuid="two", kind="task", title="Two", parent_uuid="project", sort_index=2),
        Record(uuid="three", kind="task", title="Three", parent_uuid="project", sort_index=3),
    )

    first = _call(server, "things_find", {"within": "project:project", "limit": 1})
    arguments = {"cursor": first["cursor"], "limit": 1}
    second = _call(server, "things_find", arguments)
    retry = _call(server, "things_find", arguments)

    assert retry == second


def test_request_conflict_does_not_inherit_repeat_effects() -> None:
    server, _, _ = _stack()
    arguments = {
        "request_id": "0198f0ef-3923-79b6-96a8-2bf28eac0d91",
        "items": [
            {"kind": "task", "title": "First", "repeat": {"unit": "week"}}
        ],
    }
    first = _call(server, "things_capture", arguments)
    conflict = _call(
        server,
        "things_capture",
        {
            "request_id": arguments["request_id"],
            "items": [
                {"kind": "task", "title": "Different", "repeat": {"unit": "week"}}
            ],
        },
    )

    assert first["effects"]
    assert conflict["state"] == "rejected"
    assert conflict["code"] == "request_conflict"
    assert conflict.get("effects", []) == []


def test_tags_view_marks_an_incomplete_page_for_continuation() -> None:
    server, library, _ = _stack()
    library.tags = {"one": "One", "two": "Two"}

    first = _call(server, "things_view", {"view": "tags", "limit": 1})

    assert first["next_action"] == "continue_read"
    assert "more results remain" in str(first["instruction"]).lower()


def test_awaiting_owner_trash_does_not_fence_unrelated_write_and_says_so() -> None:
    server, library, _ = _stack(
        Record(uuid="trash", kind="task", title="Trash me"),
        Record(uuid="other", kind="task", title="Other"),
    )
    staged = _call(server, "things_trash", {
        "request_id": "0198f0ef-3923-79b6-96a8-2bf28eac0d77", "ids": ["task:trash"],
    })
    changed = _call(server, "things_update", {
        "request_id": "0198f0ef-3923-79b6-96a8-2bf28eac0d78",
        "items": [{"id": "task:other", "set": {"title": "Changed"}}],
    })

    assert staged["state"] == "awaiting_owner"
    assert "does not block unrelated writes" in str(staged["instruction"])
    assert changed["state"] == "applied"
    assert library.records["trash"].trashed is False


def test_starting_repeat_discloses_hidden_template_and_visible_instance_without_titles() -> None:
    server, _, _ = _stack()
    result = _call(server, "things_capture", {
        "request_id": "0198f0ef-3923-79b6-96a8-2bf28eac0d79",
        "items": [{"kind": "task", "title": "Secret owner text", "repeat": {"unit": "week"}}],
    })

    assert result["state"] == "applied"
    effect = result["effects"][0]
    assert effect["kind"] == "repeat_started"
    assert effect["template_id"].startswith("task:")
    assert effect["instance_id"].startswith("task:")
    assert "Secret owner text" not in str(effect)


def test_starting_repeat_projects_move_tags_and_checklist_into_hidden_template() -> None:
    source = Record(uuid="source", kind="project", title="Source")
    destination = Record(uuid="destination", kind="project", title="Destination")
    task = Record(
        uuid="task",
        kind="task",
        title="Task",
        parent_uuid="source",
        tag_uuids=["keep"],
        checklists=[ChecklistLine(uuid="row", title="Old row", sort_index=10)],
    )
    server, library, _ = _stack(source, destination, task)
    library.tags = {"keep": "Keep", "add": "Add"}

    result = _call(server, "things_update", {
        "request_id": "0198f0ef-3923-79b6-96a8-2bf28eac0d84",
        "items": [{"id": "task:task", "set": {
            "into_id": "project:destination",
            "tags": {"add": ["tag:add"]},
            "checklist": {
                "update": [{"id": "check:row", "set": {"title": "Updated row"}}],
                "add": [{"title": "Added row"}],
            },
            "repeat": {"unit": "week"},
        }}],
    })

    assert result["state"] == "applied"
    template = next(
        record for record in library.records.values()
        if record.recurrence.role == "template"
    )
    assert template.parent_uuid == "destination"
    assert template.tag_uuids == ["keep", "add"]
    assert [row.title for row in template.checklists] == ["Updated row", "Added row"]
    assert result["effects"][0]["template_id"] == template.id


def test_exact_get_marks_truncated_checklist_and_tag_ids() -> None:
    task = Record(
        uuid="task",
        kind="task",
        title="Task",
        tag_uuids=[f"tag-{index}" for index in range(41)],
        checklists=[
            ChecklistLine(uuid=f"row-{index}", title=f"Row {index}")
            for index in range(101)
        ],
    )
    server, library, _ = _stack(task)
    library.tags = {f"tag-{index}": f"Tag {index}" for index in range(41)}

    item = _call(server, "things_get", {"ids": ["task:task"]})["items"][0]

    assert len(item["checklist"]) == 100
    assert len(item["direct_tag_ids"]) == 40
    assert set(item["truncated_fields"]) == {"checklist", "tags"}


def test_discovery_exposes_deep_update_without_adding_tools() -> None:
    server, _, _ = _stack()
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}

    assert len(tools) == 8
    update_schema = str(tools["things_update"].input_schema)
    for field in ("into_id", "anytime", "tags", "checklist"):
        assert field in update_schema
    output_schema = str(tools["things_get"].output_schema)
    for field in ("issues", "effects", "direct_tag_ids", "inherited_tag_ids"):
        assert field in output_schema
    set_properties = (
        tools["things_update"].input_schema["properties"]["items"]["items"]
        ["properties"]["set"]["properties"]
    )
    for field in ("into_id", "tags", "checklist"):
        assert "null" not in str(set_properties[field])
    checklist_set = set_properties["checklist"]["properties"]["update"]["items"]
    checklist_set = checklist_set["properties"]["set"]["properties"]
    assert "null" not in str(checklist_set["title"])
    assert "null" not in str(checklist_set["status"])
