from __future__ import annotations

from datetime import date

from things_orchestrator.consistency import item_conflicts
from things_orchestrator.library import MemoryLibrary, Record


def test_item_conflicts_finds_inbox_hybrids() -> None:
    project = Record(uuid="home", kind="project", title="Home")
    hybrid = Record(
        uuid="stuck",
        kind="task",
        title="Stuck",
        inbox=True,
        parent_uuid=project.uuid,
    )
    library = MemoryLibrary([project, hybrid])

    assert item_conflicts(hybrid, library) == ["inbox_with_project"]


def test_trashed_heading_is_an_orphaned_heading_for_live_children() -> None:
    project = Record(uuid="home", kind="project", title="Home")
    heading = Record(
        uuid="later",
        kind="task",
        title="Later",
        parent_uuid=project.uuid,
        heading=True,
        trashed=True,
    )
    child = Record(
        uuid="repeat",
        kind="task",
        title="Repeat",
        parent_uuid=project.uuid,
        heading_uuid=heading.uuid,
    )
    library = MemoryLibrary([project, heading, child])

    assert "orphaned_heading" in item_conflicts(child, library)


def test_item_conflicts_covers_wrong_kinds_and_malformed_reminder() -> None:
    area = Record(uuid="home", kind="area", title="Home")
    heading = Record(uuid="loose-heading", kind="task", title="Loose", heading=True)
    wrong_parent = Record(
        uuid="under-area",
        kind="task",
        title="Under area",
        parent_uuid=area.uuid,
    )
    wrong_area = Record(
        uuid="area-is-project",
        kind="task",
        title="Wrong area",
        area_uuid="under-area",
    )
    no_project_heading = Record(
        uuid="headed",
        kind="task",
        title="Headed",
        heading_uuid="loose-heading",
    )
    reminder = Record(
        uuid="bad-remind",
        kind="task",
        title="Remind",
        start=date(2026, 8, 20),
        remind="25:99",
    )
    library = MemoryLibrary(
        [area, heading, wrong_parent, wrong_area, no_project_heading, reminder]
    )

    assert "heading_entity_without_project" in item_conflicts(heading, library)
    assert "parent_not_project" in item_conflicts(wrong_parent, library)
    assert "area_not_area" in item_conflicts(wrong_area, library)
    assert "heading_without_project" in item_conflicts(no_project_heading, library)
    assert "malformed_reminder" in item_conflicts(reminder, library)

    project_parent = Record(uuid="root-project", kind="project", title="Root")
    nested = Record(
        uuid="inner-project",
        kind="project",
        title="Inner",
        parent_uuid=project_parent.uuid,
    )
    area_on_area = Record(
        uuid="inner-area",
        kind="area",
        title="Inner area",
        area_uuid=area.uuid,
    )
    area_on_project = Record(
        uuid="area-under-project",
        kind="area",
        title="Misplaced area",
        parent_uuid=project_parent.uuid,
    )
    kind_library = MemoryLibrary(
        [area, project_parent, nested, area_on_area, area_on_project]
    )
    assert "project_with_project_parent" in item_conflicts(nested, kind_library)
    assert "area_with_area_home" in item_conflicts(area_on_area, kind_library)
    assert "area_with_project_parent" in item_conflicts(area_on_project, kind_library)

    task = Record(uuid="loose-task", kind="task", title="Loose")
    area_parent_task = Record(
        uuid="area-under-task",
        kind="area",
        title="Area under task",
        parent_uuid=task.uuid,
    )
    area_parent_area = Record(
        uuid="area-under-area",
        kind="area",
        title="Area under area",
        parent_uuid=area.uuid,
    )
    area_home_task = Record(
        uuid="area-home-task",
        kind="area",
        title="Area home task",
        area_uuid=task.uuid,
    )
    area_home_project = Record(
        uuid="area-home-project",
        kind="area",
        title="Area home project",
        area_uuid=project_parent.uuid,
    )
    more_library = MemoryLibrary(
        [
            area,
            project_parent,
            task,
            area_parent_task,
            area_parent_area,
            area_home_task,
            area_home_project,
        ]
    )
    assert "area_invalid_parent" in item_conflicts(area_parent_task, more_library)
    assert "area_invalid_parent" in item_conflicts(area_parent_area, more_library)
    assert "area_invalid_home" in item_conflicts(area_home_task, more_library)
    assert "area_invalid_home" in item_conflicts(area_home_project, more_library)


def test_item_conflicts_kind_aware_missing_and_trashed_relations() -> None:
    task = Record(uuid="loose", kind="task", title="Loose")
    project = Record(uuid="launch", kind="project", title="Launch", trashed=True)
    area = Record(uuid="home", kind="area", title="Home", trashed=True)
    area_missing_parent = Record(
        uuid="area-missing-parent",
        kind="area",
        title="Missing parent",
        parent_uuid="gone-project",
    )
    area_missing_home = Record(
        uuid="area-missing-home",
        kind="area",
        title="Missing home",
        area_uuid="gone-area",
    )
    area_trashed_parent = Record(
        uuid="area-trashed-parent",
        kind="area",
        title="Trashed parent",
        parent_uuid=project.uuid,
    )
    area_trashed_home = Record(
        uuid="area-trashed-home",
        kind="area",
        title="Trashed home",
        area_uuid=area.uuid,
    )
    project_missing_parent = Record(
        uuid="project-missing-parent",
        kind="project",
        title="Missing parent",
        parent_uuid="gone-project",
    )
    library = MemoryLibrary(
        [
            task,
            project,
            area,
            area_missing_parent,
            area_missing_home,
            area_trashed_parent,
            area_trashed_home,
            project_missing_parent,
        ]
    )

    assert item_conflicts(area_missing_parent, library) == ["area_missing_parent"]
    assert item_conflicts(area_missing_home, library) == ["area_missing_home"]
    assert item_conflicts(area_trashed_parent, library) == ["area_with_project_parent"]
    assert "trashed_parent" not in item_conflicts(area_trashed_parent, library)
    assert item_conflicts(area_trashed_home, library) == ["area_with_area_home"]
    assert "trashed_area" not in item_conflicts(area_trashed_home, library)
    assert item_conflicts(project_missing_parent, library) == ["project_missing_parent"]
