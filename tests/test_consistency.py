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
    assert conflicts["tag:child"] == ("dangling_tag_parent",)
    assert item_conflicts(hybrid, library) == ["inbox_with_project"]
