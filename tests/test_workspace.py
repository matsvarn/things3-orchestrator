from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from things_orchestrator.cloud import CloudError
from things_orchestrator.interface import ApproveCall, CommitCall, ReadCall
from things_orchestrator.journal import MemoryJournal
from things_orchestrator.library import (
    ApplyResult,
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
    result = module.read(ReadCall(id=item_id))
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
    assert [section.item_ids for section in result.sections] == [
        ["task:late"],
        ["task:tonight"],
    ]
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

    item = detail(module, "task:task1")

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

    changed = module.commit(
        CommitCall(
            intent_id="change-find-unique-001",
            context_id=result.context.id,
            change=[{"ref": result.items[0].ref, "title": "Pay invoice today"}],
        )
    )
    assert changed.status == "applied"
    assert module.read(ReadCall(id="task:invoice")).items[0].title == (
        "Pay invoice today"
    )


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
    assert "matches 2 active items" in ambiguous.instruction
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
    assert "found no active item" in result.instruction


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


def test_review_find_excludes_completed_trashed_and_repeat_templates() -> None:
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
    assert review.items == []

    change = workspace(records).read(ReadCall(purpose="change", find="invoice"))
    assert change.status == "needs_input"
    assert change.context is None

    restore = workspace(records).read(ReadCall(purpose="change", find="Prep trash"))
    assert restore.status == "ok"
    assert restore.context is not None
    assert [item.id for item in restore.items if item.id == "task:trash"] == [
        "task:trash"
    ]


def test_context_change_accepts_matching_redundant_identity_but_rejects_mismatch() -> None:
    task = Record(uuid="invoice", kind="task", title="Pay invoice")
    module = workspace([task])
    exact = detail(module, task.id)
    contextual = module.read(ReadCall(purpose="change", find="invoice"))
    assert contextual.context is not None
    ref = contextual.items[0].ref
    assert ref is not None
    assert exact.revision is not None

    matching = module.commit(
        CommitCall(
            intent_id="change-find-matching-001",
            context_id=contextual.context.id,
            change=[
                {
                    "ref": ref,
                    "id": task.id,
                    "if_revision": exact.revision,
                    "title": "Pay invoice now",
                }
            ],
        )
    )
    assert matching.status == "applied"

    fresh = module.read(ReadCall(purpose="change", find="invoice"))
    assert fresh.context is not None
    fresh_ref = fresh.items[0].ref
    assert fresh_ref is not None
    mismatch = module.commit(
        CommitCall(
            intent_id="change-find-mismatch-001",
            context_id=fresh.context.id,
            change=[
                {
                    "ref": fresh_ref,
                    "id": task.id,
                    "if_revision": "r_mismatch",
                    "title": "Must not apply",
                }
            ],
        )
    )
    assert mismatch.status == "needs_input"
    assert "Remove if_revision" in mismatch.instruction
    assert module.read(ReadCall(id=task.id)).items[0].title == "Pay invoice now"


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


def test_after_can_follow_a_sibling_moved_into_the_same_new_project() -> None:
    home = Record(uuid="home", kind="area", title="Home")
    first = Record(uuid="kitchen-a", kind="task", title="Remove old tap", inbox=True)
    second = Record(uuid="kitchen-b", kind="task", title="Measure sink", inbox=True)
    module = workspace([home, first, second])
    first_rev = detail(module, first.id).revision
    second_rev = detail(module, second.id).revision

    planned = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "kitchen-renovation-group",
                "create": [
                    {
                        "key": "$kitchen",
                        "kind": "project",
                        "title": "Kitchen renovation",
                        "into": "area:home",
                    }
                ],
                "change": [
                    {
                        "id": first.id,
                        "if_revision": first_rev,
                        "into": "$kitchen",
                    },
                    {
                        "id": second.id,
                        "if_revision": second_rev,
                        "into": "$kitchen",
                        "after": first.id,
                    },
                ],
            }
        )
    )
    if planned.status == "needs_approval":
        assert planned.plan is not None
        planned = module.approve(ApproveCall(plan_id=planned.plan.id))
    assert planned.status == "applied"
    project = next(
        item
        for item in module._library.records.values()  # noqa: SLF001
        if item.kind == "project" and item.title == "Kitchen renovation"
    )
    assert first.parent_uuid == project.uuid
    assert second.parent_uuid == project.uuid
    assert first.sort_index < second.sort_index


def test_creating_a_duplicate_open_task_title_asks_instead() -> None:
    existing = Record(uuid="medicine", kind="task", title="Take medicine")
    module = workspace([existing])

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "remind-medicine-tomorrow",
                "create": [{"title": "Take medicine", "start": "2026-08-18"}],
            }
        )
    )

    assert result.status == "needs_input"
    assert result.next == "ask"
    assert "already exists" in result.instruction


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
    second = module.read(ReadCall(cursor=first.cursor)) if first.cursor else None

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


def test_ensure_tag_reuses_existing_identity_and_can_assign_it() -> None:
    library = MemoryLibrary()
    library.tags["existing"] = "Focus"
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "reuse-tag-001",
                "ensure_tags": [{"key": "$focus", "title": "focus"}],
                "create": [{"title": "Draft proposal", "tag_ids": ["$focus"]}],
            }
        )
    )

    task = next(iter(library.records.values()))
    assert result.status == "applied"
    assert [(tag.id, tag.title) for tag in result.tags] == [("tag:existing", "Focus")]
    assert task.tag_uuids == ["existing"]
    assert library.tags == {"existing": "Focus"}


def test_ensure_tag_stops_on_an_ambiguous_existing_title() -> None:
    library = MemoryLibrary()
    library.tags = {"first": "Focus", "second": "focus"}
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "ambiguous-tag-001",
                "ensure_tags": [{"key": "$focus", "title": "Focus"}],
            }
        )
    )

    assert result.status == "needs_input"
    assert result.next == "ask"
    assert library.tags == {"first": "Focus", "second": "focus"}


def test_ensure_tag_and_item_assignment_are_one_atomic_apply() -> None:
    class RecordingLibrary(MemoryLibrary):
        batches: list[list[str]]

        def __init__(self) -> None:
            super().__init__()
            self.batches = []

        def apply(self, writes):  # type: ignore[no-untyped-def]
            self.batches.append([write.action for write in writes])
            return super().apply(writes)

    library = RecordingLibrary()
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "atomic-tag-001",
                "ensure_tags": [{"key": "$focus", "title": "Focus"}],
                "create": [{"title": "Draft proposal", "tag_ids": ["$focus"]}],
            }
        )
    )

    assert result.status == "applied"
    assert library.batches == [["ensure_tag", "create"]]
    assert result.tags[0].id.startswith("tag:")
    task = next(iter(library.records.values()))
    assert library.tags[task.tag_uuids[0]] == "Focus"


def test_ensure_tag_can_be_added_to_an_existing_item_in_one_commit() -> None:
    task = Record(uuid="task", kind="task", title="Draft")
    module = workspace([task])
    current = detail(module, task.id)

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "change-local-tag-001",
                "ensure_tags": [{"key": "$focus", "title": "Focus"}],
                "change": [
                    {
                        "id": task.id,
                        "if_revision": current.revision,
                        "tags_add": ["$focus"],
                    }
                ],
            }
        )
    )

    assert result.status == "applied"
    assert result.tags[0].title == "Focus"
    assert module._library.tags[task.tag_uuids[0]] == "Focus"  # noqa: SLF001


def test_local_tag_add_cannot_alias_an_exact_removal() -> None:
    task = Record(uuid="task", kind="task", title="Draft", tag_uuids=["focus"])
    library = MemoryLibrary([task])
    library.tags["focus"] = "Focus"
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)
    current = detail(module, task.id)

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "tag-alias-conflict-001",
                "ensure_tags": [{"key": "$focus", "title": "focus"}],
                "change": [
                    {
                        "id": task.id,
                        "if_revision": current.revision,
                        "tags_add": ["$focus"],
                        "tags_remove": ["tag:focus"],
                    }
                ],
            }
        )
    )

    assert result.status == "rejected"
    assert library.tags == {"focus": "Focus"}
    assert task.tag_uuids == ["focus"]


def test_missing_tag_readback_returns_pending() -> None:
    class MissingReadback(MemoryLibrary):
        def apply(self, writes):  # type: ignore[no-untyped-def]
            return ApplyResult(verified=[], created={})

    library = MissingReadback()
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "tag-readback-001",
                "ensure_tags": [{"key": "$focus", "title": "Focus"}],
            }
        )
    )

    assert result.status == "pending"
    assert result.next == "retry_same"
    assert result.tags == []


def test_existing_ensure_tag_is_unchanged_and_returns_the_exact_fact() -> None:
    library = MemoryLibrary()
    library.tags["focus"] = "Focus"
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)
    call = CommitCall.model_validate(
        {
            "intent_id": "unchanged-tag-001",
            "ensure_tags": [{"key": "$focus", "title": "focus"}],
        }
    )

    first = module.commit(call)
    repeated = module.commit(call)

    assert first.status == "unchanged"
    assert repeated == first
    assert [(tag.id, tag.title) for tag in first.tags] == [("tag:focus", "Focus")]


def test_tag_retry_settles_without_a_second_apply() -> None:
    class AppliedThenTimedOut(MemoryLibrary):
        attempts = 0

        def apply(self, writes):  # type: ignore[no-untyped-def]
            self.attempts += 1
            result = super().apply(writes)
            if self.attempts == 1:
                raise CloudError("commit timed out; outcome unknown")
            return result

    library = AppliedThenTimedOut()
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)
    call = CommitCall.model_validate(
        {
            "intent_id": "retry-tag-001",
            "ensure_tags": [{"key": "$focus", "title": "Focus"}],
        }
    )

    pending = module.commit(call)
    settled = module.commit(call)

    assert pending.status == "pending"
    assert settled.status == "applied"
    assert settled.tags[0].title == "Focus"
    assert library.attempts == 1


def test_commit_creates_structured_project_in_one_transaction() -> None:
    module = workspace()
    call = CommitCall.model_validate(
        {
            "intent_id": "launch-001",
            "create": [
                {"key": "$project", "kind": "project", "title": "Launch"},
                {
                    "kind": "task",
                    "title": "Verify build",
                    "notes_markdown": "Use **release** checks.",
                    "checklist": ["Run tests", "Check package"],
                    "into": "$project",
                    "waiting": True,
                },
            ],
        }
    )

    result = module.commit(call)

    assert result.status == "applied"
    project = next(
        item for item in module._library.records.values() if item.kind == "project"
    )  # noqa: SLF001
    task = next(
        item for item in module._library.records.values() if item.kind == "task"
    )  # noqa: SLF001
    assert project.inbox is False
    assert task.parent_uuid == project.uuid
    assert task.notes == "Use **release** checks."
    assert [row.title for row in task.checklists] == ["Run tests", "Check package"]
    assert module._library.tags[task.tag_uuids[0]] == "Waiting"  # noqa: SLF001


def test_project_next_actions_use_the_compact_create_shape() -> None:
    module = workspace()

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "project-compact-001",
                "create": [
                    {
                        "kind": "project",
                        "title": "Move house",
                        "next_actions": ["Call mover", "Book van"],
                    }
                ],
            }
        )
    )

    assert result.status == "applied"
    project = next(
        item for item in module._library.records.values() if item.kind == "project"
    )  # noqa: SLF001
    actions = [item for item in module._library.records.values() if item.kind == "task"]  # noqa: SLF001
    assert [item.title for item in actions] == ["Call mover", "Book van"]
    assert all(item.parent_uuid == project.uuid for item in actions)


def test_all_scheduled_create_forms_settle() -> None:
    module = workspace()

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "scheduled-create-001",
                "create": [
                    {"title": "Today", "start": "today"},
                    {"title": "Future", "start": "2026-08-18"},
                    {"title": "Evening", "start": "evening"},
                    {"title": "Someday", "start": "someday"},
                ],
            }
        )
    )

    assert result.status == "applied"
    by_title = {item.title: item for item in module._library.records.values()}  # noqa: SLF001
    assert by_title["Today"].start == NOW.date()
    assert by_title["Future"].start.isoformat() == "2026-08-18"
    assert by_title["Evening"].start == NOW.date()
    assert by_title["Evening"].tonight is True
    assert by_title["Someday"].start is None
    assert by_title["Someday"].someday is True


def test_scheduled_project_does_not_keep_anytime_state() -> None:
    module = workspace()

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "scheduled-project-001",
                "create": [{"kind": "project", "title": "Launch", "start": "today"}],
            }
        )
    )

    assert result.status == "applied"
    project = next(iter(module._library.records.values()))  # noqa: SLF001
    assert project.start == NOW.date()
    assert project.inbox is False


def test_reminder_input_converts_to_the_owner_timezone() -> None:
    owner_now = datetime(
        2026,
        8,
        15,
        12,
        tzinfo=timezone(timedelta(hours=2)),
    )
    module = ThingsWorkspace(MemoryLibrary(), clock=lambda: owner_now)

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "offset-reminder-001",
                "create": [
                    {
                        "title": "Early owner time",
                        "remind_at": "2026-08-15T23:30:00-04:00",
                    }
                ],
            }
        )
    )

    assert result.status == "applied"
    item = next(iter(module._library.records.values()))  # noqa: SLF001
    assert item.start.isoformat() == "2026-08-16"
    assert item.remind == "05:30"
    assert detail(module, item.id).remind_at == "2026-08-16T05:30:00+02:00"


@pytest.mark.parametrize(
    ("start", "tonight"),
    [("2026-08-15", False), ("today", False), ("evening", True)],
)
def test_matching_explicit_start_is_preserved_with_a_reminder(
    start: str, tonight: bool
) -> None:
    module = workspace()

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": f"matching-reminder-{start}",
                "create": [
                    {
                        "title": "Call",
                        "start": start,
                        "remind_at": "2026-08-15T15:00:00+00:00",
                    }
                ],
            }
        )
    )

    assert result.status == "applied"
    item = next(iter(module._library.records.values()))  # noqa: SLF001
    assert item.start == NOW.date()
    assert item.tonight is tonight


@pytest.mark.parametrize("start", ["2026-08-16", "today", "evening", "someday"])
def test_mismatched_explicit_start_and_reminder_are_rejected(start: str) -> None:
    module = workspace()
    reminder = (
        "2026-08-16T15:00:00+00:00"
        if start in {"today", "evening"}
        else "2026-08-15T15:00:00+00:00"
    )

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": f"mismatched-reminder-{start}",
                "create": [{"title": "Call", "start": start, "remind_at": reminder}],
            }
        )
    )

    assert result.status == "rejected"
    assert result.next == "stop"
    assert module._library.records == {}  # noqa: SLF001


def test_reminder_only_change_establishes_its_required_start_date() -> None:
    task = Record(
        uuid="call",
        kind="task",
        title="Call",
        start=NOW.date() + timedelta(days=3),
    )
    module = workspace([task])
    current = detail(module, task.id)

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "reminder-only-change-001",
                "change": [
                    {
                        "id": task.id,
                        "if_revision": current.revision,
                        "remind_at": "2026-08-16T09:00:00+00:00",
                    }
                ],
            }
        )
    )

    assert result.status == "applied"
    assert task.start == NOW.date() + timedelta(days=1)
    assert task.remind == "09:00"
    assert task.tonight is False


def test_reminder_only_change_keeps_existing_evening() -> None:
    task = Record(
        uuid="call",
        kind="task",
        title="Call Rowan",
        start=NOW.date(),
        tonight=True,
    )
    module = workspace([task])
    current = detail(module, task.id)

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "reminder-keep-evening-001",
                "change": [
                    {
                        "id": task.id,
                        "if_revision": current.revision,
                        "remind_at": "2026-08-15T18:00:00+00:00",
                    }
                ],
            }
        )
    )

    assert result.status == "applied"
    assert task.start == NOW.date()
    assert task.tonight is True
    assert task.remind == "18:00"
    fresh = detail(module, task.id)
    assert fresh.remind_at == "2026-08-15T18:00:00+00:00"
    assert "evening" in fresh.signals


def test_read_back_must_prove_an_explicit_move_out_of_evening() -> None:
    class KeepsEvening(MemoryLibrary):
        def apply(self, writes):  # type: ignore[no-untyped-def]
            result = super().apply(writes)
            self.records["task"].tonight = True
            return result

    task = Record(
        uuid="task",
        kind="task",
        title="Call",
        start=NOW.date(),
        tonight=True,
    )
    library = KeepsEvening([task])
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)
    current = detail(module, task.id)

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "leave-evening-001",
                "change": [
                    {
                        "id": task.id,
                        "if_revision": current.revision,
                        "start": "today",
                    }
                ],
            }
        )
    )

    assert result.status == "pending"
    assert result.next == "retry_same"


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
        assert result.sections[0].item_ids == [item.id for item in result.items]
        if result.cursor is None:
            break
        result = module.read(ReadCall(cursor=result.cursor, limit=10))

    assert ids == [f"task:today-{index}" for index in range(30)]


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
        assert result.sections[0].item_ids == [item.id for item in result.items]
        if result.cursor is None:
            break
        result = module.read(ReadCall(cursor=result.cursor, limit=10))

    assert len(ids) == 25
    assert len(set(ids)) == 25


def test_today_after_uses_a_local_anchor_without_raw_indexes() -> None:
    module = workspace()

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "today-order-001",
                "create": [
                    {"key": "$first", "title": "First", "start": "today"},
                    {
                        "title": "Second",
                        "start": "today",
                        "today_after": "$first",
                    },
                ],
            }
        )
    )

    assert result.status == "applied"
    ordered = sorted(  # noqa: SLF001
        module._library.records.values(), key=lambda item: item.today_index
    )
    assert [item.title for item in ordered] == ["First", "Second"]


def test_after_rebalances_dense_native_indexes() -> None:
    module = workspace(
        [
            Record(uuid="a", kind="task", title="A", inbox=True, sort_index=0),
            Record(uuid="b", kind="task", title="B", inbox=True, sort_index=1),
        ]
    )

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "dense-order-001",
                "create": [{"title": "X", "into": "inbox", "after": "task:a"}],
            }
        )
    )

    assert result.status == "applied"
    ordered = sorted(module._library.records.values(), key=lambda item: item.sort_index)  # noqa: SLF001
    assert [item.title for item in ordered] == ["A", "X", "B"]


def test_today_after_rebalances_dense_native_indexes() -> None:
    module = workspace(
        [
            Record(uuid="a", kind="task", title="A", start=NOW.date(), today_index=0),
            Record(uuid="b", kind="task", title="B", start=NOW.date(), today_index=1),
        ]
    )

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "dense-today-001",
                "create": [{"title": "X", "start": "today", "today_after": "task:a"}],
            }
        )
    )

    assert result.status == "applied"
    ordered = sorted(
        module._library.records.values(), key=lambda item: item.today_index
    )  # noqa: SLF001
    assert [item.title for item in ordered] == ["A", "X", "B"]


def test_same_intent_is_exactly_once_and_conflicting_reuse_is_rejected() -> None:
    module = workspace()
    call = CommitCall.model_validate(
        {"intent_id": "capture-001", "create": [{"title": "Renew passport"}]}
    )

    first = module.commit(call)
    second = module.commit(call)
    conflict = module.commit(
        CommitCall.model_validate(
            {"intent_id": "capture-001", "create": [{"title": "Different"}]}
        )
    )

    assert first.status == "applied"
    assert second.model_dump() == first.model_dump()
    assert len(module._library.records) == 1  # noqa: SLF001
    assert conflict.status == "rejected"


def test_empty_commit_is_a_domain_rejection_not_a_schema_failure() -> None:
    module = workspace()
    call = CommitCall.model_validate({"intent_id": "empty-commit-001"})

    result = module.commit(call)

    assert result.status == "rejected"
    assert result.next == "stop"


def test_existing_change_needs_current_revision() -> None:
    module = workspace([Record(uuid="one", kind="task", title="Old")])
    current = detail(module, "task:one")
    module._library.records["one"].title = "Owner edit"  # noqa: SLF001

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "rename-001",
                "change": [
                    {
                        "id": "task:one",
                        "if_revision": current.revision,
                        "title": "AI edit",
                    }
                ],
            }
        )
    )

    assert result.status == "stale"
    assert module._library.records["one"].title == "Owner edit"  # noqa: SLF001


def test_checklist_change_preserves_native_identity_and_order() -> None:
    task = Record(
        uuid="one",
        kind="task",
        title="Deploy",
        checklists=[
            ChecklistLine("a", "Build", sort_index=0),
            ChecklistLine("b", "Ship", sort_index=1024),
        ],
    )
    module = workspace([task])
    current = detail(module, "task:one")

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "checks-001",
                "change": [
                    {
                        "id": "task:one",
                        "if_revision": current.revision,
                        "checklist_change": [{"id": "check:a", "status": "completed"}],
                        "checklist_order": ["check:b", "check:a"],
                    }
                ],
            }
        )
    )

    assert result.status == "applied"
    assert [(row.uuid, row.status) for row in task.checklists] == [
        ("b", "open"),
        ("a", "done"),
    ]


def test_checklist_change_after_null_moves_one_row_first() -> None:
    task = Record(
        uuid="one",
        kind="task",
        title="Deploy",
        checklists=[
            ChecklistLine("a", "Build", sort_index=0),
            ChecklistLine("b", "Ship", sort_index=1024),
        ],
    )
    module = workspace([task])
    current = detail(module, "task:one")

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "check-first-001",
                "change": [
                    {
                        "id": "task:one",
                        "if_revision": current.revision,
                        "checklist_change": [{"id": "check:b", "after": None}],
                    }
                ],
            }
        )
    )

    assert result.status == "applied"
    assert [row.uuid for row in task.checklists] == ["b", "a"]


def test_checklist_after_rebalances_dense_native_indexes() -> None:
    task = Record(
        uuid="one",
        kind="task",
        title="Deploy",
        checklists=[
            ChecklistLine("a", "Build", sort_index=0),
            ChecklistLine("b", "Ship", sort_index=1),
        ],
    )
    module = workspace([task])
    current = detail(module, "task:one")

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "dense-check-001",
                "change": [
                    {
                        "id": "task:one",
                        "if_revision": current.revision,
                        "checklist_add": [{"title": "Test", "after": "check:a"}],
                    }
                ],
            }
        )
    )

    assert result.status == "applied"
    assert [row.title for row in task.checklists] == ["Build", "Test", "Ship"]


def test_risky_area_change_needs_approval_and_is_revision_bound() -> None:
    module = workspace([Record(uuid="work", kind="area", title="Work")])
    current = detail(module, "area:work")
    scope_revision = system_scope(module)
    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "area-rename-001",
                "scope_revision": scope_revision,
                "change": [
                    {
                        "id": "area:work",
                        "if_revision": current.revision,
                        "title": "Office",
                    }
                ],
            }
        )
    )

    assert prepared.status == "needs_approval"
    assert prepared.plan is not None
    assert "natural confirmation" in prepared.instruction
    assert "Keep plan IDs" in prepared.instruction
    assert module._library.records["work"].title == "Work"  # noqa: SLF001

    settled = module.approve(ApproveCall(plan_id=prepared.plan.id))

    assert settled.status == "applied"
    assert module._library.records["work"].title == "Office"  # noqa: SLF001


def test_new_area_settles_after_approval() -> None:
    module = workspace()
    scope_revision = system_scope(module)

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "area-create-001",
                "scope_revision": scope_revision,
                "create": [{"kind": "area", "title": "Health"}],
            }
        )
    )
    assert prepared.plan is not None
    settled = module.approve(ApproveCall(plan_id=prepared.plan.id))

    assert settled.status == "applied"
    area = next(iter(module._library.records.values()))  # noqa: SLF001
    assert area.kind == "area"
    assert area.inbox is False


def test_area_create_rejects_a_stale_system_scope_before_staging() -> None:
    module = workspace()
    stale_scope = system_scope(module)
    module._library.records["other"] = Record(  # noqa: SLF001
        uuid="other", kind="area", title="Other"
    )

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "area-stale-scope-001",
                "scope_revision": stale_scope,
                "create": [{"kind": "area", "title": "Health"}],
            }
        )
    )

    assert result.status == "stale"
    assert result.plan is None
    assert all(
        item.title != "Health"
        for item in module._library.records.values()  # noqa: SLF001
    )


def test_area_removal_needs_system_scope_revision() -> None:
    module = workspace(
        [
            Record(uuid="old", kind="area", title="Old"),
            Record(uuid="new", kind="area", title="New"),
            Record(uuid="task", kind="task", title="Keep", area_uuid="old"),
        ]
    )
    system = module.read(ReadCall(view="system"))
    old = detail(module, "area:old")
    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "area-merge-001",
                "scope_revision": system.scope_revision,
                "change": [
                    {
                        "id": "area:old",
                        "if_revision": old.revision,
                        "move_contents_to": "area:new",
                    }
                ],
            }
        )
    )

    assert result.status == "needs_approval"
    assert result.plan is not None
    settled = module.approve(ApproveCall(plan_id=result.plan.id))
    assert settled.status == "applied"
    assert "old" not in module._library.records  # noqa: SLF001
    assert module._library.records["task"].area_uuid == "new"  # noqa: SLF001


def test_trash_needs_approval_and_tears_down_project_children() -> None:
    project = Record(uuid="project", kind="project", title="Launch")
    child = Record(uuid="child", kind="task", title="Ship", parent_uuid="project")
    module = workspace([project, child])
    current = detail(module, project.id)

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "trash-project-001",
                "change": [
                    {"id": project.id, "if_revision": current.revision, "trash": True}
                ],
            }
        )
    )

    assert prepared.status == "needs_approval"
    assert prepared.plan is not None
    assert any("contained records" in line for line in prepared.plan.summary)
    assert project.trashed is False
    assert child.trashed is False

    settled = module.approve(ApproveCall(plan_id=prepared.plan.id))

    assert settled.status == "applied"
    assert project.trashed is True
    assert child.trashed is True
    assert child.parent_uuid == project.uuid


def test_project_children_move_and_trash_as_one_approved_batch() -> None:
    source = Record(uuid="merge-source", kind="project", title="Source")
    destination = Record(uuid="merge-destination", kind="project", title="Destination")
    first = Record(
        uuid="merge-first",
        kind="task",
        title="First",
        parent_uuid=source.uuid,
        notes="Keep this note.",
        checklists=[ChecklistLine("merge-check", "Keep this row")],
    )
    second = Record(
        uuid="merge-second", kind="task", title="Second", parent_uuid=source.uuid
    )
    module = workspace([source, destination, first, second])

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "project-merge-approval-001",
                "change": [
                    {
                        "id": source.id,
                        "if_revision": detail(module, source.id).revision,
                        "trash": True,
                    },
                    {
                        "id": first.id,
                        "if_revision": detail(module, first.id).revision,
                        "into": destination.id,
                    },
                    {
                        "id": second.id,
                        "if_revision": detail(module, second.id).revision,
                        "into": destination.id,
                    },
                ],
            }
        )
    )

    assert prepared.status == "needs_approval"
    assert prepared.plan is not None
    assert source.trashed is False
    assert first.parent_uuid == source.uuid
    assert second.parent_uuid == source.uuid

    applied = module.approve(ApproveCall(plan_id=prepared.plan.id))

    assert applied.status == "applied"
    assert source.trashed is True
    assert first.parent_uuid == destination.uuid
    assert second.parent_uuid == destination.uuid
    assert first.notes == "Keep this note."
    assert [(row.uuid, row.title) for row in first.checklists] == [
        ("merge-check", "Keep this row")
    ]


def test_project_merge_tears_down_unmoved_children() -> None:
    source = Record(uuid="partial-source", kind="project", title="Source")
    destination = Record(
        uuid="partial-destination", kind="project", title="Destination"
    )
    first = Record(
        uuid="partial-first", kind="task", title="First", parent_uuid=source.uuid
    )
    second = Record(
        uuid="partial-second", kind="task", title="Second", parent_uuid=source.uuid
    )
    module = workspace([source, destination, first, second])

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "project-merge-partial-001",
                "change": [
                    {
                        "id": source.id,
                        "if_revision": detail(module, source.id).revision,
                        "trash": True,
                    },
                    {
                        "id": first.id,
                        "if_revision": detail(module, first.id).revision,
                        "into": destination.id,
                    },
                ],
            }
        )
    )

    assert result.status == "needs_approval"
    assert result.plan is not None
    applied = module.approve(ApproveCall(plan_id=result.plan.id))
    assert applied.status == "applied"
    assert source.trashed is True
    assert first.parent_uuid == destination.uuid
    assert first.trashed is False
    assert second.parent_uuid == source.uuid
    assert second.trashed is True


@pytest.mark.parametrize("edge", ["heading", "trashed"])
def test_project_merge_tears_down_hidden_leftovers(edge: str) -> None:
    source = Record(uuid=f"{edge}-source", kind="project", title="Source")
    destination = Record(
        uuid=f"{edge}-destination", kind="project", title="Destination"
    )
    child = Record(
        uuid=f"{edge}-child",
        kind="task",
        title="Child",
        parent_uuid=source.uuid,
        heading=edge == "heading",
        trashed=edge == "trashed",
    )
    movable = Record(
        uuid=f"{edge}-movable",
        kind="task",
        title="Movable",
        parent_uuid=source.uuid,
    )
    module = workspace([source, destination, child, movable])

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": f"project-merge-{edge}-001",
                "change": [
                    {
                        "id": movable.id,
                        "if_revision": detail(module, movable.id).revision,
                        "into": destination.id,
                    },
                    {
                        "id": source.id,
                        "if_revision": detail(module, source.id).revision,
                        "trash": True,
                    },
                ],
            }
        )
    )

    assert result.status == "needs_approval"
    assert result.plan is not None
    applied = module.approve(ApproveCall(plan_id=result.plan.id))
    assert applied.status == "applied"
    assert source.trashed is True
    assert movable.parent_uuid == destination.uuid
    assert movable.trashed is False
    assert child.parent_uuid == source.uuid
    assert child.trashed is True


@pytest.mark.parametrize("hidden", ["completed", "trashed"])
def test_project_merge_rejects_hidden_destination_without_writes(hidden: str) -> None:
    source = Record(uuid=f"hidden-source-{hidden}", kind="project", title="Source")
    destination = Record(
        uuid=f"hidden-destination-{hidden}",
        kind="project",
        title="Destination",
        status="done" if hidden == "completed" else "open",
        trashed=hidden == "trashed",
    )
    child = Record(
        uuid=f"hidden-child-{hidden}",
        kind="task",
        title="Child",
        parent_uuid=source.uuid,
    )
    module = workspace([source, destination, child])

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": f"project-merge-hidden-destination-{hidden}",
                "change": [
                    {
                        "id": child.id,
                        "if_revision": detail(module, child.id).revision,
                        "into": destination.id,
                    },
                    {
                        "id": source.id,
                        "if_revision": detail(module, source.id).revision,
                        "trash": True,
                    },
                ],
            }
        )
    )

    assert result.status == "rejected"
    assert source.trashed is False
    assert child.parent_uuid == source.uuid


@pytest.mark.parametrize("lifecycle", ["complete", "trash", "delete"])
def test_project_merge_rejects_same_batch_destination_lifecycle_without_writes(
    lifecycle: str,
) -> None:
    source = Record(uuid=f"lifecycle-source-{lifecycle}", kind="project", title="Source")
    destination = Record(
        uuid=f"lifecycle-destination-{lifecycle}", kind="project", title="Destination"
    )
    child = Record(
        uuid=f"lifecycle-child-{lifecycle}",
        kind="task",
        title="Child",
        parent_uuid=source.uuid,
    )
    destination_change: dict[str, object] = {
        "id": destination.id,
        "if_revision": "",
    }
    if lifecycle == "complete":
        destination_change["status"] = "completed"
    elif lifecycle == "trash":
        destination_change["trash"] = True
    else:
        destination_change["lifecycle"] = "delete_permanently"
    module = workspace([source, destination, child])
    destination_change["if_revision"] = detail(module, destination.id).revision

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": f"project-merge-destination-lifecycle-{lifecycle}",
                "change": [
                    {
                        "id": child.id,
                        "if_revision": detail(module, child.id).revision,
                        "into": destination.id,
                    },
                    destination_change,
                    {
                        "id": source.id,
                        "if_revision": detail(module, source.id).revision,
                        "trash": True,
                    },
                ],
            }
        )
    )

    assert result.status == "rejected"
    assert source.trashed is False
    assert destination.trashed is False
    assert destination.status == "open"
    assert child.parent_uuid == source.uuid


def test_project_merge_destination_change_after_approval_is_stale_without_writes() -> None:
    source = Record(uuid="race-source", kind="project", title="Source")
    destination = Record(uuid="race-destination", kind="project", title="Destination")
    child = Record(
        uuid="race-child", kind="task", title="Child", parent_uuid=source.uuid
    )
    module = workspace([source, destination, child])

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "project-merge-destination-race-001",
                "change": [
                    {
                        "id": child.id,
                        "if_revision": detail(module, child.id).revision,
                        "into": destination.id,
                    },
                    {
                        "id": source.id,
                        "if_revision": detail(module, source.id).revision,
                        "trash": True,
                    },
                ],
            }
        )
    )
    assert prepared.status == "needs_approval"
    assert prepared.plan is not None

    destination.title = "Changed while waiting"
    stale = module.approve(ApproveCall(plan_id=prepared.plan.id))

    assert stale.status == "stale"
    assert source.trashed is False
    assert child.parent_uuid == source.uuid


def test_project_merge_destination_child_change_after_approval_is_stale_without_writes() -> None:
    source = Record(uuid="race-child-source", kind="project", title="Source")
    destination = Record(
        uuid="race-child-destination", kind="project", title="Destination"
    )
    child = Record(
        uuid="race-child-move", kind="task", title="Move", parent_uuid=source.uuid
    )
    module = workspace([source, destination, child])

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "project-merge-destination-child-race-001",
                "change": [
                    {
                        "id": child.id,
                        "if_revision": detail(module, child.id).revision,
                        "into": destination.id,
                    },
                    {
                        "id": source.id,
                        "if_revision": detail(module, source.id).revision,
                        "trash": True,
                    },
                ],
            }
        )
    )
    assert prepared.status == "needs_approval"
    assert prepared.plan is not None

    # A new destination child changes membership without changing the
    # destination Project revision itself.
    module._library.records["race-child-existing"] = Record(  # noqa: SLF001
        uuid="race-child-existing",
        kind="task",
        title="Already there",
        parent_uuid=destination.uuid,
    )
    stale = module.approve(ApproveCall(plan_id=prepared.plan.id))

    assert stale.status == "stale"
    assert source.trashed is False
    assert child.parent_uuid == source.uuid


@pytest.mark.parametrize("mutation", ["title", "trashed"])
def test_project_merge_destination_area_change_after_approval_is_stale_without_writes(
    mutation: str,
) -> None:
    area = Record(uuid="race-area", kind="area", title="Work")
    source = Record(uuid="race-area-source", kind="project", title="Source")
    destination = Record(
        uuid="race-area-destination",
        kind="project",
        title="Destination",
        area_uuid=area.uuid,
    )
    child = Record(
        uuid="race-area-child",
        kind="task",
        title="Child",
        parent_uuid=source.uuid,
    )
    module = workspace([area, source, destination, child])

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": f"project-merge-destination-area-race-{mutation}-001",
                "change": [
                    {
                        "id": child.id,
                        "if_revision": detail(module, child.id).revision,
                        "into": destination.id,
                    },
                    {
                        "id": source.id,
                        "if_revision": detail(module, source.id).revision,
                        "trash": True,
                    },
                ],
            }
        )
    )
    assert prepared.status == "needs_approval"
    assert prepared.plan is not None

    if mutation == "title":
        area.title = "Changed while waiting"
    else:
        area.trashed = True
    stale = module.approve(ApproveCall(plan_id=prepared.plan.id))

    assert stale.status == "stale"
    assert source.trashed is False
    assert child.parent_uuid == source.uuid


def test_project_merge_accepts_local_area_project_task_batch() -> None:
    module = workspace()

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "local-area-project-task-001",
                "scope_revision": system_scope(module),
                "create": [
                    {"key": "$area", "kind": "area", "title": "Work"},
                    {
                        "key": "$project",
                        "kind": "project",
                        "title": "Launch",
                        "into": "$area",
                    },
                    {
                        "key": "$task",
                        "kind": "task",
                        "title": "Ship",
                        "into": "$project",
                    },
                ],
            }
        )
    )

    assert prepared.status == "needs_approval"
    assert prepared.plan is not None
    applied = module.approve(ApproveCall(plan_id=prepared.plan.id))

    assert applied.status == "applied"
    area = next(
        item
        for item in module._library.records.values()  # noqa: SLF001
        if item.kind == "area"
    )
    project = next(
        item
        for item in module._library.records.values()  # noqa: SLF001
        if item.kind == "project"
    )
    task = next(
        item
        for item in module._library.records.values()  # noqa: SLF001
        if item.kind == "task"
    )
    assert project.area_uuid == area.uuid
    assert task.parent_uuid == project.uuid
    assert task.area_uuid is None


def test_trash_rejects_areas() -> None:
    area = Record(uuid="area", kind="area", title="Work")
    module = workspace([area])
    current = detail(module, area.id)

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "trash-area-001",
                "scope_revision": system_scope(module),
                "change": [
                    {"id": area.id, "if_revision": current.revision, "trash": True}
                ],
            }
        )
    )

    assert result.status == "rejected"
    assert area.trashed is False


def test_heading_into_another_project_is_rejected_without_merge() -> None:
    source = Record(uuid="heading-source", kind="project", title="Source")
    destination = Record(uuid="heading-dest", kind="project", title="Destination")
    heading = Record(
        uuid="lonely-heading",
        kind="task",
        title="Section",
        parent_uuid=source.uuid,
        heading=True,
    )
    module = workspace([source, destination, heading])
    current = detail(module, heading.id)

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "heading-cross-project-001",
                "change": [
                    {
                        "id": heading.id,
                        "if_revision": current.revision,
                        "into": destination.id,
                    }
                ],
            }
        )
    )

    assert result.status == "rejected"
    assert "atomic Project merge" in result.instruction
    assert heading.parent_uuid == source.uuid


def test_heading_into_another_project_rejects_an_unrelated_source_trash() -> None:
    source = Record(uuid="heading-keep", kind="project", title="Keep")
    destination = Record(uuid="heading-other", kind="project", title="Other")
    decoy = Record(uuid="heading-decoy", kind="project", title="Decoy")
    heading = Record(
        uuid="heading-stay",
        kind="task",
        title="Section",
        parent_uuid=source.uuid,
        heading=True,
    )
    module = workspace([source, destination, decoy, heading])

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "heading-unrelated-trash-001",
                "change": [
                    {
                        "id": heading.id,
                        "if_revision": detail(module, heading.id).revision,
                        "into": destination.id,
                    },
                    {
                        "id": decoy.id,
                        "if_revision": detail(module, decoy.id).revision,
                        "lifecycle": "trash",
                    },
                ],
            }
        )
    )

    assert result.status == "rejected"
    assert "atomic Project merge" in result.instruction
    assert heading.parent_uuid == source.uuid
    assert decoy.trashed is False


def test_heading_into_its_current_project_is_not_a_cross_project_move() -> None:
    project = Record(uuid="heading-home", kind="project", title="Home")
    heading = Record(
        uuid="heading-same",
        kind="task",
        title="Section",
        parent_uuid=project.uuid,
        heading=True,
    )
    module = workspace([project, heading])
    current = detail(module, heading.id)

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "heading-same-project-001",
                "change": [
                    {
                        "id": heading.id,
                        "if_revision": current.revision,
                        "into": project.id,
                    }
                ],
            }
        )
    )

    assert result.status == "unchanged"
    assert heading.parent_uuid == project.uuid


def test_heading_create_rename_assignment_and_clear() -> None:
    project = Record(uuid="project", kind="project", title="Launch")
    task = Record(uuid="task", kind="task", title="Ship", parent_uuid=project.uuid)
    module = workspace([project, task])
    created = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "heading-create-001",
                "create": [{"kind": "heading", "title": "Next", "into": project.id}],
            }
        )
    )

    assert created.status == "applied"
    heading = next(item for item in module._library.records.values() if item.heading)  # noqa: SLF001
    assert heading.parent_uuid == project.uuid
    assert created.items[0].id == heading.id
    assert created.items[0].kind == "heading"
    task_fact = detail(module, task.id)
    assigned = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "heading-assign-001",
                "change": [
                    {
                        "id": task.id,
                        "if_revision": task_fact.revision,
                        "heading_id": heading.id,
                    }
                ],
            }
        )
    )
    assert assigned.status == "applied"
    assert task.heading_uuid == heading.uuid

    heading_fact = detail(module, heading.id)
    renamed = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "heading-rename-001",
                "change": [
                    {
                        "id": heading.id,
                        "if_revision": heading_fact.revision,
                        "title": "Later",
                    }
                ],
            }
        )
    )
    assert renamed.status == "applied"
    assert heading.title == "Later"

    task_fact = detail(module, task.id)
    cleared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "heading-clear-001",
                "change": [
                    {
                        "id": task.id,
                        "if_revision": task_fact.revision,
                        "heading_id": None,
                    }
                ],
            }
        )
    )
    assert cleared.status == "applied"
    assert task.heading_uuid is None
    project_items = module.read(ReadCall(view="project", within=project.id)).items
    assert any(
        item.kind == "heading" and item.title == "Later" for item in project_items
    )


def test_task_create_under_existing_exact_heading() -> None:
    project = Record(uuid="project", kind="project", title="Launch")
    heading = Record(
        uuid="heading",
        kind="task",
        title="Next",
        parent_uuid=project.uuid,
        heading=True,
    )
    module = workspace([project, heading])

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "task-existing-heading-001",
                "create": [
                    {
                        "kind": "task",
                        "title": "Ship",
                        "into": project.id,
                        "heading_id": heading.id,
                    }
                ],
            }
        )
    )

    assert result.status == "applied"
    task = next(
        item
        for item in module._library.records.values()  # noqa: SLF001
        if item.title == "Ship"
    )
    assert task.parent_uuid == project.uuid
    assert task.heading_uuid == heading.uuid


def test_task_moves_and_uses_destination_heading_in_one_change() -> None:
    source = Record(uuid="move-source", kind="project", title="Source")
    destination = Record(uuid="move-destination", kind="project", title="Destination")
    heading = Record(
        uuid="move-heading",
        kind="task",
        title="Next",
        parent_uuid=destination.uuid,
        heading=True,
    )
    task = Record(
        uuid="move-task",
        kind="task",
        title="Ship",
        parent_uuid=source.uuid,
    )
    module = workspace([source, destination, heading, task])
    current = detail(module, task.id)

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "move-and-heading-001",
                "change": [
                    {
                        "id": task.id,
                        "if_revision": current.revision,
                        "into": destination.id,
                        "heading_id": heading.id,
                    }
                ],
            }
        )
    )

    assert result.status == "applied"
    assert task.parent_uuid == destination.uuid
    assert task.heading_uuid == heading.uuid


def test_heading_delete_clears_trashed_assigned_tasks_in_one_plan() -> None:
    project = Record(uuid="heading-project", kind="project", title="Plan")
    heading = Record(
        uuid="heading-used",
        kind="task",
        title="Next",
        parent_uuid=project.uuid,
        heading=True,
    )
    task = Record(
        uuid="heading-trashed-task",
        kind="task",
        title="Hidden",
        parent_uuid=project.uuid,
        heading_uuid=heading.uuid,
        trashed=True,
    )
    module = workspace([project, heading, task])
    current = detail(module, heading.id)

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "heading-delete-used-001",
                "change": [
                    {
                        "id": heading.id,
                        "if_revision": current.revision,
                        "lifecycle": "delete_permanently",
                    }
                ],
            }
        )
    )

    assert result.status == "needs_approval"
    assert result.plan is not None
    applied = module.approve(ApproveCall(plan_id=result.plan.id))
    assert applied.status == "applied"
    assert heading.uuid not in module._library.records  # noqa: SLF001
    assert task.heading_uuid is None


def test_project_heading_and_task_create_together_with_local_heading() -> None:
    module = workspace()

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "task-local-heading-001",
                "create": [
                    {"key": "$project", "kind": "project", "title": "Launch"},
                    {
                        "key": "$section",
                        "kind": "heading",
                        "title": "Next",
                        "into": "$project",
                    },
                    {
                        "kind": "task",
                        "title": "Ship",
                        "into": "$project",
                        "heading_id": "$section",
                    },
                ],
            }
        )
    )

    assert result.status == "applied"
    records = {item.title: item for item in module._library.records.values()}  # noqa: SLF001
    assert records["Next"].parent_uuid == records["Launch"].uuid
    assert records["Ship"].parent_uuid == records["Launch"].uuid
    assert records["Ship"].heading_uuid == records["Next"].uuid


@pytest.mark.parametrize(
    "heading_uuid, expected_status, instruction",
    [
        (
            "other-heading",
            "rejected",
            "The heading must belong to the Task's Project.",
        ),
        (
            "not-heading",
            "needs_input",
            "I could not find exact item heading:not-heading.",
        ),
    ],
)
def test_task_create_rejects_heading_outside_project_or_non_heading(
    heading_uuid: str, expected_status: str, instruction: str
) -> None:
    project = Record(uuid="project", kind="project", title="Launch")
    other_project = Record(uuid="other-project", kind="project", title="Other")
    other_heading = Record(
        uuid="other-heading",
        kind="task",
        title="Other section",
        parent_uuid=other_project.uuid,
        heading=True,
    )
    not_heading = Record(
        uuid="not-heading",
        kind="task",
        title="Ordinary Task",
        parent_uuid=project.uuid,
    )
    module = workspace([project, other_project, other_heading, not_heading])

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": f"task-invalid-heading-{heading_uuid}",
                "create": [
                    {
                        "kind": "task",
                        "title": "Ship",
                        "into": project.id,
                        "heading_id": f"heading:{heading_uuid}",
                    }
                ],
            }
        )
    )

    assert result.status == expected_status
    assert result.instruction == instruction
    assert all(
        item.title != "Ship"
        for item in module._library.records.values()  # noqa: SLF001
    )


def test_area_plan_stales_when_a_new_child_appears() -> None:
    module = workspace(
        [
            Record(uuid="old", kind="area", title="Old"),
            Record(uuid="new", kind="area", title="New"),
        ]
    )
    system = module.read(ReadCall(view="system"))
    old = detail(module, "area:old")
    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "area-race-001",
                "scope_revision": system.scope_revision,
                "change": [
                    {
                        "id": "area:old",
                        "if_revision": old.revision,
                        "move_contents_to": "area:new",
                    }
                ],
            }
        )
    )
    assert prepared.plan is not None
    module._library.records["late"] = Record(  # noqa: SLF001
        uuid="late", kind="task", title="Late", area_uuid="old"
    )

    result = module.approve(ApproveCall(plan_id=prepared.plan.id))

    assert result.status == "stale"
    assert "old" in module._library.records  # noqa: SLF001


def test_clearing_missing_waiting_tag_does_not_create_one() -> None:
    module = workspace([Record(uuid="one", kind="task", title="Reply")])
    current = detail(module, "task:one")

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "waiting-off-001",
                "change": [
                    {
                        "id": "task:one",
                        "if_revision": current.revision,
                        "waiting": False,
                    }
                ],
            }
        )
    )

    assert result.status == "unchanged"
    assert module._library.tags == {}  # noqa: SLF001


def test_unknown_cloud_outcome_never_reposts_after_state_appears() -> None:
    class AppliedThenTimedOut(MemoryLibrary):
        attempts = 0

        def apply(self, writes):  # type: ignore[no-untyped-def]
            self.attempts += 1
            result = super().apply(writes)
            if self.attempts == 1:
                raise CloudError("commit timed out; outcome unknown")
            return result

    library = AppliedThenTimedOut()
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)
    call = CommitCall.model_validate(
        {"intent_id": "timeout-001", "create": [{"title": "Only once"}]}
    )

    pending = module.commit(call)
    settled = module.commit(call)

    assert pending.status == "pending"
    assert pending.next == "retry_same"
    assert settled.status == "applied"
    assert library.attempts == 1
    assert len(library.records) == 1


@pytest.mark.parametrize(
    ("role", "recurrence_type"),
    [
        ("template", "fixed"),
        ("instance", "after_completion"),
        ("instance", "fixed"),
        ("instance", "unknown"),
    ],
)
def test_repeat_templates_allow_future_metadata_but_unknown_instances_are_read_only(
    role: str, recurrence_type: str
) -> None:
    recurring = Record(
        uuid="repeat",
        kind="task",
        title="Routine",
        recurrence=RecurrenceState(
            role=role,  # type: ignore[arg-type]
            repeat_type=recurrence_type,  # type: ignore[arg-type]
        ),
    )
    module = workspace([recurring])
    repeated = detail(module, "task:repeat")

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": f"repeat-{role}-{recurrence_type}",
                "change": [
                    {
                        "id": "task:repeat",
                        "if_revision": repeated.revision,
                        "title": "Changed",
                    }
                ],
            }
        )
    )

    assert repeated.recurrence is not None
    if role == "template":
        assert result.status == "needs_approval"
        assert result.plan is not None
        applied = module.approve(ApproveCall(plan_id=result.plan.id))
        assert applied.status == "applied"
        assert recurring.title == "Changed"
    elif recurrence_type in {"fixed", "after_completion"}:
        assert result.status == "applied"
        assert recurring.title == "Changed"
    else:
        assert result.status == "unsupported"
        assert recurring.title == "Routine"


def test_template_repeat_interval_needs_approval_and_preserves_rule() -> None:
    rule = {
        "tp": 1,
        "fu": 256,
        "fa": 1,
        "of": [{"wd": 2, "future": {"keep": True}}],
        "rrv": 42,
    }
    template = Record(
        uuid="template",
        kind="task",
        title="Routine",
        recurrence=RecurrenceState(
            role="template", repeat_type="after_completion", rule=rule
        ),
    )
    module = workspace([template])
    current = detail(module, template.id)

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "repeat-interval-approval-001",
                "change": [
                    {
                        "id": template.id,
                        "if_revision": current.revision,
                        "repeat_interval": 3,
                    }
                ],
            }
        )
    )

    assert prepared.status == "needs_approval"
    assert prepared.plan is not None
    assert template.recurrence.rule == rule

    applied = module.approve(ApproveCall(plan_id=prepared.plan.id))

    assert applied.status == "applied"
    assert template.recurrence.rule == {**rule, "fa": 3}
    assert template.recurrence.rule is not None
    assert template.recurrence.rule["of"] == rule["of"]


def test_template_repeat_interval_plan_stales_when_instance_changes() -> None:
    template = Record(
        uuid="template",
        kind="task",
        title="Routine",
        recurrence=RecurrenceState(
            role="template",
            repeat_type="fixed",
            rule={"tp": 0, "fu": 16, "fa": 1},
        ),
    )
    instance = Record(
        uuid="instance",
        kind="task",
        title="Routine copy",
        recurrence=RecurrenceState(
            role="instance",
            repeat_type="fixed",
            template_uuid="template",
            links=("template",),
        ),
    )
    module = workspace([template, instance])
    current = detail(module, template.id)
    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "repeat-interval-stale-001",
                "change": [
                    {
                        "id": template.id,
                        "if_revision": current.revision,
                        "repeat_interval": 2,
                    }
                ],
            }
        )
    )
    assert prepared.status == "needs_approval"
    assert prepared.plan is not None

    instance.title = "Owner changed copy"
    result = module.approve(ApproveCall(plan_id=prepared.plan.id))

    assert result.status == "stale"
    assert template.recurrence.rule == {"tp": 0, "fu": 16, "fa": 1}


def test_repeat_interval_rejects_normal_tasks_and_generated_instances() -> None:
    normal = Record(uuid="normal", kind="task", title="Normal")
    instance = Record(
        uuid="instance",
        kind="task",
        title="Generated",
        recurrence=RecurrenceState(
            role="instance",
            repeat_type="after_completion",
            template_uuid="template",
            links=("template",),
        ),
    )
    module = workspace([normal, instance])

    for item in (normal, instance):
        current = detail(module, item.id)
        result = module.commit(
            CommitCall.model_validate(
                {
                    "intent_id": f"repeat-interval-reject-{item.uuid}",
                    "change": [
                        {
                            "id": item.id,
                            "if_revision": current.revision,
                            "repeat_interval": 2,
                        }
                    ],
                }
            )
        )
        assert result.status == "unsupported"
        assert item.recurrence.rule is None


def test_rich_note_changes_stop_safely() -> None:
    rich = Record(
        uuid="rich",
        kind="task",
        title="Rich",
        notes="styled",
        notes_format="rich",
    )
    module = workspace([rich])
    styled = detail(module, "task:rich")

    note_result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "richnote-001",
                "change": [
                    {
                        "id": "task:rich",
                        "if_revision": styled.revision,
                        "notes_markdown": "new",
                    }
                ],
            }
        )
    )

    assert note_result.status == "unsupported"


def test_rich_note_can_be_explicitly_replaced_after_one_approval() -> None:
    rich = Record(
        uuid="rich-replace",
        kind="task",
        title="Rich",
        notes="styled",
        notes_format="rich",
    )
    module = workspace([rich])
    current = detail(module, rich.id)

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "richnote-replace-001",
                "change": [
                    {
                        "id": rich.id,
                        "if_revision": current.revision,
                        "notes_markdown": "## Plain replacement",
                        "replace_rich_note": True,
                    }
                ],
            }
        )
    )

    assert prepared.status == "needs_approval"
    assert prepared.plan is not None
    applied = module.approve(ApproveCall(plan_id=prepared.plan.id))
    assert applied.status == "applied"
    assert rich.notes == "## Plain replacement"
    assert rich.notes_format == "markdown"


def test_repeat_mode_unit_and_interval_change_in_one_approved_plan() -> None:
    template = Record(
        uuid="repeat-full",
        kind="task",
        title="Review",
        recurrence=RecurrenceState(
            role="template",
            repeat_type="fixed",
            rule={
                "tp": 0,
                "fu": 8,
                "fa": 1,
                "of": [{"opaque": "keep"}],
                "sr": 1_775_232_000,
                "rrv": 99,
            },
        ),
    )
    module = workspace([template])
    current = detail(module, template.id)

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "repeat-full-rule-001",
                "change": [
                    {
                        "id": template.id,
                        "if_revision": current.revision,
                        "repeat": {
                            "mode": "after_completion",
                            "unit": "week",
                            "interval": 2,
                        },
                    }
                ],
            }
        )
    )

    assert prepared.status == "needs_approval"
    assert prepared.plan is not None
    applied = module.approve(ApproveCall(plan_id=prepared.plan.id))
    assert applied.status == "applied"
    assert template.recurrence.rule == {
        "tp": 1,
        "fu": 256,
        "fa": 2,
        "of": [],
        "sr": 1_775_232_000,
        "ts": 0,
        "rrv": 99,
    }


def test_restore_and_permanent_delete_use_explicit_lifecycle_steps() -> None:
    task = Record(uuid="lifecycle", kind="task", title="Old", trashed=True)
    module = workspace([task])
    trashed = detail(module, task.id)
    assert "trashed" in trashed.signals

    restore_plan = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "restore-task-001",
                "change": [
                    {
                        "id": task.id,
                        "if_revision": trashed.revision,
                        "lifecycle": "restore",
                    }
                ],
            }
        )
    )
    assert restore_plan.plan is not None
    restored = module.approve(ApproveCall(plan_id=restore_plan.plan.id))
    assert restored.status == "applied"
    assert task.trashed is False

    active = detail(module, task.id)
    trash_plan = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "trash-before-delete-001",
                "change": [
                    {
                        "id": task.id,
                        "if_revision": active.revision,
                        "lifecycle": "trash",
                    }
                ],
            }
        )
    )
    assert trash_plan.plan is not None
    module.approve(ApproveCall(plan_id=trash_plan.plan.id))

    ready = detail(module, task.id)
    delete_plan = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "delete-task-permanently-001",
                "change": [
                    {
                        "id": task.id,
                        "if_revision": ready.revision,
                        "lifecycle": "delete_permanently",
                    }
                ],
            }
        )
    )
    assert delete_plan.plan is not None
    deleted = module.approve(ApproveCall(plan_id=delete_plan.plan.id))
    assert deleted.status == "applied"
    assert "lifecycle" not in module._library.records  # noqa: SLF001


def test_heading_reorder_and_empty_delete_are_first_class_changes() -> None:
    project = Record(uuid="head-project", kind="project", title="Launch")
    first = Record(
        uuid="head-first",
        kind="task",
        title="First",
        heading=True,
        parent_uuid=project.uuid,
        sort_index=0,
    )
    second = Record(
        uuid="head-second",
        kind="task",
        title="Second",
        heading=True,
        parent_uuid=project.uuid,
        sort_index=1024,
    )
    module = workspace([project, first, second])
    current = detail(module, second.id)

    moved = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "heading-reorder-001",
                "change": [
                    {"id": second.id, "if_revision": current.revision, "after": None}
                ],
            }
        )
    )
    assert moved.status == "applied"
    assert second.sort_index < first.sort_index

    fresh = detail(module, second.id)
    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "heading-delete-001",
                "change": [
                    {
                        "id": second.id,
                        "if_revision": fresh.revision,
                        "lifecycle": "delete_permanently",
                    }
                ],
            }
        )
    )
    assert prepared.plan is not None
    deleted = module.approve(ApproveCall(plan_id=prepared.plan.id))
    assert deleted.status == "applied"
    assert second.uuid not in module._library.records  # noqa: SLF001


def test_tag_registry_changes_batch_with_reference_cleanup() -> None:
    task = Record(uuid="tag-task", kind="task", title="Tagged", tag_uuids=["old"])
    library = MemoryLibrary([task])
    library.tags = {"old": "Old", "child": "Child", "parent": "Parent"}
    library.tag_parents = {"child": ["old"]}
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)
    tag_read = module.read(ReadCall(view="tags"))
    assert tag_read.scope_revision is not None
    assert "tags_revision" in tag_read.instruction

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "tag-admin-delete-001",
                "tags_revision": tag_read.scope_revision,
                "change_tags": [
                    {"id": "tag:parent", "title": "People"},
                    {"id": "tag:old", "delete_permanently": True},
                ],
            }
        )
    )

    assert prepared.status == "needs_approval"
    assert prepared.plan is not None

    copied = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "tag-admin-scope-as-tags-001",
                "scope_revision": tag_read.scope_revision,
                "change_tags": [{"id": "tag:parent", "title": "People"}],
            }
        )
    )
    assert copied.status == "needs_approval"

    applied = module.approve(ApproveCall(plan_id=prepared.plan.id))
    assert applied.status == "applied"
    assert library.tags == {"child": "Child", "parent": "People"}
    assert library.tag_parents["child"] == []
    assert task.tag_uuids == []


def test_nested_tags_can_be_ensured_and_assigned_in_one_commit() -> None:
    module = workspace()

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "tag-nested-create-001",
                "ensure_tags": [
                    {"key": "$people", "title": "People"},
                    {"key": "$alex", "title": "Alex", "parent_id": "$people"},
                ],
                "create": [{"title": "Call Alex", "tag_ids": ["$alex"]}],
            }
        )
    )

    assert result.status == "applied"
    people = module._library.tag_uuid("People")  # noqa: SLF001
    alex = module._library.tag_uuid("Alex")  # noqa: SLF001
    assert people is not None and alex is not None
    assert module._library.tag_parents[alex] == [people]  # noqa: SLF001
    created = next(
        item
        for item in module._library.records.values()  # noqa: SLF001
        if item.title == "Call Alex"
    )
    assert created.tag_uuids == [alex]


def test_ensure_existing_tag_rejects_an_indirect_parent_cycle() -> None:
    library = MemoryLibrary()
    library.tags = {"parent": "Parent", "child": "Child"}
    library.tag_parents = {"parent": [], "child": ["parent"]}
    module = ThingsWorkspace(library, clock=lambda: NOW)

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "tag-ensure-cycle-001",
                "ensure_tags": [
                    {
                        "key": "$parent",
                        "title": "Parent",
                        "parent_id": "tag:child",
                    }
                ],
            }
        )
    )

    assert result.status == "rejected"
    assert library.tag_parents["parent"] == []


def test_repeat_create_and_stop_use_one_plan_each() -> None:
    module = workspace()
    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "repeat-create-atomic-001",
                "create": [
                    {
                        "title": "Review metrics",
                        "checklist": ["Open dashboard"],
                        "repeat": {
                            "unit": "week",
                            "interval": 2,
                            "weekdays": ["monday", "friday"],
                        },
                    }
                ],
            }
        )
    )
    assert prepared.status == "needs_approval"
    assert prepared.plan is not None
    applied = module.approve(ApproveCall(plan_id=prepared.plan.id))
    assert applied.status == "applied"

    template = next(
        item
        for item in module._library.records.values()  # noqa: SLF001
        if item.recurrence.role == "template"
    )
    instance = next(
        item
        for item in module._library.records.values()  # noqa: SLF001
        if item.recurrence.role == "instance"
    )
    assert instance.recurrence.template_uuid == template.uuid
    assert template.recurrence.rule is not None
    assert template.recurrence.rule["of"] == [{"wd": 1}, {"wd": 5}]
    assert [row.title for row in template.checklists] == ["Open dashboard"]
    assert [row.title for row in instance.checklists] == ["Open dashboard"]

    current = detail(module, template.id)
    stop_plan = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "repeat-stop-atomic-001",
                "change": [
                    {
                        "id": template.id,
                        "if_revision": current.revision,
                        "repeat": {"remove": True},
                    }
                ],
            }
        )
    )
    assert stop_plan.status == "needs_approval"
    assert stop_plan.plan is not None
    stopped = module.approve(ApproveCall(plan_id=stop_plan.plan.id))
    assert stopped.status == "applied"
    assert template.uuid not in module._library.records  # noqa: SLF001
    assert instance.recurrence.role == "none"


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


def test_stop_repeat_on_current_copy_deletes_the_template() -> None:
    module, template, current = _repeating_pair()
    revision = detail(module, current.id).revision

    planned = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "stop-report-repeat-on-current",
                "change": [
                    {
                        "id": current.id,
                        "if_revision": revision,
                        "repeat": {"remove": True},
                    }
                ],
            }
        )
    )
    assert planned.status == "needs_approval"
    assert planned.plan is not None
    stopped = module.approve(ApproveCall(plan_id=planned.plan.id))
    assert stopped.status == "applied"
    assert template.uuid not in module._library.records  # noqa: SLF001
    assert current.recurrence.role == "none"


def test_stop_repeat_on_current_and_template_is_one_plan() -> None:
    module, template, current = _repeating_pair()
    current_revision = detail(module, current.id).revision
    template_revision = detail(module, template.id).revision

    planned = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "stop-report-repeat-both",
                "change": [
                    {
                        "id": current.id,
                        "if_revision": current_revision,
                        "repeat": {"remove": True},
                    },
                    {
                        "id": template.id,
                        "if_revision": template_revision,
                        "repeat": {"remove": True},
                    },
                ],
            }
        )
    )
    assert planned.status == "needs_approval"
    assert planned.plan is not None
    stopped = module.approve(ApproveCall(plan_id=planned.plan.id))
    assert stopped.status == "applied"
    assert template.uuid not in module._library.records  # noqa: SLF001
    assert current.recurrence.role == "none"


def test_existing_task_starts_repeating_in_one_plan_and_preserves_metadata() -> None:
    project = Record(uuid="repeat-project", kind="project", title="Routines")
    heading = Record(
        uuid="repeat-heading",
        kind="task",
        title="Weekly",
        parent_uuid=project.uuid,
        heading=True,
    )
    task = Record(
        uuid="repeat-existing",
        kind="task",
        title="Review metrics",
        notes="Keep the decision log current.",
        start=NOW.date(),
        deadline=NOW.date() + timedelta(days=2),
        remind="09:30",
        parent_uuid=project.uuid,
        heading_uuid=heading.uuid,
        tag_uuids=["focus"],
        sort_index=2048,
        checklists=[
            ChecklistLine("repeat-row", "Open dashboard", status="done", sort_index=7)
        ],
    )
    library = MemoryLibrary([project, heading, task])
    library.tags["focus"] = "Focus"
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)
    current = detail(module, task.id)

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "repeat-existing-task-001",
                "change": [
                    {
                        "id": task.id,
                        "if_revision": current.revision,
                        "repeat": {
                            "unit": "week",
                            "interval": 2,
                            "weekdays": ["monday", "friday"],
                        },
                    }
                ],
            }
        )
    )

    assert prepared.status == "needs_approval"
    assert prepared.plan is not None
    applied = module.approve(ApproveCall(plan_id=prepared.plan.id))
    assert applied.status == "applied"

    template = next(
        item for item in library.records.values() if item.recurrence.role == "template"
    )
    assert {item.id for item in applied.items} == {task.id, template.id}
    assert task.uuid == "repeat-existing"
    assert task.recurrence.role == "instance"
    assert task.recurrence.template_uuid == template.uuid
    assert template.title == task.title
    assert template.notes == task.notes
    assert template.start == task.start
    assert template.deadline == task.deadline
    assert template.remind == task.remind
    assert template.parent_uuid == task.parent_uuid
    assert template.heading_uuid == task.heading_uuid
    assert template.tag_uuids == task.tag_uuids
    assert template.sort_index == task.sort_index
    assert [(row.title, row.status, row.sort_index) for row in template.checklists] == [
        ("Open dashboard", "open", 7)
    ]
    assert template.recurrence.interval == 2
    assert template.recurrence.weekday_codes == (1, 5)


def test_repeat_conversion_projects_one_desired_state_to_copy_and_template() -> None:
    old_project = Record(uuid="old-project", kind="project", title="Old")
    new_project = Record(uuid="new-project", kind="project", title="New")
    heading = Record(
        uuid="new-heading",
        kind="task",
        title="Cadence",
        parent_uuid=new_project.uuid,
        heading=True,
    )
    list_anchor = Record(
        uuid="list-anchor",
        kind="task",
        title="First in project",
        parent_uuid=new_project.uuid,
        sort_index=1024,
    )
    today_anchor = Record(
        uuid="today-anchor",
        kind="task",
        title="First today",
        start=NOW.date(),
        today_index=1024,
    )
    task = Record(
        uuid="repeat-batched",
        kind="task",
        title="Old routine",
        parent_uuid=old_project.uuid,
        checklists=[
            ChecklistLine("row-a", "Remove", status="done", sort_index=0),
            ChecklistLine("row-b", "Rename", sort_index=1024),
            ChecklistLine("row-c", "Keep", status="done", sort_index=2048),
        ],
    )
    module = workspace(
        [old_project, new_project, heading, list_anchor, today_anchor, task]
    )

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "repeat-desired-state-001",
                "change": [
                    {
                        "id": task.id,
                        "if_revision": detail(module, task.id).revision,
                        "title": "New routine",
                        "into": new_project.id,
                        "heading_id": heading.id,
                        "start": "today",
                        "deadline": (NOW.date() + timedelta(days=3)).isoformat(),
                        "remind_at": "2026-08-15T09:30:00+00:00",
                        "after": list_anchor.id,
                        "today_after": today_anchor.id,
                        "repeat": {"unit": "week"},
                        "checklist_add": [{"key": "$new", "title": "Added"}],
                        "checklist_change": [
                            {
                                "id": "check:row-b",
                                "title": "Renamed",
                                "status": "completed",
                            }
                        ],
                        "checklist_remove": ["check:row-a"],
                        "checklist_order": [
                            "check:row-c",
                            "$new",
                            "check:row-b",
                        ],
                    }
                ],
            }
        )
    )

    assert prepared.status == "needs_approval"
    assert prepared.plan is not None
    applied = module.approve(ApproveCall(plan_id=prepared.plan.id))
    assert applied.status == "applied"
    template = next(
        item
        for item in module._library.records.values()  # noqa: SLF001
        if item.recurrence.role == "template"
    )
    assert {item.id for item in applied.items} == {task.id, template.id}
    assert task.uuid == "repeat-batched"
    for record in (task, template):
        assert record.title == "New routine"
        assert record.parent_uuid == new_project.uuid
        assert record.heading_uuid == heading.uuid
        assert record.start == NOW.date()
        assert record.deadline == NOW.date() + timedelta(days=3)
        assert record.remind == "09:30"
        assert record.sort_index > list_anchor.sort_index
        assert record.today_index > today_anchor.today_index
    assert [(row.uuid, row.title, row.status) for row in task.checklists] == [
        ("row-c", "Keep", "done"),
        (task.checklists[1].uuid, "Added", "open"),
        ("row-b", "Renamed", "done"),
    ]
    assert [row.title for row in template.checklists] == [
        "Keep",
        "Added",
        "Renamed",
    ]
    assert [row.status for row in template.checklists] == ["open", "open", "open"]


@pytest.mark.parametrize(
    ("fields", "expected_inbox", "expected_someday"),
    [
        ({"into": "anytime"}, False, False),
        ({"into": "inbox"}, True, False),
        ({"start": "someday"}, False, True),
        ({"start": None}, False, False),
    ],
)
def test_repeat_conversion_projects_schedule_clearing_semantics(
    fields: dict[str, object], expected_inbox: bool, expected_someday: bool
) -> None:
    task = Record(
        uuid="scheduled-repeat",
        kind="task",
        title="Scheduled routine",
        start=NOW.date(),
        remind="09:30",
    )
    module = workspace([task])
    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": f"repeat-schedule-{next(iter(fields))}-001",
                "change": [
                    {
                        "id": task.id,
                        "if_revision": detail(module, task.id).revision,
                        "repeat": {"unit": "week"},
                        **fields,
                    }
                ],
            }
        )
    )

    assert prepared.status == "needs_approval"
    assert prepared.plan is not None
    assert module.approve(ApproveCall(plan_id=prepared.plan.id)).status == "applied"
    template = next(
        item
        for item in module._library.records.values()  # noqa: SLF001
        if item.recurrence.role == "template"
    )
    for record in (task, template):
        assert record.start is None
        assert record.remind is None
        assert record.inbox is expected_inbox
        assert record.someday is expected_someday


@pytest.mark.parametrize("replacement", [False, True])
def test_repeat_conversion_resolves_heading_when_moving_projects(
    replacement: bool,
) -> None:
    old_project = Record(uuid="heading-old-project", kind="project", title="Old")
    new_project = Record(uuid="heading-new-project", kind="project", title="New")
    old_heading = Record(
        uuid="heading-old",
        kind="task",
        title="Old section",
        parent_uuid=old_project.uuid,
        heading=True,
    )
    new_heading = Record(
        uuid="heading-new",
        kind="task",
        title="New section",
        parent_uuid=new_project.uuid,
        heading=True,
    )
    task = Record(
        uuid="heading-move-task",
        kind="task",
        title="Move routine",
        parent_uuid=old_project.uuid,
        heading_uuid=old_heading.uuid,
    )
    module = workspace([old_project, new_project, old_heading, new_heading, task])
    change: dict[str, object] = {
        "id": task.id,
        "if_revision": detail(module, task.id).revision,
        "repeat": {"unit": "week"},
        "into": new_project.id,
    }
    if replacement:
        change["heading_id"] = new_heading.id

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": f"repeat-heading-move-{replacement}-001",
                "change": [change],
            }
        )
    )

    assert prepared.status == "needs_approval"
    assert prepared.plan is not None
    assert module.approve(ApproveCall(plan_id=prepared.plan.id)).status == "applied"
    template = next(
        item
        for item in module._library.records.values()  # noqa: SLF001
        if item.recurrence.role == "template"
    )
    expected_heading = new_heading.uuid if replacement else None
    for record in (task, template):
        assert record.parent_uuid == new_project.uuid
        assert record.heading_uuid == expected_heading


def test_repeat_conversion_chains_checklist_after_edits_for_both_copies() -> None:
    task = Record(
        uuid="repeat-check-after",
        kind="task",
        title="Routine",
        checklists=[
            ChecklistLine("after-a", "A", sort_index=0),
            ChecklistLine("after-b", "B", sort_index=1024),
            ChecklistLine("after-c", "C", sort_index=2048),
        ],
    )
    module = workspace([task])
    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "repeat-check-after-001",
                "change": [
                    {
                        "id": task.id,
                        "if_revision": detail(module, task.id).revision,
                        "repeat": {"unit": "week"},
                        "checklist_change": [
                            {"id": "check:after-b", "after": "check:after-c"},
                            {"id": "check:after-a", "after": "check:after-b"},
                        ],
                    }
                ],
            }
        )
    )

    assert prepared.status == "needs_approval"
    assert prepared.plan is not None
    assert module.approve(ApproveCall(plan_id=prepared.plan.id)).status == "applied"
    template = next(
        item
        for item in module._library.records.values()  # noqa: SLF001
        if item.recurrence.role == "template"
    )
    assert [row.title for row in task.checklists] == ["C", "B", "A"]
    assert [row.uuid for row in task.checklists] == ["after-c", "after-b", "after-a"]
    assert [row.title for row in template.checklists] == ["C", "B", "A"]


def test_template_and_current_copy_metadata_change_in_one_approved_batch() -> None:
    template = Record(
        uuid="future-template",
        kind="task",
        title="Old routine",
        recurrence=RecurrenceState(
            role="template",
            repeat_type="fixed",
            rule={"tp": 0, "fu": 8, "fa": 1, "of": []},
        ),
    )
    current_copy = Record(
        uuid="current-copy",
        kind="task",
        title="Old routine",
        recurrence=RecurrenceState(
            role="instance",
            repeat_type="fixed",
            template_uuid=template.uuid,
            links=(template.uuid,),
        ),
    )
    module = workspace([template, current_copy])

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "repeat-future-and-current-001",
                "change": [
                    {
                        "id": template.id,
                        "if_revision": detail(module, template.id).revision,
                        "title": "Future routine",
                        "notes_markdown": "Use for later cycles.",
                    },
                    {
                        "id": current_copy.id,
                        "if_revision": detail(module, current_copy.id).revision,
                        "title": "Current routine",
                    },
                ],
            }
        )
    )

    assert prepared.status == "needs_approval"
    assert prepared.plan is not None
    applied = module.approve(ApproveCall(plan_id=prepared.plan.id))
    assert applied.status == "applied"
    assert template.title == "Future routine"
    assert template.notes == "Use for later cycles."
    assert current_copy.title == "Current routine"
    assert template.recurrence.interval == 1


def test_repeat_rule_and_template_metadata_change_in_one_plan() -> None:
    template = Record(
        uuid="repeat-metadata-template",
        kind="task",
        title="Old routine",
        recurrence=RecurrenceState(
            role="template",
            repeat_type="fixed",
            rule={"tp": 0, "fu": 8, "fa": 1, "of": []},
        ),
    )
    module = workspace([template])
    current = detail(module, template.id)

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "repeat-metadata-change-001",
                "change": [
                    {
                        "id": template.id,
                        "if_revision": current.revision,
                        "title": "New routine",
                        "notes_markdown": "Use the new checklist.",
                        "repeat": {"interval": 2},
                    }
                ],
            }
        )
    )

    assert prepared.status == "needs_approval"
    assert prepared.plan is not None
    applied = module.approve(ApproveCall(plan_id=prepared.plan.id))
    assert applied.status == "applied"
    assert template.title == "New routine"
    assert template.notes == "Use the new checklist."
    assert template.recurrence.interval == 2


def test_generated_task_completion_is_an_ordinary_exact_change() -> None:
    template = Record(
        uuid="completion-template",
        kind="task",
        title="Routine",
        recurrence=RecurrenceState(
            role="template",
            repeat_type="after_completion",
            rule={"tp": 1, "fu": 8, "fa": 1},
        ),
    )
    instance = Record(
        uuid="completion-instance",
        kind="task",
        title="Routine",
        recurrence=RecurrenceState(
            role="instance",
            repeat_type="after_completion",
            template_uuid=template.uuid,
            links=(template.uuid,),
        ),
    )
    module = workspace([template, instance])
    current = detail(module, instance.id)

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "repeat-instance-complete-001",
                "change": [
                    {
                        "id": instance.id,
                        "if_revision": current.revision,
                        "status": "completed",
                    }
                ],
            }
        )
    )

    assert result.status == "applied"
    assert instance.status == "done"


def test_trashed_project_can_purge_its_tree_in_one_approved_plan() -> None:
    project = Record(
        uuid="purge-project",
        kind="project",
        title="Old launch",
        trashed=True,
    )
    heading = Record(
        uuid="purge-heading",
        kind="task",
        title="Old group",
        parent_uuid=project.uuid,
        heading=True,
    )
    task = Record(
        uuid="purge-child",
        kind="task",
        title="Old action",
        parent_uuid=project.uuid,
        heading_uuid=heading.uuid,
        checklists=[ChecklistLine("purge-row", "Old row")],
    )
    module = workspace([project, heading, task])
    current = detail(module, project.id)

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "project-tree-purge-001",
                "change": [
                    {
                        "id": project.id,
                        "if_revision": current.revision,
                        "lifecycle": "delete_permanently",
                        "delete_contents": True,
                    }
                ],
            }
        )
    )

    assert prepared.status == "needs_approval"
    assert prepared.plan is not None
    assert any("2 contained records" in warning for warning in prepared.plan.warnings)
    applied = module.approve(ApproveCall(plan_id=prepared.plan.id))
    assert applied.status == "applied"
    assert module._library.records == {}  # noqa: SLF001


def test_project_purge_walks_nested_imported_descendants_deepest_first() -> None:
    project = Record(uuid="tree-root", kind="project", title="Root", trashed=True)
    nested = Record(
        uuid="tree-nested",
        kind="project",
        title="Nested",
        parent_uuid=project.uuid,
    )
    task = Record(
        uuid="tree-leaf",
        kind="task",
        title="Leaf",
        parent_uuid=nested.uuid,
        checklists=[ChecklistLine("tree-row", "Leaf row")],
    )
    module = workspace([project, nested, task])
    current = detail(module, project.id)

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "project-nested-purge-001",
                "change": [
                    {
                        "id": project.id,
                        "if_revision": current.revision,
                        "lifecycle": "delete_permanently",
                        "delete_contents": True,
                    }
                ],
            }
        )
    )

    assert prepared.plan is not None
    applied = module.approve(ApproveCall(plan_id=prepared.plan.id))
    assert applied.status == "applied"
    assert module._library.records == {}  # noqa: SLF001


def test_risky_intent_is_reserved_only_as_needing_approval() -> None:
    class RecordingJournal(MemoryJournal):
        def __init__(self) -> None:
            super().__init__()
            self.reserved_states: list[str] = []

        def reserve(self, record):  # type: ignore[no-untyped-def]
            self.reserved_states.append(record.state)
            return super().reserve(record)

    journal = RecordingJournal()
    module = ThingsWorkspace(MemoryLibrary(), journal=journal, clock=lambda: NOW)
    scope_revision = system_scope(module)

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "area-safe-stage-001",
                "scope_revision": scope_revision,
                "create": [{"kind": "area", "title": "Health"}],
            }
        )
    )

    assert result.status == "needs_approval"
    assert journal.reserved_states == ["needs_approval"]
    stored = journal.get("area-safe-stage-001")
    assert stored is not None and stored.state == "needs_approval"


def test_pending_approve_readback_stops_after_retry_cap() -> None:
    class MissingReadback(MemoryLibrary):
        def apply(self, writes):  # type: ignore[no-untyped-def]
            return ApplyResult(verified=[], created={})

    library = MissingReadback()
    journal = MemoryJournal()
    module = ThingsWorkspace(library, journal=journal, clock=lambda: NOW)
    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "area-pending-cap-001",
                "scope_revision": system_scope(module),
                "create": [{"kind": "area", "title": "Health"}],
            }
        )
    )
    assert prepared.plan is not None
    call = ApproveCall(plan_id=prepared.plan.id)

    pending = [module.approve(call) for _ in range(3)]
    stopped = module.approve(call)
    repeated = module.approve(call)

    assert [result.status for result in pending] == ["pending", "pending", "pending"]
    assert [result.next for result in pending] == [
        "retry_same",
        "retry_same",
        "retry_same",
    ]
    assert stopped.status == "unavailable"
    assert stopped.next == "stop"
    assert "do not retry" in stopped.instruction.casefold()
    assert repeated.next == "stop"
    assert repeated.status == "unavailable"
    stored = journal.get("area-pending-cap-001")
    assert stored is not None and stored.state == "pending"
    assert stored.plan.get("pending_attempts") == 5


def test_pending_commit_readback_stops_after_retry_cap() -> None:
    class MissingReadback(MemoryLibrary):
        def apply(self, writes):  # type: ignore[no-untyped-def]
            return ApplyResult(verified=[], created={})

    library = MissingReadback()
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)
    call = CommitCall.model_validate(
        {"intent_id": "task-pending-cap-001", "create": [{"title": "Only once"}]}
    )

    pending = [module.commit(call) for _ in range(3)]
    stopped = module.commit(call)

    assert [result.status for result in pending] == ["pending", "pending", "pending"]
    assert [result.next for result in pending] == [
        "retry_same",
        "retry_same",
        "retry_same",
    ]
    assert stopped.status == "unavailable"
    assert stopped.next == "stop"
    assert library.records == {}


def test_pending_approve_can_still_settle_after_the_retry_cap() -> None:
    class MissingReadback(MemoryLibrary):
        writes: list = []

        def apply(self, writes):  # type: ignore[no-untyped-def]
            self.writes = list(writes)
            return ApplyResult(verified=[], created={})

    library = MissingReadback()
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)
    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "area-pending-late-001",
                "scope_revision": system_scope(module),
                "create": [{"kind": "area", "title": "Health"}],
            }
        )
    )
    assert prepared.plan is not None
    call = ApproveCall(plan_id=prepared.plan.id)
    for _ in range(3):
        assert module.approve(call).next == "retry_same"
    assert module.approve(call).next == "stop"
    MemoryLibrary.apply(library, library.writes)

    settled = module.approve(call)

    assert settled.status == "applied"
    assert settled.next == "done"
    assert any(item.title == "Health" for item in library.records.values())


def test_pending_approval_returns_the_plan_id_for_retry() -> None:
    class UnknownOutcome(MemoryLibrary):
        attempts = 0

        def apply(self, writes):  # type: ignore[no-untyped-def]
            self.attempts += 1
            raise CloudError("Cloud outcome is unknown")

    library = UnknownOutcome()
    journal = MemoryJournal()
    module = ThingsWorkspace(library, journal=journal, clock=lambda: NOW)
    scope_revision = system_scope(module)
    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "area-pending-001",
                "scope_revision": scope_revision,
                "create": [{"kind": "area", "title": "Health"}],
            }
        )
    )
    assert prepared.plan is not None

    pending = module.approve(ApproveCall(plan_id=prepared.plan.id))

    assert pending.status == "pending"
    assert pending.receipt == prepared.plan.id
    stored = journal.get("area-pending-001")
    assert stored is not None and stored.state == "pending"


def test_new_area_plan_stales_when_the_registry_changes() -> None:
    library = MemoryLibrary()
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)
    scope_revision = system_scope(module)
    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "area-registry-race-001",
                "scope_revision": scope_revision,
                "create": [{"kind": "area", "title": "Health"}],
            }
        )
    )
    assert prepared.plan is not None
    library.records["other"] = Record(uuid="other", kind="area", title="Other")

    result = module.approve(ApproveCall(plan_id=prepared.plan.id))

    assert result.status == "stale"
    assert all(item.title != "Health" for item in library.records.values())


def test_risky_plan_stales_when_the_tag_catalog_changes() -> None:
    library = MemoryLibrary()
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)
    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "area-tag-race-001",
                "scope_revision": system_scope(module),
                "ensure_tags": [{"key": "$focus", "title": "Focus"}],
                "create": [{"kind": "area", "title": "Health", "tag_ids": ["$focus"]}],
            }
        )
    )
    assert prepared.plan is not None
    library.tags["new"] = "New"

    result = module.approve(ApproveCall(plan_id=prepared.plan.id))

    assert result.status == "stale"
    assert all(item.title != "Health" for item in library.records.values())


def test_risky_reorder_plan_stales_when_an_unmentioned_sibling_changes() -> None:
    anchor = Record(uuid="anchor", kind="project", title="Anchor", sort_index=0)
    target = Record(uuid="target", kind="project", title="Target", sort_index=1024)
    sibling = Record(uuid="sibling", kind="project", title="Sibling", sort_index=2048)
    child = Record(uuid="child", kind="task", title="Open", parent_uuid=target.uuid)
    module = workspace([anchor, target, sibling, child])
    current = detail(module, target.id)
    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "project-order-race-001",
                "change": [
                    {
                        "id": target.id,
                        "if_revision": current.revision,
                        "status": "completed",
                        "after": anchor.id,
                    }
                ],
            }
        )
    )
    assert prepared.plan is not None
    sibling.sort_index = 4096

    result = module.approve(ApproveCall(plan_id=prepared.plan.id))

    assert result.status == "stale"
    assert target.status == "open"


def test_risky_project_plan_stales_when_its_open_actions_change() -> None:
    project = Record(uuid="project", kind="project", title="Launch")
    library = MemoryLibrary(
        [project, Record(uuid="one", kind="task", title="One", parent_uuid="project")]
    )
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)
    current = detail(module, project.id)
    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "project-scope-race-001",
                "change": [
                    {
                        "id": project.id,
                        "if_revision": current.revision,
                        "status": "completed",
                    }
                ],
            }
        )
    )
    assert prepared.plan is not None
    library.records["two"] = Record(
        uuid="two", kind="task", title="Two", parent_uuid="project"
    )

    result = module.approve(ApproveCall(plan_id=prepared.plan.id))

    assert result.status == "stale"
    assert project.status == "open"


def test_crash_after_pending_claim_never_reposts() -> None:
    class CrashesDuringApply(MemoryLibrary):
        attempts = 0

        def apply(self, writes):  # type: ignore[no-untyped-def]
            self.attempts += 1
            raise RuntimeError("process stopped during Cloud input/output")

    library = CrashesDuringApply()
    journal = MemoryJournal()
    call = CommitCall.model_validate(
        {"intent_id": "crash-safe-001", "create": [{"title": "Only once"}]}
    )
    first = ThingsWorkspace(library, journal=journal, clock=lambda: NOW)

    with pytest.raises(RuntimeError, match="process stopped"):
        first.commit(call)

    stored = journal.get(call.intent_id)
    assert stored is not None and stored.state == "pending"
    resumed = ThingsWorkspace(library, journal=journal, clock=lambda: NOW).commit(call)
    assert resumed.status == "pending"
    assert resumed.receipt == call.intent_id
    assert library.attempts == 1


def test_definite_cloud_failure_restores_approval_state() -> None:
    class RejectsWrite(MemoryLibrary):
        def apply(self, writes):  # type: ignore[no-untyped-def]
            raise CloudError("Things Cloud HTTP 503")

    journal = MemoryJournal()
    module = ThingsWorkspace(RejectsWrite(), journal=journal, clock=lambda: NOW)
    scope_revision = system_scope(module)
    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "area-cloud-retry-001",
                "scope_revision": scope_revision,
                "create": [{"kind": "area", "title": "Health"}],
            }
        )
    )
    assert prepared.plan is not None

    result = module.approve(ApproveCall(plan_id=prepared.plan.id))

    assert result.status == "unavailable"
    assert result.receipt == prepared.plan.id
    stored = journal.get("area-cloud-retry-001")
    assert stored is not None and stored.state == "needs_approval"


def test_invalid_write_does_not_leave_a_pending_intent() -> None:
    class RejectsWrite(MemoryLibrary):
        def apply(self, writes):  # type: ignore[no-untyped-def]
            raise ValueError("invalid write")

    journal = MemoryJournal()
    module = ThingsWorkspace(RejectsWrite(), journal=journal, clock=lambda: NOW)
    call = CommitCall.model_validate(
        {"intent_id": "invalid-write-001", "create": [{"title": "Draft"}]}
    )

    result = module.commit(call)

    assert result.status == "rejected"
    stored = journal.get(call.intent_id)
    assert stored is not None and stored.state == "prepared"


def test_start_null_clears_ordinary_someday_without_moving_home() -> None:
    project = Record(uuid="launch", kind="project", title="Launch")
    heading = Record(
        uuid="next",
        kind="task",
        title="Next",
        heading=True,
        parent_uuid=project.uuid,
    )
    task = Record(
        uuid="later",
        kind="task",
        title="Later",
        someday=True,
        parent_uuid=project.uuid,
        heading_uuid=heading.uuid,
        sort_index=2048,
    )
    module = workspace([project, heading, task])

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "clear-someday-001",
                "change": [
                    {
                        "id": task.id,
                        "if_revision": detail(module, task.id).revision,
                        "start": None,
                    }
                ],
            }
        )
    )

    assert result.status == "applied"
    assert task.someday is False
    assert task.start is None
    assert task.tonight is False
    assert task.remind is None
    assert task.parent_uuid == project.uuid
    assert task.heading_uuid == heading.uuid
    fresh = detail(module, task.id)
    assert fresh.start is None
    assert "someday" not in fresh.signals


def test_today_after_accepts_a_sibling_moved_to_today_in_the_same_batch() -> None:
    first = Record(uuid="one", kind="task", title="One", inbox=True)
    second = Record(uuid="two", kind="task", title="Two", inbox=True)
    module = workspace([first, second])

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "today-batch-order-001",
                "change": [
                    {
                        "id": first.id,
                        "if_revision": detail(module, first.id).revision,
                        "start": "today",
                    },
                    {
                        "id": second.id,
                        "if_revision": detail(module, second.id).revision,
                        "start": "today",
                        "today_after": first.id,
                    },
                ],
            }
        )
    )

    assert result.status == "applied"
    assert first.start == NOW.date()
    assert second.start == NOW.date()
    assert first.today_index < second.today_index


def test_same_home_inbox_repair_does_not_stale_a_sibling_project_batch() -> None:
    project = Record(uuid="launch", kind="project", title="Launch")
    first = Record(
        uuid="a",
        kind="task",
        title="A",
        inbox=True,
        parent_uuid=project.uuid,
    )
    second = Record(
        uuid="b",
        kind="task",
        title="B",
        inbox=True,
        parent_uuid=project.uuid,
    )
    module = workspace([project, first, second])
    first_rev = detail(module, first.id).revision
    second_rev = detail(module, second.id).revision

    first_result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "repair-inbox-a",
                "change": [
                    {
                        "id": first.id,
                        "if_revision": first_rev,
                        "into": project.id,
                    }
                ],
            }
        )
    )
    assert first_result.status == "applied"

    second_result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "repair-inbox-b",
                "change": [
                    {
                        "id": second.id,
                        "if_revision": second_rev,
                        "into": project.id,
                    }
                ],
            }
        )
    )
    assert second_result.status == "applied"
    assert first.inbox is False
    assert second.inbox is False
    assert first.parent_uuid == project.uuid
    assert second.parent_uuid == project.uuid


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
    )
    trashed = Record(uuid="old", kind="task", title="Old", trashed=True)
    module = workspace([area, project, task, trashed])

    result = module.read(ReadCall(view="audit", limit=40))

    assert result.status == "ok"
    assert [item.id for item in result.items] == [area.id, project.id, task.id]
    assert "has_notes" in result.items[2].signals


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
    assert result.truncated is False


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
    payload = result.model_dump(
        mode="json", exclude_none=True, exclude_defaults=True
    )
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
    payload = result.model_dump(
        mode="json", exclude_none=True, exclude_defaults=True
    )
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
    payload = result.model_dump(
        mode="json", exclude_none=True, exclude_defaults=True
    )
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


def test_start_null_with_remind_at_is_rejected_before_a_write() -> None:
    task = Record(uuid="later", kind="task", title="Later", someday=True)
    scheduled = Record(
        uuid="dated",
        kind="task",
        title="Dated",
        start=NOW.date(),
        remind="09:00",
    )
    module = workspace([task, scheduled])
    before = (task.someday, task.start, task.remind, scheduled.start, scheduled.remind)

    with pytest.raises(Exception, match="start=null cannot combine with remind_at"):
        CommitCall.model_validate(
            {
                "intent_id": "clear-and-remind-002",
                "change": [
                    {
                        "id": task.id,
                        "if_revision": detail(module, task.id).revision,
                        "start": None,
                        "remind_at": "2026-08-20T09:00:00+00:00",
                    }
                ],
            }
        )
    with pytest.raises(Exception, match="start=null cannot combine with remind_at"):
        CommitCall.model_validate(
            {
                "intent_id": "clear-and-remind-003",
                "change": [
                    {
                        "id": scheduled.id,
                        "if_revision": detail(module, scheduled.id).revision,
                        "start": None,
                        "remind_at": "2026-08-20T09:00:00+00:00",
                    }
                ],
            }
        )
    assert (task.someday, task.start, task.remind, scheduled.start, scheduled.remind) == (
        True,
        None,
        None,
        NOW.date(),
        "09:00",
    )
    assert before == (task.someday, task.start, task.remind, scheduled.start, scheduled.remind)


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
    assert item.recurrence is not None
    assert item.recurrence.linked_item_ids == []


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


def test_approval_plan_includes_grouped_summary_and_id_sections() -> None:
    project = Record(uuid="launch", kind="project", title="Launch")
    first = Record(uuid="one", kind="task", title="One", parent_uuid=project.uuid)
    second = Record(uuid="two", kind="task", title="Two", parent_uuid=project.uuid)
    module = workspace([project, first, second])

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "broad-trash-001",
                "change": [
                    {
                        "id": first.id,
                        "if_revision": detail(module, first.id).revision,
                        "lifecycle": "trash",
                    },
                    {
                        "id": second.id,
                        "if_revision": detail(module, second.id).revision,
                        "lifecycle": "trash",
                    },
                ],
            }
        )
    )

    assert result.status == "needs_approval"
    assert result.plan is not None
    assert any("Trash" in line for line in result.plan.summary)
    assert result.sections
    trashed = next(section for section in result.sections if section.key == "trash")
    assert set(trashed.item_ids) == {first.id, second.id}


def test_approval_plan_groups_source_to_destination_moves() -> None:
    home = Record(uuid="home", kind="area", title="Home")
    project = Record(
        uuid="kitchen",
        kind="project",
        title="Kitchen",
        area_uuid=home.uuid,
    )
    task = Record(uuid="milk", kind="task", title="Buy milk", inbox=True)
    extra = Record(uuid="old", kind="task", title="Old draft", inbox=True)
    module = workspace([home, project, task, extra])

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "move-manifest-001",
                "change": [
                    {
                        "id": task.id,
                        "if_revision": detail(module, task.id).revision,
                        "into": project.id,
                    },
                    {
                        "id": extra.id,
                        "if_revision": detail(module, extra.id).revision,
                        "lifecycle": "trash",
                    },
                ],
            }
        )
    )

    assert result.status == "needs_approval"
    assert result.plan is not None
    assert any("Inbox → project:kitchen" in line for line in result.plan.summary)
    assert any(
        task.id in section.item_ids and "Inbox" in section.title
        for section in result.sections
    )


def test_approval_plan_groups_anytime_destination() -> None:
    task = Record(
        uuid="later",
        kind="task",
        title="Later",
        parent_uuid="launch",
    )
    extra = Record(uuid="old", kind="task", title="Old draft", inbox=True)
    module = workspace(
        [
            Record(uuid="launch", kind="project", title="Launch"),
            task,
            extra,
        ]
    )

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "anytime-manifest-001",
                "change": [
                    {
                        "id": task.id,
                        "if_revision": detail(module, task.id).revision,
                        "into": "anytime",
                    },
                    {
                        "id": extra.id,
                        "if_revision": detail(module, extra.id).revision,
                        "lifecycle": "trash",
                    },
                ],
            }
        )
    )

    assert result.status == "needs_approval"
    assert result.plan is not None
    assert any("project:launch → Anytime" in line for line in result.plan.summary)


def test_project_restore_returns_the_trashed_subtree() -> None:
    project = Record(uuid="done-launch", kind="project", title="Launch", trashed=True)
    heading = Record(
        uuid="done-heading",
        kind="task",
        title="Next",
        parent_uuid=project.uuid,
        heading=True,
        trashed=True,
    )
    child = Record(
        uuid="done-child",
        kind="task",
        title="Ship",
        parent_uuid=project.uuid,
        heading_uuid=heading.uuid,
        trashed=True,
    )
    module = workspace([project, heading, child])

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "restore-tree-001",
                "change": [
                    {
                        "id": project.id,
                        "if_revision": detail(module, project.id).revision,
                        "lifecycle": "restore",
                    }
                ],
            }
        )
    )
    assert prepared.status == "needs_approval"
    assert prepared.plan is not None
    applied = module.approve(ApproveCall(plan_id=prepared.plan.id))
    assert applied.status == "applied"
    assert project.trashed is False
    assert heading.trashed is False
    assert child.trashed is False
    assert child.parent_uuid == project.uuid
    assert child.heading_uuid == heading.uuid


def test_heading_trash_is_recoverable() -> None:
    project = Record(uuid="headed", kind="project", title="Launch")
    heading = Record(
        uuid="group",
        kind="task",
        title="Next",
        parent_uuid=project.uuid,
        heading=True,
    )
    module = workspace([project, heading])

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "heading-trash-001",
                "change": [
                    {
                        "id": heading.id,
                        "if_revision": detail(module, heading.id).revision,
                        "lifecycle": "trash",
                    }
                ],
            }
        )
    )
    assert prepared.status == "needs_approval"
    assert prepared.plan is not None
    applied = module.approve(ApproveCall(plan_id=prepared.plan.id))
    assert applied.status == "applied"
    assert heading.trashed is True
    assert heading.parent_uuid == project.uuid


def test_template_trash_is_allowed() -> None:
    template = Record(
        uuid="repeat-template",
        kind="task",
        title="Water plants",
        recurrence=RecurrenceState(role="template", repeat_type="fixed"),
    )
    module = workspace([template])

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "template-trash-001",
                "change": [
                    {
                        "id": template.id,
                        "if_revision": detail(module, template.id).revision,
                        "lifecycle": "trash",
                    }
                ],
            }
        )
    )
    assert prepared.status == "needs_approval"
    assert prepared.plan is not None
    applied = module.approve(ApproveCall(plan_id=prepared.plan.id))
    assert applied.status == "applied"
    assert template.trashed is True


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


def test_tag_delete_plan_uses_tag_ids() -> None:
    task = Record(uuid="tagged", kind="task", title="Ship", tag_uuids=["focus"])
    library = MemoryLibrary([task])
    library.tags["focus"] = "Focus"
    module = ThingsWorkspace(library, journal=MemoryJournal(), clock=lambda: NOW)
    tags = module.read(ReadCall(view="tags"))
    assert tags.scope_revision is not None

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "tag-delete-plan-001",
                "tags_revision": tags.scope_revision,
                "change_tags": [{"id": "tag:focus", "delete_permanently": True}],
            }
        )
    )

    assert result.status == "needs_approval"
    assert result.plan is not None
    assert any("Permanently delete tag: Focus" in line for line in result.plan.summary)
    assert all(
        not item_id.startswith("task:")
        for section in result.sections
        for item_id in section.item_ids
    )


def test_applied_receipt_echoes_placement() -> None:
    project = Record(uuid="home", kind="project", title="Home")
    task = Record(uuid="milk", kind="task", title="Buy milk", inbox=True)
    module = workspace([project, task])

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "receipt-placement-001",
                "change": [
                    {
                        "id": task.id,
                        "if_revision": detail(module, task.id).revision,
                        "into": project.id,
                        "start": "today",
                    }
                ],
            }
        )
    )

    assert result.status == "applied"
    assert result.items
    fact = result.items[0]
    assert fact.into_id == project.id
    assert fact.start == NOW.date().isoformat()
    assert "today" in fact.signals
