"""One deep workspace Module behind the three model tools."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields, replace
from datetime import date, datetime, time, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from typing import Callable, cast

from .cloud import CloudError
from .interface import (
    ApproveCall,
    ChecklistFact,
    CommitCall,
    ItemFact,
    PlanFact,
    ReadCall,
    RecurrenceFact,
    RecurrenceKind,
    Result,
    ReviewSection,
    TagFact,
)
from .interface import (
    Status as PublicStatus,
)
from .journal import IntentRecord, IntentState, Journal, JsonDict, MemoryJournal
from .library import (
    ChecklistLine,
    Kind,
    MemoryLibrary,
    Record,
    Status,
    Write,
    new_uuid,
    parse_id,
)

_READ_LIMIT = 40
_NOTES_LIMIT = 50_000
_TITLE_LIMIT = 1000
_ORDER_MIN = -(2**63)
_ORDER_MAX = 2**63 - 1
_PLAN_MINUTES = 30


@dataclass(frozen=True)
class _Prepared:
    writes: list[Write]
    preconditions: dict[str, str]
    summary: list[str]
    warnings: list[str]
    risky: bool


@dataclass(frozen=True)
class _ItemCursor:
    ids: list[str]
    offset: int
    snapshot_revision: str
    public_scope_revision: str
    full: bool
    view: str | None
    expires_at: datetime


@dataclass(frozen=True)
class _TagCursor:
    rows: list[TagFact]
    offset: int
    revision: str
    expires_at: datetime


@dataclass(frozen=True)
class _DetailCursor:
    item_id: str
    row_offset: int
    note_offset: int
    revision: str
    expires_at: datetime


class _Abort(Exception):
    def __init__(self, result: Result) -> None:
        super().__init__(result.instruction)
        self.result = result


class ThingsWorkspace:
    """Model and test Interface for one owner's Things library."""

    def __init__(
        self,
        library: MemoryLibrary,
        *,
        journal: Journal | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._library = library
        self._journal = journal or MemoryJournal()
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._cursors: dict[str, _ItemCursor] = {}
        self._tag_cursors: dict[str, _TagCursor] = {}
        self._detail_cursors: dict[str, _DetailCursor] = {}

    def read(self, call: ReadCall) -> Result:
        failed = self._refresh()
        if failed is not None:
            return failed
        if call.cursor is not None:
            return self._continue(call.cursor, call.limit)

        view = call.view or "today"
        if call.id is not None:
            item = self._exact_item(call.id)
            if item is None:
                return self._needs_input("I could not find that exact item. Read or search again.")
            return self._detail_page(
                item,
                row_offset=0,
                note_offset=0,
                limit=call.limit,
            )

        if call.find is not None:
            within = self._exact_item(call.within) if call.within else None
            if call.within and within is None:
                return self._needs_input("I could not find that exact search scope.")
            if within is not None and within.kind not in {"area", "project"}:
                return self._needs_input("Search within an exact Area or Project.")
            matches = self._search(call.find, within)
            return self._page(
                matches,
                call.limit,
                full=False,
                instruction="Use an exact ID for a change.",
            )

        if view == "tags":
            rows = [
                TagFact(id=f"tag:{uuid}", title=_bounded_tag_title(title))
                for uuid, title in sorted(
                    self._library.tags.items(), key=lambda row: row[1].casefold()
                )
                if title.strip()
            ]
            return self._tag_page(rows, offset=0, limit=call.limit)

        visible = self._view_items(call)
        if isinstance(visible, Result):
            return visible
        return self._page(
            visible,
            call.limit,
            full=False,
            instruction="Use this review as current evidence.",
            view=view,
            public_scope=(self._area_scope_revision() if view == "system" else None),
        )

    def commit(self, call: CommitCall) -> Result:
        fingerprint = _fingerprint(call.model_dump(mode="json", by_alias=True))
        stored = self._journal.get(call.intent_id)
        if stored is not None:
            if stored.fingerprint != fingerprint:
                return self._rejected("That intent_id already belongs to different work.")
            return self._resume(stored, allow_apply=stored.state == "prepared")

        failed = self._refresh(force=True)
        if failed is not None:
            return failed
        try:
            prepared = self._prepare(call)
        except _Abort as error:
            return error.result

        plan = self._plan_payload(prepared)
        record = IntentRecord(
            intent_id=call.intent_id,
            fingerprint=fingerprint,
            state="prepared",
            plan=plan,
        )
        if prepared.risky:
            return self._stage(record, prepared)
        claimed = self._journal.reserve(record)
        if claimed != record:
            if claimed.fingerprint != fingerprint:
                return self._rejected(
                    "That intent_id already belongs to different work."
                )
            return self._resume(claimed, allow_apply=claimed.state == "prepared")
        return self._apply(claimed)

    def approve(self, call: ApproveCall) -> Result:
        stored = self._journal.get_by_plan_id(call.plan_id)
        if stored is None:
            return self._rejected("That plan does not exist or is no longer available.")
        if stored.state in {"applied", "unchanged", "stale"} and stored.result is not None:
            return Result.model_validate(stored.result)
        if stored.state == "pending":
            return self._resume(stored, allow_apply=False)
        if stored.state != "needs_approval":
            return self._rejected("That plan cannot be approved.")
        if stored.expires_at is None or datetime.fromisoformat(stored.expires_at) <= self._clock():
            result = self._stale("That plan expired. Read current facts and prepare it again.")
            self._save_result(stored, "stale", result)
            return result
        failed = self._refresh(force=True)
        if failed is not None:
            return failed
        if self._preconditions_changed(stored.plan):
            result = self._stale("Relevant Things data changed. Read it and prepare a new plan.")
            self._save_result(stored, "stale", result)
            return result
        return self._apply(stored)

    def _view_items(self, call: ReadCall) -> list[Record] | Result:
        view = call.view or "today"
        today = self._clock().date()
        if view == "today":
            return self._library.today(waiting_tag=self._library.waiting_tag(), today=today)
        if view == "inbox":
            return self._library.inbox(limit=10_000)
        if view == "week":
            return self._library.week(today=today, limit=10_000)
        if view == "system":
            return self._library.system()
        if view == "project":
            assert call.within is not None
            project = self._exact_item(call.within)
            if project is None or project.kind != "project":
                return self._needs_input("I could not find that exact Project.")
            return self._library.project(project.id)
        if view == "logbook":
            assert call.from_date is not None and call.to_date is not None
            start = date.fromisoformat(call.from_date)
            end = date.fromisoformat(call.to_date)
            return sorted(
                [
                    item
                    for item in self._library.records.values()
                    if item.status != "open"
                    and not item.trashed
                    and item.completed_at is not None
                    and start
                    <= item.completed_at.astimezone(self._clock().tzinfo).date()
                    <= end
                ],
                key=lambda item: (item.completed_at, item.sort_index),
                reverse=True,
            )
        return []

    def _search(self, text: str, within: Record | None) -> list[Record]:
        needle = text.casefold()
        items = [
            item
            for item in self._library.records.values()
            if item.is_open()
            and (
                needle in item.title.casefold()
                or needle in item.notes.casefold()
                or any(needle in row.title.casefold() for row in item.checklists)
            )
        ]
        if within is not None:
            if within.kind == "area":
                projects = {
                    item.uuid
                    for item in self._library.records.values()
                    if item.kind == "project" and item.area_uuid == within.uuid
                }
                items = [
                    item
                    for item in items
                    if item.uuid == within.uuid
                    or item.area_uuid == within.uuid
                    or item.parent_uuid in projects
                ]
            elif within.kind == "project":
                items = [item for item in items if item.uuid == within.uuid or item.parent_uuid == within.uuid]
        return sorted(items, key=lambda item: (item.sort_index, item.title))

    def _page(
        self,
        items: list[Record],
        limit: int,
        *,
        full: bool,
        instruction: str,
        view: str | None = None,
        public_scope: str | None = None,
    ) -> Result:
        limit = min(limit, _READ_LIMIT)
        facts = [self._fact(item, full=full) for item in items[:limit]]
        snapshot = self._scope_revision(items)
        scope = public_scope or snapshot
        cursor = None
        if len(items) > limit:
            cursor = self._encode_cursor(
                [item.id for item in items],
                limit,
                snapshot,
                scope,
                full,
                view,
            )
        sections = self._sections(view, facts) if view is not None else []
        return Result(
            next="done",
            status="ok",
            instruction=instruction if facts else "No matching work is visible.",
            items=facts,
            sections=sections,
            scope_revision=scope,
            cursor=cursor,
            truncated=cursor is not None,
        )

    def _continue(self, cursor: str, limit: int) -> Result:
        detail_saved = self._detail_cursors.get(cursor)
        if detail_saved is not None:
            item = self._exact_item(detail_saved.item_id)
            if (
                detail_saved.expires_at <= self._clock()
                or item is None
                or self._detail_revision(item) != detail_saved.revision
            ):
                return self._stale("That item changed. Read it again.")
            return self._detail_page(
                item,
                row_offset=detail_saved.row_offset,
                note_offset=detail_saved.note_offset,
                limit=limit,
                expected_revision=detail_saved.revision,
            )
        tag_saved = self._tag_cursors.get(cursor)
        if tag_saved is not None:
            if (
                tag_saved.expires_at <= self._clock()
                or self._tag_revision() != tag_saved.revision
            ):
                return self._stale("That tag result changed. Start the read again.")
            return self._tag_page(
                tag_saved.rows, offset=tag_saved.offset, limit=limit
            )
        saved = self._cursors.get(cursor)
        if saved is None:
            return self._stale("That cursor is invalid. Start the read again.")
        if saved.expires_at <= self._clock():
            return self._stale("That cursor expired. Start the read again.")
        items = [
            item
            for value in saved.ids
            if (item := self._exact_item(value)) is not None
        ]
        if (
            len(items) != len(saved.ids)
            or self._scope_revision(items) != saved.snapshot_revision
            or (
                saved.view == "system"
                and self._area_scope_revision() != saved.public_scope_revision
            )
        ):
            return self._stale("That result changed. Start the read again.")
        page_items = items[saved.offset : saved.offset + limit]
        next_offset = saved.offset + len(page_items)
        next_cursor = (
            self._encode_cursor(
                saved.ids,
                next_offset,
                saved.snapshot_revision,
                saved.public_scope_revision,
                saved.full,
                saved.view,
            )
            if next_offset < len(items)
            else None
        )
        facts = [self._fact(item, full=saved.full) for item in page_items]
        sections = self._sections(saved.view, facts) if saved.view is not None else []
        return Result(
            next="done",
            status="ok",
            instruction="Continue with these current facts.",
            items=facts,
            sections=sections,
            scope_revision=saved.public_scope_revision,
            cursor=next_cursor,
            truncated=next_cursor is not None,
        )

    def _encode_cursor(
        self,
        ids: list[str],
        offset: int,
        snapshot_revision: str,
        public_scope_revision: str,
        full: bool,
        view: str | None,
    ) -> str:
        self._prune_cursors()
        token = f"cursor_{token_urlsafe(18)}"
        self._cursors[token] = _ItemCursor(
            ids=ids,
            offset=offset,
            snapshot_revision=snapshot_revision,
            public_scope_revision=public_scope_revision,
            full=full,
            view=view,
            expires_at=self._clock() + timedelta(minutes=10),
        )
        return token

    def _tag_page(self, rows: list[TagFact], *, offset: int, limit: int) -> Result:
        limit = min(limit, _READ_LIMIT)
        expected = self._tag_revision()
        page_rows = rows[offset : offset + limit]
        next_offset = offset + len(page_rows)
        cursor = None
        if next_offset < len(rows):
            self._prune_cursors()
            cursor = f"cursor_{token_urlsafe(18)}"
            self._tag_cursors[cursor] = _TagCursor(
                rows=rows,
                offset=next_offset,
                revision=expected,
                expires_at=self._clock() + timedelta(minutes=10),
            )
        return Result(
            next="done",
            status="ok",
            instruction="Use exact tag IDs for changes.",
            tags=page_rows,
            truncated=cursor is not None,
            cursor=cursor,
            scope_revision=expected,
        )

    def _detail_page(
        self,
        item: Record,
        *,
        row_offset: int,
        note_offset: int,
        limit: int,
        expected_revision: str | None = None,
    ) -> Result:
        revision = self._detail_revision(item)
        if expected_revision is not None and revision != expected_revision:
            return self._stale("That item changed. Read it again.")
        checklist, direct, inherited = self._detail_lists(item)
        page_start = row_offset
        page_end = row_offset + min(limit, _READ_LIMIT)

        checklist_start = 0
        direct_start = len(checklist)
        inherited_start = direct_start + len(direct)
        total = inherited_start + len(inherited)

        def bounds(group_start: int, length: int) -> slice:
            start = max(page_start - group_start, 0)
            end = max(min(page_end - group_start, length), 0)
            return slice(start, end)

        checklist_page = checklist[bounds(checklist_start, len(checklist))]
        direct_page = direct[bounds(direct_start, len(direct))]
        inherited_page = inherited[bounds(inherited_start, len(inherited))]
        next_row_offset = min(page_end, total)
        include_notes = note_offset < len(item.notes) or (
            note_offset == 0 and not item.notes
        )
        next_note_offset = (
            (
                min(note_offset + _NOTES_LIMIT, len(item.notes))
                if item.notes
                else 1
            )
            if include_notes
            else note_offset
        )
        notes_remaining = next_note_offset < len(item.notes)
        rows_remaining = next_row_offset < total
        next_cursor = None
        if notes_remaining or rows_remaining:
            self._prune_cursors()
            next_cursor = f"cursor_{token_urlsafe(18)}"
            self._detail_cursors[next_cursor] = _DetailCursor(
                item_id=item.id,
                row_offset=next_row_offset,
                note_offset=next_note_offset,
                revision=revision,
                expires_at=self._clock() + timedelta(minutes=10),
            )
        return Result(
            next="done",
            status="ok",
            instruction=(
                "Use this note chunk and continue the exact item."
                if notes_remaining
                else "Use these current facts."
                if row_offset == 0 and note_offset == 0
                else "Continue this exact item."
            ),
            items=[
                self._fact(
                    item,
                    full=True,
                    include_notes=include_notes,
                    note_offset=note_offset,
                    checklist=checklist_page,
                    direct_tags=direct_page,
                    inherited_tags=inherited_page,
                    checklist_truncated=next_row_offset < len(checklist),
                    tags_truncated=bool(direct or inherited)
                    and next_row_offset < total,
                    notes_truncated=notes_remaining,
                )
            ],
            scope_revision=revision,
            cursor=next_cursor,
            truncated=next_cursor is not None,
        )

    def _prune_cursors(self) -> None:
        now = self._clock()
        self._cursors = {
            key: value
            for key, value in self._cursors.items()
            if value.expires_at > now
        }
        self._tag_cursors = {
            key: value
            for key, value in self._tag_cursors.items()
            if value.expires_at > now
        }
        self._detail_cursors = {
            key: value
            for key, value in self._detail_cursors.items()
            if value.expires_at > now
        }
        while (
            len(self._cursors) + len(self._tag_cursors) + len(self._detail_cursors)
            >= 100
        ):
            if self._cursors:
                del self._cursors[next(iter(self._cursors))]
            elif self._tag_cursors:
                del self._tag_cursors[next(iter(self._tag_cursors))]
            elif self._detail_cursors:
                del self._detail_cursors[next(iter(self._detail_cursors))]

    def _sections(self, view: str, items: list[ItemFact]) -> list[ReviewSection]:
        if not items:
            return []
        if view == "today":
            groups = [
                ("overdue", "Overdue"),
                ("today", "Today"),
                ("evening", "Evening"),
                ("waiting", "Waiting"),
                ("inbox", "Inbox"),
            ]
            sections = []
            used: set[str] = set()
            for signal, title in groups:
                selected = [item for item in items if signal in item.signals and item.id not in used]
                if selected:
                    sections.append(
                        ReviewSection(
                            key=signal,
                            title=title,
                            item_ids=[item.id for item in selected],
                        )
                    )
                    used.update(item.id for item in selected)
            return sections
        if view == "system":
            return [
                ReviewSection(
                    key="system",
                    title="Areas and Projects",
                    item_ids=[item.id for item in items],
                )
            ]
        return [
            ReviewSection(
                key=view,
                title=view.title(),
                item_ids=[item.id for item in items],
            )
        ]

    def _detail_lists(
        self, item: Record
    ) -> tuple[list[ChecklistFact], list[TagFact], list[TagFact]]:
        checklist = [
            ChecklistFact(
                id=f"check:{row.uuid}",
                revision=self._checklist_revision(row),
                title=_bounded_title(row.title),
                status=_public_status(row.status),
                order=_bounded_order(row.sort_index),
            )
            for row in sorted(
                item.checklists, key=lambda row: (row.sort_index, row.uuid)
            )
        ]
        direct = [
            TagFact(
                id=f"tag:{uuid}",
                title=_bounded_tag_title(self._library.tags[uuid]),
            )
            for uuid in item.tag_uuids
            if uuid in self._library.tags
        ]
        inherited: list[TagFact] = []
        for source in self._tag_sources(item):
            for uuid in source.tag_uuids:
                if uuid in self._library.tags and all(
                    tag.id != f"tag:{uuid}" for tag in inherited
                ):
                    inherited.append(
                        TagFact(
                            id=f"tag:{uuid}",
                            title=_bounded_tag_title(self._library.tags[uuid]),
                            from_id=source.id,
                        )
                    )
        return checklist, direct, inherited

    def _fact(
        self,
        item: Record,
        *,
        full: bool,
        include_notes: bool = True,
        note_offset: int = 0,
        checklist: list[ChecklistFact] | None = None,
        direct_tags: list[TagFact] | None = None,
        inherited_tags: list[TagFact] | None = None,
        checklist_truncated: bool = False,
        tags_truncated: bool = False,
        notes_truncated: bool = False,
    ) -> ItemFact:
        if full and (
            checklist is None or direct_tags is None or inherited_tags is None
        ):
            checklist, direct_tags, inherited_tags = self._detail_lists(item)
        checklist = checklist or []
        direct_tags = direct_tags or []
        inherited_tags = inherited_tags or []
        recurrence = RecurrenceFact(
            kind=self._recurrence_kind(item),
            template_id=(
                f"task:{item.recurrence_template_uuid}"
                if item.recurrence_template_uuid
                else None
            ),
            rule=item.recurrence_type if item.recurrence_type != "none" else None,
            unit=item.recurrence.unit,
            interval=item.recurrence.interval,
        )
        return ItemFact(
            id=item.id,
            revision=self._revision(item),
            kind=item.public_kind,
            title=_bounded_title(item.title),
            status=_public_status(item.status),
            into_id=(
                f"project:{item.parent_uuid}"
                if item.parent_uuid
                else f"area:{item.area_uuid}" if item.area_uuid else None
            ),
            notes_markdown=(
                item.notes[note_offset : note_offset + _NOTES_LIMIT]
                if full and include_notes
                else None
            ),
            checklist=checklist,
            direct_tags=direct_tags,
            inherited_tags=inherited_tags,
            start=item.start.isoformat() if item.start else "someday" if item.someday else None,
            deadline=item.deadline.isoformat() if item.deadline else None,
            remind_at=self._reminder(item),
            recurrence=recurrence,
            order=_bounded_order(item.sort_index),
            today_order=(
                _bounded_order(item.today_index)
                if item.start == self._clock().date() or item.tonight
                else None
            ),
            signals=self._signals(
                item,
                checklist_truncated=checklist_truncated,
                tags_truncated=tags_truncated,
                notes_truncated=notes_truncated,
            ),
        )

    def _tag_sources(self, item: Record) -> list[Record]:
        sources: list[Record] = []
        if item.parent_uuid and (parent := self._library.records.get(item.parent_uuid)):
            sources.append(parent)
            if parent.area_uuid and (area := self._library.records.get(parent.area_uuid)):
                sources.append(area)
        elif item.area_uuid and (area := self._library.records.get(item.area_uuid)):
            sources.append(area)
        return sources

    def _detail_revision(self, item: Record) -> str:
        sources = self._tag_sources(item)
        tag_ids = list(
            dict.fromkeys(
                [
                    *item.tag_uuids,
                    *(uuid for source in sources for uuid in source.tag_uuids),
                ]
            )
        )
        return "s_" + _digest(
            [
                item.id,
                self._revision(item),
                [[source.id, source.tag_uuids] for source in sources],
                [[uuid, self._library.tags.get(uuid)] for uuid in tag_ids],
            ]
        )

    def _signals(
        self,
        item: Record,
        *,
        checklist_truncated: bool,
        tags_truncated: bool,
        notes_truncated: bool,
    ) -> list[str]:
        today = self._clock().date()
        signals: list[str] = []
        waiting = self._library.tag_uuid(self._library.waiting_tag())
        if item.deadline and item.deadline < today:
            signals.append("overdue")
        elif item.deadline == today:
            signals.append("today")
        if item.inbox:
            signals.append("inbox")
        if item.start == today:
            signals.append("today")
        if item.tonight:
            signals.append("evening")
        if waiting and waiting in item.tag_uuids:
            signals.append("waiting")
        if item.someday:
            signals.append("someday")
        if item.recurrence_role != "none" and item.recurrence_type == "unknown":
            signals.append("recurrence_unknown")
        if checklist_truncated:
            signals.append("checklist_truncated")
        if tags_truncated:
            signals.append("tags_truncated")
        if notes_truncated:
            signals.append("notes_truncated")
        return signals

    def _reminder(self, item: Record) -> str | None:
        if item.remind is None or item.start is None:
            return None
        hour, minute = (int(part) for part in item.remind.split(":", 1))
        tz = self._clock().tzinfo
        return datetime.combine(item.start, time(hour, minute), tzinfo=tz).isoformat()

    def _prepare(self, call: CommitCall) -> _Prepared:
        changes_areas = any(entry.kind == "area" for entry in call.create) or any(
            change.id.startswith("area:") for change in call.change
        )
        if changes_areas:
            expected_scope = self._area_scope_revision()
            if call.scope_revision != expected_scope:
                raise _Abort(
                    self._stale(
                        "The Area registry changed. Read the system and use its current scope_revision."
                    )
                )

        local: dict[str, tuple[str, Kind | str]] = {}
        for tag_entry in call.ensure_tags:
            matches = [
                uuid
                for uuid, title in self._library.tags.items()
                if title.casefold() == tag_entry.title.casefold()
            ]
            if len(matches) > 1:
                raise _Abort(
                    self._needs_input(
                        f"Several tags are named {tag_entry.title}. Use an exact tag ID."
                    )
                )
            existing = matches[0] if matches else None
            local[tag_entry.key] = (existing or new_uuid(), "tag")
        for entry in call.create:
            if entry.key:
                local[entry.key] = (new_uuid(), entry.kind)
        for change in call.change:
            for row in change.checklist_add:
                if row.key:
                    local[row.key] = (new_uuid(), "check")

        writes: list[Write] = []
        preconditions: dict[str, str] = {}
        summary: list[str] = []
        warnings: list[str] = []
        risky = False
        uses_tags = bool(call.ensure_tags) or any(
            entry.tag_ids or entry.waiting for entry in call.create
        ) or any(
            change.tags_add
            or change.tags_remove
            or change.waiting is not None
            for change in call.change
        )
        if uses_tags:
            preconditions["scope:tags"] = self._tag_revision()
        for tag_entry in call.ensure_tags:
            uuid = local[tag_entry.key][0]
            writes.append(
                Write(action="ensure_tag", uuid=uuid, title=tag_entry.title)
            )
            summary.append(f"Ensure tag: {tag_entry.title}")
        for entry in call.create:
            uuid = local[entry.key][0] if entry.key else new_uuid()
            if entry.kind == "heading":
                home = self._home(entry.into, "task", local, new_item=True)
                if home[1] != "project" or home[0] is None:
                    raise _Abort(self._rejected("A heading needs a Project."))
                project = self._library.records.get(home[0])
                if project is not None:
                    preconditions[project.id] = self._revision(project)
                    preconditions[f"scope:project:{project.uuid}"] = (
                        self._project_scope_revision(project.uuid)
                    )
                heading_indexes = [
                    item.sort_index
                    for item in self._library.records.values()
                    if item.heading and item.parent_uuid == home[0] and not item.trashed
                ]
                heading_indexes.extend(
                    write.sort_index
                    for write in writes
                    if write.action == "create_heading"
                    and write.into_uuid == home[0]
                    and write.sort_index is not None
                )
                writes.append(
                    Write(
                        action="create_heading",
                        uuid=uuid,
                        kind="task",
                        title=entry.title,
                        into_uuid=home[0],
                        into_kind="project",
                        anytime=True,
                        sort_index=max(heading_indexes, default=-1024) + 1024,
                    )
                )
                summary.append(f"Create heading: {entry.title}")
                continue
            home = self._home(entry.into, entry.kind, local, new_item=True)
            start, someday, tonight, remind = self._schedule_input(
                entry.start,
                entry.remind_at,
                start_present="start" in entry.model_fields_set,
            )
            if entry.into is None and (
                start is not None or someday or tonight
            ):
                home = (None, None, False, False)
            created_heading_uuid: str | None = None
            if entry.heading_id is not None:
                if entry.kind != "task" or home[1] != "project" or home[0] is None:
                    raise _Abort(
                        self._rejected("Only a Task in a Project can use a heading.")
                    )
                if entry.heading_id.startswith("$"):
                    created_heading_uuid = local[entry.heading_id][0]
                    heading_write = next(
                        (
                            write
                            for write in writes
                            if write.action == "create_heading"
                            and write.uuid == created_heading_uuid
                        ),
                        None,
                    )
                    if heading_write is None or heading_write.into_uuid != home[0]:
                        raise _Abort(
                            self._rejected(
                                "The heading must belong to the Task's Project."
                            )
                        )
                else:
                    heading = self._required_exact(entry.heading_id)
                    if not heading.heading or heading.parent_uuid != home[0]:
                        raise _Abort(
                            self._rejected(
                                "The heading must belong to the Task's Project."
                            )
                        )
                    created_heading_uuid = heading.uuid
                    preconditions[heading.id] = self._revision(heading)
            tags = self._tag_ids(entry.tag_ids, local)
            if entry.waiting:
                waiting, tag_write = self._waiting_tag(writes)
                if tag_write:
                    writes.append(tag_write)
                tags = list(dict.fromkeys([*tags, waiting]))
            sort_index = self._after_index(
                entry.after,
                local,
                writes,
                kind=entry.kind,
                home=home,
                present="after" in entry.model_fields_set,
                preconditions=preconditions,
            )
            today_index = self._today_after_index(
                entry.today_after,
                local,
                writes,
                present="today_after" in entry.model_fields_set,
                on_today=start == self._clock().date() or tonight,
                new_item=True,
                preconditions=preconditions,
            )
            writes.append(
                Write(
                    action="create",
                    uuid=uuid,
                    kind=entry.kind,
                    title=entry.title,
                    notes=entry.notes_markdown,
                    into_uuid=home[0],
                    into_kind=home[1],
                    inbox=home[2],
                    anytime=home[3],
                    start=start,
                    deadline=date.fromisoformat(entry.deadline) if entry.deadline else None,
                    remind=remind,
                    tonight=tonight,
                    someday=someday,
                    tag_uuids=tags,
                    heading_uuid=created_heading_uuid,
                    sort_index=sort_index,
                    today_index=today_index,
                    owner_today=self._clock().date(),
                )
            )
            for row_index, title in enumerate(entry.checklist):
                writes.append(
                    Write(
                        action="checklist",
                        uuid=new_uuid(),
                        title=title,
                        checklist_parent_uuid=uuid,
                        checklist_status="open",
                        checklist_index=row_index * 1024,
                    )
                )
            for action_index, title in enumerate(entry.next_actions):
                writes.append(
                    Write(
                        action="create",
                        uuid=new_uuid(),
                        kind="task",
                        title=title,
                        into_uuid=uuid,
                        into_kind="project",
                        anytime=True,
                        sort_index=action_index * 1024,
                    )
                )
            summary.append(f"Create {entry.kind}: {entry.title}")
            if entry.next_actions:
                summary.append(f"Add {len(entry.next_actions)} next actions to {entry.title}")
            risky = risky or entry.kind == "area"
            if entry.kind == "area":
                preconditions["scope:areas"] = self._area_scope_revision()
                warnings.append("The Area registry will change.")

        for change in call.change:
            item = self._required_exact(change.id)
            revision = self._revision(item)
            if revision != change.if_revision:
                raise _Abort(self._stale(f"{item.title} changed. Read it again."))
            preconditions[item.id] = revision
            if item.notes_format == "rich" and "notes_markdown" in change.model_fields_set:
                raise _Abort(self._unsupported("That note contains unsupported rich text."))
            if change.repeat_interval is not None:
                if (
                    item.recurrence_role != "template"
                    or item.recurrence_type not in {"fixed", "after_completion"}
                    or item.recurrence_links
                ):
                    raise _Abort(
                        self._unsupported(
                            "Change the exact repeating template, not a generated copy."
                        )
                    )
                try:
                    recurrence = item.recurrence.change_interval(
                        change.repeat_interval, kind=item.kind
                    )
                except ValueError as error:
                    raise _Abort(self._unsupported(str(error))) from error
                writes.append(
                    Write(
                        action="repeat",
                        uuid=item.uuid,
                        kind="task",
                        recurrence_rule=recurrence.rule,
                    )
                )
                preconditions[f"scope:repeat:{item.uuid}"] = (
                    self._recurrence_scope_revision(item.uuid)
                )
                summary.append(
                    f"Change repeat interval for {item.title} to {change.repeat_interval}"
                )
                warnings.append("This changes future generated Tasks.")
                risky = True
                continue
            if item.recurrence_role != "none":
                raise _Abort(
                    self._unsupported(
                        "This recurring item is read-only because its mutation semantics are not proven safe."
                    )
                )
            if item.heading:
                if change.title is None:
                    raise _Abort(self._rejected("A heading change needs a title."))
                writes.append(
                    Write(action="update", uuid=item.uuid, kind="task", title=change.title)
                )
                summary.append(f"Rename heading: {item.title} to {change.title}")
                continue
            if item.kind == "area" and change.status is not None:
                raise _Abort(self._rejected("Areas do not have a completion state."))
            if item.kind == "area" and "into" in change.model_fields_set:
                raise _Abort(self._rejected("Areas stay in the top-level registry."))
            if item.kind == "area":
                preconditions["scope:areas"] = self._area_scope_revision()

            if change.trash:
                if item.kind not in {"task", "project"}:
                    raise _Abort(self._rejected("Only a Task or Project can move to Trash."))
                writes.append(Write(action="trash", uuid=item.uuid, kind=item.kind))
                summary.append(f"Trash {item.kind}: {item.title}")
                warnings.append(f"{item.title} will move to Trash and can be restored in Things.")
                if item.kind == "project":
                    preconditions[f"scope:project:{item.uuid}"] = (
                        self._project_scope_revision(item.uuid)
                    )
                risky = True
                continue

            home = (None, None, False, False)
            if "into" in change.model_fields_set:
                home = self._home(change.into, item.kind, local, new_item=False)
            start, someday, tonight, remind = self._schedule_input(
                change.start,
                change.remind_at,
                start_present="start" in change.model_fields_set,
            )
            tags = list(item.tag_uuids)
            if change.tags_add or change.tags_remove:
                add = self._tag_ids(change.tags_add, local)
                remove = set(self._tag_ids(change.tags_remove, local))
                overlap = remove.intersection(add)
                if overlap:
                    raise _Abort(
                        self._rejected(
                            "A tag cannot be added and removed in one change."
                        )
                    )
                tags = [uuid for uuid in tags if uuid not in remove]
                tags = list(dict.fromkeys([*tags, *add]))
            if change.waiting is not None:
                if change.waiting:
                    waiting, tag_write = self._waiting_tag(writes)
                    if tag_write:
                        writes.append(tag_write)
                    tags = [uuid for uuid in tags if uuid != waiting]
                    tags.append(waiting)
                else:
                    existing_waiting = self._library.tag_uuid(
                        self._library.waiting_tag()
                    )
                    if existing_waiting is not None:
                        tags = [uuid for uuid in tags if uuid != existing_waiting]
            status = _internal_status(change.status) if change.status else None
            heading_uuid: str | None = None
            clear_heading = False
            if "heading_id" in change.model_fields_set:
                if item.kind != "task" or item.parent_uuid is None:
                    raise _Abort(
                        self._rejected("Only a Task in a Project can use a heading.")
                    )
                clear_heading = change.heading_id is None
                if change.heading_id is not None:
                    heading = self._required_exact(change.heading_id)
                    if not heading.heading or heading.parent_uuid != item.parent_uuid:
                        raise _Abort(
                            self._rejected("The heading must belong to the Task's Project.")
                        )
                    heading_uuid = heading.uuid
                    preconditions[heading.id] = self._revision(heading)
            order_home = (
                home
                if "into" in change.model_fields_set
                else self._record_home(item)
            )
            sort_index = self._after_index(
                change.after,
                local,
                writes,
                kind=item.kind,
                home=order_home,
                present="after" in change.model_fields_set,
                moving_uuid=item.uuid,
                preconditions=preconditions,
            )
            changes_schedule_date = (
                "start" in change.model_fields_set or change.remind_at is not None
            )
            desired_start = start if changes_schedule_date else item.start
            desired_tonight = tonight if changes_schedule_date else item.tonight
            today_index = self._today_after_index(
                change.today_after,
                local,
                writes,
                present="today_after" in change.model_fields_set,
                on_today=(
                    desired_start == self._clock().date() or desired_tonight
                ),
                new_item=False,
                preconditions=preconditions,
                moving_uuid=item.uuid,
            )
            notes = None
            if "notes_markdown" in change.model_fields_set:
                notes = change.notes_markdown or ""
            main_write = Write(
                action="update",
                uuid=item.uuid,
                kind=item.kind,
                title=change.title,
                notes=notes,
                status=status,
                into_uuid=(item.parent_uuid if "heading_id" in change.model_fields_set else home[0]),
                into_kind=("project" if "heading_id" in change.model_fields_set else home[1]),
                inbox=home[2],
                anytime=home[3],
                start=start,
                clear_start="start" in change.model_fields_set and change.start is None,
                deadline=date.fromisoformat(change.deadline) if change.deadline else None,
                clear_deadline="deadline" in change.model_fields_set and change.deadline is None,
                remind=remind,
                clear_remind="remind_at" in change.model_fields_set and change.remind_at is None,
                tonight=tonight,
                someday=someday,
                tag_uuids=tags if (change.tags_add or change.tags_remove or change.waiting is not None) else None,
                sort_index=sort_index,
                today_index=today_index,
                owner_today=self._clock().date(),
                heading_uuid=heading_uuid,
                clear_heading=clear_heading,
            )
            if any(
                field in change.model_fields_set
                for field in {
                    "title", "status", "notes_markdown", "into", "start", "deadline",
                    "remind_at", "waiting", "tags_add", "tags_remove", "after",
                    "today_after",
                    "heading_id",
                }
            ):
                writes.append(main_write)

            checklist_ids: dict[str, str] = {}
            for addition in change.checklist_add:
                uuid = local[addition.key][0] if addition.key else new_uuid()
                checklist_ids[addition.key or f"check:{uuid}"] = uuid
                writes.append(
                    Write(
                        action="checklist",
                        uuid=uuid,
                        title=addition.title,
                        checklist_parent_uuid=item.uuid,
                        checklist_status="open",
                        checklist_index=self._check_after_index(
                            item,
                            addition.after,
                            local,
                            writes,
                            present="after" in addition.model_fields_set,
                        ),
                    )
                )
            known_rows = {row.uuid: row for row in item.checklists}
            for row_change in change.checklist_change:
                uuid = row_change.id.removeprefix("check:")
                if uuid not in known_rows:
                    raise _Abort(
                        self._needs_input(
                            f"Checklist row {row_change.id} is not on {item.title}."
                        )
                    )
                writes.append(
                    Write(
                        action="checklist",
                        uuid=uuid,
                        title=row_change.title,
                        checklist_parent_uuid=item.uuid,
                        checklist_status=(
                            _internal_status(row_change.status)
                            if row_change.status
                            else None
                        ),
                        checklist_index=self._check_after_index(
                            item,
                            row_change.after,
                            local,
                            writes,
                            present="after" in row_change.model_fields_set,
                            moving_uuid=uuid,
                        ),
                    )
                )
            for row_id in change.checklist_remove:
                uuid = row_id.removeprefix("check:")
                if uuid not in known_rows:
                    raise _Abort(self._needs_input(f"Checklist row {row_id} is not on {item.title}."))
                writes.append(Write(action="checklist", uuid=uuid, checklist_remove=True))
            if change.checklist_order is not None:
                remaining = {f"check:{row.uuid}" for row in item.checklists} - set(change.checklist_remove)
                added = {
                    key if key.startswith("$") else f"check:{uuid}"
                    for key, uuid in checklist_ids.items()
                }
                if set(change.checklist_order) != remaining | added:
                    raise _Abort(self._rejected("checklist_order must name every remaining row once."))
                for order, reference in enumerate(change.checklist_order):
                    uuid = local[reference][0] if reference.startswith("$") else reference.removeprefix("check:")
                    if uuid not in known_rows:
                        planned_index = next(
                            (
                                index
                                for index, write in enumerate(writes)
                                if write.action == "checklist"
                                and write.uuid == uuid
                                and not write.checklist_remove
                            ),
                            None,
                        )
                        if planned_index is not None:
                            writes[planned_index] = replace(
                                writes[planned_index], checklist_index=order * 1024
                            )
                            continue
                    writes.append(
                        Write(
                            action="checklist",
                            uuid=uuid,
                            checklist_parent_uuid=item.uuid,
                            checklist_index=order * 1024,
                        )
                    )

            if change.move_contents_to is not None:
                if item.kind != "area":
                    raise _Abort(self._rejected("Only an Area can move its contents as a registry change."))
                target = self._required_exact(change.move_contents_to)
                if target.kind != "area" or target.uuid == item.uuid:
                    raise _Abort(self._rejected("Area contents must move to another exact Area."))
                preconditions[target.id] = self._revision(target)
                children = self._library.children_in_area(item.uuid)
                for child in children:
                    preconditions[child.id] = self._revision(child)
                    writes.append(
                        Write(
                            action="move",
                            uuid=child.uuid,
                            kind=child.kind,
                            into_uuid=target.uuid,
                            into_kind="area",
                        )
                    )
                writes.append(Write(action="delete_area", uuid=item.uuid, kind="area"))
                summary.append(f"Move {len(children)} items from {item.title} to {target.title}")
                summary.append(f"Remove empty Area: {item.title}")
                warnings.append("One Area will be removed.")
                risky = True
            elif change.remove_if_empty:
                if item.kind != "area":
                    raise _Abort(self._rejected("Only an empty Area can be removed."))
                if self._library.children_in_area(item.uuid):
                    raise _Abort(self._needs_input(f"{item.title} still contains work. Choose another Area."))
                writes.append(Write(action="delete_area", uuid=item.uuid, kind="area"))
                summary.append(f"Remove empty Area: {item.title}")
                warnings.append("One Area will be removed.")
                risky = True
            else:
                summary.append(f"Change {item.kind}: {item.title}")
                if item.kind == "area":
                    risky = True
                    warnings.append("The Area registry will change.")
                if item.kind == "project" and change.status in {"completed", "canceled"}:
                    open_children = [
                        child
                        for child in self._library.project(item.id)[1:]
                        if child.status == "open"
                    ]
                    if open_children:
                        risky = True
                        preconditions[
                            f"scope:project:{item.uuid}"
                        ] = self._project_scope_revision(item.uuid)
                        warnings.append(f"{item.title} still has {len(open_children)} open actions.")

        if not writes:
            raise _Abort(self._rejected("The request did not produce a change."))
        if any(write.action == "delete_area" for write in writes):
            expected_scope = self._area_scope_revision()
            if call.scope_revision != expected_scope:
                raise _Abort(self._stale("Read the system and use its current scope_revision."))
            preconditions["scope:areas"] = expected_scope
        if len(writes) > 20 or len(call.change) > 5:
            risky = True
            warnings.append("This is a broad batch change.")
        return _Prepared(
            writes=writes,
            preconditions=preconditions,
            summary=list(dict.fromkeys(summary))[:40],
            warnings=list(dict.fromkeys(warnings))[:40],
            risky=risky,
        )

    def _home(
        self,
        reference: str | None,
        kind: Kind,
        local: dict[str, tuple[str, Kind | str]],
        *,
        new_item: bool,
    ) -> tuple[str | None, Kind | None, bool, bool]:
        if kind == "area" and reference is not None:
            raise _Abort(self._rejected("Areas stay in the top-level registry."))
        if reference == "inbox":
            if kind == "project":
                raise _Abort(self._rejected("Projects cannot enter Inbox."))
            return None, None, True, False
        if reference == "anytime" or (reference is None and kind == "project"):
            return None, None, False, True
        if reference is None:
            return (
                (None, None, kind == "task", False)
                if new_item
                else (None, None, False, True)
            )
        if reference.startswith("$"):
            uuid, target_kind = local[reference]
            if target_kind not in {"project", "area"}:
                raise _Abort(self._rejected("A home must be an Area or Project."))
            target = cast(Kind, target_kind)
        else:
            item = self._required_exact(reference)
            uuid, target = item.uuid, item.kind
        if kind == "project" and target != "area":
            raise _Abort(self._rejected("Projects can enter Areas only."))
        if target not in {"project", "area"}:
            raise _Abort(self._rejected("A home must be an Area or Project."))
        return uuid, target, False, False

    def _tag_ids(
        self,
        values: list[str],
        local: dict[str, tuple[str, Kind | str]],
    ) -> list[str]:
        uuids: list[str] = []
        unknown: list[str] = []
        for value in values:
            if value.startswith("$"):
                resolved = local.get(value)
                if resolved is None or resolved[1] != "tag":
                    unknown.append(value)
                else:
                    uuids.append(resolved[0])
                continue
            uuid = value.removeprefix("tag:")
            if uuid not in self._library.tags:
                unknown.append(value)
            else:
                uuids.append(uuid)
        if unknown:
            raise _Abort(self._needs_input(f"Unknown tag IDs: {', '.join(unknown)}."))
        return uuids

    def _waiting_tag(self, planned: list[Write]) -> tuple[str, Write | None]:
        title = self._library.waiting_tag()
        existing = self._library.tag_uuid(title)
        if existing is not None:
            return existing, None
        pending = next(
            (
                write
                for write in planned
                if write.action == "ensure_tag" and write.title == title
            ),
            None,
        )
        if pending is not None:
            return pending.uuid, None
        uuid = new_uuid()
        return uuid, Write(action="ensure_tag", uuid=uuid, title=title)

    def _start(self, value: str | None) -> tuple[date | None, bool, bool]:
        if value is None:
            return None, False, False
        if value == "today":
            return self._clock().date(), False, False
        if value == "evening":
            return self._clock().date(), False, True
        if value == "someday":
            return None, True, False
        return date.fromisoformat(value), False, False

    def _remind_input(self, value: str | None) -> tuple[date | None, str | None]:
        if value is None:
            return None, None
        parsed = datetime.fromisoformat(value).astimezone(self._clock().tzinfo)
        return parsed.date(), parsed.strftime("%H:%M")

    def _schedule_input(
        self,
        start_value: str | None,
        remind_value: str | None,
        *,
        start_present: bool,
    ) -> tuple[date | None, bool, bool, str | None]:
        start, someday, tonight = self._start(start_value)
        reminder_date, reminder = self._remind_input(remind_value)
        if reminder_date is None:
            return start, someday, tonight, reminder
        if start_present and start_value is not None:
            if someday or start != reminder_date:
                raise _Abort(
                    self._rejected(
                        "start and remind_at must use the same date in the owner's timezone."
                    )
                )
            return start, someday, tonight, reminder
        return reminder_date, False, False, reminder

    def _after_index(
        self,
        reference: str | None,
        local: dict[str, tuple[str, Kind | str]],
        planned: list[Write],
        *,
        kind: Kind,
        home: tuple[str | None, Kind | None, bool, bool],
        present: bool,
        preconditions: dict[str, str],
        moving_uuid: str | None = None,
    ) -> int | None:
        wanted_scope = self._home_scope(kind, home)
        if not present and moving_uuid is not None:
            return None
        preconditions[self._list_scope_key(kind, wanted_scope)] = (
            self._list_scope_revision(kind, wanted_scope)
        )
        indexes = [
            item.sort_index
            for item in self._library.records.values()
            if item.uuid != moving_uuid
            and item.is_open()
            and item.kind == kind
            and self._record_scope(item) == wanted_scope
        ]
        indexes.extend(
            write.sort_index
            for write in planned
            if write.uuid != moving_uuid
            and write.action == "create"
            and write.kind == kind
            and write.sort_index is not None
            and self._write_scope(write) == wanted_scope
        )
        if not present:
            return max(indexes, default=-1024) + 1024 if moving_uuid is None else None
        if reference is None:
            return min(indexes, default=1024) - 1024
        if reference.startswith("$"):
            uuid = local[reference][0]
            previous = next(
                (
                    write
                    for write in reversed(planned)
                    if write.uuid == uuid and write.action == "create"
                ),
                None,
            )
            if previous is None:
                raise _Abort(self._rejected("An after reference must come earlier in the request."))
            if previous.kind != kind or self._write_scope(previous) != wanted_scope:
                raise _Abort(self._rejected("An after reference must be in the same list."))
            anchor_uuid = previous.uuid
            anchor_index = previous.sort_index or 0
        else:
            item = self._required_exact(reference)
            if item.uuid == moving_uuid:
                raise _Abort(self._rejected("An item cannot follow itself."))
            if item.kind != kind or self._record_scope(item) != wanted_scope:
                raise _Abort(self._rejected("An after reference must be in the same list."))
            preconditions[item.id] = self._revision(item)
            anchor_uuid = item.uuid
            anchor_index = item.sort_index
        later = sorted(index for index in indexes if index > anchor_index)
        if not later:
            return anchor_index + 1024
        if later[0] - anchor_index > 1:
            return anchor_index + (later[0] - anchor_index) // 2
        repaired = self._rebalance_scope(
            kind,
            wanted_scope,
            planned,
            preconditions,
            moving_uuid=moving_uuid,
        )
        return repaired[anchor_uuid] + 512

    def _rebalance_scope(
        self,
        kind: Kind,
        scope: tuple[str, str | None],
        planned: list[Write],
        preconditions: dict[str, str],
        *,
        moving_uuid: str | None,
    ) -> dict[str, int]:
        existing = sorted(
            (
                item
                for item in self._library.records.values()
                if item.uuid != moving_uuid
                and item.is_open()
                and item.kind == kind
                and self._record_scope(item) == scope
            ),
            key=lambda item: (item.sort_index, item.uuid),
        )
        planned_rows = sorted(
            (
                (index, write)
                for index, write in enumerate(planned)
                if write.uuid != moving_uuid
                and write.action == "create"
                and write.kind == kind
                and self._write_scope(write) == scope
            ),
            key=lambda pair: (pair[1].sort_index or 0, pair[1].uuid),
        )
        positions: dict[str, int] = {}
        combined = sorted(
            [
                (item.sort_index, item.uuid, "existing", item)
                for item in existing
            ]
            + [
                (write.sort_index or 0, write.uuid, "planned", index)
                for index, write in planned_rows
            ],
            key=lambda row: (row[0], row[1]),
        )
        for order, (_old, uuid, source, value) in enumerate(combined):
            new_index = order * 1024
            positions[uuid] = new_index
            if source == "existing":
                item = cast(Record, value)
                preconditions[item.id] = self._revision(item)
                if item.sort_index != new_index:
                    planned.append(
                        Write(
                            action="update",
                            uuid=item.uuid,
                            kind=item.kind,
                            sort_index=new_index,
                        )
                    )
            else:
                index = cast(int, value)
                planned[index] = replace(planned[index], sort_index=new_index)
        return positions

    @staticmethod
    def _home_scope(
        kind: Kind, home: tuple[str | None, Kind | None, bool, bool]
    ) -> tuple[str, str | None]:
        into_uuid, into_kind, inbox, _anytime = home
        if kind == "area":
            return "areas", None
        if into_kind == "project":
            return "project", into_uuid
        if into_kind == "area":
            return "area", into_uuid
        if kind == "project":
            return "projects", None
        return ("inbox", None) if inbox else ("root", None)

    @classmethod
    def _write_scope(cls, write: Write) -> tuple[str, str | None]:
        return cls._home_scope(
            write.kind,
            (write.into_uuid, write.into_kind, write.inbox, write.anytime),
        )

    @staticmethod
    def _record_scope(item: Record) -> tuple[str, str | None]:
        if item.kind == "area":
            return "areas", None
        if item.parent_uuid:
            return "project", item.parent_uuid
        if item.area_uuid:
            return "area", item.area_uuid
        if item.kind == "project":
            return "projects", None
        return ("inbox", None) if item.inbox else ("root", None)

    @staticmethod
    def _record_home(
        item: Record,
    ) -> tuple[str | None, Kind | None, bool, bool]:
        if item.parent_uuid:
            return item.parent_uuid, "project", False, False
        if item.area_uuid:
            return item.area_uuid, "area", False, False
        if item.inbox:
            return None, None, True, False
        return None, None, False, True

    def _check_after_index(
        self,
        item: Record,
        reference: str | None,
        local: dict[str, tuple[str, Kind | str]],
        planned: list[Write],
        *,
        present: bool,
        moving_uuid: str | None = None,
    ) -> int | None:
        indexes = [
            row.sort_index for row in item.checklists if row.uuid != moving_uuid
        ]
        indexes.extend(
            write.checklist_index
            for write in planned
            if write.action == "checklist"
            and write.checklist_parent_uuid == item.uuid
            and not write.checklist_remove
            and write.uuid != moving_uuid
            and write.checklist_index is not None
        )
        if not present:
            return (
                max(indexes, default=-1024) + 1024
                if moving_uuid is None
                else None
            )
        if reference is None:
            return min(indexes, default=1024) - 1024
        if reference.startswith("$"):
            uuid = local[reference][0]
            previous = next((write for write in reversed(planned) if write.uuid == uuid), None)
            if previous is None or previous.checklist_parent_uuid != item.uuid:
                raise _Abort(self._rejected("A checklist after reference must come earlier."))
            anchor_uuid = previous.uuid
            anchor_index = previous.checklist_index or 0
        else:
            uuid = reference.removeprefix("check:")
            if uuid == moving_uuid:
                raise _Abort(self._rejected("A checklist row cannot follow itself."))
            row = next((row for row in item.checklists if row.uuid == uuid), None)
            if row is None:
                raise _Abort(
                    self._needs_input(
                        f"Checklist row {reference} is not on {item.title}."
                    )
                )
            anchor_uuid = row.uuid
            anchor_index = row.sort_index
        later = sorted(index for index in indexes if index > anchor_index)
        if not later:
            return anchor_index + 1024
        if later[0] - anchor_index > 1:
            return anchor_index + (later[0] - anchor_index) // 2
        repaired = self._rebalance_checklist(
            item, planned, moving_uuid=moving_uuid
        )
        return repaired[anchor_uuid] + 512

    @staticmethod
    def _rebalance_checklist(
        item: Record,
        planned: list[Write],
        *,
        moving_uuid: str | None,
    ) -> dict[str, int]:
        existing = [
            row for row in item.checklists if row.uuid != moving_uuid
        ]
        planned_rows = [
            (index, write)
            for index, write in enumerate(planned)
            if write.action == "checklist"
            and write.checklist_parent_uuid == item.uuid
            and not write.checklist_remove
            and write.uuid != moving_uuid
        ]
        combined = sorted(
            [(row.sort_index, row.uuid, "existing", row) for row in existing]
            + [
                (write.checklist_index or 0, write.uuid, "planned", index)
                for index, write in planned_rows
            ],
            key=lambda row: (row[0], row[1]),
        )
        positions: dict[str, int] = {}
        for order, (_old, uuid, source, value) in enumerate(combined):
            new_index = order * 1024
            positions[uuid] = new_index
            if source == "existing":
                row = cast(ChecklistLine, value)
                if row.sort_index != new_index:
                    planned.append(
                        Write(
                            action="checklist",
                            uuid=row.uuid,
                            checklist_parent_uuid=item.uuid,
                            checklist_index=new_index,
                        )
                    )
            else:
                index = cast(int, value)
                planned[index] = replace(
                    planned[index], checklist_index=new_index
                )
        return positions

    def _today_after_index(
        self,
        reference: str | None,
        local: dict[str, tuple[str, Kind | str]],
        planned: list[Write],
        *,
        present: bool,
        on_today: bool,
        new_item: bool,
        preconditions: dict[str, str],
        moving_uuid: str | None = None,
    ) -> int | None:
        today = self._clock().date()
        if not present and (not new_item or not on_today):
            return None
        preconditions["scope:today"] = self._today_scope_revision()
        indexes = [
            item.today_index
            for item in self._library.records.values()
            if item.uuid != moving_uuid
            and item.is_open()
            and (item.start == today or item.tonight)
        ]
        indexes.extend(
            write.today_index
            for write in planned
            if write.uuid != moving_uuid
            and write.action == "create"
            and (write.start == today or write.tonight)
            and write.today_index is not None
        )
        if not present:
            return (
                max(indexes, default=-1024) + 1024
                if new_item and on_today
                else None
            )
        if not on_today:
            raise _Abort(
                self._rejected("today_after needs an item scheduled for Today.")
            )
        if reference is None:
            return min(indexes, default=1024) - 1024
        if reference.startswith("$"):
            uuid = local[reference][0]
            previous = next(
                (write for write in reversed(planned) if write.uuid == uuid), None
            )
            if previous is None or not (
                previous.start == today or previous.tonight
            ):
                raise _Abort(
                    self._rejected(
                        "A today_after reference must be earlier and on Today."
                    )
                )
            anchor_uuid = previous.uuid
            anchor_index = previous.today_index or 0
        else:
            item = self._required_exact(reference)
            if item.uuid == moving_uuid:
                raise _Abort(self._rejected("A Today item cannot follow itself."))
            if not item.is_open() or not (
                item.start == today or item.tonight
            ):
                raise _Abort(
                    self._rejected("A today_after reference must be on Today.")
                )
            preconditions[item.id] = self._revision(item)
            anchor_uuid = item.uuid
            anchor_index = item.today_index
        later = sorted(index for index in indexes if index > anchor_index)
        if not later:
            return anchor_index + 1024
        if later[0] - anchor_index > 1:
            return anchor_index + (later[0] - anchor_index) // 2
        repaired = self._rebalance_today(
            planned, preconditions, moving_uuid=moving_uuid
        )
        return repaired[anchor_uuid] + 512

    def _rebalance_today(
        self,
        planned: list[Write],
        preconditions: dict[str, str],
        *,
        moving_uuid: str | None,
    ) -> dict[str, int]:
        today = self._clock().date()
        existing = sorted(
            (
                item
                for item in self._library.records.values()
                if item.uuid != moving_uuid
                and item.is_open()
                and (item.start == today or item.tonight)
            ),
            key=lambda item: (item.today_index, item.uuid),
        )
        planned_rows = sorted(
            (
                (index, write)
                for index, write in enumerate(planned)
                if write.uuid != moving_uuid
                and write.action == "create"
                and (write.start == today or write.tonight)
            ),
            key=lambda pair: (pair[1].today_index or 0, pair[1].uuid),
        )
        combined = sorted(
            [
                (item.today_index, item.uuid, "existing", item)
                for item in existing
            ]
            + [
                (write.today_index or 0, write.uuid, "planned", index)
                for index, write in planned_rows
            ],
            key=lambda row: (row[0], row[1]),
        )
        positions: dict[str, int] = {}
        for order, (_old, uuid, source, value) in enumerate(combined):
            new_index = order * 1024
            positions[uuid] = new_index
            if source == "existing":
                item = cast(Record, value)
                preconditions[item.id] = self._revision(item)
                if item.today_index != new_index:
                    planned.append(
                        Write(
                            action="update",
                            uuid=item.uuid,
                            kind=item.kind,
                            today_index=new_index,
                            owner_today=today,
                        )
                    )
            else:
                index = cast(int, value)
                planned[index] = replace(
                    planned[index], today_index=new_index
                )
        return positions

    def _stage(self, record: IntentRecord, prepared: _Prepared) -> Result:
        expires = self._clock() + timedelta(minutes=_PLAN_MINUTES)
        plan_id = f"plan_{token_urlsafe(12)}"
        result = Result(
            next="approve",
            status="needs_approval",
            instruction=(
                "Ask one short, natural confirmation about the visible change and its "
                "important consequence. Keep plan IDs and control fields private. "
                "Call things_approve only after a clear yes."
            ),
            plan=PlanFact(
                id=plan_id,
                expires_at=expires.isoformat(),
                summary=prepared.summary,
                preserves=["All unmentioned work and fields"],
                warnings=prepared.warnings,
            ),
            receipt=record.intent_id,
        )
        staged = IntentRecord(
            intent_id=record.intent_id,
            fingerprint=record.fingerprint,
            state="needs_approval",
            plan=record.plan,
            plan_id=plan_id,
            expires_at=expires.isoformat(),
            result=_result_json(result),
        )
        claimed = self._journal.reserve(staged)
        if claimed != staged:
            if claimed.fingerprint != record.fingerprint:
                return self._rejected(
                    "That intent_id already belongs to different work."
                )
            return self._resume(claimed, allow_apply=claimed.state == "prepared")
        return result

    def _apply(self, record: IntentRecord) -> Result:
        writes = self._writes_from_plan(record.plan)
        if self._writes_match(writes):
            result = self._settled(record.intent_id, writes, unchanged=True)
            self._save_result(record, "unchanged", result)
            return result
        if self._preconditions_changed(record.plan):
            result = self._stale("Relevant Things data changed. Read it before a new intent.")
            self._save_result(record, "stale", result)
            return result
        pending_record = IntentRecord(
            intent_id=record.intent_id,
            fingerprint=record.fingerprint,
            state="pending",
            plan=record.plan,
            plan_id=record.plan_id,
            expires_at=record.expires_at,
        )
        if not self._journal.transition(pending_record, expected=record.state):
            current = self._journal.get(record.intent_id)
            if current is None:
                return self._rejected("The intent journal lost this receipt.")
            return self._resume(current, allow_apply=False)
        record = pending_record
        retry_state: IntentState = (
            "needs_approval" if record.plan_id is not None else "prepared"
        )
        try:
            self._library.apply(writes)
        except CloudError as error:
            if _outcome_unknown(error):
                result = Result(
                    next="retry_same",
                    status="pending",
                    instruction="The Cloud outcome is not proven. Retry only this same receipt.",
                    receipt=record.plan_id or record.intent_id,
                )
                self._save_result(record, "pending", result)
                return result
            if "conflict" in str(error).casefold() or "HTTP 409" in str(error):
                result = self._stale(
                    "Things changed during the commit. Read fresh facts."
                )
                self._save_result(record, "stale", result)
                return result
            retry_record = replace(record, state=retry_state)
            self._journal.transition(retry_record, expected="pending")
            return Result(
                next="retry_same",
                status="unavailable",
                instruction="Things Cloud did not accept the change. Retry the same receipt.",
                receipt=record.plan_id or record.intent_id,
            )
        except ValueError as error:
            retry_record = replace(record, state=retry_state)
            self._journal.transition(retry_record, expected="pending")
            return self._rejected(str(error))
        failed = self._refresh(force=True)
        if failed is not None or not self._writes_match(writes):
            result = Result(
                next="retry_same",
                status="pending",
                instruction="Cloud accepted the request, but read-back is still pending.",
                receipt=record.plan_id or record.intent_id,
            )
            self._save_result(record, "pending", result)
            return result
        result = self._settled(record.intent_id, writes, unchanged=False)
        self._save_result(record, "applied", result)
        return result

    def _resume(self, record: IntentRecord, *, allow_apply: bool) -> Result:
        if record.state in {"applied", "unchanged", "stale"} and record.result is not None:
            return Result.model_validate(record.result)
        if record.state == "needs_approval" and record.result is not None:
            return Result.model_validate(record.result)
        failed = self._refresh(force=True)
        if failed is not None:
            return failed
        writes = self._writes_from_plan(record.plan)
        if self._writes_match(writes):
            result = self._settled(record.intent_id, writes, unchanged=False)
            self._save_result(record, "applied", result)
            return result
        if allow_apply:
            return self._apply(record)
        return Result(
            next="retry_same",
            status="pending",
            instruction="The Cloud outcome is still unknown. Retry only this same receipt.",
            receipt=record.plan_id or record.intent_id,
        )

    def _settled(self, intent_id: str, writes: list[Write], *, unchanged: bool) -> Result:
        ids = list(dict.fromkeys(write.uuid for write in writes if write.action != "ensure_tag"))
        items = [
            self._fact(item, full=False)
            for uuid in ids
            if (item := self._library.records.get(uuid)) is not None
        ][:40]
        tags: list[TagFact] = []
        for write in writes:
            if write.action != "ensure_tag":
                continue
            title = write.title or ""
            uuid = self._library.tag_uuid(title)
            if uuid is None or any(tag.id == f"tag:{uuid}" for tag in tags):
                continue
            tags.append(
                TagFact(
                    id=f"tag:{uuid}",
                    title=_bounded_tag_title(self._library.tags[uuid]),
                )
            )
        return Result(
            next="done",
            status="unchanged" if unchanged else "applied",
            instruction="The requested state was already true." if unchanged else "Cloud read-back matched the requested state.",
            items=items,
            tags=tags,
            receipt=intent_id,
        )

    def _plan_payload(self, prepared: _Prepared) -> JsonDict:
        return {
            "writes": [_write_json(write) for write in prepared.writes],
            "preconditions": dict(prepared.preconditions),
            "summary": list(prepared.summary),
            "warnings": list(prepared.warnings),
        }

    def _writes_from_plan(self, plan: JsonDict) -> list[Write]:
        raw = cast(list[object], plan.get("writes", []))
        return [_write_from_json(cast(dict[str, object], value)) for value in raw]

    def _preconditions_changed(self, plan: JsonDict) -> bool:
        raw = cast(dict[str, object], plan.get("preconditions", {}))
        for public_id, expected in raw.items():
            if public_id == "scope:areas":
                if self._area_scope_revision() != expected:
                    return True
                continue
            if public_id == "scope:tags":
                if self._tag_revision() != expected:
                    return True
                continue
            if public_id == "scope:today":
                if self._today_scope_revision() != expected:
                    return True
                continue
            if public_id.startswith("scope:list:"):
                encoded_scope = public_id.removeprefix("scope:list:")
                try:
                    kind, scope_name, scope_uuid = json.loads(encoded_scope)
                except (TypeError, ValueError):
                    return True
                if kind not in {"task", "project", "area"}:
                    return True
                scope = (str(scope_name), str(scope_uuid) if scope_uuid else None)
                if self._list_scope_revision(cast(Kind, kind), scope) != expected:
                    return True
                continue
            if public_id.startswith("scope:project:"):
                uuid = public_id.removeprefix("scope:project:")
                if self._project_scope_revision(uuid) != expected:
                    return True
                continue
            if public_id.startswith("scope:repeat:"):
                uuid = public_id.removeprefix("scope:repeat:")
                if self._recurrence_scope_revision(uuid) != expected:
                    return True
                continue
            item = self._exact_item(public_id)
            if item is None or self._revision(item) != expected:
                return True
        return False

    def _writes_match(self, writes: list[Write]) -> bool:
        tag_aliases = {
            write.uuid: actual
            for write in writes
            if write.action == "ensure_tag"
            and (actual := self._library.tag_uuid(write.title or "")) is not None
        }
        normalized = [
            replace(
                write,
                tag_uuids=[tag_aliases.get(uuid, uuid) for uuid in write.tag_uuids],
            )
            if write.tag_uuids is not None
            else write
            for write in writes
        ]
        return all(self._write_matches(write) for write in normalized)

    def _write_matches(self, write: Write) -> bool:
        if write.action == "ensure_tag":
            return self._library.tags.get(write.uuid) == (write.title or "") or self._library.tag_uuid(write.title or "") is not None
        if write.action == "checklist":
            parent, row = self._library._find_checklist(write.uuid)  # noqa: SLF001
            if write.checklist_remove:
                return row is None
            return row is not None and parent is not None and all(
                (
                    write.title is None or row.title == write.title,
                    write.checklist_status is None or row.status == write.checklist_status,
                    write.checklist_parent_uuid is None or parent.uuid == write.checklist_parent_uuid,
                    write.checklist_index is None or row.sort_index == write.checklist_index,
                )
            )
        item = self._library.records.get(write.uuid)
        if write.action == "delete_area":
            return item is None
        if item is None:
            return False
        if write.action == "trash":
            return item.trashed
        if write.action == "repeat":
            return item.recurrence_rule == write.recurrence_rule
        if write.action == "complete":
            return item.status == "done"
        if write.action == "cancel":
            return item.status == "dropped"
        if write.action == "rename_area":
            return item.title == write.title
        if write.action == "tags":
            return item.tag_uuids == (write.tag_uuids or [])
        if write.action == "move":
            return self._placement_matches(item, write)
        checks = [
            write.title is None or item.title == write.title,
            write.notes is None or item.notes == write.notes,
            write.status is None or item.status == write.status,
            write.tag_uuids is None or item.tag_uuids == write.tag_uuids,
            write.deadline is None or item.deadline == write.deadline,
            not write.clear_deadline or item.deadline is None,
            write.start is None or item.start == write.start,
            not write.clear_start or item.start is None,
            write.start is None or item.tonight == write.tonight,
            not write.clear_start or not item.tonight,
            write.remind is None or item.remind == write.remind,
            not write.clear_remind or item.remind is None,
            not write.someday or (item.someday and not item.tonight),
            not write.tonight or item.tonight,
            not write.anytime
            or (
                not item.inbox
                and not item.someday
                and not item.tonight
                and item.start is None
            ),
            write.sort_index is None or item.sort_index == write.sort_index,
            write.today_index is None or item.today_index == write.today_index,
        ]
        if (
            write.action in {"create", "create_heading"}
            or write.into_uuid is not None
            or write.into_kind is not None
            or write.inbox
            or write.anytime
        ):
            checks.append(self._placement_matches(item, write))
        if write.action in {"create", "create_heading"}:
            checks.append(item.kind == write.kind)
        if write.action == "create_heading":
            checks.append(item.heading)
        return all(checks)

    @staticmethod
    def _placement_matches(item: Record, write: Write) -> bool:
        if write.kind == "area":
            return (
                not item.inbox
                and item.parent_uuid is None
                and item.area_uuid is None
            )
        if write.into_kind == "project":
            return (
                item.parent_uuid == write.into_uuid
                and item.area_uuid is None
                and not item.inbox
                and item.heading_uuid == write.heading_uuid
            )
        if write.into_kind == "area":
            return item.area_uuid == write.into_uuid and item.parent_uuid is None and not item.inbox
        if (
            write.kind == "project"
            or write.anytime
            or write.start is not None
            or write.someday
            or write.tonight
        ):
            return not item.inbox and item.parent_uuid is None and item.area_uuid is None
        return item.inbox and item.parent_uuid is None and item.area_uuid is None

    def _save_result(
        self, record: IntentRecord, state: IntentState, result: Result
    ) -> None:
        self._journal.transition(
            IntentRecord(
                intent_id=record.intent_id,
                fingerprint=record.fingerprint,
                state=state,
                plan=record.plan,
                plan_id=record.plan_id,
                expires_at=record.expires_at,
                result=_result_json(result),
            ),
            expected=record.state,
        )

    def _refresh(self, *, force: bool = False) -> Result | None:
        try:
            self._library.refresh(force=force)
        except CloudError:
            return Result(
                next="stop",
                status="unavailable",
                instruction="Things Cloud is unavailable. Do not write from cached data.",
            )
        return None

    def _exact_item(self, value: str | None) -> Record | None:
        if value is None:
            return None
        kind, uuid = parse_id(value)
        if kind is None:
            return None
        item = self._library.records.get(uuid)
        if item is None:
            return None
        return item if item.public_kind == kind else None

    def _required_exact(self, value: str) -> Record:
        item = self._exact_item(value)
        if item is None:
            raise _Abort(self._needs_input(f"I could not find exact item {value}."))
        return item

    def _revision(self, item: Record) -> str:
        payload = {
            "id": item.id,
            "title": item.title,
            "notes": item.notes,
            "notes_source": item.notes_source,
            "notes_format": item.notes_format,
            "status": item.status,
            "completed_at": item.completed_at.isoformat() if item.completed_at else None,
            "trashed": item.trashed,
            "inbox": item.inbox,
            "start": item.start.isoformat() if item.start else None,
            "deadline": item.deadline.isoformat() if item.deadline else None,
            "remind": item.remind,
            "tonight": item.tonight,
            "someday": item.someday,
            "parent": item.parent_uuid,
            "area": item.area_uuid,
            "heading": item.heading_uuid,
            "tags": item.tag_uuids,
            "sort": item.sort_index,
            "today_sort": item.today_index,
            "recurrence": cast(object, asdict(item.recurrence)),
            "checklist": [
                [row.uuid, row.title, row.status, row.sort_index]
                for row in sorted(item.checklists, key=lambda row: (row.sort_index, row.uuid))
            ],
        }
        return "r_" + _digest(payload)

    @staticmethod
    def _checklist_revision(row: ChecklistLine) -> str:
        return "r_" + _digest([row.uuid, row.title, row.status, row.sort_index])

    def _scope_revision(self, items: list[Record]) -> str:
        return "s_" + _digest([[item.id, self._revision(item)] for item in items])

    def _workspace_revision(self) -> str:
        items = sorted(self._library.records.values(), key=lambda item: item.id)
        return self._scope_revision(items)

    def _tag_revision(self) -> str:
        rows = [
            [uuid, title, *self._library.tag_parents.get(uuid, [])]
            for uuid, title in sorted(self._library.tags.items())
        ]
        return "s_" + _digest(rows)

    @staticmethod
    def _list_scope_key(
        kind: Kind, scope: tuple[str, str | None]
    ) -> str:
        return "scope:list:" + json.dumps(
            [kind, scope[0], scope[1]], separators=(",", ":")
        )

    def _list_scope_revision(
        self, kind: Kind, scope: tuple[str, str | None]
    ) -> str:
        items = sorted(
            (
                item
                for item in self._library.records.values()
                if item.is_open()
                and item.kind == kind
                and self._record_scope(item) == scope
            ),
            key=lambda item: item.id,
        )
        return self._scope_revision(items)

    def _today_scope_revision(self) -> str:
        today = self._clock().date()
        items = sorted(
            (
                item
                for item in self._library.records.values()
                if item.is_open() and (item.start == today or item.tonight)
            ),
            key=lambda item: item.id,
        )
        return self._scope_revision(items)

    def _area_scope_revision(self) -> str:
        rows: list[list[str]] = []
        for area in self._library.areas():
            rows.append([area.id, self._revision(area)])
            rows.extend(
                [child.id, self._revision(child)]
                for child in self._library.children_in_area(area.uuid)
            )
        return "s_" + _digest(rows)

    def _project_scope_revision(self, uuid: str) -> str:
        project = self._library.records.get(uuid)
        rows = [] if project is None else [[project.id, self._revision(project)]]
        rows.extend(
            [item.id, self._revision(item)]
            for item in sorted(
                self._library.records.values(), key=lambda item: item.id
            )
            if item.parent_uuid == uuid and not item.trashed
        )
        return "s_" + _digest(rows)

    def _recurrence_scope_revision(self, uuid: str) -> str:
        rows = [
            [item.id, self._revision(item)]
            for item in sorted(
                self._library.records.values(), key=lambda item: item.id
            )
            if item.uuid == uuid or uuid in item.recurrence_links
        ]
        return "s_" + _digest(rows)

    @staticmethod
    def _recurrence_kind(item: Record) -> RecurrenceKind:
        if item.recurrence_role == "template":
            return "template"
        if item.recurrence_role == "instance":
            if item.recurrence_type == "fixed":
                return "fixed_instance"
            if item.recurrence_type == "after_completion":
                return "after_completion_instance"
            return "unknown"
        return "none"

    @staticmethod
    def _needs_input(instruction: str) -> Result:
        return Result(next="ask", status="needs_input", instruction=instruction)

    @staticmethod
    def _stale(instruction: str) -> Result:
        return Result(next="read", status="stale", instruction=instruction)

    @staticmethod
    def _rejected(instruction: str) -> Result:
        return Result(next="stop", status="rejected", instruction=instruction)

    @staticmethod
    def _unsupported(instruction: str) -> Result:
        return Result(next="stop", status="unsupported", instruction=instruction)


def _fingerprint(value: object) -> str:
    return "sha256:" + sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _bounded_tag_title(title: str) -> str:
    return title if len(title) <= 1000 else title[:999] + "…"


def _bounded_title(title: str) -> str:
    return title if len(title) <= _TITLE_LIMIT else title[: _TITLE_LIMIT - 1] + "…"


def _bounded_order(value: int) -> int:
    return max(_ORDER_MIN, min(value, _ORDER_MAX))


def _digest(value: object) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()[:24]


def _public_status(status: Status) -> PublicStatus:
    return "completed" if status == "done" else "canceled" if status == "dropped" else "open"


def _internal_status(status: str | None) -> Status | None:
    if status is None:
        return None
    return "done" if status == "completed" else "dropped" if status == "canceled" else "open"


def _outcome_unknown(error: CloudError) -> bool:
    text = str(error).casefold()
    return any(term in text for term in ("timed out", "unknown", "read-back", "read back"))


def _write_json(write: Write) -> JsonDict:
    payload = cast(JsonDict, asdict(write))
    for name in ("start", "deadline", "owner_today"):
        value = payload.get(name)
        if isinstance(value, date):
            payload[name] = value.isoformat()
    return payload


def _write_from_json(payload: dict[str, object]) -> Write:
    allowed = {field.name for field in fields(Write)}
    values = {key: value for key, value in payload.items() if key in allowed}
    for name in ("start", "deadline", "owner_today"):
        value = values.get(name)
        if isinstance(value, str):
            values[name] = date.fromisoformat(value)
    return Write(**values)  # type: ignore[arg-type]


def _result_json(result: Result) -> JsonDict:
    return cast(JsonDict, result.model_dump(mode="json", exclude_defaults=True, exclude_none=True))
