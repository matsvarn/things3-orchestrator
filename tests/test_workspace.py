from __future__ import annotations

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
        ]
    )

    result = module.read(ReadCall())

    assert result.status == "ok"
    assert [section.key for section in result.sections] == ["overdue", "inbox"]
    assert [item.id for item in result.items] == ["task:late", "task:box"]
    assert [section.item_ids for section in result.sections] == [
        ["task:late"],
        ["task:box"],
    ]
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
        len(tag.title) == 1000
        for tag in [*item.direct_tags, *item.inherited_tags]
    )
    assert item.order == -(2**63)
    assert item.today_order == 2**63 - 1
    assert item.checklist[0].order == 2**63 - 1
    assert "notes_truncated" in item.signals


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
    library.tags = {
        uuid: uuid
        for uuid in [*task.tag_uuids, *parent.tag_uuids]
    }
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
    records = [Record(uuid=f"t{index}", kind="task", title=f"Task {index}", inbox=True) for index in range(45)]
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

    assert [tag.id for tag in first.tags] == [
        f"tag:tag{index}" for index in range(20)
    ]
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
    assert [(tag.id, tag.title) for tag in result.tags] == [
        ("tag:existing", "Focus")
    ]
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
    assert [(tag.id, tag.title) for tag in first.tags] == [
        ("tag:focus", "Focus")
    ]


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
    project = next(item for item in module._library.records.values() if item.kind == "project")  # noqa: SLF001
    task = next(item for item in module._library.records.values() if item.kind == "task")  # noqa: SLF001
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
    project = next(item for item in module._library.records.values() if item.kind == "project")  # noqa: SLF001
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
                "create": [
                    {"kind": "project", "title": "Launch", "start": "today"}
                ],
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
                "create": [
                    {"title": "Call", "start": start, "remind_at": reminder}
                ],
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
                "create": [
                    {"title": "X", "start": "today", "today_after": "task:a"}
                ],
            }
        )
    )

    assert result.status == "applied"
    ordered = sorted(module._library.records.values(), key=lambda item: item.today_index)  # noqa: SLF001
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
                    {"id": "task:one", "if_revision": current.revision, "title": "AI edit"}
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
                    {"id": "area:work", "if_revision": current.revision, "title": "Office"}
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


def test_trash_needs_approval_and_keeps_project_children() -> None:
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
    assert "Trash project: Launch" in prepared.plan.summary
    assert project.trashed is False

    settled = module.approve(ApproveCall(plan_id=prepared.plan.id))

    assert settled.status == "applied"
    assert project.trashed is True
    assert child.parent_uuid == project.uuid


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


def test_heading_create_rename_assignment_and_clear() -> None:
    project = Record(uuid="project", kind="project", title="Launch")
    task = Record(uuid="task", kind="task", title="Ship", parent_uuid=project.uuid)
    module = workspace([project, task])
    created = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "heading-create-001",
                "create": [
                    {"kind": "heading", "title": "Next", "into": project.id}
                ],
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
                    {"id": task.id, "if_revision": task_fact.revision, "heading_id": None}
                ],
            }
        )
    )
    assert cleared.status == "applied"
    assert task.heading_uuid is None
    project_items = module.read(ReadCall(view="project", within=project.id)).items
    assert any(item.kind == "heading" and item.title == "Later" for item in project_items)


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
        item.title != "Ship" for item in module._library.records.values()  # noqa: SLF001
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
def test_recurring_items_are_read_only_until_mutations_are_proven_safe(
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
                    {"id": "task:rich", "if_revision": styled.revision, "notes_markdown": "new"}
                ],
            }
        )
    )

    assert note_result.status == "unsupported"


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
                "create": [
                    {"kind": "area", "title": "Health", "tag_ids": ["$focus"]}
                ],
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
    child = Record(
        uuid="child", kind="task", title="Open", parent_uuid=target.uuid
    )
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
