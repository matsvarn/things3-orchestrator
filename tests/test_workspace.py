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


def test_empty_read_returns_bounded_today() -> None:
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
    assert [item.id for item in result.items] == ["task:late", "task:tonight"]
    inbox = module.read(ReadCall(view="inbox"))
    assert [item.id for item in inbox.items] == ["task:box"]
    assert result.scope_revision and result.scope_revision.startswith("s_")


def test_library_get_matches_exact_id_or_uuid_only() -> None:
    library = MemoryLibrary([Record(uuid="abcdef", kind="task", title="A")])
    assert library.get("task:abcdef") is not None
    assert library.get("abcdef") is not None
    assert library.get("abc") is None


def test_inbox_and_week_return_all_matching_records() -> None:
    today = NOW.date()
    records = [
        Record(uuid=f"in{i}", kind="task", title=str(i), inbox=True)
        for i in range(16)
    ]
    records.extend(
        Record(
            uuid=f"wk{i}",
            kind="task",
            title=f"W{i}",
            start=today + timedelta(days=1),
        )
        for i in range(16)
    )
    library = MemoryLibrary(records)
    assert len(library.inbox()) == 16
    assert len(library.week(today=today)) == 16


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


def test_find_ignores_articles_for_one_unique_title_match() -> None:
    module = workspace([Record(uuid="plants", kind="task", title="Water plants")])

    result = module.read(ReadCall(find="water the plants"))

    assert result.status == "ok"
    assert result.items[0].id == "task:plants"


def test_find_keeps_article_fallback_ambiguous() -> None:
    module = workspace(
        [
            Record(uuid="one", kind="task", title="Water plants"),
            Record(uuid="two", kind="task", title="Water the plants"),
        ]
    )

    result = module.read(ReadCall(find="water the plants"))

    assert result.status == "ok"
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
def test_find_article_fallback_matches_notes_and_checklists(
    record: Record, query: str
) -> None:
    result = workspace([record]).read(ReadCall(find=query))

    assert result.status == "ok"
    assert result.items[0].id == f"task:{record.uuid}"


def test_find_does_not_stem_or_fuzz_token_fallback() -> None:
    module = workspace([Record(uuid="plants", kind="task", title="Water plants")])

    result = module.read(ReadCall(find="water planting"))

    assert result.status == "ok"
    assert result.items == []
    assert "no match" in result.instruction.casefold()


def test_find_includes_active_headings_for_rename() -> None:
    heading = Record(
        uuid="prep", kind="task", title="Prep", heading=True, parent_uuid="project"
    )
    module = workspace([heading])

    review = module.read(ReadCall(find="Prep"))

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
    links_only = detail(module, via_links.id)
    uuid_only = detail(module, via_uuid.id)
    assert links_only.recurrence is not None
    assert uuid_only.recurrence is not None
    assert links_only.recurrence.template_id == template.id


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
        ReadCall.model_validate({"cursor": first.cursor, "view": "today"})
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


def test_area_registry_pages_keep_one_scope_revision() -> None:
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

    result = module.read_v2_registry(kind="area", limit=10)
    revision = result.scope_revision
    ids: list[str] = []
    while True:
        ids.extend(item.id for item in result.items)
        assert result.scope_revision == revision
        assert all(item.kind == "area" for item in result.items)
        if result.cursor is None:
            break
        result = module.read(ReadCall(cursor=result.cursor, limit=10))

    assert ids == [f"area:area-{index}" for index in range(12)]


def test_project_registry_pages_only_projects() -> None:
    records = [
        Record(uuid=f"project-{index:02d}", kind="project", title=f"Project {index:02d}")
        for index in range(13)
    ] + [
        Record(uuid=f"task-{index:02d}", kind="task", title=f"Task {index:02d}")
        for index in range(5)
    ]
    module = workspace(records)

    page = module.read_v2_registry(kind="project", limit=10)
    seen: list[str] = []
    while True:
        assert page.status == "ok"
        assert page.truncated == (page.cursor is not None)
        assert all(item.kind == "project" for item in page.items)
        seen.extend(item.id for item in page.items)
        if page.cursor is None:
            break
        page = module.read(ReadCall(cursor=page.cursor, limit=10))
    assert seen == [f"project:project-{index:02d}" for index in range(13)]


def test_project_registry_cursor_stales_after_area_registry_changes() -> None:
    records = [
        Record(uuid=f"project-{index:02d}", kind="project", title=f"Project {index:02d}")
        for index in range(13)
    ]
    module = workspace(records)

    first = module.read_v2_registry(kind="project", limit=10)
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


def test_project_registry_cursor_stales_after_a_project_is_added() -> None:
    records = [
        Record(uuid=f"project-{index:02d}", kind="project", title=f"Project {index:02d}")
        for index in range(13)
    ]
    module = workspace(records)

    first = module.read_v2_registry(kind="project", limit=10)
    assert first.cursor is not None
    module._library.records["new-project"] = Record(  # noqa: SLF001
        uuid="new-project",
        kind="project",
        title="New Project",
    )

    continued = module.read(ReadCall(cursor=first.cursor, limit=10))

    assert continued.status == "stale"
    assert continued.next == "read"
    assert continued.items == []


def test_registry_cursor_continues_without_repeating_view() -> None:
    records = [
        Record(uuid=f"project-{index:02d}", kind="project", title=f"Project {index:02d}")
        for index in range(13)
    ]
    module = workspace(records)
    first = module.read_v2_registry(kind="project", limit=10)
    assert first.cursor is not None

    continued = module.read(ReadCall(cursor=first.cursor, limit=10))

    assert continued.status == "ok"
    assert continued.cursor is None
    assert [item.id for item in continued.items] == [
        f"project:project-{index:02d}" for index in range(10, 13)
    ]


def test_registry_cursor_rejects_a_named_list_view() -> None:
    records = [
        Record(uuid=f"project-{index:02d}", kind="project", title=f"Project {index:02d}")
        for index in range(13)
    ]
    module = workspace(records)
    first = module.read_v2_registry(kind="project", limit=10)
    assert first.cursor is not None

    continued = module.read(
        ReadCall.model_validate(
            {"cursor": first.cursor, "view": "today", "limit": 10}
        )
    )

    assert continued.status == "needs_input"
    assert continued.next == "ask"
    assert continued.items == []


def test_exact_container_id_returns_the_item_not_membership() -> None:
    area = Record(uuid="home", kind="area", title="Home")
    project = Record(
        uuid="kitchen",
        kind="project",
        title="Kitchen",
        area_uuid=area.uuid,
    )
    nested = Record(
        uuid="tap",
        kind="task",
        title="Replace tap",
        parent_uuid=project.uuid,
    )
    module = workspace([area, project, nested])

    by_id = module.read(ReadCall(id=area.id))
    assert [item.id for item in by_id.items] == [area.id]

    project_read = module.read(ReadCall(id=project.id))
    assert [item.id for item in project_read.items] == [project.id]


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


def test_bulk_ids_accept_more_than_the_old_ten_id_chunk() -> None:
    tasks = [
        Record(uuid=f"item{index:02d}", kind="task", title=f"Item {index:02d}")
        for index in range(11)
    ]
    result = workspace(tasks).read(ReadCall(ids=[task.id for task in tasks]))

    assert result.status == "ok"
    assert [item.id for item in result.items] == [task.id for task in tasks]


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


def test_cyclic_project_graph_raises_value_error() -> None:
    module = workspace(
        [
            Record(
                uuid="project",
                kind="project",
                title="Loop",
                parent_uuid="child",
            ),
            Record(uuid="child", kind="task", title="Child", parent_uuid="project"),
        ]
    )

    with pytest.raises(ValueError, match="cycle"):
        module._project_descendants("project")
