from __future__ import annotations

from datetime import datetime, timezone

import pytest

from things_orchestrator.context import MemoryContextStore
from things_orchestrator.interface import (
    ReadCall,
    dump_result,
)
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


def test_area_change_context_stays_the_local_neighborhood() -> None:
    areas = [
        Record(uuid=f"area-{index}", kind="area", title=f"Area {index}")
        for index in range(121)
    ]
    workspace, _library, _store = contextual_workspace(areas)

    local = workspace.read(ReadCall(purpose="change", id=areas[0].id))
    assert local.status == "ok"
    assert local.context is not None
    assert {item.id for item in local.items} == {areas[0].id}


def test_area_change_include_binds_a_destination_area() -> None:
    current = Record(uuid="work", kind="area", title="Work")
    destination = Record(uuid="home", kind="area", title="Home")
    workspace, _library, _store = contextual_workspace([current, destination])

    included = workspace.read(
        ReadCall(
            purpose="change",
            id=current.id,
            include=[{"id": destination.id}],
        )
    )
    assert included.status == "ok"
    assert {item.id for item in included.items} == {current.id, destination.id}


def test_short_refs_stay_stable_when_a_fresh_read_adds_includes() -> None:
    project = Record(uuid="launch", kind="project", title="Launch")
    child = Record(
        uuid="ship",
        kind="task",
        title="Ship",
        parent_uuid=project.uuid,
    )
    loose = Record(uuid="loose", kind="task", title="Loose")
    extra = Record(uuid="extra", kind="task", title="Extra")
    tokens = iter(["ctx_first000", "ctx_second00"])
    store = MemoryContextStore(clock=lambda: NOW, token_factory=lambda: next(tokens))
    workspace, _library, _store = contextual_workspace(
        [project, child, loose, extra], store=store
    )

    first = workspace.read(
        ReadCall(
            purpose="organize",
            id=project.id,
            include=[{"id": loose.id}],
        )
    )
    second = workspace.read(
        ReadCall(
            purpose="organize",
            id=project.id,
            include=[{"id": extra.id}, {"id": loose.id}],
        )
    )
    first_refs = {item.id: item.ref for item in first.items}
    second_refs = {item.id: item.ref for item in second.items}

    assert second_refs[project.id] == first_refs[project.id]
    assert second_refs[child.id] == first_refs[child.id]
    assert second_refs[loose.id] == first_refs[loose.id]


def test_trashed_project_change_overflow_points_at_exact_id() -> None:
    project = Record(uuid="fat", kind="project", title="Fat", trashed=True)
    children = [
        Record(
            uuid=f"child-{index}",
            kind="task",
            title=f"Child {index}",
            parent_uuid=project.uuid,
            trashed=True,
        )
        for index in range(120)
    ]
    workspace, _library, _store = contextual_workspace([project, *children])

    result = workspace.read(ReadCall(purpose="change", id=project.id))

    assert result.status == "needs_input"
    assert result.next == "read"
    assert result.context is None
    assert result.items == []
    assert result.recovery and result.recovery.code == "context_incomplete"
    assert result.recovery.retry == "rebuild"
    assert result.recovery.read == {"ids": [project.id]}
    assert "if_revision" in result.instruction
    assert "purpose" not in (result.recovery.read or {})


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
    wire = dump_result(result)
    assert wire["context"]["complete"] is True
    assert wire["layouts"][0]["complete"] is True
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
    assert all(item.revision is None for item in result.items)
    assert result.layouts[0].project_ref == facts[source.id].ref
    assert result.layouts[0].sections[0].task_refs == [facts[task.id].ref]


def test_organize_find_requires_one_project() -> None:
    first = Record(uuid="first", kind="project", title="Launch")
    second = Record(uuid="second", kind="project", title="Launch follow-up")
    workspace, _library, _store = contextual_workspace([first, second])

    result = workspace.read(ReadCall(purpose="organize", find="Launch"))

    assert result.status == "needs_input"
    assert result.next == "ask"
    assert result.context is None
    assert "2 active Projects" in result.instruction


@pytest.mark.parametrize("selector", ["id", "view"])
@pytest.mark.parametrize(
    "state",
    [
        {"status": "done"},
        {"recurrence": RecurrenceState(role="template")},
    ],
)
def test_organize_of_a_closed_project_returns_the_writable_neighborhood(
    selector: str, state: dict[str, object]
) -> None:
    project = Record(uuid="closed", kind="project", title="Closed Project", **state)
    workspace, _library, _store = contextual_workspace([project])
    call_data: dict[str, object] = {"purpose": "organize"}
    if selector == "id":
        call_data["id"] = project.id
    else:
        call_data.update({"view": "project", "within": project.id})

    result = workspace.read(ReadCall(**call_data))

    assert result.status == "ok"
    assert result.context is not None
    assert {item.id for item in result.items} == {project.id}


@pytest.mark.parametrize("selector", ["id", "view", "find"])
def test_trashed_project_organize_returns_the_contained_tree(selector: str) -> None:
    project = Record(
        uuid="closed", kind="project", title="Closed Project", trashed=True
    )
    child = Record(
        uuid="inside",
        kind="task",
        title="Inside",
        parent_uuid=project.uuid,
        trashed=True,
    )
    workspace, _library, _store = contextual_workspace([project, child])
    call_data: dict[str, object] = {"purpose": "organize"}
    if selector == "id":
        call_data["id"] = project.id
    elif selector == "view":
        call_data.update({"view": "project", "within": project.id})
    else:
        call_data["find"] = project.title

    result = workspace.read(ReadCall(**call_data))

    assert result.status == "ok"
    assert result.context is not None
    assert {item.id for item in result.items} == {project.id, child.id}
    assert result.layouts
    assert result.layouts[0].complete
    facts = {item.id: item.ref for item in result.items}
    assert result.layouts[0].project_ref == facts[project.id]
    assert result.layouts[0].sections[0].task_refs == [facts[child.id]]
    assert "contained records" in result.instruction


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
    assert result.recovery.read == {"ids": [project.id]}
    assert "if_revision" in result.instruction


def test_system_review_stays_an_exact_registry_read() -> None:
    project = Record(uuid="project", kind="project", title="Launch")
    workspace, _library, _store = contextual_workspace([project])
    read = workspace.read(ReadCall(view="system"))
    assert read.status == "ok"
    assert read.next == "done"
    assert read.context is None
    assert [item.id for item in read.items] == ["project:project"]


def test_organize_layout_names_hidden_heading_occupants() -> None:
    project = Record(uuid="project", kind="project", title="KI")
    heading = Record(
        uuid="codex",
        kind="task",
        title="Codex",
        parent_uuid=project.uuid,
        heading=True,
    )
    hidden = Record(
        uuid="old",
        kind="task",
        title="Old Codex task",
        parent_uuid=project.uuid,
        heading_uuid=heading.uuid,
        trashed=True,
    )
    workspace, _library, _store = contextual_workspace([project, heading, hidden])
    read = workspace.read(
        ReadCall(purpose="organize", view="project", within=project.id)
    )

    assert read.layouts
    section = next(
        row for row in read.layouts[0].sections if row.heading_ref is not None
    )
    assert section.task_refs == []
    assert section.hidden_count == 1
    assert "trashed" in section.hidden_signals
    heading_fact = next(item for item in read.items if item.id == heading.id)
    assert "has_hidden_occupants" in heading_fact.signals
    hidden_fact = next(item for item in read.items if item.id == hidden.id)
    assert hidden_fact.ref is not None
    assert "trashed" in hidden_fact.signals


def test_living_project_change_is_the_writable_neighborhood() -> None:
    area = Record(uuid="work", kind="area", title="Work")
    project = Record(
        uuid="launch", kind="project", title="Launch", area_uuid=area.uuid
    )
    heading = Record(
        uuid="next",
        kind="task",
        title="Next",
        parent_uuid=project.uuid,
        heading=True,
    )
    task = Record(
        uuid="ship",
        kind="task",
        title="Ship",
        parent_uuid=project.uuid,
        heading_uuid=heading.uuid,
    )
    hidden = Record(
        uuid="gone",
        kind="task",
        title="Gone",
        parent_uuid=project.uuid,
        heading_uuid=heading.uuid,
        trashed=True,
    )
    workspace, _library, _store = contextual_workspace(
        [area, project, heading, task, hidden]
    )

    result = workspace.read(ReadCall(purpose="change", id=project.id))

    assert result.status == "ok"
    assert result.context is not None
    assert result.context.complete is True
    assert {item.id for item in result.items} == {
        project.id,
        area.id,
        heading.id,
        task.id,
        hidden.id,
    }
    assert result.layouts
    assert result.layouts[0].complete
    section = next(
        row for row in result.layouts[0].sections if row.heading_ref is not None
    )
    assert section.hidden_count == 1
    assert "contained records" not in result.instruction


def test_trashed_project_change_lists_contained_records() -> None:
    area = Record(uuid="work", kind="area", title="Work")
    project = Record(
        uuid="launch",
        kind="project",
        title="Launch",
        area_uuid=area.uuid,
        trashed=True,
    )
    heading = Record(
        uuid="next",
        kind="task",
        title="Next",
        parent_uuid=project.uuid,
        heading=True,
        trashed=True,
    )
    task = Record(
        uuid="ship",
        kind="task",
        title="Ship",
        parent_uuid=project.uuid,
        heading_uuid=heading.uuid,
        trashed=True,
    )
    nested = Record(
        uuid="nested",
        kind="project",
        title="Nested",
        parent_uuid=project.uuid,
        trashed=True,
    )
    leaf = Record(
        uuid="leaf",
        kind="task",
        title="Leaf",
        parent_uuid=nested.uuid,
        trashed=True,
    )
    workspace, _library, _store = contextual_workspace(
        [area, project, heading, task, nested, leaf]
    )

    result = workspace.read(ReadCall(purpose="change", id=project.id))

    assert result.status == "ok"
    assert result.context is not None
    assert result.context.complete is True
    assert {item.id for item in result.items} == {
        project.id,
        area.id,
        heading.id,
        task.id,
        nested.id,
        leaf.id,
    }
    assert result.items[0].id == project.id
    assert all(item.revision is None for item in result.items)
    assert all(item.ref for item in result.items)
    assert "trashed" in result.items[0].signals
    assert "4 contained records" in result.instruction
    assert heading.id.startswith("heading:")
    assert result.layouts
    assert [layout.complete for layout in result.layouts] == [True, True]
    facts = {item.id: item.ref for item in result.items}
    root_layout = next(
        layout for layout in result.layouts if layout.project_ref == facts[project.id]
    )
    assert [section.heading_ref for section in root_layout.sections] == [
        facts[heading.id]
    ]
    assert root_layout.sections[0].task_refs == [facts[task.id]]
    assert root_layout.sections[0].hidden_count == 0
    nested_layout = next(
        layout for layout in result.layouts if layout.project_ref == facts[nested.id]
    )
    assert nested_layout.sections[0].heading_ref is None
    assert nested_layout.sections[0].task_refs == [facts[leaf.id]]


def test_task_change_include_exposes_destination_project_headings() -> None:
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
    workspace, _library, _store = contextual_workspace(
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
    assert all(item.revision is None for item in read.items)


def test_task_change_include_resolves_named_cross_project_anchor() -> None:
    source = Record(uuid="source", kind="project", title="Source")
    destination = Record(uuid="destination", kind="project", title="Destination")
    target = Record(
        uuid="target", kind="task", title="Target", parent_uuid=source.uuid
    )
    anchor = Record(
        uuid="anchor", kind="task", title="Named anchor", parent_uuid=destination.uuid
    )
    workspace, _library, _store = contextual_workspace(
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


def test_organize_include_adds_a_second_complete_project_scope() -> None:
    first = Record(uuid="alpha", kind="project", title="Alpha")
    second = Record(uuid="beta", kind="project", title="Beta")
    first_task = Record(
        uuid="one", kind="task", title="Alpha next", parent_uuid=first.uuid
    )
    second_task = Record(
        uuid="two", kind="task", title="Beta next", parent_uuid=second.uuid
    )
    workspace, _library, _store = contextual_workspace(
        [first, second, first_task, second_task]
    )
    read = workspace.read(
        ReadCall(
            purpose="organize",
            id=first.id,
            include=[{"id": second.id}],
        )
    )
    assert read.context is not None
    assert len(read.layouts) == 2
    assert all(item.revision is None for item in read.items)
