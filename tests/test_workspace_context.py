from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from things_orchestrator.context import MemoryContextStore, SQLiteContextStore
from things_orchestrator.interface import ApproveCall, CommitCall, ReadCall
from things_orchestrator.journal import MemoryJournal
from things_orchestrator.library import ChecklistLine, MemoryLibrary, Record
from things_orchestrator.recurrence import RecurrenceState
from things_orchestrator.workspace import ThingsWorkspace

NOW = datetime(2026, 8, 16, 12, tzinfo=timezone.utc)


def contextual_workspace(
    records: list[Record],
    *,
    now: list[datetime] | None = None,
    store: MemoryContextStore | None = None,
    account_id: str = "owner-a",
) -> tuple[ThingsWorkspace, MemoryLibrary, MemoryContextStore]:
    current = now or [NOW]
    context_store = store or MemoryContextStore(
        clock=lambda: current[0], token_factory=lambda: "ctx_12345678"
    )
    library = MemoryLibrary(records)
    return (
        ThingsWorkspace(
            library,
            journal=MemoryJournal(),
            clock=lambda: current[0],
            context_store=context_store,
            account_id=account_id,
        ),
        library,
        context_store,
    )


def test_change_read_returns_complete_dependency_context() -> None:
    project = Record(uuid="project", kind="project", title="Launch")
    heading = Record(
        uuid="heading",
        kind="task",
        title="Next",
        parent_uuid=project.uuid,
        heading=True,
    )
    template = Record(
        uuid="template",
        kind="task",
        title="Review",
        parent_uuid=project.uuid,
        heading_uuid=heading.uuid,
        recurrence=RecurrenceState(
            role="template",
            repeat_type="fixed",
            rule={"tp": 0, "fu": 256, "fa": 1, "of": [{"wd": 1}]},
        ),
    )
    task = Record(
        uuid="task",
        kind="task",
        title="Review",
        notes="Use current figures.",
        parent_uuid=project.uuid,
        heading_uuid=heading.uuid,
        checklists=[ChecklistLine("check", "Open dashboard")],
        recurrence=RecurrenceState(
            role="instance",
            repeat_type="fixed",
            template_uuid=template.uuid,
            links=(template.uuid,),
        ),
    )
    workspace, _library, _store = contextual_workspace(
        [project, heading, template, task]
    )

    result = workspace.read(ReadCall(purpose="change", id=task.id, limit=10))

    assert result.status == "ok"
    assert result.context and result.context.complete
    assert result.context.purpose == "change"
    assert [item.id for item in result.items] == [
        task.id,
        project.id,
        heading.id,
        template.id,
    ]
    assert len({item.ref for item in result.items}) == 4
    assert result.items[0].notes_markdown == "Use current figures."
    assert result.items[0].checklist[0].title == "Open dashboard"


@pytest.mark.parametrize("with_heading", [False, True])
def test_task_change_context_moves_across_projects_in_one_commit(
    with_heading: bool,
) -> None:
    source = Record(uuid="source", kind="project", title="Source")
    destination = Record(uuid="destination", kind="project", title="Destination")
    source_heading = Record(
        uuid="source-heading",
        kind="task",
        title="Source heading",
        parent_uuid=source.uuid,
        heading=True,
    )
    destination_heading = Record(
        uuid="destination-heading",
        kind="task",
        title="Destination heading",
        parent_uuid=destination.uuid,
        heading=True,
    )
    task = Record(
        uuid="move-me",
        kind="task",
        title="Move me",
        parent_uuid=source.uuid,
        heading_uuid=source_heading.uuid,
    )
    workspace, library, _store = contextual_workspace(
        [source, destination, source_heading, destination_heading, task]
    )

    read = workspace.read(
        ReadCall(
            purpose="change",
            id=task.id,
            include=[{"id": destination.id}],
        )
    )

    assert read.status == "ok"
    assert read.context and read.context.complete
    refs = {item.id: item.ref for item in read.items}
    assert refs[destination.id] is not None
    assert refs[destination_heading.id] is not None
    assert next(item for item in read.items if item.id == destination.id).revision
    assert next(item for item in read.items if item.id == destination_heading.id).revision

    change: dict[str, str] = {
        "ref": refs[task.id],
        "into": refs[destination.id],
    }
    if with_heading:
        change["heading_id"] = refs[destination_heading.id]
    result = workspace.commit(
        CommitCall(
            intent_id=f"task-cross-project-{with_heading}",
            context_id=read.context.id,
            change=[change],
        )
    )

    assert result.status == "applied"
    assert library.records[task.uuid].parent_uuid == destination.uuid
    assert library.records[task.uuid].heading_uuid == (
        destination_heading.uuid if with_heading else None
    )


def test_task_change_include_binds_named_cross_project_after_anchor() -> None:
    source = Record(uuid="source", kind="project", title="Source")
    destination = Record(uuid="destination", kind="project", title="Destination")
    target = Record(
        uuid="target", kind="task", title="Target", parent_uuid=source.uuid
    )
    anchor = Record(
        uuid="anchor", kind="task", title="Named anchor", parent_uuid=destination.uuid
    )
    workspace, library, _store = contextual_workspace(
        [source, destination, target, anchor]
    )

    read = workspace.read(
        ReadCall(
            purpose="change",
            id=target.id,
            include=[{"find": "Named anchor", "within": destination.id}],
        )
    )

    assert read.status == "ok"
    assert read.context
    refs = {item.id: item.ref for item in read.items}
    assert refs[anchor.id]
    result = workspace.commit(
        CommitCall(
            intent_id="included-after-001",
            context_id=read.context.id,
            change=[
                {
                    "ref": refs[target.id],
                    "into": refs[destination.id],
                    "after": refs[anchor.id],
                }
            ],
        )
    )
    assert result.status == "applied"
    assert library.records[target.uuid].sort_index > library.records[anchor.uuid].sort_index


def test_change_include_missing_or_ambiguous_keeps_the_target_context() -> None:
    project = Record(uuid="project", kind="project", title="Work")
    target = Record(uuid="target", kind="task", title="Target", parent_uuid=project.uuid)
    first = Record(uuid="first", kind="task", title="Same", parent_uuid=project.uuid)
    second = Record(uuid="second", kind="task", title="Same", parent_uuid=project.uuid)
    workspace, _library, _store = contextual_workspace([project, target, first, second])

    result = workspace.read(
        ReadCall(
            purpose="change",
            id=target.id,
            include=[{"find": "Same"}, {"find": "Absent"}],
        )
    )
    assert result.status == "ok"
    assert result.context is not None
    assert "include_unresolved" in result.signals
    assert target.id in {item.id for item in result.items}
    assert first.id not in {item.id for item in result.items}


def test_task_change_include_today_after_anchor_is_revision_checked() -> None:
    source = Record(uuid="source", kind="project", title="Source")
    destination = Record(uuid="destination", kind="project", title="Destination")
    target = Record(
        uuid="target",
        kind="task",
        title="Target",
        parent_uuid=source.uuid,
        start=NOW.date(),
        today_index=1024,
    )
    anchor = Record(
        uuid="anchor",
        kind="task",
        title="Anchor",
        parent_uuid=destination.uuid,
        start=NOW.date(),
        today_index=0,
    )
    workspace, library, _store = contextual_workspace(
        [source, destination, target, anchor]
    )
    read = workspace.read(
        ReadCall(
            purpose="change",
            id=target.id,
            include=[{"id": anchor.id}],
        )
    )
    assert read.context
    refs = {item.id: item.ref for item in read.items}
    library.records[anchor.uuid].title = "Owner changed anchor"
    result = workspace.commit(
        CommitCall(
            intent_id="included-today-after-stale-001",
            context_id=read.context.id,
            change=[
                {
                    "ref": refs[target.id],
                    "into": refs[destination.id],
                    "today_after": refs[anchor.id],
                }
            ],
        )
    )
    assert result.status == "stale"
    assert result.recovery and result.recovery.code == "context_conflict"
    assert library.records[target.uuid].parent_uuid == source.uuid


def test_change_include_uses_combined_context_bound() -> None:
    projects = [
        Record(uuid=f"project-{index}", kind="project", title=f"Project {index}")
        for index in range(119)
    ]
    target = Record(
        uuid="target",
        kind="task",
        title="Target",
        parent_uuid=projects[0].uuid,
    )
    anchor = Record(
        uuid="anchor",
        kind="task",
        title="Anchor",
        parent_uuid=projects[1].uuid,
    )
    workspace, _library, _store = contextual_workspace([*projects, target, anchor])

    result = workspace.read(
        ReadCall(purpose="change", id=target.id, include=[{"id": anchor.id}])
    )

    assert result.status == "ok"
    assert result.context is not None
    assert {item.id for item in result.items} >= {target.id, projects[0].id, anchor.id, projects[1].id}


def test_task_change_rejects_an_invalid_destination_kind_without_a_write() -> None:
    project = Record(uuid="project", kind="project", title="Project")
    task = Record(uuid="task", kind="task", title="Task", parent_uuid=project.uuid)
    workspace, library, _store = contextual_workspace([project, task])
    read = workspace.read(ReadCall(purpose="change", id=task.id))
    assert read.context
    refs = {item.id: item.ref for item in read.items}

    result = workspace.commit(
        CommitCall(
            intent_id="task-invalid-destination-kind-001",
            context_id=read.context.id,
            change=[{"ref": refs[task.id], "into": refs[task.id]}],
        )
    )

    assert result.status == "needs_input"
    assert "must identify Area or Project" in result.instruction
    assert library.records[task.uuid].parent_uuid == project.uuid


def test_task_change_context_overflow_returns_bounded_recovery() -> None:
    project = Record(uuid="home", kind="project", title="Home")
    headings = [
        Record(
            uuid=f"heading-{index}",
            kind="task",
            title=f"Heading {index}",
            parent_uuid=project.uuid,
            heading=True,
        )
        for index in range(120)
    ]
    task = Record(
        uuid="overflow-task",
        kind="task",
        title="Overflow",
        parent_uuid=project.uuid,
    )
    workspace, _library, _store = contextual_workspace([project, *headings, task])

    result = workspace.read(ReadCall(purpose="change", id=task.id))

    assert result.status == "needs_input"
    assert result.next == "read"
    assert result.context is None
    assert result.items == []
    assert result.recovery and result.recovery.code == "context_incomplete"
    assert result.recovery.retry == "rebuild"
    assert result.recovery.read == {"id": task.id, "limit": 40}


def test_task_change_rejects_a_stale_destination_heading_without_a_write() -> None:
    source = Record(uuid="source", kind="project", title="Source")
    destination = Record(uuid="destination", kind="project", title="Destination")
    destination_heading = Record(
        uuid="destination-heading",
        kind="task",
        title="Destination heading",
        parent_uuid=destination.uuid,
        heading=True,
    )
    task = Record(
        uuid="move-me",
        kind="task",
        title="Move me",
        parent_uuid=source.uuid,
    )
    workspace, library, _store = contextual_workspace(
        [source, destination, destination_heading, task]
    )
    read = workspace.read(
        ReadCall(
            purpose="change",
            id=task.id,
            include=[{"id": destination.id}],
        )
    )
    assert read.context
    refs = {item.id: item.ref for item in read.items}

    destination_heading.title = "Changed while model was deciding"
    result = workspace.commit(
        CommitCall(
            intent_id="task-stale-destination-heading-001",
            context_id=read.context.id,
            change=[
                {
                    "ref": refs[task.id],
                    "into": refs[destination.id],
                    "heading_id": refs[destination_heading.id],
                }
            ],
        )
    )

    assert result.status == "stale"
    assert result.recovery and result.recovery.code == "context_conflict"
    assert library.records[task.uuid].parent_uuid == source.uuid
    assert library.records[task.uuid].heading_uuid is None


def test_project_change_context_moves_to_an_area_in_two_calls() -> None:
    current = Record(uuid="work", kind="area", title="Work")
    destination = Record(uuid="business", kind="area", title="Business")
    project = Record(
        uuid="website", kind="project", title="Website", area_uuid=current.uuid
    )
    workspace, library, _store = contextual_workspace(
        [current, destination, project]
    )

    read = workspace.read(
        ReadCall(
            purpose="change",
            find="Website",
            include=[{"id": destination.id}],
        )
    )

    assert read.status == "ok"
    assert read.context and read.context.complete
    refs = {item.id: item.ref for item in read.items}
    assert refs[project.id] is not None
    assert refs[current.id] is not None
    assert refs[destination.id] is not None

    result = workspace.commit(
        CommitCall(
            intent_id="project-area-two-calls-001",
            context_id=read.context.id,
            change=[
                {
                    "ref": refs[project.id],
                    "into": refs[destination.id],
                }
            ],
        )
    )

    assert result.status == "applied"
    assert library.records[project.uuid].area_uuid == destination.uuid


def test_project_change_rejects_non_area_destination_ref_without_write() -> None:
    project = Record(uuid="project", kind="project", title="Website")
    workspace, library, _store = contextual_workspace([project])
    read = workspace.read(ReadCall(purpose="change", id=project.id))
    assert read.context
    refs = {item.id: item.ref for item in read.items}

    result = workspace.commit(
        CommitCall(
            intent_id="project-task-destination-001",
            context_id=read.context.id,
            change=[
                {
                    "ref": refs[project.id],
                    "into": refs[project.id],
                }
            ],
        )
    )

    assert result.status == "needs_input"
    assert "must identify an Area" in result.instruction
    assert library.records[project.uuid].area_uuid is None


def test_area_change_registry_overflow_returns_safe_recovery() -> None:
    areas = [
        Record(uuid=f"area-{index}", kind="area", title=f"Area {index}")
        for index in range(121)
    ]
    workspace, _library, _store = contextual_workspace(areas)

    result = workspace.read(ReadCall(purpose="change", id=areas[0].id))

    assert result.status == "needs_input"
    assert result.next == "read"
    assert result.context is None
    assert result.items == []
    assert result.recovery and result.recovery.code == "context_incomplete"
    assert result.recovery.retry == "rebuild"
    assert result.recovery.read == {
        "id": areas[0].id,
        "limit": 40,
    }


def test_organize_read_returns_complete_project_layout() -> None:
    project = Record(uuid="project", kind="project", title="Launch")
    next_heading = Record(
        uuid="next",
        kind="task",
        title="Next",
        parent_uuid=project.uuid,
        heading=True,
        sort_index=0,
    )
    later_heading = Record(
        uuid="later",
        kind="task",
        title="Later",
        parent_uuid=project.uuid,
        heading=True,
        sort_index=1024,
    )
    first = Record(
        uuid="first",
        kind="task",
        title="Draft",
        parent_uuid=project.uuid,
        heading_uuid=next_heading.uuid,
        sort_index=10,
    )
    second = Record(
        uuid="second",
        kind="task",
        title="Publish",
        parent_uuid=project.uuid,
        heading_uuid=later_heading.uuid,
        sort_index=20,
    )
    loose = Record(
        uuid="loose",
        kind="task",
        title="Capture",
        parent_uuid=project.uuid,
        sort_index=30,
    )
    workspace, _library, _store = contextual_workspace(
        [project, next_heading, later_heading, first, second, loose]
    )

    result = workspace.read(
        ReadCall(
            purpose="organize", view="project", within=project.id, limit=10
        )
    )

    assert result.status == "ok"
    assert result.context and result.context.complete
    assert result.layouts[0].complete
    assert all(item.revision is None for item in result.items)
    facts = {item.id: item.ref for item in result.items}
    assert result.layouts[0].project_ref == facts[project.id]
    assert [section.heading_ref for section in result.layouts[0].sections] == [
        facts[next_heading.id],
        facts[later_heading.id],
        None,
    ]
    assert [section.task_refs for section in result.layouts[0].sections] == [
        [facts[first.id]],
        [facts[second.id]],
        [facts[loose.id]],
    ]


def test_organize_read_can_find_one_project_without_a_retry() -> None:
    project = Record(uuid="project", kind="project", title="Launch")
    task = Record(uuid="task", kind="task", title="Draft", parent_uuid="project")
    workspace, _library, _store = contextual_workspace([project, task])

    result = workspace.read(ReadCall(purpose="organize", find="Launch"))

    assert result.status == "ok"
    assert result.context and result.context.purpose == "organize"
    assert result.layouts and result.layouts[0].complete
    assert {item.id for item in result.items} == {project.id, task.id}


def test_organize_find_resolves_unique_parent_from_matching_tasks() -> None:
    project = Record(uuid="event-create", kind="project", title="Event")
    first = Record(
        uuid="venue-a", kind="task", title="Book venue", parent_uuid="event-create"
    )
    second = Record(
        uuid="venue-b", kind="task", title="Confirm venue", parent_uuid="event-create"
    )
    other = Record(
        uuid="catering", kind="task", title="Choose catering", parent_uuid="event-create"
    )
    workspace, _library, _store = contextual_workspace(
        [project, first, second, other]
    )

    result = workspace.read(ReadCall(purpose="organize", find="venue"))

    assert result.status == "ok"
    assert result.context and result.context.purpose == "organize"
    assert {item.id for item in result.items} == {
        project.id,
        first.id,
        second.id,
        other.id,
    }


def test_organize_miss_returns_inbox_tasks_to_group() -> None:
    home = Record(uuid="home", kind="area", title="Home")
    first = Record(uuid="kitchen-a", kind="task", title="Remove old tap", inbox=True)
    second = Record(uuid="kitchen-b", kind="task", title="Measure sink", inbox=True)
    workspace, _library, _store = contextual_workspace([home, first, second])

    result = workspace.read(ReadCall(purpose="organize", find="kitchen renovation"))

    assert result.status == "ok"
    assert result.next == "done"
    assert {item.id for item in result.items} == {first.id, second.id}
    assert "create one Project" in result.instruction


def test_organize_find_asks_when_matching_tasks_span_two_projects() -> None:
    alpha = Record(uuid="alpha", kind="project", title="Alpha")
    beta = Record(uuid="beta", kind="project", title="Beta")
    first = Record(uuid="one", kind="task", title="Book venue", parent_uuid="alpha")
    second = Record(uuid="two", kind="task", title="Confirm venue", parent_uuid="beta")
    workspace, _library, _store = contextual_workspace(
        [alpha, beta, first, second]
    )

    result = workspace.read(ReadCall(purpose="organize", find="venue"))

    assert result.status == "needs_input"
    assert result.next == "ask"
    assert {item.id for item in result.items} == {alpha.id, beta.id}


def test_organize_read_accepts_exact_project_id_and_exposes_merge_registry() -> None:
    source_area = Record(uuid="source-area", kind="area", title="Source")
    destination_area = Record(uuid="destination-area", kind="area", title="Destination")
    source = Record(
        uuid="source", kind="project", title="Source", area_uuid=source_area.uuid
    )
    destination = Record(
        uuid="destination",
        kind="project",
        title="Destination",
        area_uuid=destination_area.uuid,
    )
    task = Record(uuid="task", kind="task", title="Move me", parent_uuid=source.uuid)
    workspace, _library, _store = contextual_workspace(
        [source_area, destination_area, source, destination, task]
    )

    result = workspace.read(
        ReadCall(
            purpose="organize",
            id=source.id,
            include=[{"id": destination.id}],
        )
    )

    assert result.status == "ok"
    assert result.context and result.context.complete
    facts = {item.id: item for item in result.items}
    assert facts[destination.id].revision is not None
    assert facts[destination_area.id].revision is not None
    assert facts[source.id].revision is None
    assert result.layouts[0].project_ref == facts[source.id].ref
    assert result.layouts[0].sections[0].task_refs == [facts[task.id].ref]


def test_one_read_project_merge_moves_children_and_trashes_source_after_approval() -> None:
    destination_area = Record(uuid="destination-area", kind="area", title="Destination")
    source = Record(uuid="source", kind="project", title="Source")
    destination = Record(
        uuid="destination",
        kind="project",
        title="Destination",
        area_uuid=destination_area.uuid,
    )
    heading = Record(
        uuid="heading",
        kind="task",
        title="Next",
        parent_uuid=source.uuid,
        heading=True,
    )
    task = Record(
        uuid="task",
        kind="task",
        title="Move me",
        parent_uuid=source.uuid,
        heading_uuid=heading.uuid,
    )
    workspace, library, _store = contextual_workspace(
        [destination_area, source, destination, heading, task]
    )
    read = workspace.read(
        ReadCall(
            purpose="organize",
            id=source.id,
            include=[{"id": destination.id}],
        )
    )
    assert read.context
    refs = {item.id: item.ref for item in read.items}

    result = workspace.commit(
        CommitCall(
            intent_id="project-merge-001",
            context_id=read.context.id,
            change=[
                {"ref": refs[heading.id], "into": refs[destination.id]},
                {"ref": refs[task.id], "into": refs[destination.id]},
                {"ref": refs[source.id], "lifecycle": "trash"},
            ],
        )
    )

    assert result.status == "needs_approval"
    assert result.plan
    applied = workspace.approve(ApproveCall(plan_id=result.plan.id))
    assert applied.status == "applied"
    assert library.records[source.uuid].trashed
    assert library.records[heading.uuid].parent_uuid == destination.uuid
    assert library.records[task.uuid].parent_uuid == destination.uuid
    assert library.records[task.uuid].heading_uuid == heading.uuid


def test_heading_into_another_project_is_rejected_without_merge() -> None:
    source = Record(uuid="source", kind="project", title="Source")
    destination = Record(uuid="destination", kind="project", title="Destination")
    heading = Record(
        uuid="heading",
        kind="task",
        title="Next",
        parent_uuid=source.uuid,
        heading=True,
    )
    workspace, library, _store = contextual_workspace([source, destination, heading])
    read = workspace.read(
        ReadCall(
            purpose="organize",
            id=source.id,
            include=[{"id": destination.id}],
        )
    )
    assert read.context
    refs = {item.id: item.ref for item in read.items}

    result = workspace.commit(
        CommitCall(
            intent_id="heading-cross-project-001",
            context_id=read.context.id,
            change=[{"ref": refs[heading.id], "into": refs[destination.id]}],
        )
    )

    assert result.status == "rejected"
    assert "atomic Project merge" in result.instruction
    assert library.records[heading.uuid].parent_uuid == source.uuid


def test_organize_find_requires_one_project() -> None:
    first = Record(uuid="first", kind="project", title="Launch")
    second = Record(uuid="second", kind="project", title="Launch follow-up")
    workspace, _library, _store = contextual_workspace([first, second])

    result = workspace.read(ReadCall(purpose="organize", find="Launch"))

    assert result.status == "needs_input"
    assert result.next == "ask"
    assert result.context is None
    assert "2 active Projects" in result.instruction


@pytest.mark.parametrize("selector", ["id", "view", "find"])
@pytest.mark.parametrize(
    "state",
    [
        {"status": "done"},
        {"trashed": True},
        {"recurrence": RecurrenceState(role="template")},
    ],
)
def test_organize_rejects_non_active_visible_projects_without_context(
    selector: str, state: dict[str, object]
) -> None:
    project = Record(uuid="closed", kind="project", title="Closed Project", **state)
    workspace, _library, _store = contextual_workspace([project])
    call_data: dict[str, object] = {"purpose": "organize"}
    if selector == "id":
        call_data["id"] = project.id
    elif selector == "view":
        call_data.update({"view": "project", "within": project.id})
    else:
        call_data["find"] = project.title

    result = workspace.read(ReadCall(**call_data))

    assert result.status == "needs_input"
    assert result.context is None
    assert result.recovery and result.recovery.code == "context_required"


def test_organize_read_uses_context_budget_not_normal_read_limit() -> None:
    project = Record(uuid="project", kind="project", title="Large")
    tasks = [
        Record(uuid=f"task-{index}", kind="task", title=str(index), parent_uuid="project")
        for index in range(3)
    ]
    workspace, _library, _store = contextual_workspace([project, *tasks])

    result = workspace.read(
        ReadCall(purpose="organize", view="project", within=project.id, limit=2)
    )

    assert result.status == "ok"
    assert result.context and result.context.complete
    assert len(result.items) == 4
    assert len(result.layouts[0].sections[0].task_refs) == 3


def test_organize_read_guides_recovery_above_context_budget() -> None:
    project = Record(uuid="project", kind="project", title="Too large")
    tasks = [
        Record(
            uuid=f"task-{index}",
            kind="task",
            title=str(index),
            parent_uuid="project",
        )
        for index in range(120)
    ]
    fitting, _fitting_library, _fitting_store = contextual_workspace(
        [project, *tasks[:119]]
    )
    complete = fitting.read(
        ReadCall(purpose="organize", view="project", within=project.id, limit=2)
    )
    assert complete.status == "ok"
    assert complete.context and complete.context.complete
    assert len(complete.items) == 120

    workspace, _library, _store = contextual_workspace([project, *tasks])

    result = workspace.read(
        ReadCall(purpose="organize", view="project", within=project.id, limit=2)
    )

    assert result.status == "needs_input"
    assert result.next == "read"
    assert result.context is None
    assert result.recovery and result.recovery.code == "context_incomplete"
    assert result.recovery.retry == "rebuild"
    assert result.recovery.read == {
        "view": "project",
        "within": project.id,
        "limit": 40,
    }


def test_system_review_stays_an_exact_registry_read() -> None:
    project = Record(uuid="project", kind="project", title="Launch")
    workspace, _library, _store = contextual_workspace([project])
    read = workspace.read(ReadCall(view="system"))
    assert read.status == "ok"
    assert read.next == "done"
    assert read.context is None
    assert [item.id for item in read.items] == ["project:project"]


def test_context_ref_commit_uses_existing_journal_and_apply_path() -> None:
    task = Record(uuid="task", kind="task", title="Old")
    workspace, library, _store = contextual_workspace([task])
    read = workspace.read(ReadCall(purpose="change", id=task.id))
    assert read.context is not None
    task_ref = read.items[0].ref
    assert task_ref is not None

    result = workspace.commit(
        CommitCall(
            intent_id="context-rename-001",
            context_id=read.context.id,
            change=[{"ref": task_ref, "title": "New"}],
        )
    )

    assert result.status == "applied"
    assert library.records[task.uuid].title == "New"
    replay = workspace.commit(
        CommitCall(
            intent_id="context-rename-001",
            context_id=read.context.id,
            change=[{"ref": task_ref, "title": "New"}],
        )
    )
    assert replay.status == "applied"


def test_context_exact_changes_are_scoped_and_mixed_batches_are_atomic() -> None:
    first = Record(uuid="first", kind="task", title="First")
    second = Record(uuid="second", kind="task", title="Second")
    workspace, library, store = contextual_workspace([first, second])
    read = workspace.read(ReadCall(purpose="change", id=first.id))
    assert read.context is not None
    context = store.get(read.context.id, account_id="owner-a")
    first_revision = next(
        entry.revision for entry in context.refs if entry.exact_id == first.id
    )

    exact_only = workspace.commit(
        CommitCall(
            intent_id="context-exact-success-001",
            context_id=context.id,
            change=[
                {
                    "id": first.id,
                    "if_revision": first_revision,
                    "title": "First updated",
                }
            ],
        )
    )
    assert exact_only.status == "applied"
    assert library.records[first.uuid].title == "First updated"

    mixed_workspace, mixed_library, mixed_store = contextual_workspace(
        [Record(uuid="first", kind="task", title="First"),
         Record(uuid="second", kind="task", title="Second")]
    )
    mixed_read = mixed_workspace.read(ReadCall(purpose="change", id="task:first"))
    assert mixed_read.context is not None
    mixed_context = mixed_store.get(mixed_read.context.id, account_id="owner-a")
    mixed_first_revision = next(
        entry.revision
        for entry in mixed_context.refs
        if entry.exact_id == "task:first"
    )
    mixed = mixed_workspace.commit(
        CommitCall(
            intent_id="context-exact-atomic-001",
            context_id=mixed_context.id,
            change=[
                {
                    "id": "task:first",
                    "if_revision": mixed_first_revision,
                    "title": "Must not partially apply",
                },
                {
                    "id": "task:second",
                    "if_revision": "revision-second",
                    "title": "Must not apply",
                },
            ],
        )
    )
    assert mixed.status == "stale"
    assert mixed.recovery and mixed.recovery.code == "context_conflict"
    assert mixed_library.records["first"].title == "First"
    assert mixed_library.records["second"].title == "Second"


def test_organize_commit_assigns_existing_task_to_new_heading() -> None:
    project = Record(uuid="project", kind="project", title="Launch")
    task = Record(
        uuid="task", kind="task", title="Draft", parent_uuid=project.uuid
    )
    workspace, library, _store = contextual_workspace([project, task])
    read = workspace.read(
        ReadCall(
            purpose="organize", view="project", within=project.id, limit=10
        )
    )
    assert read.context and read.layouts
    refs = {item.id: item.ref for item in read.items}
    assert refs[project.id] and refs[task.id]

    result = workspace.commit(
        CommitCall(
            intent_id="organize-new-heading-001",
            context_id=read.context.id,
            organize=[
                {
                    "project_ref": refs[project.id],
                    "sections": [
                        {
                            "heading_key": "$later",
                            "heading_title": "Later",
                            "task_refs": [refs[task.id]],
                        }
                    ],
                }
            ],
        )
    )

    assert result.status == "applied"
    headings = [record for record in library.records.values() if record.heading]
    assert [heading.title for heading in headings] == ["Later"]
    assert library.records[task.uuid].heading_uuid == headings[0].uuid


def test_context_is_account_bound_and_expiry_has_structured_recovery() -> None:
    now = [NOW]
    store = MemoryContextStore(
        clock=lambda: now[0], token_factory=lambda: "ctx_12345678"
    )
    task = Record(uuid="task", kind="task", title="Old")
    anchor = Record(uuid="anchor", kind="task", title="Anchor")
    first, _library, _store = contextual_workspace(
        [task, anchor], now=now, store=store, account_id="owner-a"
    )
    read = first.read(
        ReadCall(
            purpose="change",
            id=task.id,
            include=[{"find": "Anchor"}],
        )
    )
    assert read.context and read.items[0].ref
    call = CommitCall(
        intent_id="bound-context-001",
        context_id=read.context.id,
        change=[{"ref": read.items[0].ref, "title": "New"}],
    )
    other, _other_library, _same_store = contextual_workspace(
        [Record(uuid="task", kind="task", title="Old")],
        now=now,
        store=store,
        account_id="owner-b",
    )

    wrong_account = other.commit(call)

    assert wrong_account.status == "stale"
    assert wrong_account.recovery
    assert wrong_account.recovery.code == "context_required"

    now[0] += timedelta(minutes=31)
    expired = first.commit(call.model_copy(update={"intent_id": "expired-context-001"}))
    assert expired.status == "stale"
    assert expired.recovery and expired.recovery.code == "context_expired"
    assert expired.recovery.read == {
        "purpose": "change",
        "id": task.id,
        "include": [{"find": "Anchor"}],
    }


def test_context_revision_conflict_returns_recovery_without_write() -> None:
    task = Record(uuid="task", kind="task", title="Old")
    workspace, library, _store = contextual_workspace([task])
    read = workspace.read(ReadCall(purpose="change", id=task.id))
    assert read.context and read.items[0].ref
    library.records[task.uuid].title = "Owner edit"

    result = workspace.commit(
        CommitCall(
            intent_id="context-conflict-001",
            context_id=read.context.id,
            change=[{"ref": read.items[0].ref, "title": "Model edit"}],
        )
    )

    assert result.status == "stale"
    assert result.recovery and result.recovery.code == "context_conflict"
    assert library.records[task.uuid].title == "Owner edit"


def test_context_stale_relationship_anchor_returns_recovery_without_write() -> None:
    project = Record(uuid="project", kind="project", title="Launch")
    anchor = Record(
        uuid="anchor",
        kind="task",
        title="Anchor",
        parent_uuid=project.uuid,
        sort_index=0,
    )
    task = Record(
        uuid="task",
        kind="task",
        title="Draft",
        parent_uuid=project.uuid,
        sort_index=1024,
    )
    workspace, library, _store = contextual_workspace([project, anchor, task])
    read = workspace.read(
        ReadCall(purpose="organize", view="project", within=project.id)
    )
    assert read.context
    refs = {item.id: item.ref for item in read.items}
    assert refs[task.id] and refs[anchor.id]
    original_sort = library.records[task.uuid].sort_index
    library.records[anchor.uuid].title = "Owner changed anchor"

    result = workspace.commit(
        CommitCall(
            intent_id="context-anchor-stale-001",
            context_id=read.context.id,
            change=[
                {
                    "ref": refs[task.id],
                    "after": refs[anchor.id],
                    "title": "Model edit",
                }
            ],
        )
    )

    assert result.status == "stale"
    assert result.recovery and result.recovery.code == "context_conflict"
    assert library.records[task.uuid].title == "Draft"
    assert library.records[task.uuid].sort_index == original_sort


def test_context_stale_create_destination_returns_recovery_without_write() -> None:
    area = Record(uuid="area", kind="area", title="Work")
    project = Record(uuid="project", kind="project", title="Launch")
    workspace, library, _store = contextual_workspace([area, project])
    read = workspace.read(ReadCall(view="system"))
    assert read.context is None

    # A system read is intentionally legacy. Use a complete project-change
    # context, which carries the Area registry needed by a Project create.
    read = workspace.read(
        ReadCall(
            purpose="change",
            id=project.id,
            include=[{"id": area.id}],
        )
    )
    assert read.context
    refs = {item.id: item.ref for item in read.items}
    assert refs[area.id]
    library.records[area.uuid].title = "Owner changed destination"

    result = workspace.commit(
        CommitCall(
            intent_id="context-destination-stale-001",
            context_id=read.context.id,
            create=[
                {
                    "title": "New project",
                    "kind": "project",
                    "into": refs[area.id],
                }
            ],
        )
    )

    assert result.status == "stale"
    assert result.recovery and result.recovery.code == "context_conflict"
    assert [record.title for record in library.records.values()] == [
        "Owner changed destination",
        "Launch",
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("purpose", "evil"),
        ("limit", "20"),
        ("view", "project"),
        ("account_binding", "account-one"),
    ],
)
def test_corrupt_sqlite_context_returns_safe_recovery(
    tmp_path: Path, field: str, value: object
) -> None:
    now = [NOW]
    path = tmp_path / "contexts.sqlite3"
    store = SQLiteContextStore(
        path, clock=lambda: now[0], token_factory=lambda: "ctx_12345678"
    )
    task = Record(uuid="task", kind="task", title="Old")
    workspace, library, _store = contextual_workspace(
        [task], now=now, store=store
    )
    read = workspace.read(ReadCall(purpose="change", id=task.id))
    assert read.context and read.items[0].ref

    with sqlite3.connect(path) as connection:
        if field == "account_binding":
            connection.execute(
                "UPDATE read_contexts SET account_binding = ? WHERE context_id = ?",
                (value, read.context.id),
            )
        else:
            row = connection.execute(
                "SELECT selector_json FROM read_contexts WHERE context_id = ?",
                (read.context.id,),
            ).fetchone()
            assert row is not None
            selector_data = json.loads(row[0])
            selector_data[field] = value
            connection.execute(
                "UPDATE read_contexts SET selector_json = ? WHERE context_id = ?",
                (json.dumps(selector_data), read.context.id),
            )

    result = workspace.commit(
        CommitCall(
            intent_id=f"corrupt-context-{field}-001",
            context_id=read.context.id,
            change=[{"ref": read.items[0].ref, "title": "New"}],
        )
    )

    assert result.status == "stale"
    assert result.next == "read"
    assert result.recovery and result.recovery.code == "context_corrupt"
    assert result.recovery.retry == "read"
    assert result.recovery.read is None
    assert "evil" not in result.instruction
    assert library.records[task.uuid].title == "Old"


def test_context_area_change_carries_complete_registry_precondition() -> None:
    area = Record(uuid="area", kind="area", title="Work")
    project = Record(
        uuid="project", kind="project", title="Launch", area_uuid=area.uuid
    )
    workspace, library, _store = contextual_workspace([area, project])
    read = workspace.read(ReadCall(purpose="change", id=area.id))
    assert read.context and read.context.complete
    area_fact = next(item for item in read.items if item.id == area.id)
    assert area_fact.ref

    result = workspace.commit(
        CommitCall(
            intent_id="context-area-001",
            context_id=read.context.id,
            change=[{"ref": area_fact.ref, "title": "Career"}],
        )
    )

    assert result.status == "needs_approval"
    assert result.plan
    applied = workspace.approve(ApproveCall(plan_id=result.plan.id))
    assert applied.status == "applied"
    assert library.records[area.uuid].title == "Career"


def test_legacy_exact_read_and_commit_remain_unchanged() -> None:
    task = Record(uuid="task", kind="task", title="Old")
    workspace, library, _store = contextual_workspace([task])
    read = workspace.read(ReadCall(id=task.id))
    assert read.context is None

    result = workspace.commit(
        CommitCall(
            intent_id="legacy-rename-001",
            change=[
                {
                    "id": task.id,
                    "if_revision": read.items[0].revision,
                    "title": "New",
                }
            ],
        )
    )

    assert result.status == "applied"
    assert library.records[task.uuid].title == "New"
