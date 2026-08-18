from __future__ import annotations

from things_orchestrator.consistency import diagnose, item_conflicts
from things_orchestrator.library import MemoryLibrary, Record


def test_diagnose_finds_inbox_hybrids_and_tag_orphans() -> None:
    project = Record(uuid="home", kind="project", title="Home")
    hybrid = Record(
        uuid="stuck",
        kind="task",
        title="Stuck",
        inbox=True,
        parent_uuid=project.uuid,
    )
    library = MemoryLibrary([project, hybrid])
    library.tags["child"] = "Child"
    library.tag_parents["child"] = ["missing"]

    conflicts = {row.item_id: row.signals for row in diagnose(library)}

    assert "inbox_with_project" in conflicts["task:stuck"]
    assert "dangling_tag_parent" in conflicts["tag:child"]
    assert item_conflicts(hybrid, library) == ["inbox_with_project"]


def test_diagnose_covers_wrong_kinds_malformed_reminder_and_tag_cycles() -> None:
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
        start=__import__("datetime").date(2026, 8, 20),
        remind="25:99",
    )
    library = MemoryLibrary(
        [area, heading, wrong_parent, wrong_area, no_project_heading, reminder]
    )
    library.tags["self"] = "Self"
    library.tag_parents["self"] = ["self"]
    library.tags["a"] = "A"
    library.tags["b"] = "B"
    library.tag_parents["a"] = ["b"]
    library.tag_parents["b"] = ["a"]

    conflicts = {row.item_id: set(row.signals) for row in diagnose(library)}

    assert "heading_entity_without_project" in conflicts["heading:loose-heading"]
    assert "parent_not_project" in conflicts["task:under-area"]
    assert "area_not_area" in conflicts["task:area-is-project"]
    assert "heading_without_project" in conflicts["task:headed"]
    assert "malformed_reminder" in conflicts["task:bad-remind"]
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
    kind_conflicts = {
        row.item_id: set(row.signals)
        for row in diagnose(
            MemoryLibrary(
                [area, project_parent, nested, area_on_area, area_on_project]
            )
        )
    }
    assert "project_with_project_parent" in kind_conflicts["project:inner-project"]
    assert "area_with_area_home" in kind_conflicts["area:inner-area"]
    assert "area_with_project_parent" in kind_conflicts["area:area-under-project"]
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
    more_rows = {
        row.item_id: row
        for row in diagnose(
            MemoryLibrary(
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
        )
    }
    more = {item_id: set(row.signals) for item_id, row in more_rows.items()}
    assert "area_invalid_parent" in more["area:area-under-task"]
    assert "area_invalid_parent" in more["area:area-under-area"]
    assert "area_invalid_home" in more["area:area-home-task"]
    assert "area_invalid_home" in more["area:area-home-project"]
    assert more_rows["area:area-under-task"].repair_kind == "clear_area_parent"
    assert more_rows["area:area-home-project"].repair_kind == "clear_area_home"
    assert more_rows["area:area-under-task"].repair == "clear the invalid Area parent"
    assert more_rows["area:area-home-project"].repair == "clear the invalid Area home"
    assert "tag_parent_self_reference" in conflicts["tag:self"]
    assert "tag_parent_cycle" in conflicts["tag:a"]
    assert any(row.repair for row in diagnose(library))


def test_diagnose_uses_kind_aware_repairs_for_missing_and_trashed_relations() -> None:
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
    rows = {
        row.item_id: row
        for row in diagnose(
            MemoryLibrary(
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
        )
    }

    assert rows["area:area-missing-parent"].signals == ("area_missing_parent",)
    assert rows["area:area-missing-parent"].repair_kind == "clear_area_parent"
    assert rows["area:area-missing-home"].signals == ("area_missing_home",)
    assert rows["area:area-missing-home"].repair_kind == "clear_area_home"
    assert rows["area:area-trashed-parent"].signals == ("area_with_project_parent",)
    assert rows["area:area-trashed-parent"].repair_kind == "clear_area_parent"
    assert "trashed_parent" not in rows["area:area-trashed-parent"].signals
    assert rows["area:area-trashed-home"].signals == ("area_with_area_home",)
    assert rows["area:area-trashed-home"].repair_kind == "clear_area_home"
    assert "trashed_area" not in rows["area:area-trashed-home"].signals
    assert rows["project:project-missing-parent"].signals == (
        "project_missing_parent",
    )
    assert rows["project:project-missing-parent"].repair_kind == "rehome_project"
