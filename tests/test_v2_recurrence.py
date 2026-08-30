from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from things_orchestrator.interface import ReadCall
from things_orchestrator.journal import MemoryJournal
from things_orchestrator.library import ChecklistLine, MemoryLibrary, Record
from things_orchestrator.owner_authority import (
    enroll_owner_factor,
    verified_authorization,
)
from things_orchestrator.recurrence import RecurrenceState
from things_orchestrator.v2 import ThingsV2
from things_orchestrator.workspace import ThingsWorkspace

NOW = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)
REQUESTS = (
    "0198f0ee-98d4-7bd5-91ba-8e76019b2801",
    "0198f0ee-98d4-7bd5-91ba-8e76019b2802",
    "0198f0ee-98d4-7bd5-91ba-8e76019b2803",
    "0198f0ee-98d4-7bd5-91ba-8e76019b2804",
    "0198f0ee-98d4-7bd5-91ba-8e76019b2805",
    "0198f0ee-98d4-7bd5-91ba-8e76019b2806",
    "0198f0ee-98d4-7bd5-91ba-8e76019b2807",
    "0198f0ee-98d4-7bd5-91ba-8e76019b2808",
    "0198f0ee-98d4-7bd5-91ba-8e76019b2809",
    "0198f0ee-98d4-7bd5-91ba-8e76019b2810",
    "0198f0ee-98d4-7bd5-91ba-8e76019b2811",
    "0198f0ee-98d4-7bd5-91ba-8e76019b2812",
    "0198f0ee-98d4-7bd5-91ba-8e76019b2813",
)


def _interface(*records: Record) -> tuple[ThingsV2, MemoryLibrary]:
    library = MemoryLibrary(list(records))
    workspace = ThingsWorkspace(
        library,
        journal=MemoryJournal(),
        clock=lambda: NOW,
        account_id="owner@example.com",
    )
    return ThingsV2(workspace), library


def _repeating_pair() -> tuple[Record, Record]:
    template = Record(
        uuid="template",
        kind="task",
        title="Plan week",
        recurrence=RecurrenceState(
            role="template",
            repeat_type="fixed",
            rule={
                "tp": 0,
                "fu": 256,
                "fa": 1,
                "of": [{"wd": 1}],
                "future": {"preserve": True},
            },
        ),
    )
    current = Record(
        uuid="current",
        kind="task",
        title="Plan week",
        recurrence=RecurrenceState(
            role="instance",
            repeat_type="fixed",
            template_uuid="template",
            links=("template",),
        ),
    )
    return template, current


def test_v2_capture_creates_one_visible_current_copy_and_hidden_template() -> None:
    interface, library = _interface()

    result = interface.dispatch(
        "things_capture",
        {
            "request_id": REQUESTS[0],
            "items": [
                {
                    "kind": "task",
                    "title": "Monthly close",
                    "repeat": {
                        "mode": "fixed",
                        "unit": "month",
                        "interval": 1,
                    },
                }
            ],
        },
    )

    assert result.state == "applied"
    assert len(result.items) == 1
    assert result.items[0].recurrence is not None
    assert result.items[0].recurrence.kind == "fixed_instance"
    templates = [
        item for item in library.records.values() if item.recurrence.role == "template"
    ]
    currents = [
        item for item in library.records.values() if item.recurrence.role == "instance"
    ]
    assert len(templates) == len(currents) == 1
    assert templates[0].recurrence.rule is not None
    assert templates[0].recurrence.rule["fu"] == 8
    assert currents[0].recurrence.template_uuid == templates[0].uuid
    assert result.operation_id is not None
    receipt = interface.dispatch(
        "things_receipt", {"operation_id": result.operation_id}
    )
    desired = [row["desired"]["recurrence"] for row in receipt.rows]
    assert desired[0]["kind"] == "template"
    assert desired[0]["created_through"] == "2026-08-31"
    assert desired[0]["generated_count"] == 1
    assert desired[1]["kind"] == "fixed_instance"
    assert desired[1]["template_id"] == templates[0].id


def test_v2_capture_clones_a_repeating_project_graph() -> None:
    interface, library = _interface()

    result = interface.dispatch(
        "things_capture",
        {
            "request_id": REQUESTS[10],
            "items": [
                {
                    "kind": "project",
                    "title": "Weekly launch",
                    "tasks": [
                        {"title": "Prepare"},
                        {"title": "Publish"},
                    ],
                    "repeat": {
                        "mode": "after_completion",
                        "unit": "week",
                    },
                }
            ],
        },
    )

    assert result.state == "applied"
    template = next(
        item
        for item in library.records.values()
        if item.kind == "project" and item.recurrence.role == "template"
    )
    current = next(
        item
        for item in library.records.values()
        if item.kind == "project" and item.recurrence.role == "instance"
    )
    assert template.recurrence_instance_count == 1
    assert current.recurrence.template_uuid == template.uuid
    assert {
        item.title for item in library.records.values() if item.parent_uuid == template.uuid
    } == {"Prepare", "Publish"}
    assert {
        item.title for item in library.records.values() if item.parent_uuid == current.uuid
    } == {"Prepare", "Publish"}
    assert result.items[0].recurrence is not None
    assert result.items[0].recurrence.template_id == template.id


def test_v2_project_conversion_clones_headings_tasks_and_checklists() -> None:
    project = Record(uuid="project", kind="project", title="Release train")
    heading = Record(
        uuid="heading",
        kind="task",
        title="Ship",
        heading=True,
        parent_uuid=project.uuid,
        sort_index=1024,
    )
    root_task = Record(
        uuid="root-task",
        kind="task",
        title="Announce",
        parent_uuid=project.uuid,
        sort_index=2048,
        checklists=[ChecklistLine(uuid="check-a", title="Draft", sort_index=1)],
    )
    heading_task = Record(
        uuid="heading-task",
        kind="task",
        title="Deploy",
        heading_uuid=heading.uuid,
        sort_index=3072,
        checklists=[ChecklistLine(uuid="check-b", title="Verify", sort_index=2)],
    )
    interface, library = _interface(project, heading, root_task, heading_task)

    result = interface.dispatch(
        "things_update",
        {
            "request_id": REQUESTS[11],
            "items": [
                {
                    "id": project.id,
                    "set": {
                        "repeat": {
                            "mode": "after_completion",
                            "unit": "week",
                        }
                    },
                }
            ],
        },
    )

    assert result.state == "applied"
    template = next(
        item
        for item in library.records.values()
        if item.kind == "project" and item.recurrence.role == "template"
    )
    assert library.records["project"].recurrence.template_uuid == template.uuid
    cloned_heading = next(
        item
        for item in library.records.values()
        if item.heading and item.parent_uuid == template.uuid
    )
    cloned_root = next(
        item
        for item in library.records.values()
        if item.title == "Announce" and item.parent_uuid == template.uuid
    )
    cloned_under_heading = next(
        item
        for item in library.records.values()
        if item.title == "Deploy" and item.heading_uuid == cloned_heading.uuid
    )
    assert cloned_under_heading.parent_uuid is None
    assert [row.title for row in cloned_root.checklists] == ["Draft"]
    assert [row.title for row in cloned_under_heading.checklists] == ["Verify"]
    assert all(
        item.recurrence.role == "none"
        for item in (cloned_heading, cloned_root, cloned_under_heading)
    )
    assert result.items[0].recurrence is not None
    assert result.items[0].recurrence.template_id == template.id


def test_v2_project_conversion_does_not_clone_trashed_descendants() -> None:
    project = Record(uuid="project-trash", kind="project", title="Release train")
    heading = Record(
        uuid="heading-trash",
        kind="task",
        title="Ship",
        heading=True,
        parent_uuid=project.uuid,
    )
    direct = Record(
        uuid="deleted-direct",
        kind="task",
        title="Deleted direct",
        parent_uuid=project.uuid,
        trashed=True,
    )
    assigned = Record(
        uuid="deleted-assigned",
        kind="task",
        title="Deleted assigned",
        heading_uuid=heading.uuid,
        trashed=True,
    )
    interface, library = _interface(project, heading, direct, assigned)

    result = interface.dispatch(
        "things_update",
        {
            "request_id": "0198f0ee-98d4-7bd5-91ba-8e76019b2814",
            "items": [
                {
                    "id": project.id,
                    "set": {"repeat": {"unit": "week"}},
                }
            ],
        },
    )

    assert result.state == "applied"
    template = next(
        item
        for item in library.records.values()
        if item.kind == "project" and item.recurrence.role == "template"
    )
    assert not {
        item.title
        for item in library.records.values()
        if item.uuid not in {direct.uuid, assigned.uuid}
        and item.title.startswith("Deleted")
        and (item.parent_uuid == template.uuid or item.heading_uuid is not None)
    }


def test_v2_rejects_project_repeat_conversion_with_nested_repeat_items() -> None:
    project = Record(uuid="project-nested-repeat", kind="project", title="Launch")
    nested_template = Record(
        uuid="nested-template",
        kind="task",
        title="Daily hidden",
        parent_uuid=project.uuid,
        recurrence=RecurrenceState(
            role="template",
            repeat_type="fixed",
            rule={"tp": 0, "fu": 16, "fa": 1, "of": []},
        ),
    )
    nested_current = Record(
        uuid="nested-current",
        kind="task",
        title="Daily current",
        parent_uuid=project.uuid,
        recurrence=RecurrenceState(
            role="instance",
            repeat_type="fixed",
            template_uuid=nested_template.uuid,
            links=(nested_template.uuid,),
        ),
    )
    interface, library = _interface(project, nested_template, nested_current)

    result = interface.dispatch(
        "things_update",
        {
            "request_id": "0198f0ee-98d4-7bd5-91ba-8e76019b2815",
            "items": [
                {
                    "id": project.id,
                    "set": {"repeat": {"unit": "week"}},
                }
            ],
        },
    )

    assert result.state == "rejected"
    assert library.records[project.uuid].recurrence.role == "none"
    assert len(library.records) == 3


def test_v2_rejects_project_repeat_conversion_with_nested_projects() -> None:
    project = Record(uuid="project-nested-project", kind="project", title="Launch")
    nested = Record(
        uuid="nested-project",
        kind="project",
        title="Subproject",
        parent_uuid=project.uuid,
    )
    child = Record(
        uuid="nested-project-task",
        kind="task",
        title="Nested task",
        parent_uuid=nested.uuid,
    )
    interface, library = _interface(project, nested, child)

    result = interface.dispatch(
        "things_update",
        {
            "request_id": "0198f0ee-98d4-7bd5-91ba-8e76019b2818",
            "items": [
                {
                    "id": project.id,
                    "set": {"repeat": {"unit": "week"}},
                }
            ],
        },
    )

    assert result.state == "rejected"
    assert library.records[project.uuid].recurrence.role == "none"
    assert set(library.records) == {project.uuid, nested.uuid, child.uuid}


def test_v2_create_next_copy_clones_the_project_template_and_advances_count() -> None:
    template = Record(
        uuid="template-project",
        kind="project",
        title="Release train",
        recurrence=RecurrenceState(
            role="template",
            repeat_type="after_completion",
            rule={"tp": 1, "fu": 256, "fa": 1, "of": []},
        ),
        recurrence_instance_count=1,
        someday=True,
    )
    template_heading = Record(
        uuid="template-heading",
        kind="task",
        title="Ship",
        heading=True,
        parent_uuid=template.uuid,
    )
    template_task = Record(
        uuid="template-task",
        kind="task",
        title="Deploy",
        heading_uuid=template_heading.uuid,
        checklists=[ChecklistLine(uuid="template-check", title="Verify")],
    )
    current = Record(
        uuid="current-project",
        kind="project",
        title="Release train",
        recurrence=RecurrenceState(
            role="instance",
            repeat_type="after_completion",
            template_uuid=template.uuid,
            links=(template.uuid,),
        ),
    )
    interface, library = _interface(
        template, template_heading, template_task, current
    )

    result = interface.dispatch(
        "things_update",
        {
            "request_id": REQUESTS[12],
            "items": [
                {
                    "id": current.id,
                    "set": {"repeat": {"create_next": True}},
                }
            ],
        },
    )

    assert result.state == "applied"
    assert library.records[template.uuid].recurrence_instance_count == 2
    copies = [
        item
        for item in library.records.values()
        if item.kind == "project"
        and item.recurrence.role == "instance"
        and item.uuid != current.uuid
    ]
    assert len(copies) == 1
    next_copy = copies[0]
    assert next_copy.start == NOW.date()
    assert next_copy.leavable is True
    cloned_heading = next(
        item
        for item in library.records.values()
        if item.heading and item.parent_uuid == next_copy.uuid
    )
    cloned_task = next(
        item
        for item in library.records.values()
        if item.title == "Deploy" and item.heading_uuid == cloned_heading.uuid
    )
    assert cloned_task.leavable is True
    assert [row.title for row in cloned_task.checklists] == ["Verify"]
    assert next_copy.id in {item.id for item in result.items}


def test_v2_rejects_create_next_for_project_with_nested_repeat_items() -> None:
    template = Record(
        uuid="nested-project-template",
        kind="project",
        title="Release train",
        recurrence=RecurrenceState(
            role="template",
            repeat_type="fixed",
            rule={"tp": 0, "fu": 256, "fa": 1, "of": [{"wd": 1}]},
        ),
        recurrence_instance_count=1,
    )
    nested = Record(
        uuid="nested-repeat-child",
        kind="task",
        title="Daily child",
        parent_uuid=template.uuid,
        recurrence=RecurrenceState(
            role="template",
            repeat_type="fixed",
            rule={"tp": 0, "fu": 16, "fa": 1, "of": []},
        ),
    )
    current = Record(
        uuid="nested-project-current",
        kind="project",
        title="Release train",
        recurrence=RecurrenceState(
            role="instance",
            repeat_type="fixed",
            template_uuid=template.uuid,
            links=(template.uuid,),
        ),
    )
    interface, library = _interface(template, nested, current)

    result = interface.dispatch(
        "things_update",
        {
            "request_id": "0198f0ee-98d4-7bd5-91ba-8e76019b2816",
            "items": [
                {
                    "id": current.id,
                    "set": {"repeat": {"create_next": True}},
                }
            ],
        },
    )

    assert result.state == "rejected"
    assert library.records[template.uuid].recurrence_instance_count == 1
    assert len(library.records) == 3


def test_v2_reads_repeat_relationship_and_rule_from_current_copy() -> None:
    interface, _ = _interface(*_repeating_pair())

    result = interface.dispatch("things_get", {"ids": ["task:current"]})

    recurrence = result.items[0].recurrence
    assert recurrence is not None
    assert recurrence.model_dump() == {
        "kind": "fixed_instance",
        "engine": "rt1",
        "template_id": "task:template",
        "mode": "fixed",
        "unit": "week",
        "interval": 1,
        "weekdays": ["monday"],
        "linked_item_ids": [],
        "paused": False,
        "created_through": None,
        "generated_count": 0,
        "completed_on": None,
        "next_on": None,
        "on": [
            {
                "month": None,
                "day": None,
                "weekday": "monday",
                "ordinal": None,
            }
        ],
        "until": None,
        "start_early_days": None,
        "reminder_time": None,
        "adds_deadline": False,
    }


def test_v2_repeating_view_returns_templates_without_current_copies() -> None:
    interface, _ = _interface(*_repeating_pair())

    result = interface.dispatch("things_view", {"view": "repeating"})

    assert result.state == "ok"
    assert {item.id for item in result.items} == {"task:template"}
    assert all(item.recurrence is not None for item in result.items)


def test_v2_edits_future_rule_through_current_copy_and_preserves_opaque_fields() -> (
    None
):
    interface, library = _interface(*_repeating_pair())

    result = interface.dispatch(
        "things_update",
        {
            "request_id": REQUESTS[1],
            "items": [
                {
                    "id": "task:current",
                    "set": {
                        "title": "Plan the week",
                        "repeat": {"interval": 2},
                    },
                }
            ],
        },
    )

    assert result.state == "applied"
    assert library.records["current"].title == "Plan the week"
    rule = library.records["template"].recurrence.rule
    assert rule is not None
    assert rule["fa"] == 2
    assert rule["future"] == {"preserve": True}
    assert [item.id for item in result.items] == ["task:current"]
    assert result.items[0].recurrence is not None
    assert result.items[0].recurrence.interval == 2


def test_v2_stop_repeat_needs_owner_and_removes_hidden_template(
    tmp_path: Path,
) -> None:
    factor = tmp_path / "owner-factor.json"
    enroll_owner_factor("correct horse battery staple", path=factor)
    journal = MemoryJournal(
        owner_public_key=factor.with_name("owner-public-key.ed25519").read_bytes()
    )
    template, current = _repeating_pair()
    library = MemoryLibrary([template, current])
    workspace = ThingsWorkspace(
        library,
        journal=journal,
        clock=lambda: NOW,
        account_id="owner@example.com",
    )
    interface = ThingsV2(workspace)

    staged = interface.dispatch(
        "things_update",
        {
            "request_id": REQUESTS[2],
            "items": [{"id": "task:current", "set": {"repeat": {"remove": True}}}],
        },
    )

    assert staged.state == "awaiting_owner"
    operation = journal.get_v2_operation(staged.operation_id or "")
    assert operation is not None
    authorization = verified_authorization(
        operation,
        action="approve",
        passphrase="correct horse battery staple",
        path=factor,
    )
    assert authorization is not None
    result = workspace.host_approve_v2(operation.operation_id, authorization)

    assert result["state"] == "applied"
    assert "template" not in library.records
    assert library.records["current"].recurrence == RecurrenceState()
    found = interface.dispatch("things_find", {"text": "Plan week"})
    assert [item.id for item in found.items] == ["task:current"]


def test_v2_rejects_stop_combined_with_an_ordinary_template_update() -> None:
    template, current = _repeating_pair()
    interface, library = _interface(template, current)

    with pytest.raises(
        ValidationError,
        match="repeat removal cannot combine with ordinary fields",
    ):
        interface.dispatch(
            "things_update",
            {
                "request_id": "0198f0ee-98d4-7bd5-91ba-8e76019b2821",
                "items": [
                    {
                        "id": template.id,
                        "set": {
                            "title": "Renamed",
                            "repeat": {"remove": True},
                        },
                    }
                ],
            },
        )

    assert library.records[template.uuid].title == "Plan week"
    assert library.records[template.uuid].recurrence.role == "template"
    assert library.records[current.uuid].recurrence.role == "instance"


def test_v2_rejects_cross_item_update_of_a_stopped_template() -> None:
    template, current = _repeating_pair()
    interface, library = _interface(template, current)

    result = interface.dispatch(
        "things_update",
        {
            "request_id": "0198f0ee-98d4-7bd5-91ba-8e76019b2822",
            "items": [
                {"id": current.id, "set": {"repeat": {"remove": True}}},
                {"id": template.id, "set": {"title": "Renamed"}},
            ],
        },
    )

    assert result.state == "rejected"
    assert result.code == "validation_error"
    assert library.records[template.uuid].title == "Plan week"
    assert library.records[template.uuid].recurrence.role == "template"
    assert library.records[current.uuid].recurrence.role == "instance"


def test_v2_rejects_stopping_the_same_series_twice_in_one_batch() -> None:
    template, current = _repeating_pair()
    other = Record(
        uuid="current-two",
        kind="task",
        title="Plan week",
        recurrence=RecurrenceState(
            role="instance",
            repeat_type="fixed",
            template_uuid=template.uuid,
            links=(template.uuid,),
        ),
    )
    interface, library = _interface(template, current, other)

    result = interface.dispatch(
        "things_update",
        {
            "request_id": "0198f0ee-98d4-7bd5-91ba-8e76019b2823",
            "items": [
                {"id": current.id, "set": {"repeat": {"remove": True}}},
                {"id": other.id, "set": {"repeat": {"remove": True}}},
            ],
        },
    )

    assert result.state == "rejected"
    assert result.code == "validation_error"
    assert set(library.records) == {template.uuid, current.uuid, other.uuid}
    assert all(
        record.recurrence.role != "none" for record in library.records.values()
    )


def test_v2_rejects_create_next_twice_for_one_series_in_one_batch() -> None:
    template, current = _repeating_pair()
    other = Record(
        uuid="current-two",
        kind="task",
        title="Plan week",
        recurrence=RecurrenceState(
            role="instance",
            repeat_type="fixed",
            template_uuid=template.uuid,
            links=(template.uuid,),
        ),
    )
    interface, library = _interface(template, current, other)

    result = interface.dispatch(
        "things_update",
        {
            "request_id": "0198f0ee-98d4-7bd5-91ba-8e76019b2824",
            "items": [
                {"id": current.id, "set": {"repeat": {"create_next": True}}},
                {"id": other.id, "set": {"repeat": {"create_next": True}}},
            ],
        },
    )

    assert result.state == "rejected"
    assert result.code == "validation_error"
    assert set(library.records) == {template.uuid, current.uuid, other.uuid}
    assert library.records[template.uuid].recurrence_instance_count == 0


def test_v2_rejects_conflicting_rule_edits_for_one_series_in_one_batch() -> None:
    template, current = _repeating_pair()
    other = Record(
        uuid="current-two",
        kind="task",
        title="Plan week",
        recurrence=RecurrenceState(
            role="instance",
            repeat_type="fixed",
            template_uuid=template.uuid,
            links=(template.uuid,),
        ),
    )
    interface, library = _interface(template, current, other)

    result = interface.dispatch(
        "things_update",
        {
            "request_id": "0198f0ee-98d4-7bd5-91ba-8e76019b2825",
            "items": [
                {"id": current.id, "set": {"repeat": {"interval": 2}}},
                {"id": other.id, "set": {"repeat": {"interval": 3}}},
            ],
        },
    )

    assert result.state == "rejected"
    assert result.code == "validation_error"
    assert library.records[template.uuid].recurrence.rule is not None
    assert library.records[template.uuid].recurrence.rule["fa"] == 1


def test_v2_stop_repeating_project_removes_only_hidden_template_graph(
    tmp_path: Path,
) -> None:
    factor = tmp_path / "owner-factor.json"
    enroll_owner_factor("correct horse battery staple", path=factor)
    journal = MemoryJournal(
        owner_public_key=factor.with_name("owner-public-key.ed25519").read_bytes()
    )
    template = Record(
        uuid="project-template",
        kind="project",
        title="Release train",
        recurrence=RecurrenceState(
            role="template",
            repeat_type="fixed",
            rule={"tp": 0, "fu": 256, "fa": 1, "of": [{"wd": 1}]},
        ),
    )
    template_heading = Record(
        uuid="project-template-heading",
        kind="task",
        title="Ship",
        heading=True,
        parent_uuid=template.uuid,
    )
    template_task = Record(
        uuid="project-template-task",
        kind="task",
        title="Deploy",
        heading_uuid=template_heading.uuid,
    )
    current = Record(
        uuid="project-current",
        kind="project",
        title="Release train",
        recurrence=RecurrenceState(
            role="instance",
            repeat_type="fixed",
            template_uuid=template.uuid,
            links=(template.uuid,),
        ),
    )
    current_heading = Record(
        uuid="project-current-heading",
        kind="task",
        title="Ship",
        heading=True,
        parent_uuid=current.uuid,
    )
    current_task = Record(
        uuid="project-current-task",
        kind="task",
        title="Deploy",
        heading_uuid=current_heading.uuid,
    )
    library = MemoryLibrary(
        [
            template,
            template_heading,
            template_task,
            current,
            current_heading,
            current_task,
        ]
    )
    workspace = ThingsWorkspace(
        library,
        journal=journal,
        clock=lambda: NOW,
        account_id="owner@example.com",
    )
    interface = ThingsV2(workspace)

    staged = interface.dispatch(
        "things_update",
        {
            "request_id": "0198f0ee-98d4-7bd5-91ba-8e76019b2819",
            "items": [
                {"id": current.id, "set": {"repeat": {"remove": True}}}
            ],
        },
    )
    operation = journal.get_v2_operation(staged.operation_id or "")
    assert staged.state == "awaiting_owner" and operation is not None
    assert "scope:project:project-template" in operation.manifest["preconditions"]
    authorization = verified_authorization(
        operation,
        action="approve",
        passphrase="correct horse battery staple",
        path=factor,
    )
    assert authorization is not None

    result = workspace.host_approve_v2(operation.operation_id, authorization)

    assert result["state"] == "applied"
    assert set(library.records) == {
        current.uuid,
        current_heading.uuid,
        current_task.uuid,
    }
    assert library.records[current.uuid].recurrence == RecurrenceState()


def test_v2_pauses_and_resumes_the_template_through_the_current_copy() -> None:
    interface, library = _interface(*_repeating_pair())

    paused = interface.dispatch(
        "things_update",
        {
            "request_id": REQUESTS[4],
            "items": [
                {"id": "task:current", "set": {"repeat": {"paused": True}}}
            ],
        },
    )
    resumed = interface.dispatch(
        "things_update",
        {
            "request_id": REQUESTS[5],
            "items": [
                {"id": "task:current", "set": {"repeat": {"paused": False}}}
            ],
        },
    )

    assert paused.state == resumed.state == "applied"
    assert paused.items[0].recurrence is not None
    assert paused.items[0].recurrence.paused is True
    assert resumed.items[0].recurrence is not None
    assert resumed.items[0].recurrence.paused is False
    assert library.records["template"].recurrence.paused is False


def test_v2_creates_selected_monthly_dates_with_an_end_date() -> None:
    interface, library = _interface()

    result = interface.dispatch(
        "things_capture",
        {
            "request_id": REQUESTS[6],
            "items": [
                {
                    "kind": "task",
                    "title": "Payroll",
                    "repeat": {
                        "unit": "month",
                        "on": [
                            {"day": 15},
                            {"weekday": "tuesday", "ordinal": 3},
                            {"day": -1},
                        ],
                        "until": "2027-08-30",
                    },
                }
            ],
        },
    )

    assert result.state == "applied"
    template = next(
        item for item in library.records.values() if item.recurrence.role == "template"
    )
    assert template.recurrence.rule is not None
    assert template.recurrence.rule["of"] == [
        {"dy": 14},
        {"wd": 2, "wdo": 3},
        {"dy": -1},
    ]
    assert template.recurrence.rule["rc"] == 0
    recurrence = result.items[0].recurrence
    assert recurrence is not None
    assert recurrence.until == "2027-08-30"
    assert [value.model_dump() for value in recurrence.on] == [
        {"month": None, "day": 15, "weekday": None, "ordinal": None},
        {"month": None, "day": None, "weekday": "tuesday", "ordinal": 3},
        {"month": None, "day": -1, "weekday": None, "ordinal": None},
    ]


def test_v2_edits_a_monthly_rule_to_the_fifth_weekday() -> None:
    template, current = _repeating_pair()
    template.recurrence = RecurrenceState(
        role="template",
        repeat_type="fixed",
        rule={"tp": 0, "fu": 8, "fa": 1, "of": [{"wd": 2, "wdo": 3}]},
    )
    interface, library = _interface(template, current)

    result = interface.dispatch(
        "things_update",
        {
            "request_id": "0198f0ee-98d4-7bd5-91ba-8e76019b2820",
            "items": [
                {
                    "id": current.id,
                    "set": {
                        "repeat": {
                            "on": [{"weekday": "tuesday", "ordinal": 5}]
                        }
                    },
                }
            ],
        },
    )

    assert result.state == "applied"
    assert library.records[template.uuid].recurrence.rule is not None
    assert library.records[template.uuid].recurrence.rule["of"] == [
        {"wd": 2, "wdo": 5}
    ]


def test_v2_reads_known_rt2_payload_semantically_without_mutating_it() -> None:
    interface, library = _interface(
        Record(
            uuid="rt2",
            kind="task",
            title="Next generation",
            repeater={
                "v": 1,
                "t": 0,
                "pfu": 2,
                "pfa": 2,
                "po": [{"wd": 2, "wo": 3}, {"d": -1}],
                "os": 3,
                "aa": 32_400,
                "ad": True,
                "ead": 1_798_675_200,
            },
        )
    )

    result = interface.dispatch("things_get", {"ids": ["task:rt2"]})

    recurrence = result.items[0].recurrence
    assert recurrence is not None
    assert recurrence.engine == "rt2"
    assert recurrence.mode == "fixed"
    assert recurrence.unit == "month"
    assert recurrence.interval == 2
    assert recurrence.start_early_days == 3
    assert recurrence.reminder_time == "09:00"
    assert recurrence.adds_deadline is True
    assert [value.model_dump() for value in recurrence.on] == [
        {"month": None, "day": None, "weekday": "tuesday", "ordinal": 3},
        {"month": None, "day": -1, "weekday": None, "ordinal": None},
    ]
    assert library.records["rt2"].repeater is not None


def test_v2_reads_native_fifth_weekday_rt2_selector() -> None:
    interface, _ = _interface(
        Record(
            uuid="rt2-fifth-weekday",
            kind="task",
            title="Fifth Tuesday",
            repeater={
                "v": 1,
                "t": 0,
                "pfu": 2,
                "pfa": 1,
                "po": [{"wd": 2, "wo": 5}],
            },
        )
    )

    result = interface.dispatch(
        "things_get", {"ids": ["task:rt2-fifth-weekday"]}
    )

    recurrence = result.items[0].recurrence
    assert recurrence is not None
    assert [selector.model_dump() for selector in recurrence.on] == [
        {"month": None, "day": None, "weekday": "tuesday", "ordinal": 5}
    ]


def test_v2_revision_changes_when_rt2_repeater_changes() -> None:
    interface, library = _interface(
        Record(
            uuid="rt2-revision",
            kind="task",
            title="Native RT2",
            repeater={"v": 1, "t": 0, "pfu": 0, "pfa": 1},
        )
    )
    before = interface.workspace.read(ReadCall(id="task:rt2-revision"))
    before_revision = before.items[0].revision

    library.records["rt2-revision"].repeater = {
        "v": 1,
        "t": 0,
        "pfu": 0,
        "pfa": 2,
    }
    after = interface.workspace.read(ReadCall(id="task:rt2-revision"))

    assert before_revision is not None
    assert after.items[0].revision != before_revision


def test_v2_completes_after_completion_copy_and_advances_its_template() -> None:
    template, current = _repeating_pair()
    template.recurrence = RecurrenceState(
        role="template",
        repeat_type="after_completion",
        rule={"tp": 1, "fu": 256, "fa": 1, "of": []},
    )
    current.recurrence = RecurrenceState(
        role="instance",
        repeat_type="after_completion",
        template_uuid="template",
        links=("template",),
    )
    interface, library = _interface(template, current)

    result = interface.dispatch(
        "things_complete",
        {"request_id": REQUESTS[3], "ids": ["task:current"]},
    )

    assert result.state == "applied"
    assert library.records["current"].status == "done"
    assert library.records["template"].status == "open"
    assert library.records["template"].recurrence.role == "template"
    assert library.records["template"].recurrence_completed_on == NOW.date()
    assert library.records["template"].recurrence_next_on.isoformat() == "2026-09-06"


def test_v2_does_not_advance_an_already_completed_repeat_copy_again() -> None:
    template, current = _repeating_pair()
    template.recurrence = RecurrenceState(
        role="template",
        repeat_type="after_completion",
        rule={"tp": 1, "fu": 256, "fa": 1, "of": []},
    )
    template.recurrence_completed_on = date(2026, 8, 30)
    template.recurrence_next_on = date(2026, 9, 6)
    current.status = "done"
    current.recurrence = RecurrenceState(
        role="instance",
        repeat_type="after_completion",
        template_uuid=template.uuid,
        links=(template.uuid,),
    )
    later = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)
    library = MemoryLibrary([template, current])
    interface = ThingsV2(
        ThingsWorkspace(
            library,
            journal=MemoryJournal(),
            clock=lambda: later,
            account_id="owner@example.com",
        )
    )

    result = interface.dispatch(
        "things_complete",
        {
            "request_id": "0198f0ee-98d4-7bd5-91ba-8e76019b2817",
            "ids": [current.id],
        },
    )

    assert result.state == "unchanged"
    assert library.records[template.uuid].recurrence_completed_on == date(2026, 8, 30)
    assert library.records[template.uuid].recurrence_next_on == date(2026, 9, 6)


def test_v2_rejects_selected_dates_incompatible_with_the_existing_unit() -> None:
    template, current = _repeating_pair()
    template.recurrence = RecurrenceState(
        role="template",
        repeat_type="fixed",
        rule={"tp": 0, "fu": 16, "fa": 1, "of": [], "sr": 1_788_048_000},
    )
    interface, library = _interface(template, current)

    result = interface.dispatch(
        "things_update",
        {
            "request_id": REQUESTS[7],
            "items": [
                {"id": "task:current", "set": {"repeat": {"on": [{"day": 15}]}}}
            ],
        },
    )

    assert result.state == "rejected"
    assert library.records["template"].recurrence.rule == template.recurrence.rule


def test_v2_rejects_end_date_for_existing_after_completion_rule() -> None:
    template, current = _repeating_pair()
    template.recurrence = RecurrenceState(
        role="template",
        repeat_type="after_completion",
        rule={"tp": 1, "fu": 256, "fa": 1, "of": [], "sr": 1_788_048_000},
    )
    current.recurrence = RecurrenceState(
        role="instance",
        repeat_type="after_completion",
        template_uuid="template",
        links=("template",),
    )
    interface, library = _interface(template, current)

    result = interface.dispatch(
        "things_update",
        {
            "request_id": REQUESTS[8],
            "items": [
                {
                    "id": "task:current",
                    "set": {"repeat": {"until": "2026-12-31"}},
                }
            ],
        },
    )

    assert result.state == "rejected"
    assert library.records["template"].recurrence.rule == template.recurrence.rule


def test_v2_never_layers_rt1_repetition_over_an_rt2_item() -> None:
    rt2 = Record(
        uuid="rt2",
        kind="task",
        title="New engine",
        repeater={"v": 1, "t": 0, "pfu": 0, "pfa": 1},
    )
    interface, library = _interface(rt2)

    result = interface.dispatch(
        "things_update",
        {
            "request_id": REQUESTS[9],
            "items": [
                {
                    "id": "task:rt2",
                    "set": {"repeat": {"unit": "day", "interval": 2}},
                }
            ],
        },
    )

    assert result.state == "rejected"
    assert library.records["rt2"].repeater == rt2.repeater
    assert library.records["rt2"].recurrence == RecurrenceState()
    assert len(library.records) == 1


def test_v2_rejects_explicit_null_repeat_without_fencing_the_account() -> None:
    interface, library = _interface(Record(uuid="plain", kind="task", title="Plain"))

    with pytest.raises(ValidationError, match="repeat null is not supported"):
        interface.dispatch(
            "things_update",
            {
                "request_id": "0198f0ee-98d4-7bd5-91ba-8e76019b2811",
                "items": [{"id": "task:plain", "set": {"repeat": None}}],
            },
        )
    applied = interface.dispatch(
        "things_update",
        {
            "request_id": "0198f0ee-98d4-7bd5-91ba-8e76019b2812",
            "items": [{"id": "task:plain", "set": {"title": "Still writable"}}],
        },
    )

    assert applied.state == "applied"
    assert library.records["plain"].title == "Still writable"


def test_v2_omits_malformed_persisted_repeat_selectors_instead_of_crashing() -> None:
    rt1 = Record(
        uuid="bad-rt1",
        kind="task",
        title="Bad RT1",
        recurrence=RecurrenceState(
            role="template",
            repeat_type="fixed",
            rule={"tp": 0, "fu": 32, "fa": 1, "of": [{"dy": 99}]},
        ),
    )
    rt2 = Record(
        uuid="bad-rt2",
        kind="task",
        title="Bad RT2",
        repeater={
            "v": 1,
            "t": 0,
            "pfu": 2,
            "pfa": 1,
            "po": [{"d": 99}],
            "ead": 10**30,
        },
    )
    interface, _ = _interface(rt1, rt2)

    result = interface.dispatch(
        "things_get", {"ids": ["task:bad-rt1", "task:bad-rt2"]}
    )

    assert result.state == "ok"
    assert all(item.recurrence is not None for item in result.items)
    assert all(item.recurrence.on == [] for item in result.items if item.recurrence)
    assert result.items[1].recurrence is not None
    assert result.items[1].recurrence.until is None
