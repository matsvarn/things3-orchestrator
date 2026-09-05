from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from things_orchestrator.interface import (
    ReadCall,
    dump_result,
)
from things_orchestrator.journal import MemoryJournal
from things_orchestrator.library import (
    ChecklistLine,
    MemoryLibrary,
    Record,
)
from things_orchestrator.recurrence import RecurrenceState
from things_orchestrator.workspace import ThingsWorkspace

NOW = datetime(2026, 8, 15, 12, tzinfo=timezone.utc)


def workspace(records: list[Record] | None = None) -> ThingsWorkspace:
    return ThingsWorkspace(
        MemoryLibrary(records), journal=MemoryJournal(), clock=lambda: NOW
    )


def detail(module: ThingsWorkspace, item_id: str):
    result = module.read(ReadCall(ids=[item_id]))
    assert result.status == "ok"
    return result.items[0]


def system_scope(module: ThingsWorkspace) -> str:
    result = module.read(ReadCall(view="system"))
    assert result.scope_revision is not None
    return result.scope_revision


def test_empty_read_returns_bounded_today_sections() -> None:
    module = workspace(
        [
            Record(
                uuid="late",
                kind="task",
                title="Late",
                deadline=NOW.date().replace(day=14),
            ),
            Record(uuid="box", kind="task", title="Inbox", inbox=True),
            Record(
                uuid="tonight",
                kind="task",
                title="Tonight",
                start=NOW.date(),
                tonight=True,
            ),
        ]
    )

    result = module.read(ReadCall())

    assert result.status == "ok"
    assert [section.key for section in result.sections] == ["overdue", "evening"]
    assert [item.id for item in result.items] == ["task:late", "task:tonight"]
    assert [section.item_ids for section in result.sections] == [[], []]
    inbox = module.read(ReadCall(view="inbox"))
    assert [item.id for item in inbox.items] == ["task:box"]
    assert result.scope_revision and result.scope_revision.startswith("s_")


def test_exact_read_returns_markdown_checklist_tags_and_revisions() -> None:
    task = Record(
        uuid="task1",
        kind="task",
        title="Launch",
        notes="## Outcome\n\nShip it.",
        tag_uuids=["focus"],
        checklists=[ChecklistLine("row1", "Verify", status="dropped", sort_index=5)],
    )
    library = MemoryLibrary([task])
    library.tags["focus"] = "Focus"
    module = ThingsWorkspace(library, clock=lambda: NOW)

    item = module.read(ReadCall(id="task:task1")).items[0]

    assert item.notes_markdown == "## Outcome\n\nShip it."
    assert item.checklist[0].id == "check:row1"
    assert item.checklist[0].status == "canceled"
    assert item.direct_tags[0].id == "tag:focus"
    assert item.revision.startswith("r_")


def test_change_find_resolves_one_active_item_and_hides_revision() -> None:
    module = workspace(
        [
            Record(uuid="invoice", kind="task", title="Pay invoice"),
            Record(uuid="other", kind="task", title="Book travel"),
        ]
    )

    result = module.read(ReadCall(purpose="change", find="invoice"))

    assert result.status == "ok"
    assert result.context is not None
    assert result.context.purpose == "change"
    assert result.items[0].ref is not None
    assert result.items[0].revision is None


def test_change_find_requires_one_active_match() -> None:
    empty = workspace([Record(uuid="one", kind="task", title="Pay rent")])
    no_match = empty.read(ReadCall(purpose="change", find="invoice"))
    assert no_match.status == "needs_input"
    assert no_match.context is None
    assert no_match.recovery is None

    ambiguous = workspace(
        [
            Record(uuid="one", kind="task", title="Pay invoice"),
            Record(uuid="two", kind="task", title="Email invoice"),
        ]
    ).read(ReadCall(purpose="change", find="invoice"))
    assert ambiguous.status == "needs_input"
    assert ambiguous.context is None
    assert "matches 2 items" in ambiguous.instruction
    assert {item.id for item in ambiguous.items} == {"task:one", "task:two"}
    assert all(item.revision is None for item in ambiguous.items)


def test_change_find_ignores_articles_for_one_unique_title_match() -> None:
    module = workspace([Record(uuid="plants", kind="task", title="Water plants")])

    result = module.read(ReadCall(purpose="change", find="water the plants"))

    assert result.status == "ok"
    assert result.items[0].id == "task:plants"
    assert result.context is not None


def test_change_find_keeps_article_fallback_ambiguous() -> None:
    module = workspace(
        [
            Record(uuid="one", kind="task", title="Water plants"),
            Record(uuid="two", kind="task", title="Water the plants"),
        ]
    )

    result = module.read(ReadCall(purpose="change", find="water the plants"))

    assert result.status == "needs_input"
    assert result.next == "ask"
    assert {item.id for item in result.items} == {"task:one", "task:two"}


@pytest.mark.parametrize(
    "record, query",
    [
        (
            Record(
                uuid="note", kind="task", title="Chores", notes="Water plants"
            ),
            "water the plants",
        ),
        (
            Record(
                uuid="checklist",
                kind="task",
                title="Chores",
                checklists=[ChecklistLine("row", "Water plants")],
            ),
            "water the plants",
        ),
    ],
)
def test_change_find_article_fallback_matches_notes_and_checklists(
    record: Record, query: str
) -> None:
    result = workspace([record]).read(ReadCall(purpose="change", find=query))

    assert result.status == "ok"
    assert result.items[0].id == f"task:{record.uuid}"


def test_change_find_does_not_stem_or_fuzz_token_fallback() -> None:
    module = workspace([Record(uuid="plants", kind="task", title="Water plants")])

    result = module.read(ReadCall(purpose="change", find="water planting"))

    assert result.status == "needs_input"
    assert result.context is None
    assert "found no item" in result.instruction


def test_find_includes_active_headings_for_rename() -> None:
    heading = Record(
        uuid="prep", kind="task", title="Prep", heading=True, parent_uuid="project"
    )
    module = workspace([heading])

    contextual = module.read(ReadCall(purpose="change", find="Prep"))
    review = module.read(ReadCall(find="Prep"))

    assert contextual.status == "ok"
    assert contextual.items[0].id == "heading:prep"
    assert review.status == "ok"
    assert review.items[0].id == "heading:prep"
    assert review.instruction.startswith(
        "These matches. Name one to open or stop."
    )


def test_review_find_returns_closed_matches_when_nothing_is_active() -> None:
    records = [
        Record(uuid="done", kind="task", title="Prep done", status="done"),
        Record(uuid="trash", kind="task", title="Prep trash", trashed=True),
        Record(
            uuid="template",
            kind="task",
            title="Prep template",
            heading=True,
            recurrence=RecurrenceState(role="template"),
        ),
    ]

    review = workspace(records).read(ReadCall(find="Prep"))
    assert {item.id for item in review.items} == {"task:done", "task:trash"}
    assert "not active" in review.instruction
    assert "trashed" in next(
        item.signals for item in review.items if item.id == "task:trash"
    )

    change = workspace(records).read(ReadCall(purpose="change", find="invoice"))
    assert change.status == "needs_input"
    assert change.context is None

    restore = workspace(records).read(ReadCall(purpose="change", find="Prep trash"))
    assert restore.status == "ok"
    assert restore.context is not None
    assert [item.id for item in restore.items if item.id == "task:trash"] == [
        "task:trash"
    ]


def test_exact_read_exposes_heading_repeat_pattern_and_linked_copy() -> None:
    project = Record(uuid="facts-project", kind="project", title="Plan")
    heading = Record(
        uuid="facts-heading",
        kind="task",
        title="Next",
        parent_uuid=project.uuid,
        heading=True,
    )
    template = Record(
        uuid="facts-template",
        kind="task",
        title="Review",
        parent_uuid=project.uuid,
        heading_uuid=heading.uuid,
        recurrence=RecurrenceState(
            role="template",
            repeat_type="fixed",
            rule={"tp": 0, "fu": 256, "fa": 1, "of": [{"wd": 1}, {"wd": 5}]},
        ),
    )
    copy = Record(
        uuid="facts-copy",
        kind="task",
        title="Review",
        recurrence=RecurrenceState(
            role="instance",
            repeat_type="fixed",
            template_uuid=template.uuid,
            links=(template.uuid,),
        ),
    )
    module = workspace([project, heading, template, copy])

    item = detail(module, template.id)

    assert item.heading_id == heading.id
    assert item.recurrence is not None
    assert item.recurrence.mode == "fixed"
    assert item.recurrence.weekdays == ["monday", "friday"]
    assert item.recurrence.linked_item_ids == [copy.id]


def test_template_lists_both_recurrence_relationship_forms_without_duplicates() -> None:
    template = Record(
        uuid="mix-template",
        kind="task",
        title="Template",
        recurrence=RecurrenceState(role="template", repeat_type="fixed", rule={"tp": 0}),
    )
    via_links = Record(
        uuid="via-links",
        kind="task",
        title="Links",
        sort_index=2,
        recurrence=RecurrenceState(role="instance", links=(template.uuid,)),
    )
    via_uuid = Record(
        uuid="via-uuid",
        kind="task",
        title="UUID",
        sort_index=1,
        recurrence=RecurrenceState(
            role="instance", template_uuid=template.uuid
        ),
    )
    both = Record(
        uuid="via-both",
        kind="task",
        title="Both",
        sort_index=0,
        recurrence=RecurrenceState(
            role="instance",
            template_uuid=template.uuid,
            links=(template.uuid,),
        ),
    )
    module = workspace([template, via_links, via_uuid, both])

    item = detail(module, template.id)
    bulk = module.read(ReadCall(ids=[template.id]))

    assert item.recurrence is not None
    assert item.recurrence.linked_item_ids == [both.id, via_uuid.id, via_links.id]
    assert bulk.items[0].recurrence is not None
    assert bulk.items[0].recurrence.linked_item_ids == [
        both.id,
        via_uuid.id,
        via_links.id,
    ]
    links_only = module.read(ReadCall(purpose="recurrence", id=via_links.id))
    uuid_only = module.read(ReadCall(purpose="recurrence", id=via_uuid.id))
    assert links_only.status == "ok"
    assert uuid_only.status == "ok"
    assert links_only.items[0].recurrence is not None
    assert links_only.items[0].recurrence.template_id == template.id


def test_template_detail_pages_mixed_recurrence_relationships() -> None:
    template = Record(
        uuid="page-template",
        kind="task",
        title="Template",
        recurrence=RecurrenceState(role="template", repeat_type="fixed", rule={"tp": 0}),
    )
    instances = []
    for index in range(25):
        instances.append(
            Record(
                uuid=f"link-{index:02d}",
                kind="task",
                title=f"Link {index}",
                sort_index=index,
                recurrence=RecurrenceState(role="instance", links=(template.uuid,)),
            )
        )
        instances.append(
            Record(
                uuid=f"uuid-{index:02d}",
                kind="task",
                title=f"UUID {index}",
                sort_index=index + 25,
                recurrence=RecurrenceState(
                    role="instance", template_uuid=template.uuid
                ),
            )
        )
    module = workspace([template, *instances])

    result = module.read(ReadCall(id=template.id, limit=20))
    found: list[str] = []
    pages = 0
    while True:
        pages += 1
        item = result.items[0]
        assert item.recurrence is not None
        found.extend(item.recurrence.linked_item_ids)
        if result.cursor is None:
            break
        result = module.read(ReadCall(cursor=result.cursor, limit=20))

    assert pages == 3
    assert len(found) == 50
    assert len(set(found)) == 50
    assert {f"task:link-{index:02d}" for index in range(25)}.issubset(found)
    assert {f"task:uuid-{index:02d}" for index in range(25)}.issubset(found)


def test_template_detail_cursor_stales_when_an_instance_is_removed() -> None:
    template = Record(
        uuid="stale-template",
        kind="task",
        title="Template",
        recurrence=RecurrenceState(role="template", repeat_type="fixed", rule={"tp": 0}),
    )
    instances = [
        Record(
            uuid=f"c{index:02d}",
            kind="task",
            title=f"Copy {index}",
            sort_index=index,
            recurrence=RecurrenceState(
                role="instance",
                template_uuid=template.uuid,
                links=(template.uuid,),
            ),
        )
        for index in range(25)
    ]
    library = MemoryLibrary([template, *instances])
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)

    first = module.read(ReadCall(id=template.id, limit=10))
    assert first.cursor is not None
    assert first.items[0].recurrence is not None
    assert first.items[0].recurrence.linked_item_ids == [
        f"task:c{index:02d}" for index in range(10)
    ]
    del library.records["c00"]
    stale = module.read(ReadCall(cursor=first.cursor, limit=10))

    assert stale.status == "stale"
    assert stale.next == "read"


def test_recurrence_read_verifies_template_and_generated_copy_relationship() -> None:
    template = Record(
        uuid="inspect-template",
        kind="task",
        title="Review",
        recurrence=RecurrenceState(
            role="template",
            repeat_type="fixed",
            rule={"tp": 0, "fu": 256, "fa": 1},
        ),
    )
    copy = Record(
        uuid="inspect-copy",
        kind="task",
        title="Review",
        recurrence=RecurrenceState(
            role="instance",
            repeat_type="fixed",
            template_uuid=template.uuid,
            links=(template.uuid,),
        ),
    )
    module = workspace([template, copy])

    template_result = module.read(
        ReadCall(purpose="recurrence", id=template.id)
    )
    copy_result = module.read(ReadCall(purpose="recurrence", id=copy.id))

    assert template_result.status == "ok"
    assert copy_result.status == "ok"
    assert "recurrence_relationship_verified" in template_result.signals
    assert "recurrence_relationship_verified" in copy_result.signals
    assert template_result.items[0].recurrence is not None
    assert copy_result.items[0].recurrence is not None
    assert copy.id in template_result.items[0].recurrence.linked_item_ids
    assert copy_result.items[0].recurrence.template_id == template.id


def test_recurrence_read_verifies_repeating_project_relationship() -> None:
    template = Record(
        uuid="inspect-project-template",
        kind="project",
        title="Release train",
        recurrence=RecurrenceState(
            role="template",
            repeat_type="fixed",
            rule={"tp": 0, "fu": 256, "fa": 1},
        ),
    )
    copy = Record(
        uuid="inspect-project-copy",
        kind="project",
        title="Release train",
        recurrence=RecurrenceState(
            role="instance",
            repeat_type="fixed",
            template_uuid=template.uuid,
            links=(template.uuid,),
        ),
    )
    module = workspace([template, copy])

    template_result = module.read(ReadCall(purpose="recurrence", id=template.id))
    copy_result = module.read(ReadCall(purpose="recurrence", id=copy.id))

    assert template_result.status == copy_result.status == "ok"
    assert copy.id in template_result.items[0].recurrence.linked_item_ids
    assert copy_result.items[0].recurrence.template_id == template.id


def test_recurrence_read_rejects_dangling_generated_copy() -> None:
    copy = Record(
        uuid="dangling-copy",
        kind="task",
        title="Broken repeat",
        recurrence=RecurrenceState(
            role="instance",
            repeat_type="fixed",
            template_uuid="missing-template",
            links=("missing-template",),
        ),
    )

    result = workspace([copy]).read(ReadCall(purpose="recurrence", id=copy.id))

    assert result.status == "unsupported"
    assert result.next == "stop"


def test_trash_view_returns_recoverable_exact_items() -> None:
    trashed = Record(uuid="trash-view", kind="task", title="Recover", trashed=True)
    active = Record(uuid="active-view", kind="task", title="Keep")
    module = workspace([trashed, active])

    result = module.read(ReadCall(view="trash"))

    assert [item.id for item in result.items] == [trashed.id]
    assert "trashed" in result.items[0].signals
    assert "Read an item to restore or purge." in result.instruction
    assert "purpose=change" not in result.instruction


def test_exact_read_bounds_external_text_and_order_facts() -> None:
    long_text = "x" * 100_005
    area = Record(uuid="area", kind="area", title=long_text, tag_uuids=["area-tag"])
    project = Record(
        uuid="project",
        kind="project",
        title=long_text,
        area_uuid=area.uuid,
        tag_uuids=["project-tag"],
    )
    task = Record(
        uuid="task",
        kind="task",
        title=long_text,
        notes=long_text,
        parent_uuid=project.uuid,
        heading_uuid="heading",
        tag_uuids=["direct-tag"],
        checklists=[ChecklistLine("row", long_text, sort_index=2**80)],
        start=NOW.date(),
        sort_index=-(2**80),
        today_index=2**80,
    )
    heading = Record(
        uuid="heading",
        kind="task",
        title=long_text,
        parent_uuid=project.uuid,
        heading=True,
    )
    library = MemoryLibrary([area, project, task, heading])
    library.tags = {
        "area-tag": long_text,
        "project-tag": long_text,
        "direct-tag": long_text,
    }
    module = ThingsWorkspace(library, clock=lambda: NOW)

    result = module.read(ReadCall(id=task.id))

    assert result.status == "ok"
    assert "continue the exact item" in result.instruction
    item = result.items[0]
    assert len(item.title) == 1000
    assert len(item.notes_markdown or "") == 50_000
    assert len(item.checklist[0].title) == 1000
    assert all(
        len(tag.title) == 1000 for tag in [*item.direct_tags, *item.inherited_tags]
    )
    assert item.order == -(2**63)
    assert item.today_order == 2**63 - 1
    assert item.checklist[0].order == 2**63 - 1
    assert "notes_truncated" in item.signals


def test_search_matches_pack_when_query_is_packing() -> None:
    task = Record(uuid="packing", kind="task", title="Pack for trip")
    module = workspace([task])

    result = module.read(ReadCall(find="packing"))

    assert result.status == "ok"
    assert [item.id for item in result.items] == [task.id]


def test_task_search_scope_never_falls_back_to_global_search() -> None:
    scope = Record(uuid="scope", kind="task", title="Scope")
    match = Record(uuid="match", kind="task", title="Needle")
    module = workspace([scope, match])
    malformed = ReadCall.model_construct(find="Needle", within="task:scope")

    result = module.read(malformed)

    assert result.status == "needs_input"
    assert result.next == "ask"
    assert result.items == []


def test_exact_detail_pages_checklist_and_tag_facts_once_in_stable_order() -> None:
    area_tags = [f"area-tag-{index}" for index in range(20)]
    project_tags = [f"project-tag-{index}" for index in range(25)]
    direct_tags = [f"direct-tag-{index}" for index in range(45)]
    area = Record(
        uuid="area",
        kind="area",
        title="Work",
        tag_uuids=area_tags,
    )
    project = Record(
        uuid="project",
        kind="project",
        title="Launch",
        area_uuid=area.uuid,
        tag_uuids=project_tags,
    )
    task = Record(
        uuid="task",
        kind="task",
        title="Ship",
        notes="Keep this note once.",
        parent_uuid=project.uuid,
        tag_uuids=direct_tags,
        checklists=[
            ChecklistLine(f"row-{index}", f"Step {index}", sort_index=index)
            for index in range(45)
        ],
    )
    library = MemoryLibrary([area, project, task])
    library.tags = {
        uuid: uuid.replace("-", " ").title()
        for uuid in [*area_tags, *project_tags, *direct_tags]
    }
    module = ThingsWorkspace(library, clock=lambda: NOW)

    result = module.read(ReadCall(id=task.id, limit=40))
    revision = result.scope_revision
    item_revision = result.items[0].revision
    checklist_ids: list[str] = []
    direct_ids: list[str] = []
    inherited_ids: list[str] = []
    notes: list[str | None] = []
    pages = 0
    while True:
        pages += 1
        item = result.items[0]
        checklist_ids.extend(row.id for row in item.checklist)
        direct_ids.extend(tag.id for tag in item.direct_tags)
        inherited_ids.extend(tag.id for tag in item.inherited_tags)
        notes.append(item.notes_markdown)
        assert result.scope_revision == revision
        assert item.revision == item_revision
        if result.cursor is None:
            assert result.truncated is False
            break
        assert result.truncated is True
        result = module.read(ReadCall(cursor=result.cursor, limit=40))

    assert pages == 4
    assert checklist_ids == [f"check:row-{index}" for index in range(45)]
    assert direct_ids == [f"tag:{uuid}" for uuid in direct_tags]
    assert inherited_ids == [
        *(f"tag:{uuid}" for uuid in project_tags),
        *(f"tag:{uuid}" for uuid in area_tags),
    ]
    assert notes == ["Keep this note once.", None, None, None]
    assert "checklist_truncated" not in result.items[0].signals
    assert "tags_truncated" not in result.items[0].signals


def test_empty_note_is_returned_only_on_the_first_detail_page() -> None:
    task = Record(
        uuid="task",
        kind="task",
        title="Empty note",
        notes="",
        checklists=[
            ChecklistLine(f"row-{index}", f"Step {index}", sort_index=index)
            for index in range(21)
        ],
    )
    module = workspace([task])

    first = module.read(ReadCall(id=task.id, limit=20))
    assert first.cursor is not None
    second = module.read(ReadCall(cursor=first.cursor, limit=20))

    assert first.items[0].notes_markdown == ""
    assert second.items[0].notes_markdown is None


def test_detail_cursor_rejects_a_repeated_view() -> None:
    task = Record(
        uuid="task",
        kind="task",
        title="Paged detail",
        notes="x" * 50_001,
    )
    module = workspace([task])

    first = module.read(ReadCall(id=task.id))
    assert first.cursor is not None
    continued = module.read(
        ReadCall.model_validate({"cursor": first.cursor, "view": "audit"})
    )

    assert continued.status == "needs_input"
    assert continued.next == "ask"
    assert continued.items == []


def test_exact_detail_cursor_rejects_a_changed_inherited_tag() -> None:
    parent = Record(
        uuid="project",
        kind="project",
        title="Launch",
        tag_uuids=["context"],
    )
    task = Record(
        uuid="task",
        kind="task",
        title="Ship",
        parent_uuid=parent.uuid,
        checklists=[
            ChecklistLine(f"row-{index}", f"Step {index}", sort_index=index)
            for index in range(41)
        ],
    )
    library = MemoryLibrary([parent, task])
    library.tags["context"] = "Context"
    module = ThingsWorkspace(library, clock=lambda: NOW)

    first = module.read(ReadCall(id=task.id, limit=40))
    assert first.cursor is not None
    library.tags["context"] = "Changed context"

    result = module.read(ReadCall(cursor=first.cursor, limit=40))

    assert result.status == "stale"
    assert result.next == "read"


@pytest.mark.parametrize("length", [50_001, 120_001])
def test_exact_detail_pages_long_notes_without_repeat_or_loss(length: int) -> None:
    notes = "".join(str(index % 10) for index in range(length))
    task = Record(uuid="task", kind="task", title="Long note", notes=notes)
    module = workspace([task])

    result = module.read(ReadCall(id=task.id))
    chunks: list[str] = []
    signals: list[list[str]] = []
    while True:
        item = result.items[0]
        if item.notes_markdown is not None:
            chunks.append(item.notes_markdown)
        signals.append(item.signals)
        if result.cursor is None:
            assert result.truncated is False
            break
        assert result.truncated is True
        result = module.read(ReadCall(cursor=result.cursor))

    assert "".join(chunks) == notes
    assert [len(chunk) for chunk in chunks] == [
        *([50_000] * (length // 50_000)),
        *([length % 50_000] if length % 50_000 else []),
    ]
    assert all("notes_truncated" in page for page in signals[:-1])
    assert "notes_truncated" not in signals[-1]


def test_long_notes_and_detail_rows_advance_together_to_one_final_page() -> None:
    notes = "n" * 100_001
    task = Record(
        uuid="task",
        kind="task",
        title="Mixed detail",
        notes=notes,
        tag_uuids=[f"direct-{index}" for index in range(45)],
        checklists=[
            ChecklistLine(f"row-{index}", f"Step {index}", sort_index=index)
            for index in range(45)
        ],
    )
    parent = Record(
        uuid="project",
        kind="project",
        title="Parent",
        tag_uuids=[f"inherited-{index}" for index in range(45)],
    )
    task.parent_uuid = parent.uuid
    library = MemoryLibrary([parent, task])
    library.tags = {uuid: uuid for uuid in [*task.tag_uuids, *parent.tag_uuids]}
    module = ThingsWorkspace(library, clock=lambda: NOW)

    result = module.read(ReadCall(id=task.id, limit=20))
    chunks: list[str] = []
    checklist_ids: list[str] = []
    direct_ids: list[str] = []
    inherited_ids: list[str] = []
    pages = 0
    while True:
        pages += 1
        item = result.items[0]
        if item.notes_markdown is not None:
            chunks.append(item.notes_markdown)
        checklist_ids.extend(row.id for row in item.checklist)
        direct_ids.extend(tag.id for tag in item.direct_tags)
        inherited_ids.extend(tag.id for tag in item.inherited_tags)
        if result.cursor is None:
            assert result.truncated is False
            assert "notes_truncated" not in item.signals
            assert "checklist_truncated" not in item.signals
            assert "tags_truncated" not in item.signals
            break
        result = module.read(ReadCall(cursor=result.cursor, limit=20))

    assert pages == 7
    assert "".join(chunks) == notes
    assert [len(chunk) for chunk in chunks] == [50_000, 50_000, 1]
    assert checklist_ids == [f"check:row-{index}" for index in range(45)]
    assert direct_ids == [f"tag:direct-{index}" for index in range(45)]
    assert inherited_ids == [f"tag:inherited-{index}" for index in range(45)]


def test_long_note_cursor_stales_after_the_note_changes() -> None:
    task = Record(uuid="task", kind="task", title="Long", notes="x" * 50_001)
    module = workspace([task])
    first = module.read(ReadCall(id=task.id))
    assert first.cursor is not None
    task.notes = "changed" + task.notes

    result = module.read(ReadCall(cursor=first.cursor))

    assert result.status == "stale"
    assert result.next == "read"
    assert result.items == []


def test_cursor_is_short_and_rejects_changed_snapshot() -> None:
    records = [
        Record(uuid=f"t{index}", kind="task", title=f"Task {index}", inbox=True)
        for index in range(45)
    ]
    module = workspace(records)

    first = module.read(ReadCall(view="inbox", limit=40))
    assert first.cursor and len(first.cursor) < 64
    module._library.records["t44"].title = "Changed"  # noqa: SLF001

    continued = module.read(ReadCall(cursor=first.cursor))

    assert continued.status == "stale"
    assert continued.next == "read"


def test_tag_catalog_pages_with_stable_opaque_cursors() -> None:
    library = MemoryLibrary()
    library.tags = {f"tag{index}": f"Tag {index:02d}" for index in range(25)}
    module = ThingsWorkspace(library, clock=lambda: NOW)

    first = module.read(ReadCall(view="tags"))
    second = (
        module.read(ReadCall(cursor=first.cursor, view="tags"))
        if first.cursor
        else None
    )

    assert [tag.id for tag in first.tags] == [f"tag:tag{index}" for index in range(20)]
    assert first.truncated is True
    assert second is not None
    assert [tag.id for tag in second.tags] == [
        f"tag:tag{index}" for index in range(20, 25)
    ]
    assert first.sections == []
    assert second.truncated is False


def test_tag_catalog_honors_limit_and_bounds_each_row() -> None:
    library = MemoryLibrary()
    library.tags = {
        **{f"tag{index}": f"Tag {index:02d}" for index in range(40)},
        "long": "x" * 100_000,
    }
    module = ThingsWorkspace(library, clock=lambda: NOW)

    first = module.read(ReadCall(view="tags", limit=40))

    assert len(first.tags) == 40
    assert all(len(tag.title) <= 1000 for tag in first.tags)
    assert first.cursor is not None


def test_thirty_today_items_page_without_loss() -> None:
    records = [
        Record(
            uuid=f"today-{index}",
            kind="task",
            title=f"Today {index}",
            start=NOW.date(),
            today_index=index,
        )
        for index in range(30)
    ]
    module = workspace(records)

    result = module.read(ReadCall(view="today", limit=10))
    ids: list[str] = []
    while True:
        ids.extend(item.id for item in result.items)
        assert result.sections[0].item_ids == []
        if result.cursor is None:
            break
        result = module.read(ReadCall(cursor=result.cursor, limit=10))

    assert ids == [f"task:today-{index}" for index in range(30)]


def test_today_matches_native_scheduling_and_excludes_waiting_only() -> None:
    records = [
        Record(
            uuid="past",
            kind="task",
            title="Past scheduled",
            start=NOW.date() - timedelta(days=3),
        ),
        Record(uuid="today", kind="task", title="Today", start=NOW.date()),
        Record(
            uuid="future",
            kind="task",
            title="Future",
            start=NOW.date() + timedelta(days=1),
        ),
        Record(
            uuid="waiting",
            kind="task",
            title="Waiting only",
            tag_uuids=["waiting-tag"],
        ),
    ]
    module = workspace(records)
    module._library.tags["waiting-tag"] = "Waiting"

    result = module.read(ReadCall(view="today"))

    assert {item.id for item in result.items} == {"task:past", "task:today"}


def test_system_pages_keep_one_scope_revision_and_section_shape() -> None:
    records = [
        Record(
            uuid=f"area-{index}",
            kind="area",
            title=f"Area {index:02d}",
            sort_index=index,
        )
        for index in range(12)
    ] + [
        Record(
            uuid=f"project-{index}",
            kind="project",
            title=f"Project {index:02d}",
            area_uuid=f"area-{index % 12}",
            sort_index=index,
        )
        for index in range(13)
    ]
    module = workspace(records)

    result = module.read(ReadCall(view="system", limit=10))
    revision = result.scope_revision
    ids: list[str] = []
    while True:
        ids.extend(item.id for item in result.items)
        assert result.scope_revision == revision
        assert [section.key for section in result.sections] == ["system"]
        assert result.sections[0].item_ids == []
        if result.cursor is None:
            break
        result = module.read(ReadCall(cursor=result.cursor, limit=10))

    assert len(ids) == 25
    assert len(set(ids)) == 25


def _repeating_pair() -> tuple[ThingsWorkspace, Record, Record]:
    template = Record(
        uuid="report-template",
        kind="task",
        title="Weekly report",
        recurrence=RecurrenceState(
            role="template",
            repeat_type="fixed",
            rule={"tp": 0, "fu": 8, "fa": 1, "of": []},
        ),
        recurrence_next_on=NOW.date() + timedelta(days=7),
    )
    current = Record(
        uuid="report-current",
        kind="task",
        title="Weekly report",
        recurrence=RecurrenceState(
            role="instance",
            repeat_type="fixed",
            template_uuid=template.uuid,
            links=(template.uuid,),
        ),
    )
    return workspace([template, current]), template, current


def test_area_view_returns_the_area_loose_tasks_and_projects() -> None:
    area = Record(uuid="home", kind="area", title="Home")
    project = Record(
        uuid="kitchen",
        kind="project",
        title="Kitchen",
        area_uuid=area.uuid,
    )
    loose = Record(uuid="buy-milk", kind="task", title="Buy milk", area_uuid=area.uuid)
    nested = Record(
        uuid="tap",
        kind="task",
        title="Replace tap",
        parent_uuid=project.uuid,
    )
    other = Record(uuid="work", kind="area", title="Work")
    module = workspace([area, project, loose, nested, other])

    result = module.read(ReadCall(view="area", within=area.id))

    assert result.status == "ok"
    assert {item.id for item in result.items} == {area.id, project.id, loose.id}
    assert result.items[0].id == area.id
    assert nested.id not in {item.id for item in result.items}
    assert result.sections[0].title == "Home"
    assert result.sections[0].item_ids == []
    assert any(
        item.id == project.id and item.into_title == "Home" for item in result.items
    )
    assert any(
        item.id == loose.id and item.into_title == "Home" for item in result.items
    )


def test_area_and_project_ids_expand_to_children_on_review() -> None:
    area = Record(uuid="home", kind="area", title="Home")
    project = Record(
        uuid="kitchen",
        kind="project",
        title="Kitchen",
        area_uuid=area.uuid,
    )
    loose = Record(uuid="buy-milk", kind="task", title="Buy milk", area_uuid=area.uuid)
    nested = Record(
        uuid="tap",
        kind="task",
        title="Replace tap",
        parent_uuid=project.uuid,
    )
    module = workspace([area, project, loose, nested])

    by_id = module.read(ReadCall(id=area.id))
    by_view = module.read(ReadCall(view="area", id=area.id))

    assert {item.id for item in by_id.items} == {area.id, project.id, loose.id}
    assert {item.id for item in by_view.items} == {area.id, project.id, loose.id}
    assert by_id.context is None
    assert all(item.ref is None for item in by_id.items)

    project_read = module.read(ReadCall(id=project.id))
    assert {item.id for item in project_read.items} == {
        project.id,
        area.id,
        nested.id,
    }
    assert project_read.context is not None
    assert project_read.layouts
    assert project_read.layouts[0].complete


def test_truncated_audit_pages_without_accumulating_write_context() -> None:
    records = [
        Record(uuid=f"item-{index:02d}", kind="task", title=f"Task {index:02d}")
        for index in range(25)
    ]
    module = workspace(records)
    page = module.read(ReadCall(view="audit", limit=10))
    seen = []
    while True:
        assert page.status == "ok"
        assert page.context is None
        assert all(item.ref is None for item in page.items)
        assert page.truncated == (page.cursor is not None)
        seen.extend(item.id for item in page.items)
        if page.cursor is None:
            break
        page = module.read(ReadCall(cursor=page.cursor, limit=10))
    assert seen == [record.id for record in records]


def test_truncated_audit_cursor_stales_after_area_registry_changes() -> None:
    records = [
        Record(uuid=f"item-{index:02d}", kind="task", title=f"Task {index:02d}")
        for index in range(25)
    ]
    module = workspace(records)

    first = module.read(ReadCall(view="audit", limit=10))
    assert first.cursor is not None
    module._library.records["new-area"] = Record(  # noqa: SLF001
        uuid="new-area",
        kind="area",
        title="New Area",
    )

    continued = module.read(ReadCall(cursor=first.cursor, limit=10))

    assert continued.status == "stale"
    assert continued.next == "read"
    assert continued.items == []


def test_truncated_audit_cursor_stales_after_active_item_is_added() -> None:
    records = [
        Record(uuid=f"item-{index:02d}", kind="task", title=f"Task {index:02d}")
        for index in range(25)
    ]
    module = workspace(records)

    first = module.read(ReadCall(view="audit", limit=10))
    assert first.cursor is not None
    module._library.records["new-task"] = Record(  # noqa: SLF001
        uuid="new-task",
        kind="task",
        title="New Task",
    )

    continued = module.read(ReadCall(cursor=first.cursor, limit=10))

    assert continued.status == "stale"
    assert continued.next == "read"
    assert continued.items == []


def test_truncated_filtered_audit_continues_without_changes() -> None:
    records = [
        Record(
            uuid=f"someday-{index:02d}",
            kind="task",
            title=f"Someday {index:02d}",
            someday=True,
        )
        for index in range(25)
    ]
    records.extend(
        Record(
            uuid=f"inbox-{index:02d}",
            kind="task",
            title=f"Inbox {index:02d}",
            inbox=True,
        )
        for index in range(5)
    )
    module = workspace(records)

    first = module.read(
        ReadCall(view="audit", signals_any=["someday"], limit=10)
    )
    assert first.cursor is not None

    continued = module.read(ReadCall(cursor=first.cursor, limit=10))

    assert continued.status == "ok"
    assert continued.next == "read"
    assert continued.cursor is not None
    assert all("someday" in item.signals for item in continued.items)

    final = module.read(ReadCall(cursor=continued.cursor, limit=10))

    assert final.status == "ok"
    assert final.cursor is None
    assert final.context is None
    assert final.layouts == []
    assert len(first.items) + len(continued.items) + len(final.items) == 25
    assert all("someday" in item.signals for item in final.items)


def test_audit_cursor_accepts_the_repeated_view() -> None:
    records = [
        Record(uuid=f"item-{index:02d}", kind="task", title=f"Task {index:02d}")
        for index in range(25)
    ]
    module = workspace(records)
    first = module.read(ReadCall(view="audit", limit=10))
    assert first.cursor is not None

    continued = module.read(
        ReadCall.model_validate(
            {"cursor": first.cursor, "view": "audit", "limit": 10}
        )
    )

    assert continued.status == "ok"
    assert continued.cursor is not None
    assert [item.id for item in continued.items] == [
        f"task:item-{index:02d}" for index in range(10, 20)
    ]


def test_audit_cursor_rejects_a_different_view() -> None:
    records = [
        Record(uuid=f"item-{index:02d}", kind="task", title=f"Task {index:02d}")
        for index in range(25)
    ]
    module = workspace(records)
    first = module.read(ReadCall(view="audit", limit=10))
    assert first.cursor is not None

    continued = module.read(
        ReadCall.model_validate(
            {"cursor": first.cursor, "view": "today", "limit": 10}
        )
    )

    assert continued.status == "needs_input"
    assert continued.next == "ask"
    assert continued.items == []


def test_logbook_defaults_to_the_last_fourteen_days() -> None:
    recent = Record(
        uuid="recent",
        kind="task",
        title="Recent",
        status="done",
        completed_at=NOW - timedelta(days=2),
    )
    old = Record(
        uuid="old",
        kind="task",
        title="Old",
        status="done",
        completed_at=NOW - timedelta(days=20),
    )
    module = workspace([recent, old])

    result = module.read(ReadCall(view="logbook"))

    assert result.status == "ok"
    assert [item.id for item in result.items] == [recent.id]
    assert "2026-08-02" in result.instruction
    assert "2026-08-15" in result.instruction


def test_audit_view_lists_each_active_item_once() -> None:
    area = Record(uuid="home", kind="area", title="Home")
    project = Record(
        uuid="kitchen",
        kind="project",
        title="Kitchen",
        area_uuid=area.uuid,
    )
    task = Record(
        uuid="milk",
        kind="task",
        title="Buy milk",
        notes="semi",
        area_uuid=area.uuid,
        tag_uuids=["errand"],
    )
    trashed = Record(uuid="old", kind="task", title="Old", trashed=True)
    module = workspace([area, project, task, trashed])
    module._library.tags["errand"] = "Errand"  # noqa: SLF001

    result = module.read(ReadCall(view="audit", limit=40))

    assert result.status == "ok"
    assert [item.id for item in result.items] == [area.id, project.id, task.id]
    assert "has_notes" in result.items[2].signals
    assert result.items[2].direct_tag_ids == ["tag:errand"]


def test_diagnostics_view_exposes_inbox_hybrids() -> None:
    project = Record(uuid="launch", kind="project", title="Launch")
    hybrid = Record(
        uuid="stuck",
        kind="task",
        title="Stuck",
        inbox=True,
        parent_uuid=project.uuid,
    )
    clean = Record(uuid="ok", kind="task", title="Clean", parent_uuid=project.uuid)
    module = workspace([project, hybrid, clean])

    result = module.read(ReadCall(view="diagnostics"))

    assert result.status == "ok"
    assert [item.id for item in result.items] == [hybrid.id]
    assert "inbox_with_project" in result.items[0].signals


def test_diagnostics_treats_a_project_area_on_its_child_as_inherited() -> None:
    work = Record(uuid="work", kind="area", title="Work")
    private = Record(uuid="private", kind="area", title="Private")
    project = Record(
        uuid="launch",
        kind="project",
        title="Launch",
        area_uuid=work.uuid,
    )
    inherited = Record(
        uuid="ship",
        kind="task",
        title="Ship",
        parent_uuid=project.uuid,
        area_uuid=work.uuid,
    )
    conflicting = Record(
        uuid="misfiled",
        kind="task",
        title="Misfiled",
        parent_uuid=project.uuid,
        area_uuid=private.uuid,
    )
    module = workspace([work, private, project, inherited, conflicting])

    result = module.read(ReadCall(view="diagnostics"))

    assert [item.id for item in result.items] == [conflicting.id]
    assert "both_project_and_area" in result.items[0].signals
    assert result.diagnostics[0].repair_kind == "owner_choice"


def test_diagnostics_view_includes_completed_orphans_and_tag_conflicts() -> None:
    both = Record(
        uuid="both",
        kind="task",
        title="Both homes",
        parent_uuid="launch",
        area_uuid="home",
        status="done",
    )
    orphan = Record(
        uuid="orphan",
        kind="task",
        title="Orphan heading",
        heading_uuid="missing-heading",
        parent_uuid="launch",
    )
    library = MemoryLibrary(
        [
            Record(uuid="launch", kind="project", title="Launch"),
            both,
            orphan,
        ]
    )
    library.tags["child"] = "Child"
    library.tag_parents["child"] = ["missing"]
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)

    result = module.read(ReadCall(view="diagnostics"))

    assert result.status == "ok"
    by_id = {item.id: item.signals for item in result.items}
    assert "both_project_and_area" in by_id[both.id]
    assert "orphaned_heading" in by_id[orphan.id]
    assert any(
        row.id == "tag:child" and "dangling_tag_parent" in row.conflicts
        for row in result.diagnostics
    )
    assert result.truncated is False
    child = next(row for row in result.diagnostics if row.id == "tag:child")
    assert child.repair_kind == "clear_or_repair_tag_parent"
    assert any(repair.conflict == "dangling_tag_parent" for repair in child.repairs)


def test_tag_only_diagnostics_are_not_an_empty_state() -> None:
    library = MemoryLibrary()
    library.tags["child"] = "Child"
    library.tag_parents["child"] = ["missing"]
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)

    result = module.read(ReadCall(view="diagnostics"))

    assert result.status == "ok"
    assert result.items == []
    assert result.diagnostics[0].id == "tag:child"
    assert "dangling_tag_parent" in result.diagnostics[0].conflicts
    assert "No native-state conflicts" not in result.instruction
    assert "test_residue" not in result.instruction
    assert result.truncated is False


def test_diagnostics_instruction_names_residue_only_when_that_signal_is_on_the_page() -> None:
    loose = Record(
        uuid="loose",
        kind="task",
        title="Install /unslop Skill…",
        heading_uuid="gone",
    )
    leftover = Record(
        uuid="probe",
        kind="task",
        title="__TO_PROBE__ leftover",
    )
    only_heading = workspace([loose]).read(ReadCall(view="diagnostics"))
    assert only_heading.status == "ok"
    assert "heading_without_project" in only_heading.diagnostics[0].conflicts
    assert "test_residue" not in only_heading.instruction

    page = workspace([loose, leftover]).read(ReadCall(view="diagnostics"))
    assert page.status == "ok"
    assert any("test_residue" in row.conflicts for row in page.diagnostics)
    assert "Trash test_residue with this context and short refs." in page.instruction


def test_tag_diagnostics_page_beyond_the_first_forty() -> None:
    library = MemoryLibrary()
    for index in range(45):
        library.tags[f"t{index}"] = f"Tag {index}"
        library.tag_parents[f"t{index}"] = ["missing"]
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)

    first = module.read(ReadCall(view="diagnostics", limit=40))

    assert first.status == "ok"
    assert len(first.diagnostics) == 40
    assert first.truncated is True
    assert first.cursor is not None
    second = module.read(ReadCall(cursor=first.cursor, limit=40))
    assert second.status == "ok"
    assert len(second.diagnostics) == 5
    assert second.truncated is False
    library.tag_parents["t0"] = ["t0"]
    stale = module.read(ReadCall(cursor=first.cursor, limit=40))
    assert stale.status == "stale"


def test_bulk_ids_return_found_items_when_one_id_is_missing() -> None:
    first = Record(uuid="one", kind="task", title="One", notes="kept")
    module = workspace([first])

    result = module.read(ReadCall(ids=[first.id, "task:missing"]))

    assert result.status == "needs_input"
    assert result.next == "read"
    assert [item.id for item in result.items] == [first.id]
    assert result.items[0].notes_markdown == "kept"
    assert result.missing_ids == ["task:missing"]
    assert "task:missing" in result.instruction


def test_diagnostics_bounds_a_long_conflicting_tag_title() -> None:
    library = MemoryLibrary()
    library.tags["long"] = "T" * 1001
    library.tag_parents["long"] = ["missing"]
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)

    result = module.read(ReadCall(view="diagnostics"))

    assert result.status == "ok"
    assert result.diagnostics[0].id == "tag:long"
    assert len(result.diagnostics[0].title) == 1000
    assert "dangling_tag_parent" in result.diagnostics[0].conflicts
    assert result.diagnostics[0].repair_kind == "clear_or_repair_tag_parent"


def test_diagnostics_uses_untitled_for_a_blank_conflicting_tag() -> None:
    library = MemoryLibrary()
    library.tags["blank"] = "   "
    library.tag_parents["blank"] = ["missing"]
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)

    result = module.read(ReadCall(view="diagnostics"))

    assert result.status == "ok"
    assert result.diagnostics[0].title == "(untitled)"
    assert "dangling_tag_parent" in result.diagnostics[0].conflicts


def test_diagnostics_lists_every_repair_on_a_multi_conflict_item() -> None:
    project = Record(uuid="launch", kind="project", title="Launch")
    hybrid = Record(
        uuid="stuck",
        kind="task",
        title="Stuck",
        inbox=True,
        parent_uuid=project.uuid,
        start=NOW.date(),
    )
    module = workspace([project, hybrid])

    result = module.read(ReadCall(view="diagnostics"))

    row = next(item for item in result.diagnostics if item.id == hybrid.id)
    kinds = {repair.repair_kind for repair in row.repairs}
    assert row.repair_kind is None
    assert "repeat_placement" in kinds
    assert "clear_inbox_or_schedule" in kinds
    assert [repair.conflict for repair in row.repairs] == row.conflicts


def test_bulk_exact_read_truncates_checklist_text_in_the_shared_budget() -> None:
    first = Record(uuid="one", kind="task", title="One", notes="n" * 40_000)
    second = Record(
        uuid="two",
        kind="task",
        title="Two",
        checklists=[
            ChecklistLine(f"r{index}", "C" * 1000)
            for index in range(100)
        ],
    )
    module = workspace([first, second])

    result = module.read(ReadCall(ids=[first.id, second.id]))

    assert result.status == "ok"
    assert len(result.items[0].notes_markdown or "") == 1_000
    assert "notes" in result.items[0].truncated_fields
    assert "checklist_truncated" in result.items[1].signals
    assert "checklist" in result.items[1].truncated_fields
    assert len(result.items[1].checklist) == 99
    total = sum(
        len(item.notes_markdown or "")
        + sum(len(row.title) for row in item.checklist)
        for item in result.items
    )
    assert total == 100_000


def test_bulk_exact_read_truncates_notes_across_the_batch() -> None:
    first = Record(uuid="one", kind="task", title="One", notes="a" * 40_000)
    second = Record(uuid="two", kind="task", title="Two", notes="b" * 40_000)
    third = Record(uuid="three", kind="task", title="Three", notes="c" * 40_000)
    module = workspace([first, second, third])

    result = module.read(ReadCall(ids=[first.id, second.id, third.id]))

    assert result.status == "ok"
    assert len(result.items[0].notes_markdown or "") == 40_000
    assert len(result.items[1].notes_markdown or "") == 40_000
    assert "notes_truncated" in result.items[2].signals
    assert "notes" in result.items[2].truncated_fields
    assert len(result.items[2].notes_markdown or "") == 20_000
    total = sum(len(item.notes_markdown or "") for item in result.items)
    assert total == 100_000


def test_diagnostics_serializes_a_maximally_conflicted_task() -> None:
    heading = Record(
        uuid="elsewhere",
        kind="task",
        title="Heading",
        heading=True,
        parent_uuid="other-project",
    )
    other = Record(uuid="other-project", kind="project", title="Other")
    task = Record(
        uuid="max",
        kind="task",
        title="Max",
        inbox=True,
        parent_uuid="missing-project",
        area_uuid="missing-area",
        someday=True,
        tonight=True,
        start=NOW.date(),
        remind="25:99",
        heading_uuid="elsewhere",
        recurrence=RecurrenceState(
            role="instance",
            repeat_type="unknown",
            template_uuid="missing-template",
        ),
    )
    module = workspace([heading, other, task])

    result = module.read(ReadCall(view="diagnostics"))

    row = next(item for item in result.diagnostics if item.id == task.id)
    assert result.status == "ok"
    assert row.repair is None
    assert row.repair_kind is None
    assert len(row.conflicts) >= 12
    assert [repair.conflict for repair in row.repairs] == row.conflicts


def test_diagnostics_cursor_stales_when_a_title_changes() -> None:
    library = MemoryLibrary()
    library.tags["a"] = "First"
    library.tags["b"] = "Second"
    library.tag_parents["a"] = ["missing"]
    library.tag_parents["b"] = ["missing"]
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)

    first = module.read(ReadCall(view="diagnostics", limit=1))
    assert first.status == "ok"
    assert first.cursor is not None
    library.tags["b"] = "Renamed"
    stale = module.read(ReadCall(cursor=first.cursor, limit=1))

    assert stale.status == "stale"
    assert stale.next == "read"


def test_area_invalid_relations_recommend_clearing_the_relation() -> None:
    task = Record(uuid="loose", kind="task", title="Loose")
    project = Record(uuid="launch", kind="project", title="Launch")
    under_task = Record(
        uuid="area-under-task",
        kind="area",
        title="Under task",
        parent_uuid=task.uuid,
    )
    home_project = Record(
        uuid="area-home-project",
        kind="area",
        title="Home project",
        area_uuid=project.uuid,
    )
    module = workspace([task, project, under_task, home_project])

    result = module.read(ReadCall(view="diagnostics"))
    by_id = {row.id: row for row in result.diagnostics}

    assert "area_invalid_parent" in by_id[under_task.id].conflicts
    assert by_id[under_task.id].repair_kind == "clear_area_parent"
    assert by_id[under_task.id].repair == "clear the invalid Area parent"
    assert "area_invalid_home" in by_id[home_project.id].conflicts
    assert by_id[home_project.id].repair_kind == "clear_area_home"
    assert by_id[home_project.id].repair == "clear the invalid Area home"


def test_bulk_read_keeps_bounded_inherited_tags_when_already_truncated() -> None:
    area_tags = [f"area-tag-{index}" for index in range(41)]
    area = Record(uuid="home", kind="area", title="Home", tag_uuids=area_tags)
    task = Record(uuid="ship", kind="task", title="Ship", area_uuid=area.uuid)
    other = Record(uuid="other", kind="task", title="Other")
    library = MemoryLibrary([area, task, other])
    library.tags = {uuid: uuid for uuid in area_tags}
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)

    exact = module.read(ReadCall(id=task.id, limit=40))
    assert len(exact.items[0].inherited_tags) == 40
    assert "tags" in exact.items[0].truncated_fields
    assert "tags_truncated" in exact.items[0].signals

    bulk = module.read(ReadCall(ids=[task.id, other.id]))
    item = next(row for row in bulk.items if row.id == task.id)
    assert len(item.inherited_tag_ids) == 40
    assert item.inherited_tags == []
    assert "tags" in item.truncated_fields
    assert "tags_truncated" in item.signals


def test_bulk_truncation_fields_survive_a_full_signal_list() -> None:
    hog = Record(
        uuid="hog",
        kind="task",
        title="Hog",
        notes="n" * 50_000,
        checklists=[
            ChecklistLine(f"h{index}", "H" * 1000) for index in range(100)
        ],
    )
    heading = Record(
        uuid="elsewhere",
        kind="task",
        title="Heading",
        heading=True,
        parent_uuid="other-project",
    )
    other = Record(uuid="other-project", kind="project", title="Other")
    task = Record(
        uuid="max",
        kind="task",
        title="Max",
        inbox=True,
        parent_uuid="missing-project",
        area_uuid="missing-area",
        someday=True,
        tonight=True,
        start=NOW.date(),
        deadline=NOW.date().replace(day=14),
        remind="25:99",
        heading_uuid="elsewhere",
        notes="m" * 5_000,
        checklists=[
            ChecklistLine(f"r{index}", "C" * 300) for index in range(10)
        ],
        tag_uuids=[f"t{index}-{'w' * 80}" for index in range(10)],
        recurrence=RecurrenceState(
            role="instance",
            repeat_type="unknown",
            template_uuid="missing-template",
        ),
    )
    library = MemoryLibrary([hog, heading, other, task])
    library.tags = {uuid: "T" * 50 for uuid in task.tag_uuids}
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)

    result = module.read(ReadCall(ids=[hog.id, task.id]))
    item = next(row for row in result.items if row.id == task.id)

    assert len(item.signals) == 20
    assert set(item.truncated_fields) == {"notes", "checklist", "tags"}
    assert "notes_truncated" in item.signals
    assert "checklist_truncated" in item.signals
    assert "tags_truncated" in item.signals
    assert item.notes_markdown != "m" * 5_000
    assert len(item.checklist) < 10
    assert len(item.direct_tag_ids) < 10


def test_one_item_ids_read_uses_the_bulk_detail_budget() -> None:
    task = Record(
        uuid="one",
        kind="task",
        title="One",
        notes="n" * 50_000,
        checklists=[
            ChecklistLine(f"r{index}", "C" * 1000) for index in range(80)
        ],
    )
    module = workspace([task])

    result = module.read(ReadCall(ids=[task.id]))

    item = result.items[0]
    total = len(item.notes_markdown or "") + sum(
        len(row.title) for row in item.checklist
    )
    assert total == 100_000
    assert "notes" in item.truncated_fields
    assert "notes_truncated" in item.signals
    assert len(item.notes_markdown or "") == 20_000
    assert len(item.checklist) == 80


def test_bulk_read_hoists_shared_tag_parents_under_the_wire_budget() -> None:
    tag_uuids = [f"t{index:02d}" for index in range(40)]
    parent_uuids = [f"p{index:02d}-{slot}" for index in range(40) for slot in range(20)]
    tasks = [
        Record(
            uuid=f"item{index}",
            kind="task",
            title=f"Item {index}",
            tag_uuids=tag_uuids,
        )
        for index in range(10)
    ]
    library = MemoryLibrary(tasks)
    library.tags = {uuid: uuid for uuid in [*tag_uuids, *parent_uuids]}
    library.tag_parents = {
        uuid: [f"p{index:02d}-{slot}" for slot in range(20)]
        for index, uuid in enumerate(tag_uuids)
    }
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)

    result = module.read(ReadCall(ids=[task.id for task in tasks]))
    payload = dump_result(result)
    wire = len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    )

    assert result.status == "ok"
    assert wire <= 256_000
    assert {tag.id for tag in result.tags} == {f"tag:{uuid}" for uuid in tag_uuids}
    assert all(len(tag.parent_ids) == 20 for tag in result.tags)
    assert all(not item.direct_tags and not item.inherited_tags for item in result.items)
    assert all(len(item.direct_tag_ids) == 40 for item in result.items)
    assert all(not item.truncated_fields for item in result.items)


def test_bulk_note_reserve_is_shared_across_items() -> None:
    first = Record(
        uuid="one",
        kind="task",
        title="One",
        notes="a" * 50_000,
        checklists=[
            ChecklistLine(f"a{index}", "C" * 1000) for index in range(50)
        ],
    )
    second = Record(
        uuid="two",
        kind="task",
        title="Two",
        notes="b" * 50_000,
        checklists=[
            ChecklistLine(f"b{index}", "C" * 1000) for index in range(50)
        ],
    )
    module = workspace([first, second])

    result = module.read(ReadCall(ids=[first.id, second.id]))

    assert len(result.items[0].notes_markdown or "") >= 400
    assert len(result.items[1].notes_markdown or "") >= 400
    assert result.items[0].truncated_fields
    assert result.items[1].truncated_fields


def test_bulk_read_keeps_tag_membership_when_parents_are_huge() -> None:
    tag_uuids = [f"t{index:02d}" for index in range(40)]
    parent_uuids = [
        f"p{index:02d}-{slot}-" + "z" * 400
        for index in range(40)
        for slot in range(20)
    ]
    tasks = [
        Record(
            uuid=f"item{index}",
            kind="task",
            title=f"Item {index}",
            tag_uuids=tag_uuids,
        )
        for index in range(10)
    ]
    library = MemoryLibrary(tasks)
    library.tags = {uuid: uuid[:20] for uuid in [*tag_uuids, *parent_uuids]}
    library.tag_parents = {
        uuid: [f"p{index:02d}-{slot}-" + "z" * 400 for slot in range(20)]
        for index, uuid in enumerate(tag_uuids)
    }
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)

    result = module.read(ReadCall(ids=[task.id for task in tasks]))
    payload = dump_result(result)
    wire = len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    )

    assert result.status == "ok"
    assert wire <= 256_000
    assert len(result.tags) == 40
    assert all(not tag.parent_ids for tag in result.tags)
    assert all(tag.parents_truncated for tag in result.tags)
    assert all(len(item.direct_tag_ids) == 40 for item in result.items)
    assert all(not item.direct_tags for item in result.items)
    assert all("tags" not in item.truncated_fields for item in result.items)


def test_bulk_recurrence_links_do_not_exceed_the_wire_budget() -> None:
    templates = []
    instances = []
    for index in range(10):
        template_uuid = f"tmpl{index}"
        templates.append(
            Record(
                uuid=template_uuid,
                kind="task",
                title="é" * 1000,
                recurrence=RecurrenceState(role="template"),
            )
        )
        for slot in range(40):
            instances.append(
                Record(
                    uuid=f"i{index:02d}-{slot:02d}-" + "y" * 460,
                    kind="task",
                    title="copy",
                    recurrence=RecurrenceState(
                        role="instance", links=(template_uuid,)
                    ),
                )
            )
    module = workspace([*templates, *instances])

    result = module.read(ReadCall(ids=[item.id for item in templates]))
    payload = dump_result(result)
    wire = len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    )

    assert result.status == "ok"
    assert wire <= 256_000
    assert result.items
    if result.cursor is not None:
        assert result.truncated is True
        assert "wire_trimmed" in result.signals
    else:
        assert all(
            "recurrence" in item.truncated_fields
            or (
                item.recurrence is not None
                and len(item.recurrence.linked_item_ids) <= 40
            )
            for item in result.items
        )


def test_bulk_tag_registry_caps_unique_tags_without_crashing() -> None:
    area_tags = [f"area-{index:03d}" for index in range(41)]
    tasks = []
    all_tags = list(area_tags)
    area = Record(uuid="home", kind="area", title="Home", tag_uuids=area_tags)
    for index in range(10):
        direct = [f"d{index:02d}-{slot:02d}" for slot in range(40)]
        all_tags.extend(direct)
        tasks.append(
            Record(
                uuid=f"item{index}",
                kind="task",
                title=f"Item {index}",
                area_uuid=area.uuid,
                tag_uuids=direct,
            )
        )
    library = MemoryLibrary([area, *tasks])
    library.tags = {uuid: uuid for uuid in all_tags}
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)

    result = module.read(ReadCall(ids=[task.id for task in tasks]))

    assert result.status == "ok"
    assert len(result.tags) == 400
    assert any("tags" in item.truncated_fields for item in result.items)


def test_area_missing_relations_use_clear_repairs() -> None:
    area = Record(
        uuid="lost",
        kind="area",
        title="Lost",
        parent_uuid="gone-project",
        area_uuid="gone-area",
    )
    module = workspace([area])

    result = module.read(ReadCall(view="diagnostics"))
    row = next(item for item in result.diagnostics if item.id == area.id)
    kinds = {repair.repair_kind for repair in row.repairs}
    assert "area_missing_parent" in row.conflicts
    assert "area_missing_home" in row.conflicts
    assert "clear_area_parent" in kinds
    assert "clear_area_home" in kinds
    assert "rehome_item" not in kinds
    assert "rehome_or_clear_area" not in kinds
    assert row.repair_kind is None


def test_audit_can_filter_by_signal() -> None:
    later = Record(uuid="later", kind="task", title="Later", someday=True)
    inbox = Record(uuid="box", kind="task", title="Inbox", inbox=True)
    module = workspace([later, inbox])

    result = module.read(ReadCall(view="audit", signals_any=["someday"]))

    assert [item.id for item in result.items] == [later.id]


def test_all_missing_bulk_ids_name_every_missing_id() -> None:
    module = workspace()

    result = module.read(ReadCall(ids=["task:a", "task:b", "task:c"]))

    assert result.status == "needs_input"
    assert result.next == "read"
    assert result.items == []
    assert result.missing_ids == ["task:a", "task:b", "task:c"]
    assert "task:a" in result.instruction
    assert "task:b" in result.instruction
    assert "task:c" in result.instruction


def test_bulk_ids_return_full_exact_facts() -> None:
    first = Record(
        uuid="one",
        kind="task",
        title="One",
        notes="first note",
        checklists=[ChecklistLine("row", "Check")],
    )
    second = Record(uuid="two", kind="task", title="Two", notes="second note")
    module = workspace([first, second])

    result = module.read(ReadCall(ids=[first.id, second.id]))

    assert result.status == "ok"
    assert [item.id for item in result.items] == [first.id, second.id]
    assert result.items[0].notes_markdown == "first note"
    assert result.items[0].checklist[0].title == "Check"
    assert result.items[1].notes_markdown == "second note"


def test_bulk_ids_can_omit_unrequested_detail_fields() -> None:
    task = Record(
        uuid="one",
        kind="task",
        title="One",
        notes="secret note",
        checklists=[ChecklistLine("row", "Check")],
        tag_uuids=["focus"],
    )
    library = MemoryLibrary([task])
    library.tags["focus"] = "Focus"
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)

    result = module.read(ReadCall(ids=[task.id], fields=[]))

    assert result.status == "ok"
    item = result.items[0]
    assert item.notes_markdown is None
    assert item.checklist == []
    assert item.direct_tag_ids == []
    assert item.direct_tags == []
    assert result.tags == []
    assert item.recurrence is None


def test_bulk_empty_fields_survive_a_continuation_page() -> None:
    tasks = [
        Record(uuid=f"item{index}", kind="task", title=f"Item {index}", notes="n" * 800)
        for index in range(3)
    ]
    module = workspace(tasks)

    first = module.read(ReadCall(ids=[task.id for task in tasks], fields=[], limit=1))
    assert first.cursor is not None
    assert first.items[0].notes_markdown is None
    second = module.read(ReadCall(cursor=first.cursor, limit=1))

    assert second.status == "ok"
    assert second.items[0].notes_markdown is None
    assert second.items[0].checklist == []


def test_links_only_instance_resolves_repeat_type_after_apply() -> None:
    template = Record(
        uuid="tmpl",
        kind="task",
        title="Template",
        recurrence=RecurrenceState(
            role="template",
            repeat_type="fixed",
            rule={"tp": 0, "fu": 256, "fa": 1},
        ),
    )
    instance = Record(
        uuid="copy",
        kind="task",
        title="Copy",
        recurrence=RecurrenceState(role="instance", links=(template.uuid,)),
    )
    library = MemoryLibrary([template, instance])

    library.apply([])

    assert library.records["copy"].recurrence.repeat_type == "fixed"


def test_trash_view_serializes_untitled_and_malformed_records() -> None:
    untitled = Record(
        uuid="blank",
        kind="task",
        title="   ",
        trashed=True,
        remind="25:00",
        start=NOW.date(),
    )
    orphan = Record(
        uuid="orphan",
        kind="task",
        title="Orphan",
        trashed=True,
        heading_uuid="missing-heading",
        parent_uuid="missing-project",
    )
    module = workspace([untitled, orphan])

    result = module.read(ReadCall(view="trash"))

    assert result.status == "ok"
    assert [item.id for item in result.items] == [untitled.id, orphan.id]
    assert result.items[0].title == "(untitled)"
    assert result.items[0].remind_at is None
    assert "trashed" in result.items[0].signals
    assert "orphaned_heading" in result.items[1].signals


def test_instance_recurrence_fact_inherits_the_template_rule() -> None:
    template = Record(
        uuid="habit-template",
        kind="task",
        title="Water plants",
        recurrence=RecurrenceState(
            role="template",
            repeat_type="after_completion",
            rule={"tp": 1, "fu": 256, "fa": 2, "of": [{"wd": 1}]},
        ),
    )
    instance = Record(
        uuid="habit-copy",
        kind="task",
        title="Water plants",
        recurrence=RecurrenceState(
            role="instance",
            template_uuid=template.uuid,
            links=(template.uuid,),
        ),
    )
    module = workspace([template, instance])

    fact = detail(module, instance.id)
    assert fact.recurrence is not None
    assert fact.recurrence.kind == "after_completion_instance"
    assert fact.recurrence.mode == "after_completion"
    assert fact.recurrence.unit == "week"
    assert fact.recurrence.interval == 2
    assert fact.recurrence.weekdays == ["monday"]
    assert fact.recurrence.template_id == template.id


def test_change_find_can_bind_trashed_work() -> None:
    trashed = Record(uuid="old-invoice", kind="task", title="Pay invoice", trashed=True)
    module = workspace([trashed])

    result = module.read(ReadCall(purpose="change", find="invoice"))

    assert result.status == "ok"
    assert result.context is not None
    assert [item.id for item in result.items] == [trashed.id]


def test_paged_read_asks_for_the_cursor() -> None:
    records = [
        Record(uuid=f"page-{index}", kind="task", title=f"Task {index}", inbox=True)
        for index in range(50)
    ]
    module = workspace(records)

    result = module.read(ReadCall(view="inbox", limit=20))

    assert result.cursor is not None
    assert result.next == "read"
    assert "cursor" in result.instruction.casefold()


def test_empty_week_does_not_use_find_copy() -> None:
    module = workspace()

    result = module.read(ReadCall(view="week"))

    assert result.status == "ok"
    assert result.items == []
    assert "week" in result.instruction.casefold()
    assert "find" not in result.instruction.casefold()


def test_weekly_review_returns_exception_first_facts() -> None:
    healthy = Record(uuid="healthy", kind="project", title="Submit tax return")
    healthy_action = Record(
        uuid="healthy-action",
        kind="task",
        title="Download bank statement",
        parent_uuid=healthy.uuid,
    )
    gap = Record(uuid="gap", kind="project", title="Renew office contract")
    waiting_action = Record(
        uuid="waiting-action",
        kind="task",
        title="Receive landlord reply",
        parent_uuid=gap.uuid,
        tag_uuids=["waiting"],
    )
    vague = Record(uuid="vague", kind="project", title="Launch new site")
    vague_action = Record(
        uuid="vague-action",
        kind="task",
        title="Plan launch",
        parent_uuid=vague.uuid,
    )
    inherited_wait = Record(
        uuid="inherited-wait",
        kind="project",
        title="Receive contract",
        tag_uuids=["waiting"],
    )
    inherited_wait_action = Record(
        uuid="inherited-wait-action",
        kind="task",
        title="Read contract reply",
        parent_uuid=inherited_wait.uuid,
    )
    parked = Record(
        uuid="parked",
        kind="project",
        title="Create workshop",
        someday=True,
    )
    parked_active_task = Record(
        uuid="parked-active",
        kind="task",
        title="Draft workshop outline",
        parent_uuid=parked.uuid,
    )
    records = [
        healthy,
        healthy_action,
        gap,
        waiting_action,
        vague,
        vague_action,
        inherited_wait,
        inherited_wait_action,
        parked,
        parked_active_task,
        Record(uuid="inbox", kind="task", title="Book dentist", inbox=True),
        Record(
            uuid="stale",
            kind="task",
            title="Correct invoice",
            start=NOW.date() - timedelta(days=2),
        ),
        Record(
            uuid="today",
            kind="task",
            title="Submit report",
            start=NOW.date(),
        ),
        Record(
            uuid="upcoming",
            kind="task",
            title="Call accountant",
            start=NOW.date() + timedelta(days=3),
        ),
        Record(uuid="someday", kind="task", title="Learn pottery", someday=True),
        Record(
            uuid="finished-checklist",
            kind="task",
            title="Send application",
            checklists=[ChecklistLine("row", "Attach file", status="done")],
        ),
        Record(uuid="duplicate-1", kind="task", title="Review contract"),
        Record(uuid="duplicate-2", kind="task", title="Review contract"),
        Record(
            uuid="done-project",
            kind="project",
            title="Move from Cursor to Paper",
            status="done",
            completed_at=NOW - timedelta(days=5),
        ),
    ]
    module = workspace(records)
    module._library.tags["waiting"] = "Waiting"  # noqa: SLF001

    result = module.read(ReadCall(view="weekly_review", limit=40))

    assert result.status == "ok"
    assert result.context is None
    assert [section.key for section in result.sections] == [
        "get_clear",
        "get_current",
        "get_creative",
        "plan_week",
    ]
    by_id = {item.id: item for item in result.items}
    assert "project:healthy" not in by_id
    assert "task:healthy-action" not in by_id
    assert "project:gap" in by_id
    assert "project_without_candidate_task" in by_id["project:gap"].signals
    assert "project_without_candidate_task" in by_id["project:vague"].signals
    assert "project_without_candidate_task" in by_id["project:inherited-wait"].signals
    assert "waiting" in by_id["task:inherited-wait-action"].signals
    assert "stale_start" in by_id["task:stale"].signals
    assert "upcoming" in by_id["task:upcoming"].signals
    assert "task:someday" not in by_id
    assert "possible_duplicate" in by_id["task:duplicate-1"].signals
    assert "active_task_in_someday_project" in by_id["task:parked-active"].signals
    assert "open_task_with_finished_checklist" in by_id["task:finished-checklist"].signals
    assert "has_checklist" in by_id["task:finished-checklist"].signals
    assert "project:done-project" not in by_id
    current = next(section for section in result.sections if section.key == "get_current")
    assert "project:healthy" not in current.item_ids
    assert any("Active Projects: 4" in signal for signal in current.signals)
    assert any(
        "recently completed Projects available on request: 1" in signal
        for signal in current.signals
    )
    assert result.signals == [
        "capture_check_required",
        "calendar_scan_required",
        "weekly_planning_optional",
    ]
    plan = next(section for section in result.sections if section.key == "plan_week")
    assert plan.item_ids == []
    assert any(NOW.date().isoformat() in signal for signal in plan.signals)
    assert "real begin day" in result.instruction

    planning = module.read(
        ReadCall(view="weekly_review", category="weekly_candidate", limit=40)
    )
    planning_ids = {item.id for item in planning.items}
    assert "task:healthy-action" in planning_ids
    assert "project:healthy" not in planning_ids
    assert "task:vague-action" not in planning_ids
    assert "exact IDs and current revisions" in planning.instruction
    assert "send those exact IDs" in planning.instruction
    assert "Open one named category" not in planning.instruction

    recent = module.read(
        ReadCall(
            view="weekly_review",
            category="recently_completed_project",
            limit=40,
        )
    )
    assert [item.id for item in recent.items] == ["project:done-project"]
    assert recent.context is None

    project_review = module.read(
        ReadCall(view="weekly_review", category="project_review", limit=40)
    )
    review_ids = {item.id for item in project_review.items}
    assert review_ids == {
        "task:healthy-action",
        "task:waiting-action",
        "task:vague-action",
        "task:inherited-wait-action",
    }


def test_weekly_review_category_pages_keep_exact_revisions() -> None:
    records = [
        Record(uuid=f"later-{index}", kind="task", title=f"Later {index}", someday=True)
        for index in range(45)
    ]
    module = workspace(records)

    page = module.read(
        ReadCall(view="weekly_review", category="someday", limit=10)
    )
    assert page.context is None
    seen = len(page.items)
    assert all(item.revision is not None for item in page.items)
    while page.cursor is not None:
        page = module.read(
            ReadCall(view="weekly_review", cursor=page.cursor, limit=10)
        )
        assert page.context is None
        assert all(item.revision is not None for item in page.items)
        seen += len(page.items)

    assert seen == 45


def test_weekly_review_default_is_bounded_and_summarized() -> None:
    records = [
        Record(uuid=f"gap-{index}", kind="project", title=f"Finish result {index}")
        for index in range(80)
    ]
    module = workspace(records)

    result = module.read(ReadCall(view="weekly_review", limit=40))

    assert len(result.items) == 40
    assert result.cursor is None
    assert result.context is None
    assert "weekly_review_summarized" in result.signals
    assert "do not repeat the default read" in result.instruction
    returned_ids = {item.id for item in result.items}
    assert all(
        item_id in returned_ids
        for section in result.sections
        for item_id in section.item_ids
    )


def test_weekly_review_default_balances_exception_categories() -> None:
    records = [
        Record(
            uuid=f"stale-{index}",
            kind="task",
            title=f"Stale {index}",
            start=NOW.date() - timedelta(days=1),
        )
        for index in range(60)
    ]
    records.append(Record(uuid="gap", kind="project", title="Ship release"))
    module = workspace(records)

    result = module.read(ReadCall(view="weekly_review", limit=40))

    assert len(result.items) == 40
    assert "project:gap" in {item.id for item in result.items}


def test_weekly_review_filtered_cursor_is_bounded_and_membership_safe() -> None:
    records = [
        Record(uuid=f"later-{index}", kind="task", title=f"Later {index}", someday=True)
        for index in range(125)
    ]
    module = workspace(records)

    first = module.read(
        ReadCall(view="weekly_review", category="someday", limit=40)
    )
    assert first.cursor is not None
    module._library.records["later-124"].title = "Changed while paging"  # noqa: SLF001

    stale = module.read(ReadCall(view="weekly_review", cursor=first.cursor, limit=40))

    assert stale.status == "stale"


def test_weekly_review_filtered_category_returns_every_row() -> None:
    records = [
        Record(uuid=f"later-{index}", kind="task", title=f"Later {index}", someday=True)
        for index in range(125)
    ]
    module = workspace(records)

    page = module.read(
        ReadCall(view="weekly_review", category="someday", limit=40)
    )
    seen = len(page.items)
    assert page.context is None
    while page.cursor is not None:
        page = module.read(
            ReadCall(view="weekly_review", cursor=page.cursor, limit=40)
        )
        seen += len(page.items)
        assert page.context is None

    assert seen == 125


def test_weekly_review_project_category_returns_all_projects() -> None:
    records: list[Record] = []
    for index in range(125):
        project = Record(uuid=f"project-{index}", kind="project", title=f"Result {index}")
        records.extend(
            [
                project,
                Record(
                    uuid=f"action-{index}",
                    kind="task",
                    title=f"Write result {index}",
                    parent_uuid=project.uuid,
                ),
            ]
        )
    module = workspace(records)

    page = module.read(
        ReadCall(view="weekly_review", category="project_review", limit=40)
    )
    seen = len(page.items)
    assert [section.key for section in page.sections] == ["get_current"]
    while page.cursor is not None:
        page = module.read(
            ReadCall(view="weekly_review", cursor=page.cursor, limit=40)
        )
        seen += len(page.items)

    assert seen == 125


def test_weekly_review_uses_first_task_in_native_heading_order() -> None:
    project = Record(uuid="project", kind="project", title="Ship product")
    later_heading = Record(
        uuid="later-heading",
        kind="task",
        title="Later",
        heading=True,
        parent_uuid=project.uuid,
        sort_index=2048,
    )
    first_heading = Record(
        uuid="first-heading",
        kind="task",
        title="First",
        heading=True,
        parent_uuid=project.uuid,
        sort_index=1024,
    )
    records = [
        project,
        later_heading,
        first_heading,
        Record(
            uuid="later-task",
            kind="task",
            title="Send release notes",
            parent_uuid=project.uuid,
            heading_uuid=later_heading.uuid,
            sort_index=0,
        ),
        Record(
            uuid="first-task",
            kind="task",
            title="Plan launch",
            parent_uuid=project.uuid,
            heading_uuid=first_heading.uuid,
            sort_index=9999,
        ),
    ]
    module = workspace(records)

    default = module.read(ReadCall(view="weekly_review", limit=40))
    review = module.read(
        ReadCall(view="weekly_review", category="project_review", limit=40)
    )
    planning = module.read(
        ReadCall(view="weekly_review", category="weekly_candidate", limit=40)
    )

    assert "project:project" in {item.id for item in default.items}
    assert [item.id for item in review.items] == ["task:first-task"]
    assert planning.items == []


def test_weekly_review_does_not_skip_a_waiting_first_task() -> None:
    project = Record(uuid="project", kind="project", title="Sign contract")
    records = [
        project,
        Record(
            uuid="waiting-first",
            kind="task",
            title="Receive legal reply",
            parent_uuid=project.uuid,
            tag_uuids=["waiting"],
            sort_index=0,
        ),
        Record(
            uuid="later-action",
            kind="task",
            title="Sign contract PDF",
            parent_uuid=project.uuid,
            sort_index=1024,
        ),
    ]
    module = workspace(records)
    module._library.tags["waiting"] = "Waiting"  # noqa: SLF001

    review = module.read(
        ReadCall(view="weekly_review", category="project_review", limit=40)
    )
    planning = module.read(
        ReadCall(view="weekly_review", category="weekly_candidate", limit=40)
    )

    assert [item.id for item in review.items] == ["task:waiting-first"]
    assert planning.items == []


def test_weekly_review_cursor_depends_on_date_and_tag_catalog() -> None:
    now = [NOW]
    records = [
        Record(
            uuid=f"wait-{index}",
            kind="task",
            title=f"Wait {index}",
            tag_uuids=["waiting"],
        )
        for index in range(45)
    ]
    library = MemoryLibrary(records)
    library.tags["waiting"] = "Waiting"
    module = ThingsWorkspace(
        library,
        journal=MemoryJournal(),
        clock=lambda: now[0],
    )

    dated = module.read(
        ReadCall(view="weekly_review", category="waiting", limit=20)
    )
    assert dated.cursor is not None
    now[0] = NOW + timedelta(days=1)
    assert module.read(
        ReadCall(view="weekly_review", cursor=dated.cursor, limit=20)
    ).status == "stale"

    now[0] = NOW
    tagged = module.read(
        ReadCall(view="weekly_review", category="waiting", limit=20)
    )
    assert tagged.cursor is not None
    library.tags["waiting"] = "Delegated"
    assert module.read(
        ReadCall(view="weekly_review", cursor=tagged.cursor, limit=20)
    ).status == "stale"


def test_weekly_planning_excludes_tonight_without_a_start_date() -> None:
    module = workspace(
        [Record(uuid="tonight", kind="task", title="Send note", tonight=True)]
    )

    result = module.read(
        ReadCall(view="weekly_review", category="weekly_candidate", limit=40)
    )

    assert result.items == []


def test_find_within_trash_ignores_living_notes_hits() -> None:
    living = Record(
        uuid="outlook",
        kind="task",
        title="Outlook pilot",
        notes="later enterprise provisioning",
    )
    heading = Record(
        uuid="later-head",
        kind="task",
        title="Later",
        heading=True,
        parent_uuid="gone",
        trashed=True,
    )
    module = workspace([living, heading])

    living_hit = module.read(ReadCall(find="Later"))
    assert living.id in {item.id for item in living_hit.items}

    trash_hit = module.read(ReadCall(find="Later", within="trash"))
    assert [item.id for item in trash_hit.items] == [heading.id]
    assert "trashed" in trash_hit.items[0].signals
    assert "Read one to restore or purge." in trash_hit.instruction
    assert "purpose=change" not in trash_hit.instruction


def test_compact_today_names_homes_and_omits_inert_defaults() -> None:
    home = Record(uuid="kitchen", kind="project", title="Kitchen")
    task = Record(
        uuid="milk",
        kind="task",
        title="Buy milk",
        parent_uuid=home.uuid,
        start=NOW.date(),
    )
    module = workspace([home, task])

    result = module.read(ReadCall(view="today"))
    payload = dump_result(result)

    assert result.status == "ok"
    assert "into_title" in result.instruction
    assert result.items[0].into_title == "Kitchen"
    assert result.items[0].order is None
    assert result.items[0].recurrence is None
    dumped = payload["items"][0]
    assert dumped["into_title"] == "Kitchen"
    assert "order" not in dumped
    assert "recurrence" not in dumped
    assert result.sections[0].item_ids == []
    assert "item_ids" not in payload["sections"][0]


def test_exact_id_keeps_order_and_omits_none_recurrence() -> None:
    task = Record(uuid="desk", kind="task", title="Clear desk", sort_index=2048)
    module = workspace([task])

    item = detail(module, task.id)
    payload = dump_result(module.read(ReadCall(ids=[task.id])))

    assert item.order == 2048
    assert item.recurrence is None
    assert payload["items"][0]["order"] == 2048
    assert "recurrence" not in payload["items"][0]


def test_tags_page_instruction_is_the_catalog() -> None:
    library = MemoryLibrary()
    library.tags["errands"] = "Errands"
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)

    result = module.read(ReadCall(view="tags"))

    assert result.status == "ok"
    assert "catalog" in result.instruction
    assert "tag_ids" in result.instruction
    assert "change_tags" in result.instruction


def test_system_review_copy_names_area_scope() -> None:
    area = Record(uuid="home", kind="area", title="Home")
    project = Record(
        uuid="kitchen",
        kind="project",
        title="Kitchen",
        area_uuid=area.uuid,
    )
    module = workspace([area, project])

    result = module.read(ReadCall(view="system"))

    assert result.context is None
    assert "scope_revision" in result.instruction
    assert "Area" in result.instruction
    kitchen = next(item for item in result.items if item.id == project.id)
    assert kitchen.into_title == "Home"
