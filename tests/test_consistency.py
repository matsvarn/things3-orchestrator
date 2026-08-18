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
    assert "tag_parent_self_reference" in conflicts["tag:self"]
    assert "tag_parent_cycle" in conflicts["tag:a"]
    assert any(row.repair for row in diagnose(library))
