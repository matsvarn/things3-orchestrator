"""One deep workspace Module behind the three model tools."""

from __future__ import annotations

import json
import re
from base64 import b32encode
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime, time, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from typing import Any, Callable, Literal, cast

from .cloud import CloudError
from .consistency import Conflict, diagnose, item_conflicts
from .context import (
    CompletenessFact,
    ContextConflict,
    ContextCorrupt,
    ContextExpired,
    ContextNotFound,
    ContextRef,
    ContextStore,
    MemoryContextStore,
    ReadContext,
    ReadIncludeSelector,
    ReadSelector,
    UnknownReference,
)
from .contextual import (
    ContextualCommitCompiler,
    ContextualCompileError,
    ContextualInputError,
)
from .interface import (
    DETAIL_FIELDS,
    ApproveCall,
    ChangeEntry,
    ChecklistFact,
    CommitCall,
    ContextFact,
    CreateEntry,
    DiagnosticFact,
    DiagnosticRepair,
    ItemFact,
    LayoutFact,
    LayoutSectionFact,
    PlanFact,
    ReadCall,
    ReceiptItemFact,
    RecoveryFact,
    RecurrenceFact,
    RecurrenceKind,
    Result,
    ResultStatus,
    ReviewSection,
    TagFact,
    TruncatedField,
    View,
    Weekday,
    dump_result,
)
from .interface import (
    Status as PublicStatus,
)
from .journal import (
    IntentRecord,
    IntentState,
    Journal,
    JsonDict,
    MemoryJournal,
    V2Operation,
    V2State,
    v2_manifest_is_valid,
)
from .library import (
    ChecklistLine,
    Kind,
    MemoryLibrary,
    PublicKind,
    Record,
    Status,
    Write,
    new_uuid,
    parse_id,
    public_id,
    template_uuid_of,
)
from .preferences import Preferences, PreferencesError
from .recurrence import RepeatMode, new_rule
from .source_document import (
    SourceDocumentError,
    compile_project_document,
    has_project_meaning,
    has_task_meaning,
    is_source_document,
    is_stripped_source_skeleton,
    prose_chars,
)

_READ_LIMIT = 40
_BULK_TEXT_BUDGET = 100_000
_BULK_WIRE_BUDGET = 256_000
_NOTE_RESERVE = 400
_TAG_REGISTRY_LIMIT = 400
_TRUNCATION_SIGNALS = {
    "notes": "notes_truncated",
    "checklist": "checklist_truncated",
    "tags": "tags_truncated",
    "recurrence": "recurrence_links_truncated",
}
_CONTEXT_LIMIT = 120
_WEEKLY_DEFAULT_LIMIT = 40
_CHANGE_FIND_LIMIT = 40
_NOTES_LIMIT = 50_000
_TITLE_LIMIT = 1000
_ORDER_MIN = -(2**63)
_ORDER_MAX = 2**63 - 1
_PLAN_MINUTES = 30
_PENDING_RETRY_LIMIT = 3
_LOGBOOK_DAYS = 14
_REVIEW_CONTEXT_VIEWS = {
    "today",
    "inbox",
    "week",
    "weekly_review",
    "area",
    "project",
    "audit",
    "logbook",
    "trash",
    "diagnostics",
}
_WEEKDAY_CODES = {
    "sunday": 0,
    "monday": 1,
    "tuesday": 2,
    "wednesday": 3,
    "thursday": 4,
    "friday": 5,
    "saturday": 6,
}
_WEEKDAY_NAMES = {code: name for name, code in _WEEKDAY_CODES.items()}
_SEARCH_ARTICLES = frozenset({"a", "an", "the"})
_SEARCH_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)
_FAKE_ACTION_ROW = re.compile(
    r"(?i)^\s*(decide|think about|work on|figure out|look into|adopt vs|assess)\b"
)
_NON_STARTABLE_ACTION_ROW = re.compile(
    r"(?i)^\s*(audit|consider|decide|explore|figure out|handle|investigate|"
    r"look into|plan|research|think about|work on)\b"
)
_JOINED_FINISHES = re.compile(
    r"(?i)^\s*(?P<first>adopt|audit|build|choose|collect|compare|create|draft|"
    r"extract|gather|install|list|make|mark|match|pin|propose|record|review|run|"
    r"set up|study|summarize|test|verify|write)\b.+?"
    r"(?:\band\b|,|;|&|\+|\bthen\b)\s*(?:then\s+)?"
    r"(?P<second>adopt|assess|compare|create|draft|install|make|mark|pin|propose|"
    r"run|set up|verify|write)\b"
)
_DISTILL_NOTE_CHARS = 800
_DUMP_CREATE_INSTRUCTION = (
    "That create is a dump. Split finishes into separate titles. "
    "Distill each note below 800 characters of prose; keep labeled full URLs. "
    "Chat already has the brief. Write a visible action, not Decide, "
    "Think about, Work on, or Assess."
)


@dataclass(frozen=True)
class _NormalizedSearchText:
    """Keep the old substring search and provide exact token fallback."""

    folded: str
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class _WeeklyReviewSnapshot:
    records: list[Record]
    signals_by_id: dict[str, list[str]]
    sections: list[ReviewSection]
    result_signals: list[str]
    membership_revision: str
    summary_instruction: str = ""


def _undistilled_create(entry: CreateEntry) -> str | None:
    """Return ask-copy when a create is a mashed title, fake action, or pasted brief."""
    notes = [] if has_project_meaning(entry) else [entry.notes_markdown or ""]
    notes.extend(
        task.notes_markdown or ""
        for task in entry.tasks
        if not has_task_meaning(task)
    )
    notes.extend(task.heading_title or "" for task in entry.tasks)
    action_rows = [
        entry.title,
        *entry.checklist,
        *[task.title for task in entry.tasks],
        *[row for task in entry.tasks for row in task.checklist],
    ]
    headings = [(task.heading_title or "") for task in entry.tasks]
    fake = any(_FAKE_ACTION_ROW.match(row) for row in [*action_rows, *headings])
    pasted_brief = entry.document != "source" and any(
        prose_chars(note) > _DISTILL_NOTE_CHARS for note in notes
    )
    mashed = any(_joins_two_finishes(row) for row in action_rows)
    if fake or mashed or pasted_brief:
        return _DUMP_CREATE_INSTRUCTION
    return None


def _joins_two_finishes(row: str) -> bool:
    match = _JOINED_FINISHES.search(row)
    if match is None:
        return False
    return not (
        match.group("first").casefold() == "review"
        and match.group("second").casefold() == "mark"
    )


def _new_checklist_writes(parent_uuid: str, titles: Sequence[str]) -> list[Write]:
    """Create ordered native checklist rows for one new Task."""
    return [
        Write(
            action="checklist",
            uuid=new_uuid(),
            title=title,
            checklist_parent_uuid=parent_uuid,
            checklist_status="open",
            checklist_index=index * 1024,
        )
        for index, title in enumerate(titles)
    ]


def _normalize_search_text(text: str) -> _NormalizedSearchText:
    folded = text.casefold()
    return _NormalizedSearchText(
        folded=folded,
        tokens=tuple(_SEARCH_TOKEN_PATTERN.findall(folded)),
    )


@dataclass(frozen=True)
class _Prepared:
    writes: list[Write]
    preconditions: dict[str, str]
    summary: list[str]
    warnings: list[str]
    risky: bool
    already_correct: list[str] = field(default_factory=list)


@dataclass
class _PreparationContext:
    """Mutable planning state shared by cohesive preparation branches."""

    local: dict[str, tuple[str, Kind | str]]
    writes: list[Write]
    preconditions: dict[str, str]
    summary: list[str]
    warnings: list[str]
    already_correct: list[str] = field(default_factory=list)
    project_heading_moves: dict[str, str] = field(default_factory=dict)
    allow_project_heading_moves: bool = False
    risky: bool = False

    def result(self) -> _Prepared:
        return _Prepared(
            writes=self.writes,
            preconditions=self.preconditions,
            summary=list(dict.fromkeys(self.summary))[:40],
            warnings=list(dict.fromkeys(self.warnings))[:40],
            risky=self.risky,
            already_correct=list(dict.fromkeys(self.already_correct))[:120],
        )


@dataclass
class _Neighborhood:
    """Local records for one change or organize include."""

    records: list[Record] = field(default_factory=list)
    placement_ids: set[str] = field(default_factory=set)
    missing_ids: list[str] = field(default_factory=list)
    include_signals: list[str] = field(default_factory=list)
    include_note: str | None = None

    def add(self, item: Record | None, *, placement: bool = False) -> None:
        if item is None:
            return
        if all(saved.uuid != item.uuid for saved in self.records):
            self.records.append(item)
        if placement:
            self.placement_ids.add(item.uuid)


@dataclass(frozen=True)
class _DesiredItemChange:
    """One projected item state and the write that moves the current item to it."""

    update: Write
    home: tuple[str | None, Kind | None, bool, bool]
    title: str
    notes: str
    start: date | None
    deadline: date | None
    remind: str | None
    tonight: bool
    someday: bool
    tag_uuids: list[str]
    heading_uuid: str | None
    sort_index: int
    today_index: int
    checklist: _ChecklistProjection


@dataclass(frozen=True)
class _ProjectedChecklistRow:
    uuid: str
    title: str
    status: Status
    sort_index: int


@dataclass(frozen=True)
class _ChecklistProjection:
    rows: tuple[_ProjectedChecklistRow, ...]
    removed_uuids: tuple[str, ...]


@dataclass(frozen=True)
class _HeadingOrderRow:
    uuid: str
    sort_index: int
    record: Record | None = None
    create_index: int | None = None


@dataclass(frozen=True)
class _ProjectedListRow:
    uuid: str
    scope: tuple[str, str | None]
    sort_index: int
    record: Record | None = None
    write_index: int | None = None


@dataclass(frozen=True)
class _ItemCursor:
    ids: list[str]
    offset: int
    snapshot_revision: str
    public_scope_revision: str
    full: bool
    view: View | None
    detail: tuple[str, ...]
    expires_at: datetime
    within: str | None = None
    item_id: str | None = None
    from_date: str | None = None
    to_date: str | None = None
    signals_any: tuple[str, ...] = ()
    membership_revision: str | None = None
    context_id: str | None = None


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
        context_store: ContextStore | None = None,
        account_id: str | None = None,
        preferences: Callable[[], Preferences] | None = None,
    ) -> None:
        self._library = library
        self._journal = journal or MemoryJournal()
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._context_store = context_store or MemoryContextStore(
            clock=self._clock,
            token_factory=lambda: token_urlsafe(18),
        )
        self._account_id = account_id or f"workspace:{token_urlsafe(18)}"
        self._preferences = preferences or Preferences
        self._contextual_compiler = ContextualCommitCompiler()
        self._cursors: dict[str, _ItemCursor] = {}
        self._tag_cursors: dict[str, _TagCursor] = {}
        self._detail_cursors: dict[str, _DetailCursor] = {}

    def read(self, call: ReadCall) -> Result:
        failed = self._refresh()
        if failed is not None:
            return failed
        if call.cursor is not None:
            return self._continue(call.cursor, call.limit, view=call.view)

        if call.purpose == "change":
            return self._context_change(call)
        if call.purpose == "organize":
            return self._context_organize(call)
        if call.purpose == "recurrence":
            return self._recurrence_read(call)
        if call.ids:
            return self._bulk_exact(call)

        view = call.view or "today"
        if call.id is not None:
            item = self._exact_item(call.id)
            if item is None:
                return self._needs_input(
                    "I could not find that exact item. Read or search again."
                )
            if (
                call.purpose == "review"
                and item.kind in {"area", "project"}
                and not item.heading
            ):
                if item.kind == "project":
                    return self._writable_project(item, call)
                return self._page(
                    self._library.area(item.id),
                    call.limit,
                    full=False,
                    instruction=(
                        "This Area, its loose tasks, and its Projects. "
                        "Read a Project for its layout and contents."
                    ),
                    view="area",
                    call=call,
                )
            return self._detail_page(
                item,
                row_offset=0,
                note_offset=0,
                limit=call.limit,
            )

        if call.find is not None:
            if call.within == "trash":
                matches = [
                    item
                    for item in self._search(call.find, None, closed=True)
                    if item.trashed
                ]
                return self._page(
                    matches,
                    call.limit,
                    full=False,
                    instruction=(
                        "These Trash matches. Read one to restore or purge."
                        if matches
                        else "No Trash item matched that find."
                    ),
                    view="trash",
                    call=call,
                )
            within = self._exact_item(call.within) if call.within else None
            if call.within and within is None:
                return self._context_recovery(
                    code="context_required",
                    instruction="I could not find that exact search scope. Read it again.",
                    retry="read",
                    read=self._selector_arguments(call),
                    status="needs_input",
                )
            if within is not None and within.kind not in {"area", "project"}:
                return self._needs_input("Search within an exact Area or Project.")
            matches = self._search(call.find, within)
            if matches:
                return self._page(
                    matches,
                    call.limit,
                    full=False,
                    instruction="These matches. Name one to open or stop.",
                    call=call,
                )
            closed = [
                item
                for item in self._search(call.find, within, closed=True)
                if item.trashed or item.status != "open"
            ]
            if closed:
                only_trash = all(item.trashed for item in closed)
                instruction = (
                    "These matches are in Trash. "
                    "Read one to restore or purge, or view=trash."
                    if only_trash
                    else "These matches are not active. "
                    "Read the exact id, or view=trash."
                )
                return self._page(
                    closed,
                    call.limit,
                    full=False,
                    instruction=instruction,
                    call=call,
                )
            return self._page(
                matches,
                call.limit,
                full=False,
                instruction="No match. Try a shorter title token.",
                call=call,
            )

        if view == "tags":
            rows = [
                self._tag_fact(uuid)
                for uuid, title in sorted(
                    self._library.tags.items(), key=lambda row: row[1].casefold()
                )
                if title.strip()
            ]
            return self._tag_page(rows, offset=0, limit=call.limit)

        if view == "diagnostics":
            return self._diagnostics_page(call.limit, call=call)
        if view == "weekly_review":
            return self._weekly_review_page(call)
        if view == "project":
            container = call.within or call.id
            assert container is not None
            project = self._exact_item(container)
            if project is None or project.kind != "project" or project.heading:
                return self._needs_input("I could not find that exact Project.")
            return self._writable_project(project, call)
        visible = self._view_items(call)
        if isinstance(visible, Result):
            return visible
        audit_membership_revision = (
            self._scope_revision(visible) if view == "audit" else None
        )
        if view == "audit":
            visible = self._filter_audit_items(visible, call.signals_any)
        instruction = "Use this review as current evidence."
        if view in {"today", "inbox", "week"}:
            instruction = (
                "Each into_id is the Area or Project home. Titles are on into_title."
            )
        elif view == "system":
            instruction = (
                "This registry is grouped by Area only as prose. "
                "Send this scope_revision only when creating or renaming an Area."
            )
        elif view == "audit":
            instruction = (
                "This audit lists each active item once. Continue the cursor for the rest."
            )
        elif view == "area":
            instruction = (
                "This Area, its loose tasks, and its Projects. "
                "Read a Project for its layout and contents."
            )
        elif view == "trash":
            instruction = "This is Trash. Read an item to restore or purge."
        return self._page(
            visible,
            call.limit,
            full=False,
            instruction=instruction,
            view=view,
            public_scope=(
                self._area_scope_revision() if view in {"system", "audit"} else None
            ),
            membership_revision=audit_membership_revision,
            call=call,
        )

    def read_v2_registry(
        self, *, kind: Literal["project", "area"], limit: int
    ) -> Result:
        """Page one homogeneous v2 registry without leaking mixed internal views."""

        failed = self._refresh()
        if failed is not None:
            return failed
        view: View = "audit" if kind == "project" else "system"
        source = self._library.audit() if kind == "project" else self._library.system()
        visible = [item for item in source if item.public_kind == kind]
        return self._page(
            visible,
            limit,
            full=False,
            instruction=f"Current Things {kind}s.",
            view=view,
            public_scope=self._area_scope_revision(),
            membership_revision=(
                self._scope_revision(source) if view == "audit" else None
            ),
        )

    def _diagnostics_page(
        self,
        limit: int,
        *,
        offset: int = 0,
        expected_ids: list[str] | None = None,
        expected_digest: str | None = None,
        call: ReadCall | None = None,
        existing_context_id: str | None = None,
    ) -> Result:
        conflicts = diagnose(self._library)
        ids = [row.item_id for row in conflicts]
        digest = _diagnostics_digest(
            conflicts, [self._diagnostic_title(row) for row in conflicts]
        )
        if expected_ids is not None and (
            ids != expected_ids or digest != expected_digest
        ):
            return self._stale("That result changed. Start the read again.")
        limit = min(limit, _READ_LIMIT)
        page = conflicts[offset : offset + limit]
        next_offset = offset + len(page)
        cursor = None
        if next_offset < len(conflicts):
            cursor = self._encode_cursor(
                ids,
                next_offset,
                digest,
                digest,
                False,
                "diagnostics",
            )
        diagnostics = [self._diagnostic_fact(row) for row in page]
        records = [
            item
            for row in page
            if (item := self._exact_item(row.item_id)) is not None
        ]
        items = [self._fact(item, full=False) for item in records]
        result = self._follow_cursor(
            Result(
                next="done",
                status="ok",
                instruction=_diagnostics_instruction(diagnostics),
                items=items,
                diagnostics=diagnostics,
                cursor=cursor,
                truncated=cursor is not None,
            )
        )
        return self._bind_review_context(
            result,
            call,
            records,
            existing_context_id=existing_context_id,
        )

    def _weekly_review_page(
        self,
        call: ReadCall,
        *,
        offset: int = 0,
        expected_ids: list[str] | None = None,
        expected_snapshot: str | None = None,
        expected_membership: str | None = None,
        existing_context_id: str | None = None,
    ) -> Result:
        categories = [call.category] if call.category is not None else []
        review = self._weekly_review_snapshot(categories)
        ids = [record.id for record in review.records]
        snapshot = self._scope_revision(review.records)
        if expected_ids is not None and (
            ids != expected_ids
            or snapshot != expected_snapshot
            or review.membership_revision != expected_membership
        ):
            return self._stale("That weekly review changed. Start the read again.")

        limit = min(call.limit, _READ_LIMIT)
        page_records = review.records[offset : offset + limit]
        next_offset = offset + len(page_records)
        cursor = None
        if next_offset < len(review.records):
            cursor = self._encode_cursor(
                ids,
                next_offset,
                snapshot,
                snapshot,
                False,
                "weekly_review",
                signals_any=categories,
                membership_revision=review.membership_revision,
            )
        facts = []
        for record in page_records:
            fact = self._fact(record, full=False)
            facts.append(
                fact.model_copy(
                    update={
                        "signals": list(
                            dict.fromkeys(
                                [*fact.signals, *review.signals_by_id[record.id]]
                            )
                        )
                    }
                )
            )
        sections = review.sections
        if call.category is not None:
            signal = call.category
            section_key = (
                "get_clear"
                if signal == "inbox"
                else "get_creative"
                if signal == "someday"
                else "plan_week"
                if signal == "weekly_candidate"
                else "get_current"
            )
            sections = [section for section in sections if section.key == section_key]
        instruction = (
            "This category returns exact IDs and current revisions. Continue its "
            "cursor when present. For a change, send those exact IDs with the returned "
            "revisions. Do not create a second write context."
            if call.category is not None
            else (
                "This review contains the exceptions and choices for Get Clear, "
                "Get Current, and Get Creative. Ask for uncaptured work and a "
                "past-and-upcoming calendar scan before date changes. Open one "
                "named category only when it needs a decision. Offer weekly planning "
                "only after the review. A start date is a real begin day."
                + (
                    f" {review.summary_instruction}"
                    if review.summary_instruction
                    else ""
                )
            )
        )
        result = self._follow_cursor(
            Result(
                next="done",
                status="ok",
                instruction=instruction,
                items=facts,
                sections=sections if offset == 0 else [],
                signals=review.result_signals if offset == 0 else [],
                cursor=cursor,
                truncated=cursor is not None,
            )
        )
        if call.category is not None:
            return result
        return self._bind_review_context(
            result,
            call,
            page_records,
            existing_context_id=existing_context_id,
        )

    def _weekly_review_snapshot(
        self, signals_any: Sequence[str] = ()
    ) -> _WeeklyReviewSnapshot:
        today = self._clock().date()
        week_end = today + timedelta(days=6)
        audit = self._library.audit()
        waiting_tag = self._library.tag_uuid(self._library.waiting_tag())

        inbox = [item for item in audit if item.inbox]
        stale_starts = [
            item for item in audit if item.start is not None and item.start < today
        ]
        overdue = [
            item for item in audit if item.deadline is not None and item.deadline < today
        ]
        today_items = [
            item
            for item in audit
            if item.start == today or item.deadline == today or item.tonight
        ]
        upcoming = [
            item
            for item in audit
            if (
                item.start is not None and today < item.start <= week_end
            )
            or (
                item.deadline is not None and today < item.deadline <= week_end
            )
        ]
        someday = [item for item in audit if item.someday and not item.heading]
        by_title: dict[tuple[str, str], list[Record]] = {}
        for item in audit:
            if item.kind == "area" or item.heading:
                continue
            key = (item.kind, " ".join(item.title.casefold().split()))
            by_title.setdefault(key, []).append(item)
        possible_duplicates = [
            item
            for group in by_title.values()
            if len(group) > 1
            for item in group
        ]

        active_projects = [
            item
            for item in audit
            if item.kind == "project"
            and not item.heading
            and not item.someday
            and (item.start is None or item.start <= today)
        ]
        direct_tasks: dict[str, list[Record]] = {}
        project_headings: dict[str, list[Record]] = {}
        for item in audit:
            if item.kind == "task" and not item.heading and item.parent_uuid is not None:
                direct_tasks.setdefault(item.parent_uuid, []).append(item)
            elif item.heading and item.parent_uuid is not None:
                project_headings.setdefault(item.parent_uuid, []).append(item)

        def project_tasks_in_native_order(project: Record) -> list[Record]:
            tasks = direct_tasks.get(project.uuid, [])
            ordered: list[Record] = []
            for heading in sorted(
                project_headings.get(project.uuid, []),
                key=lambda item: (item.sort_index, item.uuid),
            ):
                ordered.extend(
                    sorted(
                        (task for task in tasks if task.heading_uuid == heading.uuid),
                        key=lambda item: (item.sort_index, item.uuid),
                    )
                )
            ordered.extend(
                sorted(
                    (task for task in tasks if task.heading_uuid is None),
                    key=lambda item: (item.sort_index, item.uuid),
                )
            )
            return ordered

        def has_waiting_tag(item: Record) -> bool:
            if waiting_tag is None:
                return False
            return waiting_tag in {
                *item.tag_uuids,
                *(tag for source in self._tag_sources(item) for tag in source.tag_uuids),
            }

        waiting = [item for item in audit if has_waiting_tag(item)]

        def is_candidate_action(item: Record) -> bool:
            if item.someday or (item.start is not None and item.start > today):
                return False
            if has_waiting_tag(item):
                return False
            return _NON_STARTABLE_ACTION_ROW.match(item.title) is None

        first_project_tasks = {
            project.uuid: tasks[0] if tasks else None
            for project in active_projects
            for tasks in [project_tasks_in_native_order(project)]
        }
        project_gaps = [
            project
            for project in active_projects
            if (first := first_project_tasks[project.uuid]) is None
            or not is_candidate_action(first)
        ]
        project_review: list[Record] = []
        for project in active_projects:
            project_review.append(first_project_tasks[project.uuid] or project)
        plan_candidates: list[Record] = []
        for project in active_projects:
            first = first_project_tasks[project.uuid]
            if (
                first is not None
                and is_candidate_action(first)
                and first.start is None
                and not first.tonight
            ):
                plan_candidates.append(first)
        plan_candidates.extend(
            item
            for item in audit
            if item.kind == "task"
            and item.parent_uuid is None
            and not item.inbox
            and item.start is None
            and not item.tonight
            and is_candidate_action(item)
        )
        someday_project_ids = {
            item.uuid for item in audit if item.kind == "project" and item.someday
        }
        active_tasks_in_someday = [
            item
            for item in audit
            if item.kind == "task"
            and item.parent_uuid in someday_project_ids
            and not item.someday
        ]

        finished_checklists = [
            item
            for item in audit
            if item.kind == "task"
            and item.checklists
            and all(row.status != "open" for row in item.checklists)
        ]
        completed_since = today - timedelta(days=_LOGBOOK_DAYS - 1)
        recent_completed_projects = [
            item
            for item in self._library.records.values()
            if item.kind == "project"
            and item.status == "done"
            and not item.trashed
            and item.completed_at is not None
            and completed_since
            <= item.completed_at.astimezone(self._clock().tzinfo).date()
            <= today
        ]

        signal_groups: list[tuple[str, list[Record]]] = [
            ("inbox", inbox),
            ("stale_start", stale_starts),
            ("overdue", overdue),
            ("today", today_items),
            ("upcoming", upcoming),
            ("possible_duplicate", possible_duplicates),
            ("waiting", waiting),
            ("project_without_candidate_task", project_gaps),
            ("project_review", project_review),
            ("active_task_in_someday_project", active_tasks_in_someday),
            ("open_task_with_finished_checklist", finished_checklists),
            ("recently_completed_project", recent_completed_projects),
            ("someday", someday),
            ("weekly_candidate", plan_candidates),
        ]
        signal_map: dict[str, list[str]] = {}
        for signal, group in signal_groups:
            for item in group:
                signal_map.setdefault(item.id, []).append(signal)

        selected: list[Record] = []
        seen: set[str] = set()

        def add(group: Sequence[Record], *, count: int, limit: int) -> None:
            added = 0
            if len(selected) >= limit:
                return
            for item in group:
                if item.id in seen:
                    continue
                seen.add(item.id)
                selected.append(item)
                added += 1
                if added >= count or len(selected) >= limit:
                    return

        default_signals = {
            "inbox",
            "stale_start",
            "overdue",
            "today",
            "possible_duplicate",
            "waiting",
            "project_without_candidate_task",
            "active_task_in_someday_project",
            "open_task_with_finished_checklist",
            "upcoming",
        }
        selected_signals = set(signals_any) or default_signals
        result_limit = len(audit) + len(recent_completed_projects)
        if not signals_any:
            result_limit = _WEEKLY_DEFAULT_LIMIT
        selected_groups = [
            group
            for signal, group in signal_groups
            if signal in selected_signals and group
        ]
        if signals_any:
            for group in selected_groups:
                add(group, count=result_limit, limit=result_limit)
                if len(selected) >= result_limit:
                    break
        elif selected_groups:
            quota = max(1, result_limit // len(selected_groups))
            for group in selected_groups:
                add(group, count=quota, limit=result_limit)
            for group in selected_groups:
                add(group, count=result_limit, limit=result_limit)
                if len(selected) >= result_limit:
                    break
        selected_ids = {item.id for item in selected}
        requested_total = len(
            {
                item.id
                for signal, group in signal_groups
                if signal in selected_signals
                for item in group
            }
        )
        summarized = not signals_any and requested_total > len(selected)
        summary_instruction = ""
        if summarized:
            summary_instruction = (
                f"This index shows {len(selected)} of {requested_total} exception "
                "rows. Open one named category; do not repeat the default read."
            )

        def section_ids(groups: Sequence[list[Record]]) -> list[str]:
            values: list[str] = []
            for group in groups:
                for item in group:
                    if item.id in selected_ids and item.id not in values:
                        values.append(item.id)
            return values[:40]

        day_load = []
        for offset in range(7):
            day = today + timedelta(days=offset)
            count = sum(
                1
                for item in audit
                if item.start == day or item.deadline == day
            )
            day_load.append(f"{day.isoformat()}: {count}")

        sections = [
            ReviewSection(
                key="get_clear",
                title="Get Clear",
                item_ids=section_ids([inbox]),
                signals=[
                    f"Inbox: {len(inbox)}.",
                    "Ask for work that is not yet in Things.",
                ],
            ),
            ReviewSection(
                key="get_current",
                title="Get Current",
                item_ids=section_ids(
                    [
                        stale_starts,
                        overdue,
                        today_items,
                        possible_duplicates,
                        waiting,
                        project_gaps,
                        project_review,
                        active_tasks_in_someday,
                        finished_checklists,
                        recent_completed_projects,
                        upcoming,
                    ]
                ),
                signals=[
                    f"Active Projects: {len(active_projects)}; obvious candidate gaps: {len(project_gaps)}. Open category=project_review to verify each Project's first Task.",
                    f"Stale starts: {len(stale_starts)}; overdue deadlines: {len(overdue)}; Waiting: {len(waiting)}.",
                    f"Possible duplicate items: {len(possible_duplicates)}.",
                    f"Active Tasks inside Someday Projects: {len(active_tasks_in_someday)}.",
                    f"Open Tasks whose checklist is finished: {len(finished_checklists)}.",
                    f"Seven-day window: {len(today_items)} today and {len(upcoming)} future-dated; recently completed Projects available on request: {len(recent_completed_projects)}.",
                    "Scan the past and upcoming calendars before changing dates.",
                ],
            ),
            ReviewSection(
                key="get_creative",
                title="Get Creative",
                item_ids=section_ids([someday]),
                signals=[
                    f"Someday: {len(someday)}.",
                    "Open category=someday only when the owner requests it or says it is due.",
                ],
            ),
            ReviewSection(
                key="plan_week",
                title="Plan the week, if requested",
                item_ids=section_ids(
                    [
                        plan_candidates
                        if "weekly_candidate" in set(signals_any)
                        else [],
                    ]
                ),
                signals=[
                    "This step is optional and starts after the review is current.",
                    "Things load by day: " + "; ".join(day_load) + ".",
                    "Calendar capacity is unknown until the owner scans the calendar.",
                    "Keep an item in Anytime unless the owner chooses a real start day.",
                    f"Planning actions: {len(plan_candidates)}. Open category=weekly_candidate only when the owner requests planning.",
                ],
            ),
        ]
        result_signals = [
            "capture_check_required",
            "calendar_scan_required",
            "weekly_planning_optional",
        ]
        if summarized:
            result_signals.append("weekly_review_summarized")
        membership_records = [*audit, *recent_completed_projects]
        membership_revision = "s_" + _digest(
            [
                self._scope_revision(membership_records),
                today.isoformat(),
                self._tag_revision(),
            ]
        )
        return _WeeklyReviewSnapshot(
            records=selected,
            signals_by_id=signal_map,
            sections=sections,
            result_signals=result_signals,
            membership_revision=membership_revision,
            summary_instruction=summary_instruction,
        )

    def _diagnostic_title(self, conflict: Conflict) -> str:
        if conflict.item_id.startswith("tag:"):
            uuid = conflict.item_id.removeprefix("tag:")
            title = _bounded_tag_title(self._library.tags.get(uuid) or "")
            return title if title.strip() else "(untitled)"
        item = self._exact_item(conflict.item_id)
        return _bounded_title(item.title) if item is not None else "(untitled)"

    def _diagnostic_kind(
        self, conflict: Conflict
    ) -> Literal["task", "project", "area", "heading", "tag"]:
        if conflict.item_id.startswith("tag:"):
            return "tag"
        item = self._exact_item(conflict.item_id)
        return item.public_kind if item is not None else "task"

    def _diagnostic_fact(self, conflict: Conflict) -> DiagnosticFact:
        return DiagnosticFact(
            id=conflict.item_id,
            kind=self._diagnostic_kind(conflict),
            title=self._diagnostic_title(conflict),
            conflicts=list(conflict.signals),
            repair=conflict.repair,
            repair_kind=conflict.repair_kind,
            repairs=[
                DiagnosticRepair(conflict=name, repair_kind=kind)
                for name, kind in conflict.repairs
            ],
        )

    def _bulk_exact(self, call: ReadCall) -> Result:
        items: list[Record] = []
        missing: list[str] = []
        for item_id in call.ids:
            item = self._exact_item(item_id)
            if item is None:
                missing.append(item_id)
            else:
                items.append(item)
        missing_text = _bounded_id_list(missing)
        if missing and not items:
            return Result(
                next="read",
                status="needs_input",
                instruction=(
                    f"{len(missing)} requested IDs were not found. "
                    f"Missing: {missing_text}."
                ),
                missing_ids=missing,
            )
        result = self._page(
            items,
            call.limit,
            full=True,
            instruction=(
                "Use these exact facts."
                if not missing
                else (
                    f"{len(missing)} requested IDs were not found. "
                    f"Use these exact facts for the others. Missing: {missing_text}."
                )
            ),
            missing_ids=missing,
            detail=(
                tuple(call.fields)
                if "fields" in call.model_fields_set
                else DETAIL_FIELDS
            ),
        )
        if missing:
            return result.model_copy(
                update={
                    "next": "read",
                    "status": "needs_input",
                }
            )
        return result

    def _context_change(self, call: ReadCall) -> Result:
        if call.id is not None:
            target = self._exact_item(call.id)
            if target is None:
                return self._needs_input(
                    "I could not find that exact item. Read or search again."
                )
        else:
            assert call.find is not None
            if call.within == "trash":
                matches = [
                    item
                    for item in self._search(call.find, None, closed=True)
                    if item.trashed
                ]
            else:
                within = self._exact_item(call.within) if call.within else None
                if call.within and within is None:
                    return self._needs_input(
                        "I could not find that exact search scope. Read the Project or Area, then search again."
                    )
                if within is not None and within.kind not in {"area", "project"}:
                    return self._needs_input("Search within an exact Area or Project.")
                matches = self._search(call.find, within, closed=True)
            if len(matches) > _CHANGE_FIND_LIMIT:
                return self._needs_input(
                    f"That change search matches more than {_CHANGE_FIND_LIMIT} items. Use a narrower find or exact id."
                )
            if not matches:
                return self._needs_input(
                    "That change search found no item. Use a narrower find or exact id."
                )
            if len(matches) > 1:
                return Result(
                    next="ask",
                    status="needs_input",
                    instruction=(
                        f"That change search matches {len(matches)} items. "
                        "Choose one item, then read it with purpose=change and its exact id."
                    ),
                    items=[
                        self._fact(item, full=False, include_revision=False)
                        for item in matches
                    ],
                )
            target = matches[0]
        if target.kind == "project" and not target.heading:
            return self._writable_project(target, call)
        if not self._context_detail_is_complete(target):
            return self._context_recovery(
                code="context_incomplete",
                instruction=(
                    "That item is too large for one safe change context. "
                    "Use the exact paged read and a revisioned change."
                ),
                retry="rebuild",
                read=self._selector_arguments(call),
                status="unsupported",
            )
        try:
            neighborhood = self._neighborhood_collect(target)
            self._neighborhood_include(neighborhood, call)
        except _Abort as error:
            return error.result
        if len(neighborhood.records) > _CONTEXT_LIMIT:
            return self._oversized_context(call, len(neighborhood.records))
        refs, by_id = self._context_refs(neighborhood.records)
        context = self._create_context(
            call,
            refs,
            scopes=[f"change:{target.id}"],
        )
        facts = [
            self._fact(
                record,
                full=record.uuid == target.uuid,
                include_revision=False,
            ).model_copy(update={"ref": by_id[record.id]})
            for record in neighborhood.records
        ]
        instruction = (
            "Use context_id and short refs for one coherent change. "
            "Omitted item fields remain unchanged. Include a destination to move."
        )
        if neighborhood.include_note:
            instruction = f"{instruction} {neighborhood.include_note}"
        if target.kind == "area":
            scope_revision = self._area_scope_revision()
        else:
            scope_revision = self._detail_revision(target)
        return Result(
            next="done",
            status="ok",
            instruction=instruction,
            items=facts,
            signals=neighborhood.include_signals,
            context=self._public_context(context),
            scope_revision=scope_revision,
            missing_ids=neighborhood.missing_ids,
        )

    def _neighborhood_collect(self, target: Record) -> _Neighborhood:
        """Collect the local neighborhood for one Task, Area, or heading change."""
        neighborhood = _Neighborhood()

        def place(item: Record) -> None:
            neighborhood.add(self._library.records.get(item.parent_uuid or ""))
            neighborhood.add(self._library.records.get(item.area_uuid or ""))
            neighborhood.add(self._library.records.get(item.heading_uuid or ""))

        neighborhood.add(target)
        place(target)
        if target.kind == "project" and target.trashed:
            for descendant in reversed(self._project_descendants(target.uuid)):
                neighborhood.add(descendant)
        if target.kind == "task":
            parent = self._library.records.get(target.parent_uuid or "")
            if parent is not None and parent.kind == "project":
                for heading in self._project_headings(parent.uuid):
                    neighborhood.add(heading)
        template = self._library.records.get(template_uuid_of(target) or "")
        if template is not None:
            neighborhood.add(template)
            place(template)
        if target.recurrence.role == "template":
            for candidate in self._library.recurrence_instances(target.uuid):
                neighborhood.add(candidate)
        return neighborhood

    def _neighborhood_include(self, neighborhood: _Neighborhood, call: ReadCall) -> None:
        """Add resolved includes without aborting the target neighborhood."""
        notes: list[str] = []
        for include in call.include:
            within = self._exact_item(include.within) if include.within else None
            if include.within and (
                within is None
                or within.kind not in {"area", "project"}
                or not self._is_searchable(within)
            ):
                notes.append("An include scope must identify an active Area or Project.")
                neighborhood.include_signals.append("include_unresolved")
                continue
            if include.id is not None:
                exact = self._exact_item(include.id)
                matches = [exact] if exact is not None else []
                if not matches:
                    neighborhood.missing_ids.append(include.id)
            else:
                assert include.find is not None
                matches = self._search(include.find, within)
            if len(matches) != 1:
                neighborhood.include_signals.append("include_unresolved")
                if not matches:
                    notes.append(
                        "An include found no item. Use an exact id or a narrower find."
                    )
                else:
                    notes.append(
                        "An include was not unique. Choose one item or narrow the find."
                    )
                continue
            for dependency in self._include_dependencies(matches[0]):
                neighborhood.add(dependency, placement=True)
        neighborhood.include_signals = list(dict.fromkeys(neighborhood.include_signals))
        neighborhood.missing_ids = list(dict.fromkeys(neighborhood.missing_ids))[:10]
        neighborhood.include_note = " ".join(dict.fromkeys(notes)) or None

    def _include_dependencies(self, record: Record) -> list[Record]:
        """Return one included item, its anchors, and destination headings."""
        dependencies: list[Record] = []

        def add(item: Record | None) -> None:
            if item is not None and all(saved.uuid != item.uuid for saved in dependencies):
                dependencies.append(item)

        add(record)
        add(self._library.records.get(record.parent_uuid or ""))
        add(self._library.records.get(record.area_uuid or ""))
        add(self._library.records.get(record.heading_uuid or ""))
        if record.kind == "project":
            for heading in self._project_headings(record.uuid):
                add(heading)
        return dependencies

    def _project_headings(self, project_uuid: str) -> list[Record]:
        return sorted(
            (
                item
                for item in self._library.records.values()
                if item.heading
                and item.parent_uuid == project_uuid
                and not item.trashed
                and item.status == "open"
            ),
            key=lambda item: (item.sort_index, item.title, item.uuid),
        )

    def _recurrence_read(self, call: ReadCall) -> Result:
        """Read one Task and verify its repeat template/copy relationship."""
        assert call.id is not None
        target = self._exact_item(call.id)
        if target is None or target.kind != "task" or target.heading:
            return self._unsupported(
                "Recurrence inspection needs one exact Task, not a Project or heading."
            )
        if not self._recurrence_relationship_is_valid(target):
            return self._unsupported(
                "Things returned an inconsistent repeat template and generated-copy relationship."
            )
        relationship = (
            "No existing repeat relationship is present."
            if target.recurrence.role == "none"
            else "The repeat template and generated copy relationship is verified."
        )
        return Result(
            next="done",
            status="ok",
            instruction=(
                f"{relationship} Use this recurrence fact before a repeat mutation."
            ),
            items=[self._fact(target, full=True)],
            signals=["recurrence_relationship_verified"],
        )

    def _context_organize(self, call: ReadCall) -> Result:
        project: Record | None = None
        if call.id is not None:
            project = self._exact_item(call.id)
            if project is None or project.kind != "project" or project.heading:
                return self._organize_unavailable(project)
        elif call.find is not None:
            within = self._exact_item(call.within) if call.within else None
            if call.within and (within is None or not within.is_open()):
                return self._context_recovery(
                    code="context_required",
                    instruction=(
                        "That search scope is not an active visible Area or Project. "
                        "Read it again."
                    ),
                    retry="read",
                    read=self._selector_arguments(call),
                    status="needs_input",
                )
            if within is not None and within.kind not in {"area", "project"}:
                return self._needs_input("Search within an exact Area or Project.")
            hits = self._search(call.find, within)
            projects = [
                item
                for item in hits
                if item.kind == "project" and not item.heading
            ]
            if not projects:
                parents: list[Record] = []
                seen_parents: set[str] = set()
                for item in hits:
                    parent_uuid = item.parent_uuid
                    if parent_uuid is None or parent_uuid in seen_parents:
                        continue
                    parent = self._library.records.get(parent_uuid)
                    if (
                        parent is not None
                        and parent.kind == "project"
                        and parent.is_open()
                    ):
                        seen_parents.add(parent_uuid)
                        parents.append(parent)
                if len(parents) == 1:
                    projects = parents
                elif len(parents) > 1:
                    return Result(
                        next="ask",
                        status="needs_input",
                        instruction=(
                            f"Those matching items sit in {len(parents)} Projects. "
                            "Choose one Project, then read it with purpose=organize "
                            "and its exact id."
                        ),
                        items=[
                            self._fact(item, full=False, include_revision=False)
                            for item in parents
                        ],
                        recovery=RecoveryFact(
                            code="context_incomplete",
                            retry="rebuild",
                        ),
                    )
                else:
                    closed_projects = [
                        item
                        for item in self._search(call.find, within, closed=True)
                        if item.kind == "project"
                        and not item.heading
                        and item.trashed
                    ]
                    if len(closed_projects) == 1:
                        return self._writable_project(closed_projects[0], call)
                    orphans = [
                        item
                        for item in self._library.records.values()
                        if item.kind == "task"
                        and not item.heading
                        and item.is_open()
                        and item.parent_uuid is None
                    ]
                    if orphans:
                        orphans.sort(key=lambda item: (item.sort_index, item.title, item.uuid))
                        return Result(
                            next="done",
                            status="ok",
                            instruction=(
                                "No Project matched. To group these existing tasks, "
                                "create one Project and move them in their current order."
                            ),
                            items=[
                                self._fact(item, full=False, include_revision=True)
                                for item in orphans[:40]
                            ],
                        )
                    return self._context_recovery(
                        code="context_required",
                        instruction=(
                            "I could not find one active Project. Use a narrower find "
                            "or exact id."
                        ),
                        retry="rebuild",
                        read=self._selector_arguments(call),
                        status="needs_input",
                    )
            if len(projects) > 1:
                return Result(
                    next="ask",
                    status="needs_input",
                    instruction=(
                        f"That Project find matches {len(projects)} active Projects. "
                        "Choose one Project, then read it with purpose=organize and its exact id."
                    ),
                    items=[self._fact(item, full=False, include_revision=False) for item in projects],
                    recovery=RecoveryFact(
                        code="context_incomplete",
                        retry="rebuild",
                    ),
                )
            project = projects[0]
        elif call.view == "project":
            assert call.within is not None
            project = self._exact_item(call.within)
            if project is None or project.kind != "project" or project.heading:
                return self._organize_unavailable(project)
        else:
            raise AssertionError("organize selector must identify a Project")
        assert project is not None
        return self._writable_project(project, call)

    def _writable_project(self, project: Record, call: ReadCall) -> Result:
        """Return the one writable Project neighborhood."""
        neighborhood = _Neighborhood()
        neighborhood.add(project)
        neighborhood.add(self._library.records.get(project.area_uuid or ""))
        if project.trashed:
            for descendant in reversed(self._project_descendants(project.uuid)):
                neighborhood.add(descendant)
        else:
            for record in self._library.project(project.id):
                neighborhood.add(record)
            for record in self._hidden_project_occupants(project.uuid):
                neighborhood.add(record)
        self._neighborhood_include(neighborhood, call)
        extra_projects: list[Record] = []
        for record in list(neighborhood.records):
            if record.kind != "project" or record.uuid == project.uuid:
                continue
            extra_projects.append(record)
            if record.trashed:
                for descendant in reversed(self._project_descendants(record.uuid)):
                    neighborhood.add(descendant)
                continue
            for member in self._library.project(record.id):
                neighborhood.add(member)
            for occupant in self._hidden_project_occupants(record.uuid):
                neighborhood.add(occupant)
        if len(neighborhood.records) > _CONTEXT_LIMIT:
            return self._project_overflow(call, project, len(neighborhood.records))
        refs, by_id = self._context_refs(neighborhood.records)
        scopes = [project.id, *[item.id for item in extra_projects]]
        context = self._create_context(call, refs, scopes=scopes)
        facts = [
            self._fact(
                record,
                full=record.uuid == project.uuid,
                include_revision=False,
            ).model_copy(update={"ref": by_id[record.id]})
            for record in neighborhood.records
        ]
        layouts = [
            self._project_layout(
                project, self._project_layout_records(project), by_id
            )
        ]
        layouts.extend(
            self._project_layout(
                item, self._project_layout_records(item), by_id
            )
            for item in extra_projects
            if item.id in by_id
        )
        instruction = (
            "Use this context to rename, date, trash, or send one organize draft. "
            "Listed work can move; unlisted work stays unchanged. "
            "Empty open sections can still have hidden occupants."
        )
        if project.trashed:
            contained = len(self._project_descendants(project.uuid))
            instruction = (
                f"This Project is in Trash with {contained} contained records. "
                "Restore or permanently delete with this context."
                if contained
                else "This Project is in Trash. Restore or permanently delete with this context."
            )
        if extra_projects:
            instruction = (
                f"{instruction} Included Projects can be organized in this same commit."
            )
        if neighborhood.include_note:
            instruction = f"{instruction} {neighborhood.include_note}"
        return Result(
            next="done",
            status="ok",
            instruction=instruction,
            items=facts,
            layouts=layouts,
            signals=neighborhood.include_signals,
            context=self._public_context(context),
            scope_revision=self._project_scope_revision(project.uuid),
            missing_ids=neighborhood.missing_ids,
        )

    def _project_overflow(
        self, call: ReadCall, project: Record, count: int
    ) -> Result:
        return self._context_recovery(
            code="context_incomplete",
            instruction=(
                f"This Project has {count} required items. A safe context can contain "
                f"at most {_CONTEXT_LIMIT}. Search within it, or change the Project "
                "with id and if_revision."
            ),
            retry="rebuild",
            read={"ids": [project.id]},
            status="needs_input",
        )

    def _organize_unavailable(self, _project: Record | None) -> Result:
        """Point organize recovery at a live look, never the same dead selector."""
        return self._context_recovery(
            code="context_required",
            instruction=(
                "That exact Project is not an active visible Project. "
                "Read an active Project again."
            ),
            retry="read",
            read={"view": "system"},
            status="needs_input",
        )

    def _context_detail_is_complete(self, item: Record) -> bool:
        checklist, direct, inherited = self._detail_lists(item)
        linked = self._library.recurrence_instances(item.uuid)
        return (
            len(item.notes) <= _NOTES_LIMIT
            and len(checklist) <= 100
            and len(direct) <= 40
            and len(inherited) <= 40
            and len(linked) <= 40
        )

    def _context_refs(
        self, records: list[Record], *, existing: Sequence[ContextRef] = ()
    ) -> tuple[list[ContextRef], dict[str, str]]:
        prefixes = {"task": "t", "project": "p", "area": "a", "heading": "h"}
        by_id = {entry.exact_id: entry.ref for entry in existing}
        used = {entry.ref for entry in existing}
        refs: list[ContextRef] = []
        for record in records:
            if record.id in by_id:
                continue
            kind = record.public_kind
            short = self._stable_context_ref(record.id, prefixes[kind], used)
            refs.append(
                ContextRef(
                    ref=short,
                    exact_id=record.id,
                    revision=self._revision(record),
                )
            )
            by_id[record.id] = short
            used.add(short)
        return refs, by_id

    @staticmethod
    def _stable_context_ref(exact_id: str, prefix: str, used: set[str]) -> str:
        for salt in range(16):
            digest = sha256(f"{salt}:{exact_id}".encode()).digest()
            token = b32encode(digest).decode("ascii").lower().rstrip("=")[:11]
            candidate = f"{prefix}{token}"
            if candidate not in used:
                return candidate
        raise ContextConflict("could not allocate a unique context reference")

    def _create_context(
        self,
        call: ReadCall,
        refs: list[ContextRef],
        *,
        scopes: Sequence[str],
        complete: bool = True,
        seen: int | None = None,
        total: int | None = None,
        next_cursor: str | None = None,
    ) -> ReadContext:
        view, item_id, within, from_date, to_date = self._selector_fields(call)
        count = seen if seen is not None else len(refs)
        known_total = total if total is not None else (count if complete else None)
        return self._context_store.create(
            account_id=self._account_id,
            selector=ReadSelector(
                purpose=call.purpose,
                view=view,
                item_id=item_id,
                find=call.find,
                within=within,
                from_date=from_date,
                to_date=to_date,
                limit=call.limit,
                includes=tuple(
                    ReadIncludeSelector(
                        item_id=entry.id,
                        find=entry.find,
                        within=entry.within,
                    )
                    for entry in call.include
                ),
            ),
            refs=refs,
            completeness=tuple(
                CompletenessFact(
                    scope=scope,
                    seen=count,
                    total=known_total,
                    complete=complete,
                    next_cursor=None if complete else next_cursor,
                )
                for scope in scopes
            ),
        )

    def _selector_fields(
        self, call: ReadCall
    ) -> tuple[View | None, str | None, str | None, str | None, str | None]:
        view: View | None = call.view
        item_id = call.id
        within = call.within
        if call.purpose == "review" and call.id is not None:
            if call.id.startswith("area:"):
                view = view or "area"
                within = within or call.id
                if view == "area":
                    item_id = None
            elif call.id.startswith("project:"):
                view = view or "project"
                within = within or call.id
                if view == "project":
                    item_id = None
        elif call.view in {"area", "project"} and call.id is not None and within is None:
            within = call.id
            item_id = None
        from_date = call.from_date
        to_date = call.to_date
        if call.view == "logbook" and from_date is None and to_date is None:
            start, end = self._logbook_range(call)
            from_date = start.isoformat()
            to_date = end.isoformat()
        return view, item_id, within, from_date, to_date

    def _logbook_range(self, call: ReadCall) -> tuple[date, date]:
        if call.from_date is not None and call.to_date is not None:
            return date.fromisoformat(call.from_date), date.fromisoformat(call.to_date)
        end = self._clock().date()
        return end - timedelta(days=_LOGBOOK_DAYS - 1), end

    def _call_from_cursor(self, saved: _ItemCursor, limit: int) -> ReadCall:
        payload: dict[str, object] = {"limit": limit}
        if saved.view is not None:
            payload["view"] = saved.view
        if saved.within is not None:
            payload["within"] = saved.within
        elif saved.item_id is not None:
            payload["id"] = saved.item_id
        if saved.from_date is not None:
            payload["from"] = saved.from_date
        if saved.to_date is not None:
            payload["to"] = saved.to_date
        if saved.signals_any:
            payload["signals_any"] = list(saved.signals_any)
        return ReadCall.model_validate(payload)

    def _bind_review_context(
        self,
        result: Result,
        call: ReadCall | None,
        records: list[Record],
        *,
        existing_context_id: str | None = None,
    ) -> Result:
        if (
            call is None
            or call.purpose != "review"
            or call.ids
            or not records
            or result.context is not None
        ):
            return result
        view = call.view or (
            "today"
            if call.id is None and call.find is None
            else None
        )
        if view is not None and view not in _REVIEW_CONTEXT_VIEWS:
            return result
        if view is None and call.find is None and call.id is None:
            view = "today"
        extra: list[Record] = []
        if call.include:
            neighborhood = _Neighborhood()
            self._neighborhood_include(neighborhood, call)
            extra.extend(neighborhood.records)
        bound: list[Record] = []
        seen: set[str] = set()
        for record in [*records, *extra]:
            if record.uuid in seen:
                continue
            seen.add(record.uuid)
            bound.append(record)
        existing: tuple[ContextRef, ...] = ()
        if existing_context_id is not None:
            try:
                existing = self._context_store.get(
                    existing_context_id, account_id=self._account_id
                ).refs
            except (ContextNotFound, ContextExpired, ContextCorrupt):
                existing = ()
        if len(existing) + len(bound) > _CONTEXT_LIMIT:
            return self._oversized_context(call, len(existing) + len(bound))
        new_refs, by_id = self._context_refs(bound, existing=existing)
        scope = f"review:{call.within or call.id or view or call.find or 'page'}"
        seen_count = len(existing) + len(new_refs)
        finished = result.cursor is None
        layouts = result.layouts
        if existing_context_id is not None and existing:
            try:
                context = self._context_store.extend(
                    existing_context_id,
                    account_id=self._account_id,
                    refs=new_refs,
                    completeness=(
                        CompletenessFact(
                            scope=scope,
                            seen=seen_count,
                            complete=finished,
                            next_cursor=None if finished else result.cursor,
                        ),
                    ),
                )
            except (ContextNotFound, ContextExpired, ContextCorrupt, ContextConflict):
                context = self._create_context(
                    call,
                    [*existing, *new_refs],
                    scopes=[scope],
                    complete=finished,
                    seen=seen_count,
                    next_cursor=result.cursor,
                )
        else:
            context = self._create_context(
                call,
                new_refs,
                scopes=[scope],
                complete=finished,
                seen=seen_count,
                next_cursor=result.cursor,
            )
        if view == "audit" and finished and not call.signals_any:
            projects = self._complete_audit_projects(context)
            context = self._context_store.extend(
                context.id,
                account_id=self._account_id,
                completeness=self._audit_project_completeness(projects),
            )
            layouts = self._audit_project_layouts(projects, context)
        if result.cursor is not None:
            saved = self._cursors.get(result.cursor)
            if saved is not None:
                self._cursors[result.cursor] = replace(saved, context_id=context.id)
        items = [
            item.model_copy(update={"ref": by_id[item.id], "revision": None})
            if item.id in by_id
            else item
            for item in result.items
        ]
        instruction = result.instruction
        if view == "audit" and finished:
            if call.signals_any:
                instruction = "This final audit page completes the selected filter."
            else:
                instruction = (
                    "This final audit page completes the active list and includes "
                    "each complete Project layout in native order."
                )
        if items and "short ref" not in instruction.casefold():
            instruction = (
                instruction.rstrip(".")
                + ". Use this context and short refs to change listed work in one commit."
            )
        if result.truncated and not finished:
            instruction = (
                instruction.rstrip(".")
                + ". Continue the cursor to add the rest to this same context."
            )
        return result.model_copy(
            update={
                "items": items,
                "layouts": layouts,
                "context": self._public_context(context),
                "instruction": instruction,
            }
        )

    def _complete_audit_projects(
        self, context: ReadContext
    ) -> list[tuple[Record, list[Record]]]:
        exact_ids = {entry.exact_id for entry in context.refs}
        projects: list[tuple[Record, list[Record]]] = []
        for entry in context.refs:
            project = self._exact_item(entry.exact_id)
            if project is None or project.kind != "project" or project.heading:
                continue
            members = self._library.project(project.id)
            if any(member.id not in exact_ids for member in members):
                continue
            projects.append((project, members))
        return projects

    @staticmethod
    def _audit_project_completeness(
        projects: Sequence[tuple[Record, list[Record]]],
    ) -> tuple[CompletenessFact, ...]:
        return tuple(
            CompletenessFact(
                scope=project.id,
                seen=len(members),
                total=len(members),
                complete=True,
            )
            for project, members in projects
        )

    def _audit_project_layouts(
        self,
        projects: Sequence[tuple[Record, list[Record]]],
        context: ReadContext,
    ) -> list[LayoutFact]:
        by_id = {entry.exact_id: entry.ref for entry in context.refs}
        return [
            self._project_layout(project, members, by_id)
            for project, members in projects
        ]

    @staticmethod
    def _public_context(context: ReadContext) -> ContextFact:
        return ContextFact(
            id=context.id,
            purpose=context.selector.purpose,
            expires_at=context.expires_at.isoformat(),
            complete=context.complete,
        )

    def _project_layout_records(self, project: Record) -> list[Record]:
        """Direct members the layout can name, including Trash children."""
        if not project.trashed:
            return self._library.project(project.id)
        children = [
            item
            for item in self._library.records.values()
            if item.parent_uuid == project.uuid
        ]
        children.sort(key=lambda item: (item.sort_index, item.title, item.uuid))
        return [project, *children]

    def _project_layout(
        self, project: Record, records: list[Record], by_id: dict[str, str]
    ) -> LayoutFact:
        listed = {record.uuid for record in records}
        headings = sorted(
            [
                record
                for record in records
                if record.heading and record.parent_uuid == project.uuid
            ],
            key=lambda item: (item.sort_index, item.uuid),
        )
        tasks = [
            record
            for record in records
            if record.kind == "task"
            and not record.heading
            and record.parent_uuid == project.uuid
        ]
        sections = [
            LayoutSectionFact(
                heading_ref=by_id[heading.id],
                task_refs=[
                    by_id[task.id]
                    for task in sorted(
                        [task for task in tasks if task.heading_uuid == heading.uuid],
                        key=lambda item: (item.sort_index, item.uuid),
                    )
                    if task.id in by_id
                ],
                hidden_count=hidden[0],
                hidden_signals=hidden[1],
            )
            for heading in headings
            for hidden in [
                self._heading_hidden_occupancy(heading.uuid, listed=listed)
            ]
        ]
        unheaded = sorted(
            [task for task in tasks if task.heading_uuid is None],
            key=lambda item: (item.sort_index, item.uuid),
        )
        if unheaded:
            sections.append(
                LayoutSectionFact(task_refs=[by_id[task.id] for task in unheaded])
            )
        return LayoutFact(
            project_ref=by_id[project.id], sections=sections, complete=True
        )

    def _heading_hidden_occupancy(
        self, heading_uuid: str, *, listed: set[str] | None = None
    ) -> tuple[int, list[str]]:
        signals: list[str] = []
        count = 0
        for item in self._library.records.values():
            if item.heading_uuid != heading_uuid:
                continue
            if listed is not None and item.uuid in listed:
                continue
            if (
                not item.trashed
                and item.status == "open"
                and item.recurrence.role != "template"
            ):
                continue
            count += 1
            if item.trashed:
                signals.append("trashed")
            elif item.recurrence.role == "template":
                signals.append("template")
            elif item.status == "done":
                signals.append("completed")
            elif item.status == "dropped":
                signals.append("canceled")
        return count, list(dict.fromkeys(signals))

    def _hidden_project_occupants(self, project_uuid: str) -> list[Record]:
        return [
            item
            for item in self._library.records.values()
            if item.parent_uuid == project_uuid
            and not item.heading
            and (
                item.trashed
                or item.status != "open"
                or item.recurrence.role == "template"
            )
        ]

    def _oversized_context(self, call: ReadCall, count: int) -> Result:
        read = self._selector_arguments(call)
        if not call.include:
            read.pop("purpose", None)
        read["limit"] = _READ_LIMIT
        return self._context_recovery(
            code="context_incomplete",
            instruction=(
                f"This scope has {count} required items. A safe context can contain "
                f"at most {_CONTEXT_LIMIT}. Use the suggested paged review, then "
                "narrow the requested structure."
            ),
            retry="rebuild",
            read=read,
            status="needs_input",
        )

    @staticmethod
    def _selector_arguments(call: ReadCall) -> dict[str, object]:
        values: dict[str, object] = {"purpose": call.purpose}
        selectors = {
            "view": call.view,
            "id": call.id,
            "find": call.find,
            "within": call.within,
            "from": call.from_date,
            "to": call.to_date,
            "category": call.category,
        }
        values.update({key: value for key, value in selectors.items() if value})
        if call.limit != 20:
            values["limit"] = call.limit
        if call.include:
            values["include"] = [
                {
                    key: value
                    for key, value in {
                        "id": entry.id,
                        "find": entry.find,
                        "within": entry.within,
                    }.items()
                    if value is not None
                }
                for entry in call.include
            ]
        return values

    def execute_v2(self, draft: object) -> JsonDict:
        """Prepare one immutable v2 operation and run or stage its manifest."""

        from .v2 import OperationDraft

        if not isinstance(draft, OperationDraft):
            raise TypeError("execute_v2 needs an OperationDraft")
        journal = self._journal
        existing = journal.get_v2_request(
            self._account_id, draft.api_version, draft.request_id
        )
        if existing is not None:
            if existing.request_hash != draft.request_hash:
                return {
                    "state": "rejected",
                    "code": "request_conflict",
                    "next_action": "correct_request",
                    "instruction": "That request_id belongs to different arguments.",
                    "operation_id": existing.operation_id,
                }
            return self._resume_v2(existing)
        blockers = journal.blocking_v2_operations(self._account_id)
        if blockers:
            return {
                "state": "rejected",
                "code": "write_fenced",
                "next_action": "run_cli",
                "instruction": "An unresolved operation blocks writes for this account.",
                "blocking_operation_ids": blockers,
            }
        failed = self._refresh(force=True)
        if failed is not None:
            return {"state": "rejected", "code": "internal_error", "next_action": "retry_same", "instruction": failed.instruction}
        prepared = self._prepare_v2_manifest(draft)
        if isinstance(prepared, dict):
            return prepared
        manifest, writes, before = prepared
        operation_id = f"op_{token_urlsafe(18)}"
        already_current = self._writes_match(writes)
        initial_state: V2State = (
            "pending"
            if already_current
            else "awaiting_owner"
            if manifest.requires_owner
            else "pending"
        )
        operation = V2Operation(
            account_id=self._account_id,
            api_version=draft.api_version,
            request_id=draft.request_id,
            request_hash=draft.request_hash,
            operation_id=operation_id,
            tool=draft.tool,
            state=initial_state,
            manifest=manifest.to_json(),
            manifest_hash=manifest.manifest_hash,
            safety_policy_digest=manifest.safety_policy_digest,
            expires_at=manifest.expires_at,
        )
        outcome, stored, blockers = journal.create_v2(
            operation,
            claim_fence=initial_state == "pending",
        )
        if outcome == "blocked":
            return {
                "state": "rejected",
                "code": "write_fenced",
                "next_action": "run_cli",
                "instruction": "An unresolved operation blocks writes for this account.",
                "blocking_operation_ids": blockers,
            }
        assert stored is not None
        if outcome == "conflict":
            return {
                "state": "rejected",
                "code": "request_conflict",
                "next_action": "correct_request",
                "instruction": "That request_id belongs to different arguments.",
                "operation_id": stored.operation_id,
            }
        if outcome == "existing":
            return self._resume_v2(stored)
        if initial_state == "awaiting_owner":
            response: JsonDict = {
                "state": "awaiting_owner",
                "code": "awaiting_owner",
                "next_action": "run_cli",
                "instruction": "Review this operation with the CLI-only operation command.",
                "operation_id": operation_id,
            }
            return response
        result = self._apply_v2(operation, writes=writes, before=before)
        if result.get("item_ids"):
            return {**result, "_fresh_items": True}
        return result

    def _prepare_v2_manifest(
        self, draft: object
    ) -> tuple[Any, list[Write], list[JsonDict | None]] | JsonDict:
        from .v2 import OperationDraft, OperationManifest

        assert isinstance(draft, OperationDraft)
        writes: list[Write] = []
        before: list[JsonDict | None] = []
        preconditions: dict[str, str] = {}
        touched: list[list[str]] = []
        display_titles: list[str] = []
        payload = draft.payload
        if draft.tool == "things_capture":
            for capture_item in cast(list[dict[str, object]], payload["items"]):
                kind = cast(Kind, capture_item["kind"])
                parent_uuid: str | None = None
                area_uuid: str | None = None
                into_id = capture_item.get("into_id")
                if isinstance(into_id, str):
                    destination = self._exact_item(into_id)
                    if destination is None or destination.kind not in {"project", "area"}:
                        return {"state": "rejected", "code": "invalid_destination", "next_action": "correct_request", "instruction": "An exact destination was not found."}
                    if destination.status != "open" or destination.trashed or destination.recurrence.role != "none":
                        return {"state": "rejected", "code": "inactive_destination", "next_action": "correct_request", "instruction": "The destination is not an active ordinary container."}
                    if kind == "project" and destination.kind != "area":
                        return {"state": "rejected", "code": "invalid_destination", "next_action": "correct_request", "instruction": "Projects may only be captured into Areas."}
                    preconditions[destination.id] = self._revision(destination)
                    if destination.kind == "project":
                        parent_uuid = destination.uuid
                    else:
                        area_uuid = destination.uuid
                uuid = new_uuid()
                start_value = cast(str | None, capture_item.get("start"))
                start, tonight, someday = self._start(start_value)
                write = Write(
                    action="create", uuid=uuid, kind=kind,
                    title=cast(str, capture_item["title"]),
                    notes=cast(str | None, capture_item.get("notes")),
                    into_uuid=parent_uuid or area_uuid,
                    into_kind="project" if parent_uuid else "area" if area_uuid else None,
                    inbox=kind == "task" and into_id is None,
                    start=start, tonight=tonight, someday=someday,
                    deadline=date.fromisoformat(cast(str, capture_item["deadline"])) if capture_item.get("deadline") else None,
                )
                writes.append(write)
                before.append(None)
                touched.append(["title", "notes", "start", "deadline", "into"])
                display_titles.append(cast(str, capture_item["title"]))
                if kind == "project":
                    for child in cast(list[dict[str, object]], capture_item.get("tasks", [])):
                        child_write = Write(
                            action="create", uuid=new_uuid(), kind="task",
                            title=cast(str, child["title"]),
                            notes=cast(str | None, child.get("notes")),
                            into_uuid=uuid, into_kind="project",
                        )
                        writes.append(child_write)
                        before.append(None)
                        touched.append(["title", "notes", "into"])
                        display_titles.append(cast(str, child["title"]))
        else:
            ids = cast(list[str], payload["ids"] if "ids" in payload else [row["id"] for row in cast(list[dict[str, object]], payload["items"])])
            expanded_ids: set[str] = set()
            targets: list[Record] = []
            for item_id in ids:
                target = self._exact_item(item_id)
                if target is None or target.kind not in {"task", "project"} or target.heading:
                    return {"state": "rejected", "code": "missing_target", "next_action": "correct_request", "instruction": "Mutation targets must be exact Tasks or Projects."}
                if draft.tool == "things_trash" and target.kind == "project":
                    preconditions[f"scope:project:{target.uuid}"] = (
                        self._project_scope_revision(target.uuid)
                    )
                candidates = [*self._project_descendants(target.uuid), target] if draft.tool == "things_trash" and target.kind == "project" else [target]
                for candidate in candidates:
                    if candidate.recurrence.role == "template":
                        return {
                            "state": "rejected",
                            "code": "validation_error",
                            "next_action": "correct_request",
                            "instruction": "The bounded v2 mutation tools do not edit, complete, or trash recurrence templates.",
                        }
                    if candidate.id not in expanded_ids:
                        expanded_ids.add(candidate.id)
                        targets.append(candidate)
            if len(targets) > 120:
                return {"state": "rejected", "code": "expanded_write_limit", "next_action": "correct_request", "instruction": "The operation expands beyond 120 writes."}
            for target in targets:
                item_id = target.id
                preconditions[target.id] = self._revision(target)
                if target.parent_uuid:
                    parent = self._library.records.get(target.parent_uuid)
                    if parent is not None:
                        preconditions[parent.id] = self._revision(parent)
                if draft.tool == "things_complete":
                    if target.kind == "project":
                        open_actions = [
                            child
                            for child in self._project_descendants(target.uuid)
                            if child.kind == "task"
                            and not child.heading
                            and child.status == "open"
                            and not child.trashed
                            and child.recurrence.role != "template"
                        ]
                        if open_actions:
                            return {
                                "state": "rejected",
                                "code": "validation_error",
                                "next_action": "correct_request",
                                "instruction": "Complete or move every open Project action before completing the Project.",
                            }
                        preconditions[f"scope:project:{target.uuid}"] = (
                            self._project_scope_revision(target.uuid)
                        )
                    writes.append(Write(action="complete", uuid=target.uuid, kind=target.kind, status="done"))
                    touched.append(["status"])
                    before.append(self._v2_observed(target, ("status",)))
                    display_titles.append(target.title)
                elif draft.tool == "things_trash":
                    writes.append(Write(action="trash", uuid=target.uuid, kind=target.kind, heading=target.heading))
                    touched.append(["trashed"])
                    before.append(self._v2_observed(target, ("trashed",)))
                    display_titles.append(target.title)
                else:
                    row = next(entry for entry in cast(list[dict[str, object]], payload["items"]) if entry["id"] == item_id)
                    fields = cast(dict[str, object], row["set"])
                    if "notes" in fields and target.notes_format == "rich":
                        return {
                            "state": "rejected",
                            "code": "validation_error",
                            "next_action": "correct_request",
                            "instruction": "The bounded v2 update cannot replace a rich-text note.",
                        }
                    start_set = "start" in fields
                    start, tonight, someday = self._start(cast(str | None, fields.get("start"))) if start_set else (None, False, False)
                    remind_set = "remind_at" in fields
                    if (
                        start_set
                        and fields.get("start") is None
                        and target.remind is not None
                        and not remind_set
                    ):
                        return {
                            "state": "rejected",
                            "code": "validation_error",
                            "next_action": "correct_request",
                            "instruction": "Clearing a start with an existing reminder also requires remind_at=null.",
                        }
                    reminder_date, reminder = (
                        self._remind_input(
                            cast(str | None, fields.get("remind_at"))
                        )
                        if remind_set
                        else (
                            None,
                            target.remind
                            if start_set and fields.get("start") is not None
                            else None,
                        )
                    )
                    if reminder_date is not None:
                        if start_set and (start is None or start != reminder_date):
                            return {"state": "rejected", "code": "validation_error", "next_action": "correct_request", "instruction": "start and remind_at must use the same local date."}
                        if not start_set:
                            if target.start != reminder_date:
                                return {"state": "rejected", "code": "validation_error", "next_action": "correct_request", "instruction": "remind_at may omit start only when the existing start uses the same local date."}
                            start = reminder_date
                            tonight = target.tonight
                    writes.append(Write(
                        action="update", uuid=target.uuid, kind=target.kind,
                        title=cast(str | None, fields.get("title")),
                        notes=cast(str | None, fields.get("notes")),
                        start=start, clear_start=start_set and fields.get("start") is None,
                        tonight=tonight, someday=someday,
                        deadline=date.fromisoformat(cast(str, fields["deadline"])) if fields.get("deadline") else None,
                        clear_deadline="deadline" in fields and fields.get("deadline") is None,
                        remind=reminder,
                        clear_remind="remind_at" in fields and fields.get("remind_at") is None,
                    ))
                    touched.append(sorted(fields))
                    before.append(self._v2_observed(target, tuple(sorted(fields))))
                    display_titles.append(target.title)
        manifest = OperationManifest.build(
            account_id=self._account_id,
            draft=draft,
            preconditions=preconditions,
            writes=[_write_json(write) for write in writes],
            touched=touched,
            before=before,
            display_titles=display_titles,
            requires_owner=draft.tool == "things_trash",
            clock=self._clock(),
        )
        return manifest, writes, before

    def _apply_v2(
        self,
        operation: V2Operation,
        *,
        writes: list[Write] | None = None,
        before: list[JsonDict | None] | None = None,
    ) -> JsonDict:
        if not v2_manifest_is_valid(operation):
            return self._invalid_v2_manifest(operation.operation_id)
        writes = writes or [
            _write_from_json(cast(dict[str, object], row))
            for row in cast(list[object], operation.manifest["writes"])
        ]
        before = before or cast(list[JsonDict | None], operation.manifest.get("before", [None] * len(writes)))
        failed = self._refresh(force=True)
        if failed is not None:
            return {"state": "pending", "code": "pending_unknown", "next_action": "run_cli", "instruction": "Cloud precondition read-back is unavailable.", "operation_id": operation.operation_id}
        if not self._v2_preconditions_match(operation):
            response: JsonDict = {"state": "not_applied", "code": "not_applied_precondition", "next_action": "read_receipt", "instruction": "A frozen precondition changed before the Cloud write.", "operation_id": operation.operation_id}
            rows = self._v2_receipt_rows(operation, writes, before, "not_applied")
            settled = self._journal.settle_v2(operation.operation_id, expected="pending", state="not_applied", response=response, rows=rows)
            return response if settled else self._persisted_v2_outcome(operation.operation_id)
        if self._writes_match(writes):
            response = {
                "state": "unchanged",
                "code": "unchanged",
                "next_action": "read_receipt",
                "instruction": "The requested state was already current.",
                "operation_id": operation.operation_id,
            }
            rows = self._v2_receipt_rows(operation, writes, before, "unchanged")
            settled = self._journal.settle_v2(
                operation.operation_id,
                expected="pending",
                state="unchanged",
                response=response,
                rows=rows,
            )
            return response if settled else self._persisted_v2_outcome(
                operation.operation_id
            )
        try:
            applied = self._library.apply(writes)
        except CloudError:
            failed = self._refresh(force=True)
            if failed is not None:
                return {"state": "pending", "code": "pending_unknown", "next_action": "run_cli", "instruction": "The commit outcome is unknown and will never be replayed.", "operation_id": operation.operation_id}
            return self._reconcile_v2(operation, writes, before)
        if not applied.read_back_verified:
            failed = self._refresh(force=True)
            if failed is not None:
                return {"state": "pending", "code": "pending_unknown", "next_action": "run_cli", "instruction": "Cloud read-back is not yet proven.", "operation_id": operation.operation_id}
        return self._reconcile_v2(operation, writes, before)

    def _reconcile_v2(self, operation: V2Operation, writes: list[Write], before: list[JsonDict | None]) -> JsonDict:
        if not v2_manifest_is_valid(operation):
            return self._invalid_v2_manifest(operation.operation_id)
        matched = [self._writes_match([write]) for write in writes]
        if all(matched):
            state = "applied"
        elif any(matched):
            state = "partial"
        else:
            return {"state": "pending", "code": "pending_unknown", "next_action": "run_cli", "instruction": "The Cloud outcome remains unresolved.", "operation_id": operation.operation_id}
        item_ids = [_write_public_id(write) for write in writes]
        response: JsonDict = {"state": state, "code": state, "next_action": "run_cli" if state == "partial" else "read_receipt", "instruction": "Cloud read-back recorded the operation outcome.", "operation_id": operation.operation_id, "item_ids": item_ids}
        rows = self._v2_receipt_rows(operation, writes, before, state)
        settled = self._journal.settle_v2(operation.operation_id, expected="pending", state=cast(Any, state), response=response, rows=rows)
        return response if settled else self._persisted_v2_outcome(operation.operation_id)

    def _persisted_v2_outcome(self, operation_id: str) -> JsonDict:
        current = self._journal.get_v2_operation(operation_id)
        if current is not None and current.response is not None:
            return current.response
        return {"state": "pending", "code": "pending_unknown", "next_action": "run_cli", "instruction": "A concurrent reconciliation changed the operation; inspect its persisted receipt.", "operation_id": operation_id}

    def _v2_preconditions_match(self, operation: V2Operation) -> bool:
        preconditions = cast(dict[str, object], operation.manifest.get("preconditions", {}))
        for item_id, expected in preconditions.items():
            if item_id.startswith("scope:project:"):
                uuid = item_id.removeprefix("scope:project:")
                if self._project_scope_revision(uuid) != expected:
                    return False
                continue
            item = self._exact_item(item_id)
            if item is None or self._revision(item) != expected:
                return False
        return True

    def _resume_v2(self, operation: V2Operation) -> JsonDict:
        if operation.response is not None:
            return operation.response
        if not v2_manifest_is_valid(operation):
            return self._invalid_v2_manifest(operation.operation_id)
        if operation.state == "pending":
            failed = self._refresh(force=True)
            if failed is not None:
                return {"state": "pending", "code": "pending_unknown", "next_action": "run_cli", "instruction": "Cloud read-back is unavailable.", "operation_id": operation.operation_id}
            writes = [_write_from_json(cast(dict[str, object], row)) for row in cast(list[object], operation.manifest["writes"])]
            before = cast(list[JsonDict | None], operation.manifest.get("before", [None] * len(writes)))
            return self._reconcile_v2(operation, writes, before)
        return {"state": operation.state, "instruction": "This immutable operation is unchanged.", "operation_id": operation.operation_id}

    def host_get_operation_v2(self, operation_id: str) -> V2Operation | None:
        """Return an operation only when it belongs to this workspace account."""

        operation = self._journal.get_v2_operation(operation_id)
        if (
            operation is None
            or operation.account_id != self._account_id
            or not v2_manifest_is_valid(operation)
        ):
            return None
        return operation

    @staticmethod
    def _invalid_v2_manifest(operation_id: str) -> JsonDict:
        return {
            "state": "rejected",
            "code": "internal_error",
            "next_action": "contact_operator",
            "instruction": "The persisted operation manifest failed its integrity check; no write was made.",
            "operation_id": operation_id,
        }

    def host_reconcile_v2(self, operation_id: str) -> JsonDict:
        """Force current evidence for one pending operation without replaying it."""

        operation = self.host_get_operation_v2(operation_id)
        if operation is None:
            return {"state": "rejected", "code": "missing_target", "next_action": "correct_request", "instruction": "That operation does not belong to this account."}
        if operation.state != "pending":
            return self._resume_v2(operation)
        failed = self._refresh(force=True)
        if failed is not None:
            return {"state": "pending", "code": "pending_unknown", "next_action": "run_cli", "instruction": "Cloud read-back is unavailable; nothing was replayed.", "operation_id": operation_id}
        writes = [_write_from_json(cast(dict[str, object], row)) for row in cast(list[object], operation.manifest["writes"])]
        before = cast(list[JsonDict | None], operation.manifest.get("before", [None] * len(writes)))
        return self._reconcile_v2(operation, writes, before)

    def host_settle_not_applied_v2(self, operation_id: str, authorization: object) -> JsonDict:
        """Settle pending only when forced evidence proves no frozen write landed."""

        operation = self.host_get_operation_v2(operation_id)
        if operation is None or operation.state != "pending":
            return {"state": "rejected", "code": "missing_target", "next_action": "correct_request", "instruction": "That operation is not pending for this account."}
        if self._journal.verify_v2_authorization(operation, "settle_not_applied", authorization) is None:
            return {"state": "rejected", "code": "validation_error", "next_action": "run_cli", "instruction": "Verified CLI authorization is required.", "operation_id": operation_id}
        failed = self._refresh(force=True)
        if failed is not None:
            return {"state": "pending", "code": "pending_unknown", "next_action": "run_cli", "instruction": "Cloud evidence is unavailable.", "operation_id": operation_id}
        writes = [_write_from_json(cast(dict[str, object], row)) for row in cast(list[object], operation.manifest["writes"])]
        if any(self._writes_match([write]) for write in writes):
            return self.host_reconcile_v2(operation_id)
        before = cast(list[JsonDict | None], operation.manifest.get("before", [None] * len(writes)))
        if not self._v2_current_equals_before(operation, writes, before):
            return {"state": "pending", "code": "pending_unknown", "next_action": "run_cli", "instruction": "Current touched fields differ from both the frozen before and desired observations; nothing was replayed.", "operation_id": operation_id}
        response: JsonDict = {"state": "not_applied", "code": "not_applied_precondition", "next_action": "read_receipt", "instruction": "Forced read-back proved that no frozen write landed; nothing was replayed.", "operation_id": operation_id}
        rows = self._v2_receipt_rows(operation, writes, before, "not_applied")
        settled = self._journal.settle_v2(
            operation_id, expected="pending", state="not_applied", response=response,
            rows=rows, authorization=authorization, action="settle_not_applied",
        )
        return response if settled else self._persisted_v2_outcome(operation_id)

    def _v2_current_equals_before(
        self,
        operation: V2Operation,
        writes: list[Write],
        before: list[JsonDict | None],
    ) -> bool:
        touched = cast(list[list[str]], operation.manifest["touched"])
        for index, write in enumerate(writes):
            current = self._library.records.get(write.uuid)
            observed = self._v2_observed(current, touched[index]) if current is not None else None
            if observed != before[index]:
                return False
        return True

    def host_reconcile_v1_pending(self, intent_id: str) -> JsonDict:
        """Classify retained v1 pending evidence once, without replaying its writes."""

        record = self._journal.get(intent_id)
        if record is None or record.state != "pending":
            return {"status": "rejected", "code": "missing_target", "instruction": "That retained v1 operation is not pending."}
        if not _legacy_recovery_plan_is_complete(record.plan):
            malformed_result: JsonDict = {"status": "pending_unknown", "classification": "malformed", "instruction": "The retained v1 row has no complete frozen write evidence and remains fenced."}
            self._journal.annotate_v1_pending(intent_id, result=malformed_result)
            return malformed_result
        writes = self._writes_from_plan(record.plan)
        failed = self._refresh(force=True)
        if failed is not None:
            return {"status": "pending", "code": "pending_unknown", "instruction": "Cloud read-back is unavailable; nothing was replayed."}
        matched = [self._writes_match([write]) for write in writes]
        classification = "applied" if all(matched) else "partial" if any(matched) else "unknown"
        result: JsonDict = {
            "status": "reconciled_no_replay" if classification == "applied" else "pending_unknown",
            "classification": classification,
            "instruction": (
                "Forced Cloud read-back proved every retained v1 write applied; no write was replayed."
                if classification == "applied"
                else "Current evidence is partial or ambiguous; the retained v1 fence remains until signed CLI resolution."
            ),
        }
        changed = (
            self._journal.resolve_v1_pending(
                intent_id,
                expected_fingerprint=record.fingerprint,
                expected_plan_digest=_legacy_plan_digest(record.plan),
                state="applied",
                result=result,
            )
            if classification == "applied"
            else self._journal.annotate_v1_pending(intent_id, result=result)
        )
        if not changed:
            return {"status": "rejected", "code": "request_conflict", "instruction": "The retained v1 operation changed during reconciliation."}
        return result

    def host_get_legacy_resolution_v1(self, intent_id: str) -> V2Operation | None:
        record = self._journal.get(intent_id)
        if record is None or record.state != "pending":
            return None
        digest = _legacy_plan_digest(record.plan)
        raw_writes = record.plan.get("writes", [])
        writes = raw_writes if isinstance(raw_writes, list) else []
        display_titles = [
            cast(str, row["title"])
            if isinstance(row, dict) and isinstance(row.get("title"), str)
            else ""
            for row in writes
        ]
        return V2Operation(
            account_id=self._account_id,
            api_version="legacy-v1",
            request_id=record.fingerprint,
            request_hash=digest,
            operation_id="legacy_" + sha256((intent_id + record.fingerprint).encode()).hexdigest()[:24],
            tool="legacy_pending_resolution",
            state="pending",
            manifest={
                "intent_id_hash": "sha256:v1:" + sha256(intent_id.encode()).hexdigest(),
                "writes": json.loads(json.dumps(writes)),
                "display_titles": display_titles,
                "legacy_plan": json.loads(json.dumps(record.plan)),
            },
            manifest_hash=digest,
            safety_policy_digest="sha256:v1:legacy-no-replay-resolution",
        )

    def host_resolve_legacy_v1(
        self,
        intent_id: str,
        resolution: Literal["accepted_as_is", "superseded"],
        authorization: object,
    ) -> bool:
        operation = self.host_get_legacy_resolution_v1(intent_id)
        if operation is None:
            return False
        action = f"legacy_{resolution}"
        authorization_record = self._journal.verify_v2_authorization(operation, action, authorization)
        if authorization_record is None:
            return False
        record = self._journal.get(intent_id)
        if record is None or record.state != "pending":
            return False
        prior = record.result or {}
        return self._journal.resolve_v1_pending(
            intent_id,
            expected_fingerprint=operation.request_id,
            expected_plan_digest=operation.manifest_hash,
            state="stale",
            result={
                "status": "owner_resolved_no_replay",
                "classification": prior.get("classification", "unknown"),
                "resolution": resolution,
                "authorization": authorization_record,
                "instruction": "The owner released this retained v1 fence without a Cloud write.",
            },
        )

    def host_approve_v2(self, operation_id: str, authorization: object) -> JsonDict:
        operation = self._journal.get_v2_operation(operation_id)
        if operation is None or operation.account_id != self._account_id:
            return {"state": "rejected", "instruction": "That operation does not belong to this account."}
        if self._journal.verify_v2_authorization(operation, "approve", authorization) is None:
            return {"state": "rejected", "instruction": "Verified host authorization is required.", "operation_id": operation_id}
        if operation.state != "awaiting_owner":
            return self._resume_v2(operation)
        if operation.expires_at is None or datetime.fromisoformat(operation.expires_at) <= self._clock():
            response: JsonDict = {"state": "stale", "instruction": "The owner approval window expired.", "operation_id": operation_id}
            self._journal.transition_v2(operation_id, expected="awaiting_owner", state="stale", response=response)
            return response
        failed = self._refresh(force=True)
        if failed is not None:
            return {"state": "awaiting_owner", "instruction": "Cloud state could not be rechecked.", "operation_id": operation_id}
        if not self._v2_preconditions_match(operation):
            response = {"state": "stale", "instruction": "A private operation precondition changed.", "operation_id": operation_id}
            self._journal.transition_v2(operation_id, expected="awaiting_owner", state="stale", response=response)
            return response
        authorized, blockers = self._journal.authorize_v2(operation_id, authorization)
        if not authorized:
            return {"state": "rejected", "instruction": "Another unresolved operation blocks approval.", "operation_id": operation_id, "blocking_operation_ids": blockers}
        pending = self._journal.get_v2_operation(operation_id)
        assert pending is not None
        return self._apply_v2(pending)

    def host_decline_v2(self, operation_id: str, authorization: object) -> bool:
        operation = self._journal.get_v2_operation(operation_id)
        return bool(
            operation is not None
            and operation.account_id == self._account_id
            and self._journal.verify_v2_authorization(operation, "decline", authorization) is not None
            and self._journal.transition_v2(
                operation_id,
                expected="awaiting_owner",
                state="declined",
                authorization=authorization,
                response={"state": "declined", "instruction": "The owner declined this operation.", "operation_id": operation_id},
            )
        )

    def host_resolve_partial_v2(
        self,
        operation_id: str,
        resolution: Literal["accepted_as_is", "superseded"],
        authorization: object,
    ) -> bool:
        operation = self._journal.get_v2_operation(operation_id)
        return bool(
            operation is not None
            and operation.account_id == self._account_id
            and self._journal.verify_v2_authorization(operation, resolution, authorization) is not None
            and self._journal.transition_v2(
                operation_id,
                expected="partial",
                state="partial_resolved",
                authorization=authorization,
                resolution=resolution,
                response={"state": "partial_resolved", "instruction": "The owner recorded the partial outcome without replay.", "operation_id": operation_id},
            )
        )

    def _v2_receipt_rows(self, operation: V2Operation, writes: list[Write], before: list[JsonDict | None], outcome: str) -> list[JsonDict]:
        rows: list[JsonDict] = []
        touched = cast(list[list[str]], operation.manifest["touched"])
        for index, write in enumerate(writes, start=1):
            item = self._library.records.get(write.uuid)
            observed = self._v2_observed(item, touched[index - 1]) if item is not None else None
            result = outcome
            if outcome == "partial":
                result = "applied" if self._writes_match([write]) else "not_applied"
            desired_fields = set(touched[index - 1]) | {"action", "uuid", "kind"}
            desired = {key: value for key, value in _write_json(write).items() if key in desired_fields}
            if "remind_at" in touched[index - 1]:
                desired["remind_at"] = None if write.clear_remind else self._reminder_from_write(write)
            rows.append({"sequence": index, "action": write.action, "target_id": _write_public_id(write), "before": _taint_things_text(before[index - 1]), "desired": desired, "observed": _taint_things_text(observed), "result": result})
        return rows

    def _reminder_from_write(self, write: Write) -> str | None:
        if write.remind is None or write.start is None:
            return None
        return datetime.combine(write.start, time.fromisoformat(write.remind), tzinfo=self._clock().tzinfo).isoformat()

    def _v2_observed(self, item: Record, fields: Sequence[str]) -> JsonDict:
        values: JsonDict = {"id": item.id}
        selected = set(fields) or {"title", "notes", "status", "trashed", "start", "deadline", "into"}
        if "title" in selected:
            values["title"] = item.title
        if "notes" in selected:
            values["notes"] = item.notes
        if "status" in selected:
            values["status"] = item.status
        if "trashed" in selected:
            values["trashed"] = item.trashed
        if "start" in selected:
            values["start"] = item.start.isoformat() if item.start else None
        if "deadline" in selected:
            values["deadline"] = item.deadline.isoformat() if item.deadline else None
        if "remind_at" in selected:
            values["remind_at"] = self._reminder(item)
        if "into" in selected:
            values["into_id"] = f"project:{item.parent_uuid}" if item.parent_uuid else f"area:{item.area_uuid}" if item.area_uuid else None
        return values


    def commit(self, call: CommitCall) -> Result:
        fingerprint = _fingerprint(call.model_dump(mode="json", by_alias=True))
        stored = self._journal.get(call.intent_id)
        if stored is not None:
            if stored.fingerprint != fingerprint:
                return self._rejected(
                    "That intent_id already belongs to different work."
                )
            return self._resume(stored, allow_apply=stored.state == "prepared")

        failed = self._refresh(force=True)
        if failed is not None:
            return failed
        prepared_call = call
        contextual_commit = False
        if call.context_id is not None:
            contextual = self._compile_contextual(call)
            if isinstance(contextual, Result):
                return contextual
            prepared_call = contextual
            contextual_commit = True
        try:
            prepared = self._prepare(
                prepared_call, contextual_commit=contextual_commit
            )
        except _Abort as error:
            return error.result

        plan = self._plan_payload(prepared)
        record = IntentRecord(
            intent_id=call.intent_id,
            fingerprint=fingerprint,
            state="prepared",
            plan=plan,
        )
        if prepared.writes and (prepared.risky or call.require_approval):
            return self._stage(record, prepared)
        claimed = self._journal.reserve(record)
        if claimed != record:
            if claimed.fingerprint != fingerprint:
                return self._rejected(
                    "That intent_id already belongs to different work."
                )
            return self._resume(claimed, allow_apply=claimed.state == "prepared")
        return self._apply(claimed)

    def _compile_contextual(self, call: CommitCall) -> CommitCall | Result:
        assert call.context_id is not None
        try:
            context = self._context_store.get(
                call.context_id, account_id=self._account_id
            )
        except ContextCorrupt:
            return self._context_recovery(
                code="context_corrupt",
                instruction=(
                    "That saved read context is not usable. Read the target again."
                ),
                retry="read",
                status="stale",
            )
        except ContextExpired as expired:
            return self._context_recovery(
                code="context_expired",
                instruction="That read context expired. Repeat the suggested read.",
                retry="read",
                read=(
                    expired.selector.recovery_arguments()
                    if expired.selector is not None
                    else None
                ),
                status="stale",
            )
        except ContextNotFound:
            return self._context_recovery(
                code="context_required",
                instruction=(
                    "That read context is not available for this Things account. "
                    "Read the target again."
                ),
                retry="read",
                status="stale",
            )
        if call.organize and not context.complete:
            return self._context_recovery(
                code="context_incomplete",
                instruction="Complete the organization read before changing structure.",
                retry="read",
                read=context.selector.recovery_arguments(),
                status="stale",
            )
        context_refs = {entry.ref for entry in context.refs}
        context_ids = {entry.exact_id: entry.ref for entry in context.refs}
        touched_refs = {
            reference
            for change in call.change
            for reference in (change.ref, change.into)
            if reference in context_refs
        }
        # Relationship destinations and anchors are part of the write's read
        # evidence. Check them before compilation, too. This keeps a changed
        # destination from producing a plan against an old ordering or scope.
        for change in call.change:
            for reference in (
                change.into,
                change.after,
                change.today_after,
                change.move_contents_to,
                change.heading_id,
            ):
                if reference in context_refs:
                    touched_refs.add(reference)
                elif reference in context_ids:
                    touched_refs.add(context_ids[reference])
        for create in call.create:
            for reference in (
                create.into,
                create.after,
                create.today_after,
                create.heading_id,
            ):
                if reference in context_refs:
                    touched_refs.add(reference)
                elif reference in context_ids:
                    touched_refs.add(context_ids[reference])
        if call.organize:
            for draft in call.organize:
                if draft.project_ref in context_refs:
                    touched_refs.add(draft.project_ref)
                touched_refs.update(
                    ref for ref in draft.delete_headings if ref in context_refs
                )
                for section in draft.sections:
                    if section.heading_ref in context_refs:
                        touched_refs.add(section.heading_ref)
                    touched_refs.update(
                        ref for ref in section.task_refs if ref in context_refs
                    )
        if context.is_complete("system"):
            touched_refs.update(entry.ref for entry in context.refs)
        for entry in context.refs:
            if entry.ref not in touched_refs:
                continue
            current = self._exact_item(entry.exact_id)
            if current is None or self._revision(current) != entry.revision:
                return self._context_recovery(
                    code="context_conflict",
                    instruction=(
                        "Relevant Things data changed. Read it and prepare again."
                    ),
                    retry="read",
                    read=context.selector.recovery_arguments(),
                    status="stale",
                )
        compile_call = call
        if context.is_complete("system"):
            current_system_ids = {item.id for item in self._library.system()}
            context_system_ids = {entry.exact_id for entry in context.refs}
            if current_system_ids != context_system_ids:
                return self._context_recovery(
                    code="context_conflict",
                    instruction=(
                        "The Area or Project registry changed. Read it again."
                    ),
                    retry="read",
                    read=context.selector.recovery_arguments(),
                    status="stale",
                )
            if call.scope_revision is None:
                compile_call = call.model_copy(
                    update={"scope_revision": self._area_scope_revision()}
                )
        elif call.scope_revision is None and any(
            entry.exact_id.startswith("area:") for entry in context.refs
        ):
            compile_call = call.model_copy(
                update={"scope_revision": self._area_scope_revision()}
            )
        try:
            return self._contextual_compiler.compile(
                compile_call, context, self._library
            )
        except ContextualInputError as error:
            return self._needs_input(str(error))
        except (ContextualCompileError, ContextConflict, UnknownReference) as error:
            message = str(error).casefold()
            incomplete = "incomplete" in message or "complete project scope" in message
            return self._context_recovery(
                code="context_incomplete" if incomplete else "context_conflict",
                instruction=(
                    "The saved context is incomplete for that change. Read it again."
                    if incomplete
                    else "Relevant Things data changed. Read it and prepare again."
                ),
                retry="read",
                read=context.selector.recovery_arguments(),
                status="stale",
            )

    @staticmethod
    def _context_recovery(
        *,
        code: Literal[
            "context_required",
            "context_expired",
            "context_incomplete",
            "context_conflict",
            "context_corrupt",
        ],
        instruction: str,
        retry: Literal["read", "same", "rebuild"],
        status: ResultStatus,
        read: dict[str, object] | None = None,
    ) -> Result:
        return Result(
            next="read" if retry in {"read", "rebuild"} else "retry_same",
            status=status,
            instruction=instruction,
            recovery=RecoveryFact(code=code, retry=retry, read=read),
        )

    def approve(self, call: ApproveCall) -> Result:
        stored = self._journal.get_by_plan_id(call.plan_id)
        if stored is None:
            return self._rejected("That plan does not exist or is no longer available.")
        if (
            stored.state in {"applied", "unchanged", "stale"}
            and stored.result is not None
        ):
            return Result.model_validate(stored.result)
        if stored.state == "pending":
            return self._resume(stored, allow_apply=False)
        if stored.state != "needs_approval":
            return self._rejected("That plan cannot be approved.")
        if (
            stored.expires_at is None
            or datetime.fromisoformat(stored.expires_at) <= self._clock()
        ):
            result = self._stale(
                "That plan expired. Read current facts and prepare it again."
            )
            self._save_result(stored, "stale", result)
            return result
        failed = self._refresh(force=True)
        if failed is not None:
            return failed
        if self._preconditions_changed(stored.plan):
            result = self._stale(
                "Relevant Things data changed. Read it and prepare a new plan."
            )
            self._save_result(stored, "stale", result)
            return result
        return self._apply(stored)

    def _view_items(self, call: ReadCall) -> list[Record] | Result:
        view = call.view or "today"
        today = self._clock().date()
        if view == "today":
            return self._library.today(
                waiting_tag=self._library.waiting_tag(), today=today
            )
        if view == "inbox":
            return self._library.inbox(limit=10_000)
        if view == "week":
            return self._library.week(today=today, limit=10_000)
        if view == "trash":
            return self._library.trash()
        if view == "system":
            return self._library.system()
        if view == "area":
            container = call.within or call.id
            assert container is not None
            area = self._exact_item(container)
            if area is None or area.kind != "area":
                return self._needs_input("I could not find that exact Area.")
            return self._library.area(area.id)
        if view == "audit":
            return self._library.audit()
        if view == "project":
            container = call.within or call.id
            assert container is not None
            project = self._exact_item(container)
            if project is None or project.kind != "project":
                return self._needs_input("I could not find that exact Project.")
            return self._library.project(project.id)
        if view == "logbook":
            start, end = self._logbook_range(call)
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

    def _search(
        self, text: str, within: Record | None, *, closed: bool = False
    ) -> list[Record]:
        needle = _normalize_search_text(text)
        items = [
            item
            for item in self._library.records.values()
            if self._is_searchable(item, closed=closed)
            and self._search_item(item, needle)
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
                items = [
                    item
                    for item in items
                    if item.uuid == within.uuid or item.parent_uuid == within.uuid
                ]
        return sorted(items, key=lambda item: (item.sort_index, item.title))

    @staticmethod
    def _inflected_token(word: str, token: str) -> bool:
        if word == token:
            return True
        suffixes = {"s", "es", "ed", "ing"}
        if len(word) >= 4 and token.startswith(word) and token[len(word) :] in suffixes:
            return True
        if len(token) >= 4 and word.startswith(token) and word[len(token) :] in suffixes:
            return True
        return False

    @staticmethod
    def _search_item(item: Record, needle: _NormalizedSearchText) -> bool:
        fields = (item.title, item.notes, *(row.title for row in item.checklists))
        if any(needle.folded in field.casefold() for field in fields):
            return True

        # Retry only with whole content words after the normal substring search
        # finds nothing. Articles are safe filler to ignore; all other words
        # must occur in one field. This avoids stemming and fuzzy matches.
        terms = {token for token in needle.tokens if token not in _SEARCH_ARTICLES}
        if not terms:
            return False
        for text in fields:
            words = set(_normalize_search_text(text).tokens)
            if terms.issubset(words):
                return True
            if all(
                any(ThingsWorkspace._inflected_token(word, term) for word in words)
                for term in terms
            ):
                return True
        return False

    @staticmethod
    def _is_searchable(item: Record, *, closed: bool = False) -> bool:
        """Search active work, or any record when ending or restoring work."""

        if closed:
            return True
        return (
            item.status == "open"
            and not item.trashed
            and item.recurrence.role != "template"
        )

    def _filter_audit_items(
        self, items: Sequence[Record], signals_any: Sequence[str]
    ) -> list[Record]:
        if not signals_any:
            return list(items)
        wanted = set(signals_any)
        return [
            item
            for item in items
            if wanted.intersection(
                self._item_signals(
                    item,
                    checklist_truncated=False,
                    tags_truncated=False,
                    notes_truncated=False,
                )
            )
        ]

    def _page(
        self,
        items: list[Record],
        limit: int,
        *,
        full: bool,
        instruction: str,
        view: View | None = None,
        public_scope: str | None = None,
        result_signals: list[str] | None = None,
        extra_truncated: bool = False,
        missing_ids: list[str] | None = None,
        detail: tuple[str, ...] = DETAIL_FIELDS,
        membership_revision: str | None = None,
        call: ReadCall | None = None,
    ) -> Result:
        limit = min(limit, _READ_LIMIT)
        page_records = items[:limit]
        facts = [
            self._fact(item, full=full, detail=detail) for item in page_records
        ]
        snapshot = self._scope_revision(items)
        scope = public_scope or snapshot
        all_ids = [item.id for item in items]
        cursor = None
        if len(items) > limit:
            cursor = self._encode_cursor(
                all_ids,
                limit,
                snapshot,
                scope,
                full,
                view,
                detail,
                within=call.within if call is not None else None,
                item_id=call.id if call is not None else None,
                from_date=call.from_date if call is not None else None,
                to_date=call.to_date if call is not None else None,
                signals_any=call.signals_any if call is not None else (),
                membership_revision=membership_revision,
            )
        sections = self._sections(view, facts) if view is not None else []
        extra_truncated = extra_truncated or (
            view == "audit" and self._audit_sections_truncated(facts)
        )
        empty = {
            "today": "Nothing on Today.",
            "week": "Nothing on the week.",
            "inbox": "Inbox is empty.",
            "diagnostics": "No native-state conflicts are visible.",
            "logbook": "Nothing in the Logbook for that range.",
        }.get(view or "", "No matching work is visible. Search with find and one title token.")
        visible = bool(facts or result_signals)
        if view == "logbook" and visible and call is not None:
            start, end = self._logbook_range(call)
            instruction = (
                f"{instruction.rstrip('.')} from {start.isoformat()} to "
                f"{end.isoformat()}."
            )
        result = Result(
            next="done",
            status="ok",
            instruction=instruction if visible else empty,
            items=facts,
            sections=sections,
            signals=result_signals or [],
            scope_revision=scope,
            cursor=cursor,
            missing_ids=missing_ids or [],
            truncated=cursor is not None or extra_truncated,
        )
        if full:
            result = self._enforce_bulk_result(
                result,
                all_ids=all_ids,
                page_start=0,
                snapshot=snapshot,
                scope=scope,
                view=view,
                detail=detail,
            )
        result = self._follow_cursor(result)
        return self._bind_review_context(result, call, page_records)

    def _continue(self, cursor: str, limit: int, *, view: View | None = None) -> Result:
        detail_saved = self._detail_cursors.get(cursor)
        if detail_saved is not None:
            if error := self._cursor_view_error(view, expected=None, repeatable=False):
                return self._needs_input(error)
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
            if error := self._cursor_view_error(view, expected="tags"):
                return self._needs_input(error)
            if (
                tag_saved.expires_at <= self._clock()
                or self._tag_revision() != tag_saved.revision
            ):
                return self._stale("That tag result changed. Start the read again.")
            return self._tag_page(tag_saved.rows, offset=tag_saved.offset, limit=limit)
        saved = self._cursors.get(cursor)
        if saved is None:
            return self._stale("That cursor is invalid. Start the read again.")
        if error := self._cursor_view_error(view, expected=saved.view):
            return self._needs_input(error)
        if saved.expires_at <= self._clock():
            return self._stale("That cursor expired. Start the read again.")
        if saved.view == "diagnostics":
            continued = ReadCall.model_validate(
                {"view": "diagnostics", "limit": limit}
            )
            return self._diagnostics_page(
                limit,
                offset=saved.offset,
                expected_ids=saved.ids,
                expected_digest=saved.snapshot_revision,
                call=continued,
                existing_context_id=saved.context_id,
            )
        if saved.view == "weekly_review":
            continued = ReadCall.model_validate(
                {
                    "view": "weekly_review",
                    "limit": limit,
                    "category": saved.signals_any[0] if saved.signals_any else None,
                }
            )
            return self._weekly_review_page(
                continued,
                offset=saved.offset,
                expected_ids=saved.ids,
                expected_snapshot=saved.snapshot_revision,
                expected_membership=saved.membership_revision,
                existing_context_id=saved.context_id,
            )
        items = [
            item for value in saved.ids if (item := self._exact_item(value)) is not None
        ]
        if (
            len(items) != len(saved.ids)
            or self._scope_revision(items) != saved.snapshot_revision
            or (
                saved.view == "audit"
                and self._scope_revision(self._library.audit())
                != saved.membership_revision
            )
            or (
                saved.view in {"system", "audit"}
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
                saved.detail,
                within=saved.within,
                item_id=saved.item_id,
                from_date=saved.from_date,
                to_date=saved.to_date,
                signals_any=saved.signals_any,
                membership_revision=saved.membership_revision,
            )
            if next_offset < len(items)
            else None
        )
        facts = [
            self._fact(item, full=saved.full, detail=saved.detail)
            for item in page_items
        ]
        sections = self._sections(saved.view, facts) if saved.view is not None else []
        extra_truncated = (
            saved.view == "audit" and self._audit_sections_truncated(facts)
        )
        result = Result(
            next="done",
            status="ok",
            instruction="Continue with these current facts.",
            items=facts,
            sections=sections,
            scope_revision=saved.public_scope_revision,
            cursor=next_cursor,
            truncated=next_cursor is not None or extra_truncated,
        )
        if saved.full:
            result = self._enforce_bulk_result(
                result,
                all_ids=saved.ids,
                page_start=saved.offset,
                snapshot=saved.snapshot_revision,
                scope=saved.public_scope_revision,
                view=saved.view,
                detail=saved.detail,
            )
        result = self._follow_cursor(result)
        continued = self._call_from_cursor(saved, limit)
        return self._bind_review_context(
            result,
            continued,
            page_items,
            existing_context_id=saved.context_id,
        )

    @staticmethod
    def _cursor_view_error(
        requested: View | None,
        *,
        expected: View | None,
        repeatable: bool = True,
    ) -> str | None:
        if requested is None or (repeatable and requested == expected):
            return None
        if expected is None:
            return "Use this cursor without view."
        return f"That cursor belongs to view {expected}."

    def _encode_cursor(
        self,
        ids: list[str],
        offset: int,
        snapshot_revision: str,
        public_scope_revision: str,
        full: bool,
        view: View | None,
        detail: tuple[str, ...] = (),
        *,
        within: str | None = None,
        item_id: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        signals_any: Sequence[str] = (),
        membership_revision: str | None = None,
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
            detail=detail,
            expires_at=self._clock() + timedelta(minutes=10),
            within=within,
            item_id=item_id,
            from_date=from_date,
            to_date=to_date,
            signals_any=tuple(signals_any),
            membership_revision=membership_revision,
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
        return self._follow_cursor(
            Result(
                next="done",
                status="ok",
                instruction=(
                    "These TagFact titles and ids are the catalog to reuse on "
                    "tag_ids and tags_add. Send this scope_revision as "
                    "tags_revision only with change_tags."
                ),
                tags=page_rows,
                truncated=cursor is not None,
                cursor=cursor,
                scope_revision=expected,
            )
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
        linked = [candidate.id for candidate in self._library.recurrence_instances(item.uuid)]
        page_start = row_offset
        page_end = row_offset + min(limit, _READ_LIMIT)

        checklist_start = 0
        direct_start = len(checklist)
        inherited_start = direct_start + len(direct)
        linked_start = inherited_start + len(inherited)
        total = linked_start + len(linked)

        def bounds(group_start: int, length: int) -> slice:
            start = max(page_start - group_start, 0)
            end = max(min(page_end - group_start, length), 0)
            return slice(start, end)

        checklist_page = checklist[bounds(checklist_start, len(checklist))]
        direct_page = direct[bounds(direct_start, len(direct))]
        inherited_page = inherited[bounds(inherited_start, len(inherited))]
        linked_page = linked[bounds(linked_start, len(linked))]
        next_row_offset = min(page_end, total)
        include_notes = note_offset < len(item.notes) or (
            note_offset == 0 and not item.notes
        )
        next_note_offset = (
            (min(note_offset + _NOTES_LIMIT, len(item.notes)) if item.notes else 1)
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
        return self._follow_cursor(
            Result(
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
                        and next_row_offset < linked_start,
                        notes_truncated=notes_remaining,
                        linked_item_ids=linked_page,
                        links_truncated=next_row_offset < linked_start + len(linked),
                    )
                ],
                scope_revision=revision,
                cursor=next_cursor,
                truncated=next_cursor is not None,
            )
        )

    @staticmethod
    def _follow_cursor(result: Result) -> Result:
        """Keep next honest: a cursor means the model should read again."""
        if result.cursor is None:
            return result
        instruction = result.instruction
        if "cursor" not in instruction.casefold():
            instruction = instruction.rstrip(".") + ". Continue this cursor for the rest."
        return result.model_copy(update={"next": "read", "instruction": instruction})

    def _prune_cursors(self) -> None:
        now = self._clock()
        self._cursors = {
            key: value for key, value in self._cursors.items() if value.expires_at > now
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
                ("evening", "Evening"),
                ("today", "Today"),
                ("waiting", "Waiting"),
            ]
            sections = []
            used: set[str] = set()
            for signal, title in groups:
                selected = [
                    item
                    for item in items
                    if signal in item.signals and item.id not in used
                ]
                if selected:
                    sections.append(
                        ReviewSection(
                            key=signal,
                            title=title,
                        )
                    )
                    used.update(item.id for item in selected)
            return sections
        if view == "system":
            return [
                ReviewSection(
                    key="system",
                    title="Areas and Projects",
                )
            ]
        if view == "audit":
            homes: dict[str, list[str]] = {}
            home_titles: dict[str, str] = {}
            for item in items:
                if item.kind in {"area", "project"}:
                    home = item.id
                    home_titles[home] = item.title
                else:
                    home = item.into_id or "unfiled"
                    if home not in home_titles:
                        home_titles[home] = self._audit_home_title(home)
                homes.setdefault(home, []).append(item.id)
            return [
                ReviewSection(
                    key=home[:80],
                    title=home_titles[home][:200],
                    item_ids=ids[:40],
                )
                for home, ids in homes.items()
            ][:40]
        titles = {
            "area": next(
                (item.title[:200] for item in items if item.kind == "area"),
                "Area",
            ),
            "audit": "Active items",
            "diagnostics": "Conflicts",
            "trash": "Trash",
            "inbox": "Inbox",
            "week": "Week",
            "logbook": "Logbook",
            "tags": "Tags",
            "project": "Project",
        }
        return [
            ReviewSection(
                key=view,
                title=titles.get(view, view.title()),
            )
        ]

    def _audit_home_title(self, home: str) -> str:
        if home == "unfiled":
            return "Unfiled"
        item = self._exact_item(home)
        return item.title if item is not None else home

    def _audit_sections_truncated(self, facts: list[ItemFact]) -> bool:
        homes = {
            item.id if item.kind in {"area", "project"} else item.into_id or "unfiled"
            for item in facts
        }
        return len(homes) > 40

    def _enforce_bulk_result(
        self,
        result: Result,
        *,
        all_ids: list[str],
        page_start: int,
        snapshot: str,
        scope: str,
        view: View | None,
        detail: tuple[str, ...] = DETAIL_FIELDS,
    ) -> Result:
        facts = list(result.items)
        tags: list[TagFact] = []
        if "tags" in detail:
            facts, tags = _hoist_bulk_tags(facts)
            facts = _normalize_bulk_tag_ids(facts)
        facts = _bound_bulk_text(facts)
        tags = _prune_unused_tags(facts, tags)
        result = result.model_copy(update={"items": facts, "tags": tags})
        while _result_bytes(result) > _BULK_WIRE_BUDGET:
            result = result.model_copy(
                update={
                    "signals": list(
                        dict.fromkeys([*result.signals, "wire_trimmed"])
                    )
                }
            )
            reduced = _trim_optional_detail(result)
            if reduced is not None:
                result = reduced
                continue
            if len(result.items) > 1:
                kept = list(result.items[:-1])
                offset = page_start + len(kept)
                cursor = (
                    self._encode_cursor(
                        all_ids, offset, snapshot, scope, True, view, detail
                    )
                    if offset < len(all_ids)
                    else None
                )
                result = result.model_copy(
                    update={
                        "items": kept,
                        "tags": _prune_unused_tags(kept, list(result.tags)),
                        "cursor": cursor,
                        "truncated": True,
                    }
                )
                continue
            return Result(
                next="read",
                status="needs_input",
                instruction=(
                    "That item is too large for a bulk ids read. "
                    f"Read {result.items[0].id} as an exact id."
                ),
                signals=["wire_trimmed"],
            )
        return result

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
            self._tag_fact(uuid)
            for uuid in item.tag_uuids
            if uuid in self._library.tags
        ]
        inherited: list[TagFact] = []
        for source in self._tag_sources(item):
            for uuid in source.tag_uuids:
                if uuid in self._library.tags and all(
                    tag.id != f"tag:{uuid}" for tag in inherited
                ):
                    inherited.append(self._tag_fact(uuid, from_id=source.id))
        return checklist, direct, inherited

    def _tag_fact(self, uuid: str, *, from_id: str | None = None) -> TagFact:
        return TagFact(
            id=f"tag:{uuid}",
            title=_bounded_tag_title(self._library.tags[uuid]),
            parent_ids=[
                f"tag:{parent}"
                for parent in self._library.tag_parents.get(uuid, [])[:20]
            ],
            parents_truncated=len(self._library.tag_parents.get(uuid, [])) > 20,
            from_id=from_id,
        )

    def _fact(
        self,
        item: Record,
        *,
        full: bool,
        include_revision: bool = True,
        include_notes: bool = True,
        note_offset: int = 0,
        checklist: list[ChecklistFact] | None = None,
        direct_tags: list[TagFact] | None = None,
        inherited_tags: list[TagFact] | None = None,
        checklist_truncated: bool = False,
        tags_truncated: bool = False,
        notes_truncated: bool = False,
        linked_item_ids: list[str] | None = None,
        links_truncated: bool = False,
        detail: tuple[str, ...] = DETAIL_FIELDS,
    ) -> ItemFact:
        want_notes = "notes" in detail
        want_checklist = "checklist" in detail
        want_tags = "tags" in detail
        want_recurrence = "recurrence" in detail
        if full and (
            checklist is None or direct_tags is None or inherited_tags is None
        ):
            if want_checklist or want_tags:
                checklist, direct_tags, inherited_tags = self._detail_lists(item)
                if not want_checklist:
                    checklist = []
                if not want_tags:
                    direct_tags, inherited_tags = [], []
            else:
                checklist, direct_tags, inherited_tags = [], [], []
            if want_checklist and len(checklist) > 100:
                checklist = checklist[:100]
                checklist_truncated = True
            if want_tags and len(direct_tags) > 40:
                direct_tags = direct_tags[:40]
                tags_truncated = True
            if want_tags and len(inherited_tags) > 40:
                inherited_tags = inherited_tags[:40]
                tags_truncated = True
        checklist = checklist or []
        direct_tags = direct_tags or []
        inherited_tags = inherited_tags or []
        computed_links = (
            [
                candidate.id
                for candidate in self._library.recurrence_instances(item.uuid)
            ]
            if full and want_recurrence and item.recurrence.role == "template"
            else []
        )
        if linked_item_ids is None:
            linked_ids = computed_links[:40]
            links_truncated = links_truncated or len(computed_links) > 40
        else:
            linked_ids = linked_item_ids
        compact_direct_tag_ids: list[str] = []
        if not full and want_tags:
            compact_direct_tag_ids = [
                f"tag:{uuid}"
                for uuid in item.tag_uuids
                if uuid in self._library.tags
            ]
            if len(compact_direct_tag_ids) > 40:
                compact_direct_tag_ids = compact_direct_tag_ids[:40]
                tags_truncated = True
        recurrence_kind = self._recurrence_kind(item)
        recurrence = None
        if recurrence_kind != "none":
            rule = item.recurrence
            if item.recurrence.role == "instance":
                template_record = self._library.records.get(
                    template_uuid_of(item) or ""
                )
                if template_record is not None and template_record.recurrence.rule:
                    rule = template_record.recurrence
            recurrence = RecurrenceFact(
                kind=recurrence_kind,
                template_id=(
                    f"task:{template}"
                    if (template := template_uuid_of(item))
                    else None
                ),
                mode=(
                    cast(RepeatMode, rule.repeat_type)
                    if rule.repeat_type in {"fixed", "after_completion"}
                    else None
                ),
                unit=rule.unit,
                interval=rule.interval,
                weekdays=[
                    cast(Weekday, _WEEKDAY_NAMES[code])
                    for code in rule.weekday_codes
                    if code in _WEEKDAY_NAMES
                ],
                linked_item_ids=linked_ids[:40],
            )
        into_id = (
            f"project:{item.parent_uuid}"
            if item.parent_uuid
            else f"area:{item.area_uuid}"
            if item.area_uuid
            else None
        )
        heading_id = f"heading:{item.heading_uuid}" if item.heading_uuid else None
        parent_name = self._library.parent_title(item) if into_id else None
        heading_name = self._library.heading_title(item) if heading_id else None
        return ItemFact(
            id=item.id,
            revision=self._revision(item) if include_revision else None,
            kind=item.public_kind,
            title=_bounded_title(item.title),
            status=_public_status(item.status),
            into_id=into_id,
            into_title=_bounded_title(parent_name) if parent_name else None,
            heading_id=heading_id,
            heading_title=_bounded_title(heading_name) if heading_name else None,
            notes_markdown=(
                item.notes[note_offset : note_offset + _NOTES_LIMIT]
                if full and include_notes and want_notes
                else None
            ),
            checklist=checklist,
            direct_tags=direct_tags,
            inherited_tags=inherited_tags,
            direct_tag_ids=compact_direct_tag_ids,
            start=item.start.isoformat()
            if item.start
            else "someday"
            if item.someday
            else None,
            deadline=item.deadline.isoformat() if item.deadline else None,
            remind_at=self._reminder(item),
            recurrence=recurrence,
            order=_bounded_order(item.sort_index) if full else None,
            today_order=(
                _bounded_order(item.today_index)
                if item.start == self._clock().date() or item.tonight
                else None
            ),
            truncated_fields=_truncated_fields(
                notes=notes_truncated
                or (
                    full
                    and want_notes
                    and include_notes
                    and note_offset + _NOTES_LIMIT < len(item.notes)
                ),
                checklist=checklist_truncated,
                tags=tags_truncated,
                recurrence=links_truncated,
            ),
            signals=self._item_signals(
                item,
                checklist_truncated=checklist_truncated,
                tags_truncated=tags_truncated,
                notes_truncated=(
                    notes_truncated
                    or (
                        full
                        and want_notes
                        and include_notes
                        and note_offset + _NOTES_LIMIT < len(item.notes)
                    )
                ),
                links_truncated=links_truncated,
            ),
        )

    def _tag_sources(self, item: Record) -> list[Record]:
        sources: list[Record] = []
        if item.parent_uuid and (parent := self._library.records.get(item.parent_uuid)):
            sources.append(parent)
            if parent.area_uuid and (
                area := self._library.records.get(parent.area_uuid)
            ):
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
                (
                    self._recurrence_scope_revision(item.uuid)
                    if item.recurrence.role == "template"
                    else None
                ),
            ]
        )

    def _item_signals(
        self,
        item: Record,
        *,
        checklist_truncated: bool,
        tags_truncated: bool,
        notes_truncated: bool,
        links_truncated: bool = False,
    ) -> list[str]:
        today = self._clock().date()
        conflicts = item_conflicts(item, self._library)
        ordinary: list[str] = []
        waiting = self._library.tag_uuid(self._library.waiting_tag())
        if item.deadline and item.deadline < today:
            ordinary.append("overdue")
        elif item.deadline == today:
            ordinary.append("today")
        if item.inbox:
            ordinary.append("inbox")
        if item.start == today:
            ordinary.append("today")
        if item.tonight:
            ordinary.append("evening")
        effective_tags = {
            *item.tag_uuids,
            *(tag for source in self._tag_sources(item) for tag in source.tag_uuids),
        }
        if waiting and waiting in effective_tags:
            ordinary.append("waiting")
        if item.someday:
            ordinary.append("someday")
        if item.trashed:
            ordinary.append("trashed")
        if item.heading and self._heading_hidden_occupancy(item.uuid)[0]:
            ordinary.append("has_hidden_occupants")
        if item.notes:
            ordinary.append("has_notes")
        if item.checklists:
            ordinary.append("has_checklist")
        if item.recurrence.role != "none" and item.recurrence.repeat_type == "unknown":
            ordinary.append("recurrence_unknown")
        extras = ["recurrence_links_truncated"] if links_truncated else []
        return _signals_with_truncation(
            [*conflicts, *ordinary],
            _truncated_fields(
                notes=notes_truncated,
                checklist=checklist_truncated,
                tags=tags_truncated,
            ),
            *extras,
        )

    def _reminder(self, item: Record) -> str | None:
        if item.remind is None or item.start is None:
            return None
        try:
            hour_text, minute_text = item.remind.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
            tz = self._clock().tzinfo
            return datetime.combine(
                item.start, time(hour, minute), tzinfo=tz
            ).isoformat()
        except (TypeError, ValueError):
            return None

    def _prepare(
        self, call: CommitCall, *, contextual_commit: bool = False
    ) -> _Prepared:
        if any(is_stripped_source_skeleton(entry) for entry in call.create):
            raise _Abort(
                self._revise(
                    "Revise this source-shaped Project before writing. Send "
                    "document=source with semantic Project fields and a finish for "
                    "every Task. Do not ask the owner."
                )
            )
        source_entries = [entry for entry in call.create if is_source_document(entry)]
        if source_entries and (
            len(call.create) != 1
            or call.change
            or call.organize
            or call.ensure_tags
            or call.change_tags
        ):
            raise _Abort(
                self._revise(
                    "Revise this source Project before writing. Send it as the only "
                    "mutation in one things_commit. Do not ask the owner."
                )
            )
        preferences = Preferences()
        if any(entry.kind == "project" for entry in call.create):
            try:
                preferences = self._preferences()
            except PreferencesError as error:
                raise _Abort(
                    self._rejected(
                        f"Saved Project preferences are invalid: {error}. Fix the "
                        "serving host preferences file or run things-orchestrator "
                        "configure again, then retry this commit."
                    )
                ) from error
        context = self._preparation_context(
            call, contextual_commit=contextual_commit
        )
        self._prepare_tag_registry(call, context)
        self._prepare_items(call, context, preferences=preferences)
        self._finish_preparation(call, context)
        return context.result()

    def _prepare_items(
        self,
        call: CommitCall,
        context: _PreparationContext,
        *,
        preferences: Preferences,
    ) -> None:
        # Pre-index heading moves. A Task that follows its heading in the same
        # merge must retain that heading UUID after both records enter the
        # destination Project.
        for change in call.change:
            if change.id is None or (
                "into" not in change.model_fields_set
                and "into_title" not in change.model_fields_set
            ):
                continue
            item = self._library.records.get(parse_id(change.id)[1])
            if item is None or not item.heading:
                continue
            context.project_heading_moves[item.uuid] = (
                self._require_heading_destination(item, change, call, context)
            )
        self._prepare_creates(call, context, preferences=preferences)
        self._prepare_changes(call, context)

    def _prepare_creates(
        self,
        call: CommitCall,
        context: _PreparationContext,
        *,
        preferences: Preferences,
    ) -> None:
        """Plan all create entries, including generated copies and children."""
        local = context.local
        writes = context.writes
        preconditions = context.preconditions
        summary = context.summary
        warnings = context.warnings
        for entry in call.create:
            try:
                entry = compile_project_document(
                    entry,
                    style=entry.note_style or preferences.note_style,
                    allowed_source_schemes=preferences.source_schemes,
                )
            except SourceDocumentError as error:
                raise _Abort(self._revise(str(error))) from error
            if entry.kind in {"task", "project"}:
                dump = _undistilled_create(entry)
                if dump is not None:
                    result = (
                        self._revise(f"{dump} Revise the payload. Do not ask the owner.")
                        if entry.document == "source"
                        else self._needs_input(dump)
                    )
                    raise _Abort(result)
            uuid = local[entry.key][0] if entry.key else new_uuid()
            create_titles = (
                [(entry.kind, entry.title)]
                if entry.kind in {"task", "project", "area"}
                else []
            )
            create_titles.extend(("task", task.title) for task in entry.tasks)
            for kind, title in create_titles:
                twins = [
                    item
                    for item in self._library.records.values()
                    if item.kind == kind
                    and item.is_open()
                    and item.title.casefold() == title.casefold()
                ]
                if len(twins) == 1:
                    label = {"task": "Task", "project": "Project", "area": "Area"}[kind]
                    identity = (
                        f"{twins[0].title} already exists. Change that {label}."
                        if kind == "task"
                        else (
                            f"{twins[0].title} already exists as {twins[0].id}. "
                            f"Change that {label}."
                        )
                    )
                    reminder = (
                        " If this is a reminder, ask for the clock time."
                        if kind == "task"
                        else ""
                    )
                    raise _Abort(self._needs_input(f"{identity}{reminder}"))
            if entry.kind == "heading":
                home = self._home(
                    entry.into,
                    "task",
                    local,
                    new_item=True,
                    into_title=entry.into_title,
                )
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
                        sort_index=max(heading_indexes, default=0) + 1024,
                    )
                )
                if "after" in entry.model_fields_set:
                    writes.extend(
                        self._project_heading_order_writes(
                            _HeadingOrderRow(
                                uuid=uuid,
                                sort_index=writes[-1].sort_index or 0,
                                create_index=len(writes) - 1,
                            ),
                            project_uuid=home[0],
                            after=entry.after,
                            local=local,
                            planned=writes,
                            preconditions=preconditions,
                        )
                    )
                summary.append(f"Create heading: {entry.title}")
                continue
            home = self._home(
                entry.into,
                entry.kind,
                local,
                new_item=True,
                into_title=entry.into_title,
            )
            start, someday, tonight, remind = self._schedule_input(
                entry.start,
                entry.remind_at,
                start_present="start" in entry.model_fields_set,
            )
            if entry.into is None and (start is not None or someday or tonight):
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
            common_create = Write(
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
            repeat_template_uuid: str | None = None
            if entry.repeat is not None:
                repeat_template_uuid = new_uuid()
                rule = new_rule(
                    mode=entry.repeat.mode,
                    unit=entry.repeat.unit,
                    interval=entry.repeat.interval,
                    anchor=start or self._clock().date(),
                    weekday_codes=[
                        _WEEKDAY_CODES[weekday] for weekday in entry.repeat.weekdays
                    ],
                )
                writes.append(
                    replace(
                        common_create,
                        uuid=repeat_template_uuid,
                        recurrence_rule=rule,
                        sort_index=sort_index,
                        today_index=None,
                    )
                )
                common_create = replace(
                    common_create,
                    recurrence_links=[repeat_template_uuid],
                    recurrence_generated=True,
                )
                warnings.append("This creates future generated Tasks.")
                context.risky = True
            writes.append(common_create)
            if repeat_template_uuid is not None:
                writes.extend(
                    _new_checklist_writes(repeat_template_uuid, entry.checklist)
                )
            writes.extend(_new_checklist_writes(uuid, entry.checklist))
            current_heading: str | None = None
            heading_uuid: str | None = None
            heading_index = 0
            task_index = 0
            for task in entry.tasks:
                if task.heading_title != current_heading:
                    current_heading = task.heading_title
                    if current_heading is not None:
                        heading_uuid = new_uuid()
                        writes.append(
                            Write(
                                action="create_heading",
                                uuid=heading_uuid,
                                kind="task",
                                title=current_heading,
                                into_uuid=uuid,
                                into_kind="project",
                                anytime=True,
                                sort_index=(heading_index + 1) * 1024,
                            )
                        )
                        heading_index += 1
                task_uuid = new_uuid()
                writes.append(
                    Write(
                        action="create",
                        uuid=task_uuid,
                        kind="task",
                        title=task.title,
                        notes=task.notes_markdown,
                        into_uuid=uuid,
                        into_kind="project",
                        anytime=True,
                        sort_index=(task_index + 1) * 1024,
                        heading_uuid=heading_uuid,
                    )
                )
                writes.extend(_new_checklist_writes(task_uuid, task.checklist))
                task_index += 1
            summary.append(
                f"Create repeating {entry.kind}: {entry.title}"
                if entry.repeat is not None
                else f"Create {entry.kind}: {entry.title}"
            )
            if entry.tasks:
                summary.append(f"Add {len(entry.tasks)} Tasks to {entry.title}")
            context.risky = context.risky or entry.kind == "area"
            if entry.kind == "area":
                preconditions["scope:areas"] = self._area_scope_revision()
                warnings.append("The Area registry will change.")

    def _prepare_recurrence_change(
        self,
        item: Record,
        change: ChangeEntry,
        context: _PreparationContext,
        desired: _DesiredItemChange | None = None,
    ) -> bool:
        """Plan a repeat-rule change and report whether it ends item planning."""
        writes = context.writes
        preconditions = context.preconditions
        summary = context.summary
        warnings = context.warnings
        repeat_edit = change.repeat
        repeat_rule_changed = False
        repeat_interval = (
            change.repeat_interval
            if change.repeat_interval is not None
            else repeat_edit.interval
            if repeat_edit is not None
            else None
        )
        if (
            item.recurrence.role == "none"
            and repeat_edit is not None
            and not repeat_edit.remove
        ):
            if desired is None:
                raise RuntimeError("repeat conversion needs a projected item state")
            if item.kind != "task" or item.trashed or item.status != "open":
                raise _Abort(
                    self._rejected(
                        "Only an open Task outside Trash can start repeating."
                    )
                )
            if repeat_edit.unit is None:
                raise _Abort(
                    self._needs_input(
                        "Starting repetition needs day, week, month, or year."
                    )
                )
            if item.notes_format == "rich" and not change.replace_rich_note:
                raise _Abort(
                    self._unsupported(
                        "This Task has a rich note. Replace it explicitly with Markdown "
                        "before it becomes the future repeating template."
                    )
                )
            template_uuid = new_uuid()
            rule = new_rule(
                mode=repeat_edit.mode or "fixed",
                unit=repeat_edit.unit,
                interval=repeat_edit.interval or 1,
                anchor=desired.start or self._clock().date(),
                weekday_codes=(
                    [_WEEKDAY_CODES[weekday] for weekday in repeat_edit.weekdays]
                    if repeat_edit.weekdays is not None
                    else None
                ),
            )
            writes.append(
                Write(
                    action="create",
                    uuid=template_uuid,
                    kind="task",
                    title=desired.title,
                    notes=desired.notes,
                    into_uuid=desired.home[0],
                    into_kind=desired.home[1],
                    inbox=desired.home[2],
                    anytime=desired.home[3],
                    start=desired.start,
                    deadline=desired.deadline,
                    remind=desired.remind,
                    tonight=desired.tonight,
                    someday=desired.someday,
                    tag_uuids=desired.tag_uuids,
                    heading_uuid=desired.heading_uuid,
                    sort_index=desired.sort_index,
                    today_index=desired.today_index,
                    owner_today=self._clock().date(),
                    recurrence_rule=rule,
                )
            )
            for projected_row in desired.checklist.rows:
                writes.append(
                    Write(
                        action="checklist",
                        uuid=new_uuid(),
                        title=projected_row.title,
                        checklist_parent_uuid=template_uuid,
                        checklist_status="open",
                        checklist_index=projected_row.sort_index,
                    )
                )
            writes.append(
                Write(
                    action="repeat_link",
                    uuid=item.uuid,
                    kind="task",
                    recurrence_links=[template_uuid],
                    recurrence_generated=True,
                )
            )
            preconditions[f"scope:repeat:{template_uuid}"] = (
                self._recurrence_scope_revision(template_uuid)
            )
            summary.append(f"Start repeating: {item.title}")
            warnings.append(
                "This keeps the current Task and creates its future repeating template."
            )
            context.risky = True
            return False
        if change.repeat_interval is not None or repeat_edit is not None:
            if repeat_edit is not None and repeat_edit.remove:
                target = item
                if item.recurrence.role == "instance" and (
                    source := template_uuid_of(item)
                ):
                    template = self._library.records.get(source)
                    if template is not None:
                        target = template
                if any(
                    write.action == "permanent_delete" and write.uuid == target.uuid
                    for write in writes
                ):
                    return True
                try:
                    target.recurrence.validate_interval_template(kind=target.kind)
                except ValueError as error:
                    raise _Abort(self._unsupported(str(error))) from error
                linked = self._library.recurrence_instances(target.uuid)
                for candidate in linked:
                    preconditions[candidate.id] = self._revision(candidate)
                    writes.append(
                        Write(
                            action="repeat_link",
                            uuid=candidate.uuid,
                            kind="task",
                            recurrence_links=[],
                        )
                    )
                for checklist_row in target.checklists:
                    writes.append(
                        Write(
                            action="checklist",
                            uuid=checklist_row.uuid,
                            checklist_parent_uuid=target.uuid,
                            checklist_remove=True,
                        )
                    )
                writes.append(_delete_write(target))
                preconditions[target.id] = self._revision(target)
                preconditions[f"scope:repeat:{target.uuid}"] = (
                    self._recurrence_scope_revision(target.uuid)
                )
                summary.append(f"Stop repeating: {target.title}")
                warnings.append(
                    "The repeat template will be deleted. Linked copies stay as ordinary Tasks."
                )
                context.risky = True
                return True
            target = item
            if item.recurrence.role == "instance" and (
                source := template_uuid_of(item)
            ):
                template = self._library.records.get(source)
                if template is not None:
                    target = template
            try:
                if (
                    repeat_edit is not None
                    and repeat_edit.weekdays
                    and (repeat_edit.mode or target.recurrence.repeat_type) != "fixed"
                ):
                    raise ValueError("Weekdays need fixed repeat mode")
                recurrence = target.recurrence.transition(
                    kind=target.kind,
                    mode=repeat_edit.mode if repeat_edit else None,
                    unit=repeat_edit.unit if repeat_edit else None,
                    interval=repeat_interval,
                    weekday_codes=(
                        [_WEEKDAY_CODES[weekday] for weekday in repeat_edit.weekdays]
                        if repeat_edit is not None and repeat_edit.weekdays is not None
                        else None
                    ),
                )
            except ValueError as error:
                raise _Abort(self._unsupported(str(error))) from error
            writes.append(
                Write(
                    action="repeat",
                    uuid=target.uuid,
                    kind="task",
                    recurrence_rule=recurrence.rule,
                )
            )
            preconditions[target.id] = self._revision(target)
            preconditions[f"scope:repeat:{target.uuid}"] = (
                self._recurrence_scope_revision(target.uuid)
            )
            parts = []
            if repeat_edit is not None and repeat_edit.mode is not None:
                parts.append(f"mode to {repeat_edit.mode.replace('_', ' ')}")
            if repeat_edit is not None and repeat_edit.unit is not None:
                parts.append(f"unit to {repeat_edit.unit}")
            if repeat_interval is not None:
                parts.append(f"interval to {repeat_interval}")
            summary.append(f"Change repeat rule for {item.title}: {', '.join(parts)}")
            warnings.append("This changes future generated Tasks.")
            context.risky = True
            repeat_rule_changed = True
        if item.recurrence.role == "template" and not repeat_rule_changed:
            safe_template_fields = {
                "id",
                "if_revision",
                "title",
                "notes_markdown",
                "replace_rich_note",
                "waiting",
                "tags_add",
                "tags_remove",
                "checklist_add",
                "checklist_change",
                "checklist_remove",
                "checklist_order",
                "lifecycle",
                "trash",
                "delete_contents",
            }
            if change.model_fields_set - safe_template_fields:
                raise _Abort(
                    self._unsupported(
                        "Use repeat removal for template lifecycle changes."
                    )
                )
            warnings.append("This changes future generated Tasks.")
            context.risky = True
        if (
            item.recurrence.role == "instance"
            and item.recurrence.repeat_type == "unknown"
        ):
            raise _Abort(
                self._unsupported(
                    "This generated Task has an unknown repeat template. Read its "
                    "template before changing it."
                )
            )
        return False

    def _desired_item_change(
        self,
        item: Record,
        change: ChangeEntry,
        local: dict[str, tuple[str, Kind | str]],
        context: _PreparationContext,
    ) -> _DesiredItemChange:
        """Project one change for both its current item and a future template."""
        home = (
            self._home(
                change.into,
                item.kind,
                local,
                new_item=False,
                into_title=change.into_title,
            )
            if "into" in change.model_fields_set
            or "into_title" in change.model_fields_set
            else self._record_home(item)
        )
        start, someday, tonight, remind = self._schedule_input(
            change.start,
            change.remind_at,
            start_present="start" in change.model_fields_set,
            existing_tonight=item.tonight,
        )
        start_present = "start" in change.model_fields_set
        remind_present = "remind_at" in change.model_fields_set
        placement_clears_schedule = (
            "into" in change.model_fields_set
            or "into_title" in change.model_fields_set
        ) and (home[2] or home[3])
        if placement_clears_schedule:
            desired_start = None
            desired_someday = False
            desired_tonight = False
            desired_remind = None
        elif start_present:
            desired_start = start
            desired_someday = someday
            desired_tonight = tonight
            desired_remind = (
                remind
                if remind_present
                else item.remind
                if start is not None and not someday
                else None
            )
        elif change.remind_at is not None:
            desired_start = start
            desired_someday = someday
            desired_tonight = tonight
            desired_remind = remind
        else:
            desired_start = item.start
            desired_someday = item.someday
            desired_tonight = item.tonight
            desired_remind = None if remind_present else item.remind
        desired_deadline = (
            date.fromisoformat(change.deadline)
            if change.deadline
            else None
            if "deadline" in change.model_fields_set
            else item.deadline
        )

        deleted_tag_uuids = {
            write.uuid for write in context.writes if write.action == "delete_tag"
        }
        tags = [
            uuid for uuid in item.tag_uuids if uuid not in deleted_tag_uuids
        ]
        if change.tags_add or change.tags_remove:
            add = self._tag_ids(change.tags_add, local)
            remove = set(self._tag_ids(change.tags_remove, local))
            if remove.intersection(add):
                raise _Abort(
                    self._rejected("A tag cannot be added and removed in one change.")
                )
            tags = [uuid for uuid in tags if uuid not in remove]
            tags = list(dict.fromkeys([*tags, *add]))
        if change.waiting is not None:
            if change.waiting:
                waiting, tag_write = self._waiting_tag(context.writes)
                if tag_write:
                    context.writes.append(tag_write)
                tags = [uuid for uuid in tags if uuid != waiting]
                tags.append(waiting)
            else:
                existing_waiting = self._library.tag_uuid(self._library.waiting_tag())
                if existing_waiting is not None:
                    tags = [uuid for uuid in tags if uuid != existing_waiting]

        heading_uuid = item.heading_uuid
        clear_heading = (
            "into" in change.model_fields_set
            and item.heading_uuid is not None
            and (home[1] != "project" or home[0] != item.parent_uuid)
        )
        if (
            clear_heading
            and item.heading_uuid in context.project_heading_moves
            and context.project_heading_moves[item.heading_uuid] == home[0]
        ):
            clear_heading = False
        if clear_heading:
            heading_uuid = None
        if "heading_id" in change.model_fields_set:
            clear_heading = change.heading_id is None
            heading_uuid = None
            if change.heading_id is not None:
                if item.kind != "task":
                    raise _Abort(self._rejected("Only a Task can use a heading."))
                if home[1] != "project":
                    raise _Abort(
                        self._rejected("A heading needs a destination Project.")
                    )
                if change.heading_id.startswith("$"):
                    heading_uuid = local[change.heading_id][0]
                    heading_write = next(
                        (
                            write
                            for write in context.writes
                            if write.action == "create_heading"
                            and write.uuid == heading_uuid
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
                    heading = self._required_exact(change.heading_id)
                    if not heading.heading or heading.parent_uuid != home[0]:
                        raise _Abort(
                            self._rejected(
                                "The heading must belong to the Task's Project."
                            )
                        )
                    heading_uuid = heading.uuid
                    context.preconditions[heading.id] = self._revision(heading)

        sort_index = self._after_index(
            change.after,
            local,
            context.writes,
            kind=item.kind,
            home=home,
            present="after" in change.model_fields_set,
            moving_uuid=item.uuid,
            preconditions=context.preconditions,
        )
        desired_sort_index = sort_index if sort_index is not None else item.sort_index
        today_index = self._today_after_index(
            change.today_after,
            local,
            context.writes,
            present="today_after" in change.model_fields_set,
            on_today=(desired_start == self._clock().date() or desired_tonight),
            new_item=False,
            preconditions=context.preconditions,
            moving_uuid=item.uuid,
        )
        desired_today_index = (
            today_index if today_index is not None else item.today_index
        )
        notes = (
            change.notes_markdown or ""
            if "notes_markdown" in change.model_fields_set
            else item.notes
        )
        template_home = home
        if (
            "into" not in change.model_fields_set
            and home[3]
            and (desired_start is not None or desired_someday or desired_tonight)
        ):
            template_home = (None, None, False, False)
        update_home = (
            home
            if "into" in change.model_fields_set
            or "heading_id" in change.model_fields_set
            else (None, None, False, False)
        )
        return _DesiredItemChange(
            update=Write(
                action="update",
                uuid=item.uuid,
                kind=item.kind,
                title=change.title,
                notes=notes if "notes_markdown" in change.model_fields_set else None,
                status=_internal_status(change.status) if change.status else None,
                into_uuid=update_home[0],
                into_kind=update_home[1],
                inbox=update_home[2],
                anytime=update_home[3],
                start=start,
                clear_start="start" in change.model_fields_set and change.start is None,
                deadline=date.fromisoformat(change.deadline)
                if change.deadline
                else None,
                clear_deadline="deadline" in change.model_fields_set
                and change.deadline is None,
                remind=desired_remind if start_present else remind,
                clear_remind="remind_at" in change.model_fields_set
                and change.remind_at is None,
                tonight=tonight,
                someday=someday,
                tag_uuids=tags
                if change.tags_add or change.tags_remove or change.waiting is not None
                else None,
                sort_index=sort_index,
                today_index=today_index,
                owner_today=self._clock().date(),
                heading_uuid=(
                    heading_uuid
                    if "heading_id" in change.model_fields_set
                    or ("into" in change.model_fields_set and not clear_heading)
                    else None
                ),
                clear_heading=clear_heading,
            ),
            home=template_home,
            title=change.title or item.title,
            notes=notes,
            start=desired_start,
            deadline=desired_deadline,
            remind=desired_remind,
            tonight=desired_tonight,
            someday=desired_someday,
            tag_uuids=tags,
            heading_uuid=heading_uuid,
            sort_index=desired_sort_index,
            today_index=desired_today_index,
            checklist=self._project_checklist(item, change, local),
        )

    def _project_checklist(
        self,
        item: Record,
        change: ChangeEntry,
        local: dict[str, tuple[str, Kind | str]],
    ) -> _ChecklistProjection:
        """Project the final current checklist once for both repeat copies."""
        if not (
            change.checklist_add
            or change.checklist_change
            or change.checklist_remove
            or change.checklist_order is not None
        ):
            return _ChecklistProjection(
                rows=tuple(
                    _ProjectedChecklistRow(
                        uuid=row.uuid,
                        title=row.title,
                        status=row.status,
                        sort_index=row.sort_index,
                    )
                    for row in sorted(
                        item.checklists, key=lambda row: (row.sort_index, row.uuid)
                    )
                ),
                removed_uuids=(),
            )
        rows = {
            f"check:{row.uuid}": _ProjectedChecklistRow(
                uuid=row.uuid,
                title=row.title,
                status=row.status,
                sort_index=row.sort_index,
            )
            for row in item.checklists
        }
        order = [
            f"check:{row.uuid}"
            for row in sorted(
                item.checklists, key=lambda row: (row.sort_index, row.uuid)
            )
        ]

        def place(reference: str, after: str | None, *, present: bool) -> None:
            if not present:
                return
            order.remove(reference)
            if after is None:
                order.insert(0, reference)
                return
            if after not in order:
                raise _Abort(
                    self._needs_input(
                        f"Checklist row {after} is not available for final order."
                    )
                )
            order.insert(order.index(after) + 1, reference)

        for index, addition in enumerate(change.checklist_add):
            reference = addition.key or f"$checklist_{index}_{new_uuid()}"
            uuid = local[addition.key][0] if addition.key else new_uuid()
            rows[reference] = _ProjectedChecklistRow(
                uuid=uuid,
                title=addition.title,
                status="open",
                sort_index=0,
            )
            order.append(reference)
            place(
                reference,
                addition.after,
                present="after" in addition.model_fields_set,
            )
        for row_change in change.checklist_change:
            reference = row_change.id
            row = rows.get(reference)
            if row is None:
                raise _Abort(
                    self._needs_input(
                        f"Checklist row {reference} is not on {item.title}."
                    )
                )
            rows[reference] = replace(
                row,
                title=row_change.title or row.title,
                status=(
                    cast(Status, _internal_status(row_change.status))
                    if row_change.status
                    else row.status
                ),
            )
            place(
                reference,
                row_change.after,
                present="after" in row_change.model_fields_set,
            )
        removed_uuids: list[str] = []
        for reference in change.checklist_remove:
            row = rows.pop(reference, None)
            if row is None:
                raise _Abort(
                    self._needs_input(
                        f"Checklist row {reference} is not on {item.title}."
                    )
                )
            order.remove(reference)
            removed_uuids.append(row.uuid)
        if change.checklist_order is not None:
            if set(change.checklist_order) != set(order):
                raise _Abort(
                    self._rejected(
                        "checklist_order must name every remaining row once."
                    )
                )
            order = list(change.checklist_order)
        return _ChecklistProjection(
            rows=tuple(
                replace(rows[reference], sort_index=index * 1024)
                for index, reference in enumerate(order)
            ),
            removed_uuids=tuple(removed_uuids),
        )

    def _prepare_lifecycle_or_heading_change(
        self,
        item: Record,
        change: ChangeEntry,
        context: _PreparationContext,
        call: CommitCall,
    ) -> bool:
        """Plan heading and lifecycle changes that end normal item planning."""
        writes = context.writes
        preconditions = context.preconditions
        summary = context.summary
        warnings = context.warnings
        lifecycle = change.lifecycle or ("trash" if change.trash else None)
        if item.heading:
            if lifecycle == "trash":
                writes.append(Write(action="trash", uuid=item.uuid, kind="task"))
                summary.append(f"Trash heading: {item.title}")
                warnings.append(
                    f"{item.title} will move to Trash and can be restored in Things."
                )
                context.risky = True
                return True
            if lifecycle == "restore":
                if not item.trashed:
                    raise _Abort(self._rejected(f"{item.title} is not in Trash."))
                writes.append(Write(action="restore", uuid=item.uuid, kind="task"))
                summary.append(f"Restore heading: {item.title}")
                warnings.append(
                    f"{item.title} will return to its prior Things location."
                )
                context.risky = True
                return True
            if (
                "into" in change.model_fields_set
                or "into_title" in change.model_fields_set
            ):
                # A merge moves headings with their source Project. Their
                # assigned Tasks keep the heading UUID, so moving the heading
                # first preserves the source layout in the destination.
                self._require_heading_destination(item, change, call, context)
                desired = self._desired_item_change(
                    item, change, context.local, context
                )
                writes.append(desired.update)
                summary.append(f"Move heading: {item.title}")
                return True
            if lifecycle == "delete_permanently":
                assigned = [
                    child
                    for child in self._library.records.values()
                    if child.heading_uuid == item.uuid
                ]
                writes.append(_delete_write(item))
                summary.append(f"Permanently delete heading: {item.title}")
                if assigned:
                    hidden = [
                        child
                        for child in assigned
                        if child.trashed
                        or child.status != "open"
                        or child.recurrence.role == "template"
                    ]
                    open_assigned = len(assigned) - len(hidden)
                    parts: list[str] = []
                    if open_assigned:
                        parts.append(f"{open_assigned} open")
                    trashed = sum(1 for child in hidden if child.trashed)
                    completed = sum(
                        1
                        for child in hidden
                        if not child.trashed and child.status == "done"
                    )
                    canceled = sum(
                        1
                        for child in hidden
                        if not child.trashed and child.status == "dropped"
                    )
                    if trashed:
                        parts.append(f"{trashed} trashed")
                    if completed:
                        parts.append(f"{completed} completed")
                    if canceled:
                        parts.append(f"{canceled} canceled")
                    warnings.append(
                        f"{len(assigned)} assigned Tasks stay in the Project "
                        f"without this heading ({', '.join(parts)})."
                    )
                warnings.append("This heading deletion cannot be undone.")
                context.risky = True
                return True
            if change.title is not None:
                writes.append(
                    Write(
                        action="update", uuid=item.uuid, kind="task", title=change.title
                    )
                )
                summary.append(f"Rename heading: {item.title} to {change.title}")
            if "after" in change.model_fields_set:
                writes.extend(
                    self._heading_order_writes(
                        item,
                        after=change.after,
                        local=context.local,
                        planned=writes,
                        preconditions=preconditions,
                    )
                )
                summary.append(f"Reorder heading: {item.title}")
            return True
        if item.kind == "area" and change.status is not None:
            raise _Abort(self._rejected("Areas do not have a completion state."))
        if item.kind == "area" and "into" in change.model_fields_set:
            raise _Abort(self._rejected("Areas stay in the top-level registry."))
        if item.kind == "area":
            preconditions["scope:areas"] = self._area_scope_revision()

        if lifecycle is None:
            return False
        if item.kind not in {"task", "project"}:
            raise _Abort(self._rejected("Only a Task or Project has this lifecycle."))
        if item.kind == "project":
            preconditions[f"scope:project:{item.uuid}"] = self._project_scope_revision(
                item.uuid
            )
        if lifecycle == "trash":
            writes.append(Write(action="trash", uuid=item.uuid, kind=item.kind))
            summary.append(f"Trash {item.kind}: {item.title}")
            warnings.append(
                f"{item.title} will move to Trash and can be restored in Things."
            )
        elif lifecycle == "restore":
            if not item.trashed:
                raise _Abort(self._rejected(f"{item.title} is not in Trash."))
            writes.append(Write(action="restore", uuid=item.uuid, kind=item.kind))
            restored = 0
            if item.kind == "project":
                for descendant in reversed(self._project_descendants(item.uuid)):
                    if not descendant.trashed:
                        continue
                    preconditions[descendant.id] = self._revision(descendant)
                    writes.append(
                        Write(
                            action="restore",
                            uuid=descendant.uuid,
                            kind=descendant.kind,
                        )
                    )
                    restored += 1
            if restored:
                summary.append(
                    f"Restore {item.kind}: {item.title} and {restored} contained records"
                )
            else:
                summary.append(f"Restore {item.kind}: {item.title}")
            warnings.append(f"{item.title} will return to its prior Things location.")
        else:
            if not item.trashed:
                raise _Abort(
                    self._needs_input(
                        f"Move {item.title} to Trash before permanent deletion."
                    )
                )
            descendants = (
                self._project_descendants(item.uuid) if item.kind == "project" else []
            )
            if item.kind == "project" and descendants and not change.delete_contents:
                raise _Abort(
                    self._needs_input(
                        f"{item.title} still contains {len(descendants)} records. "
                        "Move them first or retry with delete_contents true."
                    )
                )
            if item.kind == "project" and change.delete_contents:
                for descendant in descendants:
                    preconditions[descendant.id] = self._revision(descendant)
                    for child_row in descendant.checklists:
                        writes.append(
                            Write(
                                action="checklist",
                                uuid=child_row.uuid,
                                checklist_parent_uuid=descendant.uuid,
                                checklist_remove=True,
                            )
                        )
                    writes.append(_delete_write(descendant))
            for item_row in item.checklists:
                writes.append(
                    Write(
                        action="checklist",
                        uuid=item_row.uuid,
                        checklist_parent_uuid=item.uuid,
                        checklist_remove=True,
                    )
                )
            writes.append(_delete_write(item))
            summary.append(f"Permanently delete {item.kind}: {item.title}")
            warnings.append("This deletion cannot be undone.")
            if descendants:
                warnings.append(
                    f"{len(descendants)} contained records will also be deleted."
                )
        context.risky = True
        return True

    def _prepare_checklist_change(
        self,
        item: Record,
        projection: _ChecklistProjection,
        context: _PreparationContext,
    ) -> None:
        """Move the current checklist to its already validated final projection."""
        existing = {row.uuid: row for row in item.checklists}
        for row in projection.rows:
            previous = existing.get(row.uuid)
            if previous is not None and (
                previous.title,
                previous.status,
                previous.sort_index,
            ) == (row.title, row.status, row.sort_index):
                continue
            context.writes.append(
                Write(
                    action="checklist",
                    uuid=row.uuid,
                    title=row.title
                    if previous is None or row.title != previous.title
                    else None,
                    checklist_parent_uuid=item.uuid,
                    checklist_status=(
                        row.status
                        if previous is None or row.status != previous.status
                        else None
                    ),
                    checklist_index=row.sort_index,
                )
            )
        for uuid in projection.removed_uuids:
            context.writes.append(
                Write(action="checklist", uuid=uuid, checklist_remove=True)
            )

    def _prepare_changes(self, call: CommitCall, context: _PreparationContext) -> None:
        """Plan existing-item changes as one atomic batch."""
        local = context.local
        writes = context.writes
        preconditions = context.preconditions
        summary = context.summary
        warnings = context.warnings
        self._validate_requested_project_destinations(call, context)
        for change in call.change:
            if change.id is None or change.if_revision is None:
                raise _Abort(
                    self._rejected(
                        "A context change must compile to an exact revision first."
                    )
                )
            item = self._required_exact(change.id)
            revision = self._revision(item)
            if revision != change.if_revision:
                raise _Abort(self._stale(f"{item.title} changed. Read it again."))
            preconditions[item.id] = revision
            if (
                item.notes_format == "rich"
                and "notes_markdown" in change.model_fields_set
            ):
                if not change.replace_rich_note:
                    raise _Abort(
                        self._unsupported(
                            "That note contains rich text. Retry with replace_rich_note true "
                            "only if the owner wants to replace all formatting with Markdown."
                        )
                    )
                warnings.append(
                    f"{item.title}'s rich formatting will be replaced with Markdown."
                )
                context.risky = True
            starts_repeating = (
                item.recurrence.role == "none"
                and change.repeat is not None
                and not change.repeat.remove
            )
            desired = (
                self._desired_item_change(item, change, local, context)
                if starts_repeating
                else None
            )
            if self._prepare_recurrence_change(item, change, context, desired):
                continue
            if self._prepare_lifecycle_or_heading_change(item, change, context, call):
                continue
            desired = desired or self._desired_item_change(item, change, local, context)
            writes_before_change = len(writes)
            if any(
                field in change.model_fields_set
                for field in {
                    "title",
                    "status",
                    "notes_markdown",
                    "into",
                    "start",
                    "deadline",
                    "remind_at",
                    "waiting",
                    "tags_add",
                    "tags_remove",
                    "after",
                    "today_after",
                    "heading_id",
                }
            ):
                if not self._writes_match([desired.update]):
                    writes.append(desired.update)

            self._prepare_checklist_change(item, desired.checklist, context)

            if change.move_contents_to is not None:
                if item.kind != "area":
                    raise _Abort(
                        self._rejected(
                            "Only an Area can move its contents as a registry change."
                        )
                    )
                target = self._required_exact(change.move_contents_to)
                if target.kind != "area" or target.uuid == item.uuid:
                    raise _Abort(
                        self._rejected("Area contents must move to another exact Area.")
                    )
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
                summary.append(
                    f"Move {len(children)} items from {item.title} to {target.title}"
                )
                summary.append(f"Remove empty Area: {item.title}")
                warnings.append("One Area will be removed.")
                context.risky = True
            elif change.remove_if_empty:
                if item.kind != "area":
                    raise _Abort(self._rejected("Only an empty Area can be removed."))
                if self._library.children_in_area(item.uuid):
                    raise _Abort(
                        self._needs_input(
                            f"{item.title} still contains work. Choose another Area."
                        )
                    )
                writes.append(Write(action="delete_area", uuid=item.uuid, kind="area"))
                summary.append(f"Remove empty Area: {item.title}")
                warnings.append("One Area will be removed.")
                context.risky = True
            else:
                if len(writes) == writes_before_change:
                    context.already_correct.append(item.id)
                    summary.append(f"Already correct: {item.title}")
                else:
                    summary.append(f"Change {item.kind}: {item.title}")
                if item.kind == "area":
                    context.risky = True
                    warnings.append("The Area registry will change.")
                if item.kind == "project" and change.status in {
                    "completed",
                    "canceled",
                }:
                    open_children = [
                        child
                        for child in self._library.project(item.id)[1:]
                        if child.status == "open"
                    ]
                    if open_children:
                        context.risky = True
                        preconditions[f"scope:project:{item.uuid}"] = (
                            self._project_scope_revision(item.uuid)
                        )
                        warnings.append(
                            f"{item.title} still has {len(open_children)} open actions."
                        )

    def _validate_requested_project_destinations(
        self, call: CommitCall, context: _PreparationContext
    ) -> None:
        """Reject lifecycle edits that compete with a same-batch merge target."""
        destinations: dict[str, Record] = {}
        for change in call.change:
            if "into" not in change.model_fields_set or change.into in {
                None,
                "inbox",
                "anytime",
            }:
                continue
            target = self._exact_item(change.into)
            if target is not None and target.kind == "project":
                destinations[target.uuid] = target
        if not destinations:
            return
        for target in destinations.values():
            if not target.is_open():
                raise _Abort(
                    self._rejected(
                        "A merge destination Project must be active and visible."
                    )
                )
            context.preconditions[target.id] = self._revision(target)
        for change in call.change:
            if change.id is None:
                continue
            item = self._exact_item(change.id)
            if item is None or item.uuid not in destinations:
                continue
            if (
                change.status is not None
                or change.trash
                or change.lifecycle is not None
            ):
                raise _Abort(
                    self._rejected(
                        "A merge destination Project cannot change lifecycle in "
                        "the same batch."
                    )
                )

    def _finish_preparation(
        self, call: CommitCall, context: _PreparationContext
    ) -> None:
        """Apply batch-wide invariants after all planners finish."""
        if not context.writes and context.already_correct:
            return
        if not context.writes:
            raise _Abort(self._rejected("The request did not produce a change."))
        context.writes = self._collapse_companion_updates(context.writes)
        self._validate_project_destinations(context)
        self._plan_project_teardown(context)
        if any(write.action == "delete_area" for write in context.writes):
            expected_scope = self._area_scope_revision()
            if call.scope_revision != expected_scope:
                raise _Abort(
                    self._stale("Read the system and use its current scope_revision.")
                )
            context.preconditions["scope:areas"] = expected_scope
        if len(context.writes) > 20 or len(call.change) > 5:
            context.risky = True
            context.warnings.append("This is a broad batch change.")

    @staticmethod
    def _collapse_companion_updates(writes: list[Write]) -> list[Write]:
        """Fold compiler-only order and tag writes into each final item edit."""

        def is_order_only_update(write: Write) -> bool:
            return replace(
                write,
                sort_index=None,
                today_index=None,
                owner_today=None,
            ) == Write(action="update", uuid=write.uuid, kind=write.kind)

        collapsed = list(writes)
        removed: set[int] = set()
        for index, write in enumerate(collapsed):
            if write.action != "update" or not is_order_only_update(write):
                continue
            later_index = next(
                (
                    candidate
                    for candidate in range(index + 1, len(collapsed))
                    if collapsed[candidate].action == "update"
                    and collapsed[candidate].uuid == write.uuid
                ),
                None,
            )
            if later_index is None:
                continue
            later = collapsed[later_index]
            collapsed[later_index] = replace(
                later,
                sort_index=later.sort_index
                if later.sort_index is not None
                else write.sort_index,
                today_index=later.today_index
                if later.today_index is not None
                else write.today_index,
                owner_today=later.owner_today or write.owner_today,
            )
            removed.add(index)
        for index in range(len(collapsed) - 1, -1, -1):
            write = collapsed[index]
            if index in removed or write.action != "tags":
                continue
            later_index = next(
                (
                    candidate
                    for candidate in range(len(collapsed) - 1, index, -1)
                    if candidate not in removed
                    and collapsed[candidate].uuid == write.uuid
                    and collapsed[candidate].action in {"tags", "update"}
                ),
                None,
            )
            if later_index is None:
                continue
            later = collapsed[later_index]
            if later.tag_uuids is None:
                collapsed[later_index] = replace(later, tag_uuids=write.tag_uuids)
            removed.add(index)
        return [write for index, write in enumerate(collapsed) if index not in removed]

    def _validate_project_destinations(
        self, context: _PreparationContext
    ) -> None:
        """Keep every merge destination visible throughout the whole batch.

        A destination Project is part of the merge's safety contract.  Validate
        its current and projected lifecycle, plus its parent Area, before an
        approval plan can be staged.  The destination revision is also a plan
        precondition, so an approval-window edit cannot turn the destination
        into a hidden or detached result.
        """
        destination_uuids = list(
            dict.fromkeys(
                write.into_uuid
                for write in context.writes
                if write.into_kind == "project"
                and write.into_uuid is not None
                and (
                    (current := self._library.records.get(write.uuid)) is None
                    or current.parent_uuid != write.into_uuid
                )
            )
        )
        for destination_uuid in destination_uuids:
            destination = self._library.records.get(destination_uuid)
            destination_create = next(
                (
                    write
                    for write in context.writes
                    if write.uuid == destination_uuid
                    and write.action == "create"
                    and write.kind == "project"
                ),
                None,
            )
            if destination is None and destination_create is not None:
                projected_status = destination_create.status or "open"
                projected_trashed = False
                projected_parent = None
                projected_area = (
                    destination_create.into_uuid
                    if destination_create.into_kind == "area"
                    else None
                )
                destination_exists = True
            elif destination is not None and destination.kind == "project":
                if not destination.is_open():
                    raise _Abort(
                        self._rejected(
                            "A merge destination Project must be active and visible."
                        )
                    )
                projected_status = destination.status
                projected_trashed = destination.trashed
                projected_parent = destination.parent_uuid
                projected_area = destination.area_uuid
                destination_exists = True
            else:
                raise _Abort(
                    self._rejected(
                        "A merge destination Project must be active and visible."
                    )
                )
            if destination is not None:
                context.preconditions[destination.id] = self._revision(destination)
                # Same-home repairs (Inbox list-state, notes, schedule) must
                # not invalidate a sibling batch. Bind the child list only
                # when membership, heading, or order actually changes.
                if self._project_membership_changes(destination.uuid, context.writes):
                    context.preconditions[f"scope:project:{destination.uuid}"] = (
                        self._project_scope_revision(destination.uuid)
                    )

            projected_exists = destination_exists
            lifecycle_conflict = False
            for write in context.writes:
                if write.uuid != destination_uuid:
                    continue
                if write.action in {
                    "complete",
                    "cancel",
                    "trash",
                    "restore",
                    "permanent_delete",
                    "delete_area",
                }:
                    lifecycle_conflict = True
                if write.action == "update" and write.status is not None:
                    lifecycle_conflict = True
                if write.action in {"permanent_delete", "delete_area"}:
                    projected_exists = False
                elif write.action == "trash":
                    projected_trashed = True
                elif write.action == "restore":
                    projected_trashed = False
                elif write.action == "complete":
                    projected_status = "done"
                elif write.action == "cancel":
                    projected_status = "dropped"
                elif write.action in {"update", "move"}:
                    if write.status is not None:
                        projected_status = write.status
                    if write.into_kind == "project":
                        projected_parent = write.into_uuid
                        projected_area = None
                    elif write.into_kind == "area":
                        projected_parent = None
                        projected_area = write.into_uuid
                    elif (
                        write.into_uuid is not None
                        or write.inbox
                        or write.anytime
                    ):
                        projected_parent = None
                        projected_area = None

            if lifecycle_conflict:
                raise _Abort(
                    self._rejected(
                        "A merge destination Project cannot change lifecycle in "
                        "the same batch."
                    )
                )
            if not projected_exists or projected_status != "open" or projected_trashed:
                raise _Abort(
                    self._rejected(
                        "A merge destination Project must remain active and visible."
                    )
                )
            if projected_parent is not None:
                raise _Abort(
                    self._rejected(
                        "A merge destination Project must not be hidden under "
                        "another Project."
                    )
                )
            if projected_area is None:
                continue
            area = self._library.records.get(projected_area)
            area_create = next(
                (
                    write
                    for write in context.writes
                    if write.uuid == projected_area
                    and write.action == "create"
                    and write.kind == "area"
                ),
                None,
            )
            area_deleted = any(
                write.uuid == projected_area
                and write.action in {"delete_area", "permanent_delete", "trash"}
                for write in context.writes
            )
            if (
                area is not None
                and area.kind == "area"
                and not area.trashed
                and not area_deleted
            ):
                # The destination Area is part of the approval contract. A
                # title, placement, or lifecycle change during approval must
                # make this whole batch stale, even when child move writes
                # still look satisfied by themselves.
                context.preconditions[area.id] = self._revision(area)
            elif area_create is None or area_deleted:
                raise _Abort(
                    self._rejected(
                        "A merge destination Project must remain attached to a "
                        "visible Area or the top-level registry."
                    )
                )

    def _plan_project_teardown(self, context: _PreparationContext) -> None:
        """Trash remaining descendants when a Project is trashed in this batch."""
        leaving = {
            write.uuid
            for write in context.writes
            if write.action in {"update", "move"}
            and (write.inbox or write.anytime or write.into_uuid is not None)
        }
        already = {
            write.uuid
            for write in context.writes
            if write.action in {"trash", "restore", "permanent_delete"}
        }
        insertions: list[tuple[int, list[Write]]] = []
        for index, write in enumerate(context.writes):
            if write.action != "trash":
                continue
            item = self._library.records.get(write.uuid)
            if item is None or item.kind != "project":
                continue
            torn: list[Write] = []
            for descendant in self._project_descendants(item.uuid):
                if (
                    descendant.uuid in leaving
                    or descendant.uuid in already
                    or descendant.trashed
                ):
                    continue
                context.preconditions[descendant.id] = self._revision(descendant)
                torn.append(
                    Write(
                        action="trash",
                        uuid=descendant.uuid,
                        kind=descendant.kind,
                    )
                )
                already.add(descendant.uuid)
            if not torn:
                continue
            insertions.append((index, torn))
            context.summary.append(
                f"Trash {item.kind}: {item.title} and {len(torn)} contained records"
            )
            context.warnings.append(
                f"{len(torn)} contained records will also move to Trash."
            )
        offset = 0
        for index, torn in insertions:
            at = index + offset
            context.writes[at:at] = torn
            offset += len(torn)

    def _preparation_context(
        self, call: CommitCall, *, contextual_commit: bool = False
    ) -> _PreparationContext:
        """Validate registry snapshots and allocate all local references."""
        changes_areas = any(entry.kind == "area" for entry in call.create) or any(
            change.id is not None and change.id.startswith("area:")
            for change in call.change
        )
        if changes_areas:
            expected_scope = self._area_scope_revision()
            if call.scope_revision != expected_scope:
                raise _Abort(
                    self._stale(
                        "The Area registry changed. Read the system and use its "
                        "current scope_revision."
                    )
                )
        if call.change_tags:
            expected_tags = self._tag_revision()
            if call.tags_revision != expected_tags:
                raise _Abort(
                    self._stale(
                        "The tag registry changed. Read tags and use its current "
                        "scope_revision as tags_revision."
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

        context = _PreparationContext(
            local=local,
            writes=[],
            preconditions={},
            summary=[],
            warnings=[],
            allow_project_heading_moves=contextual_commit,
        )
        uses_tags = (
            bool(call.ensure_tags or call.change_tags)
            or any(entry.tag_ids or entry.waiting for entry in call.create)
            or any(
                change.tags_add or change.tags_remove or change.waiting is not None
                for change in call.change
            )
        )
        if uses_tags:
            context.preconditions["scope:tags"] = self._tag_revision()
        return context

    def _prepare_tag_registry(
        self, call: CommitCall, context: _PreparationContext
    ) -> None:
        """Plan tag registry and hierarchy changes in one place."""
        for tag_entry in call.ensure_tags:
            uuid = context.local[tag_entry.key][0]
            context.writes.append(
                Write(action="ensure_tag", uuid=uuid, title=tag_entry.title)
            )
            context.summary.append(f"Ensure tag: {tag_entry.title}")
            if tag_entry.parent_id is None:
                continue
            parent_uuid = (
                context.local[tag_entry.parent_id][0]
                if tag_entry.parent_id.startswith("$")
                else tag_entry.parent_id.removeprefix("tag:")
            )
            if (
                not tag_entry.parent_id.startswith("$")
                and parent_uuid not in self._library.tags
            ):
                raise _Abort(
                    self._needs_input(
                        f"I could not find exact tag {tag_entry.parent_id}."
                    )
                )
            self._validate_tag_parent(uuid, parent_uuid, context.writes)
            context.writes.append(
                Write(
                    action="reparent_tag",
                    uuid=uuid,
                    tag_parent_uuids=[parent_uuid],
                )
            )
            context.summary.append(f"Place tag under: {tag_entry.title}")

        for tag_change in call.change_tags:
            uuid = tag_change.id.removeprefix("tag:")
            current_title = self._library.tags.get(uuid)
            if current_title is None:
                raise _Abort(
                    self._needs_input(f"I could not find exact tag {tag_change.id}.")
                )
            if tag_change.title is not None:
                collision = next(
                    (
                        other_uuid
                        for other_uuid, title in self._library.tags.items()
                        if other_uuid != uuid
                        and title.casefold() == tag_change.title.casefold()
                    ),
                    None,
                )
                if collision is not None:
                    raise _Abort(
                        self._needs_input(
                            f"Another tag is already named {tag_change.title}."
                        )
                    )
                context.writes.append(
                    Write(action="rename_tag", uuid=uuid, title=tag_change.title)
                )
                context.summary.append(
                    f"Rename tag: {current_title} to {tag_change.title}"
                )
            if "parent_id" in tag_change.model_fields_set:
                requested_parent_uuid = (
                    tag_change.parent_id.removeprefix("tag:")
                    if tag_change.parent_id is not None
                    else None
                )
                if (
                    requested_parent_uuid is not None
                    and requested_parent_uuid not in self._library.tags
                ):
                    raise _Abort(
                        self._needs_input(
                            f"I could not find exact tag {tag_change.parent_id}."
                        )
                    )
                self._validate_tag_parent(uuid, requested_parent_uuid, context.writes)
                context.writes.append(
                    Write(
                        action="reparent_tag",
                        uuid=uuid,
                        tag_parent_uuids=(
                            [requested_parent_uuid] if requested_parent_uuid else []
                        ),
                    )
                )
                context.summary.append(f"Change tag parent: {current_title}")
            if tag_change.delete_permanently:
                self._prepare_tag_deletion(uuid, current_title, context)
            context.risky = True

    def _prepare_tag_deletion(
        self, uuid: str, title: str, context: _PreparationContext
    ) -> None:
        """Detach every reference before the irreversible tag delete."""
        for item in self._library.records.values():
            if uuid not in item.tag_uuids:
                continue
            context.preconditions[item.id] = self._revision(item)
            prior_tags = next(
                (
                    write.tag_uuids
                    for write in reversed(context.writes)
                    if write.uuid == item.uuid and write.tag_uuids is not None
                ),
                item.tag_uuids,
            )
            context.writes.append(
                Write(
                    action="tags",
                    uuid=item.uuid,
                    kind=item.kind,
                    tag_uuids=[tag for tag in prior_tags if tag != uuid],
                )
            )
        for child_uuid, parents in self._library.tag_parents.items():
            if uuid not in parents:
                continue
            context.writes.append(
                Write(
                    action="reparent_tag",
                    uuid=child_uuid,
                    tag_parent_uuids=[parent for parent in parents if parent != uuid],
                )
            )
        context.writes.append(Write(action="delete_tag", uuid=uuid, title=title))
        context.summary.append(f"Permanently delete tag: {title}")
        context.warnings.append(
            "The tag will be removed from all items and cannot be restored."
        )

    def _validate_tag_parent(
        self,
        tag_uuid: str,
        parent_uuid: str | None,
        planned: list[Write],
    ) -> None:
        """Reject a parent that reaches this tag through current or planned links."""
        if parent_uuid is None:
            return
        planned_parents = {
            write.uuid: list(write.tag_parent_uuids or [])
            for write in planned
            if write.action == "reparent_tag"
        }
        pending = [parent_uuid]
        seen: set[str] = set()
        while pending:
            ancestor = pending.pop()
            if ancestor == tag_uuid:
                raise _Abort(self._rejected("A tag parent cannot create a cycle."))
            if ancestor in seen:
                continue
            seen.add(ancestor)
            pending.extend(
                planned_parents.get(
                    ancestor,
                    self._library.tag_parents.get(ancestor, []),
                )
            )

    def _heading_destination_uuid(
        self, change: ChangeEntry, context: _PreparationContext
    ) -> str | None:
        if change.into is None and change.into_title is None:
            return None
        home = self._home(
            change.into,
            "task",
            context.local,
            new_item=False,
            into_title=None if change.into is not None else change.into_title,
        )
        if home[1] != "project" or home[0] is None:
            return None
        return home[0]

    @staticmethod
    def _heading_follows_source_merge(heading: Record, call: CommitCall) -> bool:
        if heading.parent_uuid is None:
            return False
        source_id = f"project:{heading.parent_uuid}"
        return any(
            change.id == source_id
            and (change.lifecycle == "trash" or change.trash is True)
            for change in call.change
        )

    def _require_heading_destination(
        self,
        item: Record,
        change: ChangeEntry,
        call: CommitCall,
        context: _PreparationContext,
    ) -> str:
        destination_uuid = self._heading_destination_uuid(change, context)
        if destination_uuid is None:
            raise _Abort(self._rejected("A heading needs a destination Project."))
        if (
            destination_uuid != item.parent_uuid
            and not self._heading_follows_source_merge(item, call)
        ):
            raise _Abort(
                self._rejected(
                    "A heading cannot move into a different Project except "
                    "during an atomic Project merge."
                )
            )
        return destination_uuid

    def _home(
        self,
        reference: str | None,
        kind: Kind,
        local: dict[str, tuple[str, Kind | str]],
        *,
        new_item: bool,
        into_title: str | None = None,
    ) -> tuple[str | None, Kind | None, bool, bool]:
        if kind == "area" and (reference is not None or into_title is not None):
            raise _Abort(self._rejected("Areas stay in the top-level registry."))
        if reference == "inbox":
            if kind == "project":
                raise _Abort(self._rejected("Projects cannot enter Inbox."))
            return None, None, True, False
        if reference == "anytime" or (
            reference is None and into_title is None and kind == "project"
        ):
            return None, None, False, True
        if reference is None and into_title is None:
            return (
                (None, None, kind == "task", False)
                if new_item
                else (None, None, False, True)
            )
        if reference is None:
            assert into_title is not None
            uuid, target = self._resolve_into_title(into_title)
        elif reference.startswith("$"):
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

    def _resolve_into_title(self, title: str) -> tuple[str, Kind]:
        resolved = self._library.resolve_into(title)
        if resolved is None:
            raise _Abort(
                self._needs_input(f"No Area or Project named {title}.")
            )
        if isinstance(resolved, list):
            pairs = ", ".join(f"{item.id} {item.title}" for item in resolved[:8])
            raise _Abort(
                self._needs_input(
                    f"Several homes match {title}: {pairs}. Use an exact id."
                )
            )
        return resolved.uuid, resolved.kind

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
        deleted = {
            write.uuid for write in planned if write.action == "delete_tag"
        }
        if existing is not None and existing not in deleted:
            return existing, None
        if existing is not None:
            title = "Waiting"
            existing = self._library.tag_uuid(title)
            if existing is not None and existing not in deleted:
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
        if value == "tomorrow":
            return self._clock().date() + timedelta(days=1), False, False
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
        existing_tonight: bool = False,
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
        return reminder_date, False, existing_tonight, reminder

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
        projected = self._projected_list_rows(
            kind, planned, moving_uuid=moving_uuid
        )
        indexes = [
            row.sort_index for row in projected.values() if row.scope == wanted_scope
        ]
        if not present:
            return max(indexes, default=0) + 1024 if moving_uuid is None else None
        if reference is None:
            if not indexes:
                return 1024
            first = min(indexes)
            if first > 1:
                return max(1, first // 2)
            self._rebalance_scope(
                kind,
                wanted_scope,
                planned,
                preconditions,
                moving_uuid=moving_uuid,
            )
            return 512
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
                raise _Abort(
                    self._rejected(
                        "An after reference must come earlier in the request."
                    )
                )
            if previous.kind != kind or self._write_scope(previous) != wanted_scope:
                raise _Abort(
                    self._rejected("An after reference must be in the same list.")
                )
            anchor_uuid = previous.uuid
            anchor_index = previous.sort_index or 0
        else:
            item = self._required_exact(reference)
            if item.uuid == moving_uuid:
                raise _Abort(self._rejected("An item cannot follow itself."))
            anchor = projected.get(item.uuid)
            if item.kind != kind or anchor is None or anchor.scope != wanted_scope:
                raise _Abort(
                    self._rejected("An after reference must be in the same list.")
                )
            preconditions[item.id] = self._revision(item)
            anchor_uuid = anchor.uuid
            anchor_index = anchor.sort_index
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

    def _heading_order_writes(
        self,
        heading: Record,
        *,
        after: str | None,
        local: dict[str, tuple[str, Kind | str]],
        planned: list[Write],
        preconditions: dict[str, str],
    ) -> list[Write]:
        project_uuid = heading.parent_uuid
        if project_uuid is None:
            raise _Abort(self._rejected("A heading needs a Project."))
        return self._project_heading_order_writes(
            _HeadingOrderRow(
                uuid=heading.uuid,
                sort_index=heading.sort_index,
                record=heading,
            ),
            project_uuid=project_uuid,
            after=after,
            local=local,
            planned=planned,
            preconditions=preconditions,
        )

    def _project_heading_order_writes(
        self,
        moving: _HeadingOrderRow,
        *,
        project_uuid: str,
        after: str | None,
        local: dict[str, tuple[str, Kind | str]],
        planned: list[Write],
        preconditions: dict[str, str],
    ) -> list[Write]:
        """Return one complete order across current and newly created headings."""
        projected_indexes = {
            write.uuid: write.sort_index
            for write in planned
            if write.action == "update" and write.sort_index is not None
        }
        ordered = [
            _HeadingOrderRow(
                uuid=item.uuid,
                sort_index=projected_indexes.get(item.uuid, item.sort_index),
                record=item,
            )
            for item in self._library.records.values()
            if item.heading
            and item.parent_uuid == project_uuid
            and not item.trashed
            and item.uuid != moving.uuid
        ]
        ordered.extend(
            _HeadingOrderRow(
                uuid=write.uuid,
                sort_index=write.sort_index or 0,
                create_index=index,
            )
            for index, write in enumerate(planned)
            if write.action == "create_heading"
            and write.into_uuid == project_uuid
            and write.uuid != moving.uuid
        )
        ordered.sort(key=lambda row: (row.sort_index, row.uuid))
        moving = replace(
            moving,
            sort_index=projected_indexes.get(moving.uuid, moving.sort_index),
        )
        if after is None:
            ordered.insert(0, moving)
        else:
            if after.startswith("$"):
                anchor_uuid = local[after][0]
                anchor_row = next(
                    (row for row in ordered if row.uuid == anchor_uuid), None
                )
                if anchor_row is None or anchor_row.create_index is None:
                    raise _Abort(
                        self._rejected(
                            "A local heading anchor must be created in this Project."
                        )
                    )
            else:
                anchor = self._required_exact(after)
                if (
                    not anchor.heading
                    or anchor.parent_uuid != project_uuid
                    or anchor.uuid == moving.uuid
                ):
                    raise _Abort(
                        self._rejected(
                            "A heading after reference must be in the same Project."
                        )
                    )
                anchor_uuid = anchor.uuid
                preconditions[anchor.id] = self._revision(anchor)
            position = next(
                index for index, row in enumerate(ordered) if row.uuid == anchor_uuid
            )
            ordered.insert(position + 1, moving)
        writes: list[Write] = []
        for index, row in enumerate(ordered):
            wanted = (index + 1) * 1024
            if row.record is not None:
                if row.sort_index == wanted:
                    continue
                preconditions[row.record.id] = self._revision(row.record)
                writes.append(
                    Write(
                        action="update",
                        uuid=row.uuid,
                        kind="task",
                        sort_index=wanted,
                    )
                )
            else:
                assert row.create_index is not None
                planned[row.create_index] = replace(
                    planned[row.create_index], sort_index=wanted
                )
        return writes

    def _rebalance_scope(
        self,
        kind: Kind,
        scope: tuple[str, str | None],
        planned: list[Write],
        preconditions: dict[str, str],
        *,
        moving_uuid: str | None,
    ) -> dict[str, int]:
        projected = self._projected_list_rows(
            kind, planned, moving_uuid=moving_uuid
        )
        rows = sorted(
            (row for row in projected.values() if row.scope == scope),
            key=lambda row: (row.sort_index, row.uuid),
        )
        positions: dict[str, int] = {}
        for order, row in enumerate(rows):
            new_index = (order + 1) * 1024
            positions[row.uuid] = new_index
            if row.write_index is not None:
                planned[row.write_index] = replace(
                    planned[row.write_index], sort_index=new_index
                )
            elif row.record is not None:
                preconditions[row.record.id] = self._revision(row.record)
                if row.record.sort_index != new_index:
                    planned.append(
                        Write(
                            action="update",
                            uuid=row.uuid,
                            kind=row.record.kind,
                            sort_index=new_index,
                        )
                    )
        return positions

    def _projected_list_rows(
        self,
        kind: Kind,
        planned: list[Write],
        *,
        moving_uuid: str | None,
    ) -> dict[str, _ProjectedListRow]:
        rows = {
            item.uuid: _ProjectedListRow(
                uuid=item.uuid,
                scope=self._record_scope(item),
                sort_index=item.sort_index,
                record=item,
            )
            for item in self._library.records.values()
            if item.uuid != moving_uuid and item.is_open() and item.kind == kind
        }
        for index, write in enumerate(planned):
            if (
                write.uuid == moving_uuid
                or write.kind != kind
                or write.action not in {"create", "update"}
            ):
                continue
            current = rows.get(write.uuid)
            changes_home = (
                write.action == "create"
                or write.into_kind is not None
                or write.inbox
                or write.anytime
            )
            scope = (
                self._write_scope(write)
                if changes_home or current is None
                else current.scope
            )
            rows[write.uuid] = _ProjectedListRow(
                uuid=write.uuid,
                scope=scope,
                sort_index=(
                    write.sort_index
                    if write.sort_index is not None
                    else current.sort_index
                    if current is not None
                    else 0
                ),
                record=current.record if current is not None else None,
                write_index=(
                    index
                    if write.sort_index is not None
                    else current.write_index
                    if current is not None
                    else None
                ),
            )
        return rows

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
        indexes = [row.sort_index for row in item.checklists if row.uuid != moving_uuid]
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
            return max(indexes, default=-1024) + 1024 if moving_uuid is None else None
        if reference is None:
            return min(indexes, default=1024) - 1024
        if reference.startswith("$"):
            uuid = local[reference][0]
            previous = next(
                (write for write in reversed(planned) if write.uuid == uuid), None
            )
            if previous is None or previous.checklist_parent_uuid != item.uuid:
                raise _Abort(
                    self._rejected("A checklist after reference must come earlier.")
                )
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
        repaired = self._rebalance_checklist(item, planned, moving_uuid=moving_uuid)
        return repaired[anchor_uuid] + 512

    @staticmethod
    def _rebalance_checklist(
        item: Record,
        planned: list[Write],
        *,
        moving_uuid: str | None,
    ) -> dict[str, int]:
        existing = [row for row in item.checklists if row.uuid != moving_uuid]
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
                planned[index] = replace(planned[index], checklist_index=new_index)
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
            and write.today_index is not None
            and (write.start == today or write.tonight)
        )
        if not present:
            return max(indexes, default=-1024) + 1024 if new_item and on_today else None
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
            if previous is None or not (previous.start == today or previous.tonight):
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
            companion = next(
                (
                    write
                    for write in reversed(planned)
                    if write.uuid == item.uuid
                    and write.action == "update"
                    and (write.start == today or write.tonight)
                ),
                None,
            )
            on_today = item.is_open() and (item.start == today or item.tonight)
            if not on_today:
                if companion is None:
                    raise _Abort(
                        self._rejected("A today_after reference must be on Today.")
                    )
                preconditions[item.id] = self._revision(item)
                anchor_uuid = companion.uuid
                anchor_index = (
                    companion.today_index
                    if companion.today_index is not None
                    else item.today_index
                )
            else:
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
        planned_today = {
            write.uuid
            for write in planned
            if write.uuid != moving_uuid
            and write.today_index is not None
            and (write.start == today or write.tonight)
        }
        existing = sorted(
            (
                item
                for item in self._library.records.values()
                if item.uuid != moving_uuid
                and item.uuid not in planned_today
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
                and write.today_index is not None
                and (write.start == today or write.tonight)
            ),
            key=lambda pair: (pair[1].today_index or 0, pair[1].uuid),
        )
        combined = sorted(
            [(item.today_index, item.uuid, "existing", item) for item in existing]
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
                planned[index] = replace(planned[index], today_index=new_index)
        return positions

    def _plan_manifest(
        self, prepared: _Prepared
    ) -> tuple[list[str], list[ReviewSection]]:
        counts: dict[str, int] = {}
        sections: dict[str, list[str]] = {}

        def add(key: str, title: str, item_id: str | None = None) -> None:
            counts[key] = counts.get(key, 0) + 1
            if item_id is not None:
                sections.setdefault(key, [])
                if item_id not in sections[key] and len(sections[key]) < 40:
                    sections[key].append(item_id)

        titles = {
            "create_area": "Areas created",
            "create_project": "Projects created",
            "create_task": "Tasks created",
            "create_heading": "Headings created",
            "inbox": "Move from Inbox",
            "someday": "Move to Someday",
            "trash": "Move to recoverable Trash",
            "restore": "Restore",
            "permanent": "Permanent deletes",
            "rename": "Rename",
            "checklist": "Checklist edits",
            "area_remove": "Remove empty Areas",
        }
        move_titles: dict[str, str] = {}
        for write in prepared.writes:
            item = self._library.records.get(write.uuid)
            item_id: str | None
            if write.action == "create_heading":
                item_id = f"heading:{write.uuid}"
            elif write.action == "checklist":
                checklist_parent_uuid = write.checklist_parent_uuid
                if checklist_parent_uuid is None:
                    checklist_parent, _ = self._library._find_checklist(write.uuid)
                    checklist_parent_uuid = (
                        checklist_parent.uuid if checklist_parent is not None else None
                    )
                item_id = (
                    f"task:{checklist_parent_uuid}"
                    if checklist_parent_uuid is not None
                    else None
                )
            elif write.action in {"delete_tag", "rename_tag", "reparent_tag", "ensure_tag"}:
                item_id = None
            elif item is not None:
                item_id = item.id
            else:
                item_id = f"{write.kind}:{write.uuid}"
            if write.action == "create":
                add(f"create_{write.kind}", write.title or write.kind, item_id)
            elif write.action == "create_heading":
                add("create_heading", write.title or "Heading", item_id)
            elif write.action == "trash":
                add("trash", "Trash", item_id)
            elif write.action == "restore":
                add("restore", "Restore", item_id)
            elif write.action in {"permanent_delete", "delete_area", "delete_tag"}:
                add("permanent", "Permanent", item_id)
            elif write.action == "checklist":
                add("checklist", "Checklist", item_id)
            elif write.someday:
                add("someday", "Someday", item_id)
            elif write.title is not None and item is not None and write.title != item.title:
                add("rename", "Rename", item_id)
            if (
                item is not None
                and item.inbox
                and (
                    write.into_uuid is not None
                    or write.anytime
                    or write.someday
                    or write.start is not None
                    or write.tonight
                    or write.clear_start
                )
            ):
                add("inbox", "Inbox", item.id)
            dest = (
                "Anytime"
                if write.anytime
                else "Inbox"
                if write.inbox
                else self._home_title(write.into_kind, write.into_uuid)
                if write.into_uuid and write.into_kind
                else None
            )
            if item is not None and dest is not None:
                source = (
                    "Inbox"
                    if item.inbox
                    else self._record_home_title(item)
                )
                if source != dest:
                    move_key = f"move:{source}->{dest}"[:80]
                    move_title = f"{source} → {dest}"
                    add(move_key, move_title, item.id)
                    move_titles[move_key] = move_title
                    titles[move_key] = move_title
        header = [
            f"{titles[key]}: {counts[key]}"
            for key in titles
            if key in counts
        ]
        detail = list(dict.fromkeys(prepared.summary))
        summary = list(dict.fromkeys([*header, *detail]))[:40]
        if not summary:
            summary = ["Planned change"]
        category_review = [
            ReviewSection(
                key=key,
                title=titles.get(key, key),
                item_ids=ids,
            )
            for key, ids in sections.items()
            if ids
        ]
        manifest = self._manifest_review_sections(prepared.writes)
        if len(manifest) > 40:
            raise _Abort(
                self._revise(
                    "The exact approval manifest exceeds 40 sections. Split the "
                    "request at a Project or note boundary. Keep related headings "
                    "and Tasks together. Use a new intent_id for each batch. Do not "
                    "ask the owner to approve an incomplete manifest."
                )
            )
        review = [*manifest, *category_review][:40]
        return summary, review

    def _manifest_review_sections(self, writes: list[Write]) -> list[ReviewSection]:
        pairs = self._manifest_pairs(writes)
        chunks: list[list[tuple[str | None, str]]] = []
        current: list[tuple[str | None, str]] = []
        current_chars = 0
        for pair in pairs:
            pair_chars = len(pair[1])
            if current and (len(current) >= 40 or current_chars + pair_chars > 1600):
                chunks.append(current)
                current = []
                current_chars = 0
            current.append(pair)
            current_chars += pair_chars
        if current:
            chunks.append(current)
        sections: list[ReviewSection] = []
        for index, chunk in enumerate(chunks, start=1):
            suffix = f" ({index}/{len(chunks)})" if len(chunks) > 1 else ""
            sections.append(
                ReviewSection(
                    key=f"manifest_{index}",
                    title=f"Exact before-and-after manifest{suffix}",
                    item_ids=list(
                        dict.fromkeys(item_id for item_id, _signal in chunk if item_id)
                    )[:40],
                    signals=[signal for _item_id, signal in chunk],
                )
            )
        return sections

    def _manifest_pairs(self, writes: list[Write]) -> list[tuple[str | None, str]]:
        pairs: list[tuple[str | None, str]] = []
        tag_titles = dict(self._library.tags)
        item_titles = {
            uuid: item.title for uuid, item in self._library.records.items()
        }
        for write in writes:
            if write.action == "ensure_tag" and write.title is not None:
                tag_titles[write.uuid] = write.title
            elif write.action == "rename_tag" and write.title is not None:
                tag_titles[write.uuid] = write.title
            elif write.title is not None:
                item_titles[write.uuid] = write.title

        for write in writes:
            item = self._library.records.get(write.uuid)
            item_id = self._manifest_item_id(write, item)
            signal = self._manifest_write_signal(
                write, item, tag_titles, item_titles
            )
            if signal is None:
                continue
            pairs.append((item_id, signal))

        pairs.extend(self._order_manifest_pairs(writes, item_titles))
        if any(
            write.action in {"ensure_tag", "rename_tag", "reparent_tag", "delete_tag"}
            for write in writes
        ):
            final_tags = dict(tag_titles)
            for write in writes:
                if write.action == "delete_tag":
                    final_tags.pop(write.uuid, None)
            before = ", ".join(sorted(self._library.tags.values(), key=str.casefold))
            after = ", ".join(sorted(final_tags.values(), key=str.casefold))
            pairs.append((None, f"Tag catalog: [{before}] -> [{after}]."))
        bounded: list[tuple[str | None, str]] = []
        for item_id, signal in pairs:
            pieces = [
                signal[index : index + 1500]
                for index in range(0, len(signal), 1500)
            ]
            if len(pieces) == 1:
                bounded.append((item_id, pieces[0]))
                continue
            bounded.extend(
                (
                    item_id,
                    f"{pieces[index - 1]}\n[part {index}/{len(pieces)}]",
                )
                for index in range(1, len(pieces) + 1)
            )
        return bounded

    def _manifest_item_id(self, write: Write, item: Record | None) -> str | None:
        if write.action == "create_heading":
            return f"heading:{write.uuid}"
        if write.action == "checklist":
            parent_uuid = write.checklist_parent_uuid
            if parent_uuid is None:
                parent, _row = self._library._find_checklist(write.uuid)
                parent_uuid = parent.uuid if parent is not None else None
            return f"task:{parent_uuid}" if parent_uuid is not None else None
        if write.action in {"ensure_tag", "rename_tag", "reparent_tag", "delete_tag"}:
            return None
        if item is not None:
            return item.id
        return f"{write.kind}:{write.uuid}"

    def _manifest_write_signal(
        self,
        write: Write,
        item: Record | None,
        tag_titles: dict[str, str],
        item_titles: dict[str, str],
    ) -> str | None:
        title = item.title if item is not None else write.title or write.kind
        kind = "Heading" if write.action == "create_heading" else write.kind.title()
        if write.action == "create":
            details: list[str] = []
            if write.notes is not None:
                details.append(f"notes:\n{write.notes}")
            if (
                write.start is not None
                or write.someday
                or write.tonight
                or write.inbox
            ):
                details.append(f'when "{self._write_when_title(write)}"')
            if write.deadline is not None:
                details.append(f"deadline {write.deadline}")
            if write.remind is not None:
                details.append(f"reminder {write.remind}")
            if write.tag_uuids:
                tags = [tag_titles.get(uuid, uuid) for uuid in write.tag_uuids]
                details.append(f"tags [{', '.join(tags)}]")
            base = (
                f'Create {kind} "{write.title}" in '
                f"{self._write_home_title(write, item_titles)}"
            )
            return f"{base}: {'; '.join(details)}." if details else f"{base}."
        if write.action == "create_heading":
            return f'Create Heading "{write.title}" in {self._write_home_title(write, item_titles)}.'
        if write.action == "ensure_tag":
            return f'Create tag "{write.title}".'
        if write.action == "rename_tag":
            return f'Rename tag "{self._library.tags.get(write.uuid, write.uuid)}" -> "{write.title}".'
        if write.action == "reparent_tag":
            parents = [tag_titles.get(uuid, uuid) for uuid in write.tag_parent_uuids or []]
            return f'Parents for tag "{tag_titles.get(write.uuid, write.uuid)}" -> [{", ".join(parents)}].'
        if write.action == "delete_tag":
            return f'Permanently delete tag "{write.title or self._library.tags.get(write.uuid, write.uuid)}".'
        if write.action == "trash":
            return f'Move {kind} "{title}" to recoverable Trash.'
        if write.action == "restore":
            return f'Restore {kind} "{title}" from Trash.'
        if write.action in {"permanent_delete", "delete_area"}:
            return f'Permanently delete {kind} "{title}".'
        if write.action == "complete":
            return f'Complete {kind} "{title}".'
        if write.action == "cancel":
            return f'Cancel {kind} "{title}".'
        if write.action == "checklist":
            action = "Remove" if write.checklist_remove else "Change"
            return f'{action} checklist row "{write.title or write.uuid}" on "{title}".'
        if write.action in {"repeat", "repeat_link"}:
            return f'Change repetition for {kind} "{title}".'

        changes: list[str] = []
        if write.title is not None and write.title != title:
            changes.append(f'title "{title}" -> "{write.title}"')
        if item is not None and (
            write.action == "move"
            or write.into_kind is not None
            or write.inbox
            or write.anytime
        ):
            changes.append(
                f'home "{self._record_home_title(item)}" -> "{self._write_home_title(write, item_titles)}"'
            )
        if write.notes is not None and item is not None and write.notes != item.notes:
            changes.append(
                f"notes before:\n{item.notes}\nnotes after:\n{write.notes}"
            )
        if write.status is not None and item is not None and write.status != item.status:
            changes.append(
                f"status {self._owner_status(item.status)} -> "
                f"{self._owner_status(write.status)}"
            )
        if item is not None and (write.start is not None or write.clear_start):
            changes.append(f"start {item.start or 'none'} -> {write.start or 'none'}")
        if item is not None and (write.deadline is not None or write.clear_deadline):
            changes.append(
                f"deadline {item.deadline or 'none'} -> {write.deadline or 'none'}"
            )
        if item is not None and (write.remind is not None or write.clear_remind):
            changes.append(f"reminder {item.remind or 'none'} -> {write.remind or 'none'}")
        if item is not None and (
            write.start is not None
            or write.clear_start
            or write.someday
            or write.tonight
            or write.inbox
            or write.anytime
        ):
            before_when = self._record_when_title(item)
            after_when = self._write_when_title(write)
            if before_when != after_when:
                changes.append(f'when "{before_when}" -> "{after_when}"')
        if write.tag_uuids is not None and item is not None:
            before = [tag_titles.get(uuid, uuid) for uuid in item.tag_uuids]
            after = [tag_titles.get(uuid, uuid) for uuid in write.tag_uuids]
            if before != after:
                changes.append(f"tags [{', '.join(before)}] -> [{', '.join(after)}]")
        if not changes:
            return None
        return f'{kind} "{title}": ' + "; ".join(changes) + "."

    @staticmethod
    def _owner_status(status: str) -> str:
        return "canceled" if status == "dropped" else status

    def _record_when_title(self, item: Record) -> str:
        if item.someday:
            return "Someday"
        if item.tonight:
            return "Evening"
        if item.start == self._clock().date():
            return "Today"
        if item.start is not None:
            return item.start.isoformat()
        return "Inbox" if item.inbox else "Anytime"

    def _write_when_title(self, write: Write) -> str:
        if write.someday:
            return "Someday"
        if write.tonight:
            return "Evening"
        if write.start == self._clock().date():
            return "Today"
        if write.start is not None:
            return write.start.isoformat()
        if write.inbox:
            return "Inbox"
        return "Anytime"

    def _order_manifest_pairs(
        self, writes: list[Write], item_titles: dict[str, str]
    ) -> list[tuple[str | None, str]]:
        pairs: list[tuple[str | None, str]] = []
        for kind in ("area", "project", "task"):
            projected = self._projected_list_rows(
                kind, writes, moving_uuid=None
            )
            touched: set[tuple[str, str | None]] = set()
            for write in writes:
                if write.kind != kind:
                    continue
                item = self._library.records.get(write.uuid)
                if item is not None and (
                    write.sort_index is not None
                    or write.action
                    in {"move", "trash", "permanent_delete", "delete_area"}
                    or write.into_kind is not None
                    or write.inbox
                    or write.anytime
                ):
                    touched.add(self._record_scope(item))
                row = projected.get(write.uuid)
                if row is not None:
                    touched.add(row.scope)
            removed = {
                write.uuid
                for write in writes
                if write.action in {"trash", "permanent_delete", "delete_area"}
            }
            final_titles = {
                write.uuid: write.title
                for write in writes
                if write.title is not None
            }
            for scope in sorted(touched):
                before_rows = sorted(
                    (
                        item
                        for item in self._library.records.values()
                        if item.is_open()
                        and item.kind == kind
                        and self._record_scope(item) == scope
                    ),
                    key=lambda item: (item.sort_index, item.uuid),
                )
                after_rows = sorted(
                    (
                        row
                        for row in projected.values()
                        if row.scope == scope and row.uuid not in removed
                    ),
                    key=lambda row: (row.sort_index, row.uuid),
                )
                before = ", ".join(item.title for item in before_rows)
                after = ", ".join(
                    final_titles.get(
                        row.uuid,
                        row.record.title if row.record is not None else row.uuid,
                    )
                    or row.uuid
                    for row in after_rows
                )
                if before != after:
                    pairs.append(
                        (
                            None,
                            f"Order in {self._scope_title(scope, item_titles)}: [{before}] -> [{after}].",
                        )
                    )
        return pairs

    def _scope_title(
        self, scope: tuple[str, str | None], item_titles: dict[str, str]
    ) -> str:
        name, uuid = scope
        if name == "areas":
            return "Areas"
        if name == "inbox":
            return "Inbox"
        if name == "root":
            return "Anytime"
        return f'"{self._home_title(name, uuid, item_titles)}"'

    def _write_home_title(
        self, write: Write, item_titles: dict[str, str]
    ) -> str:
        if write.kind == "area":
            return "Areas"
        if write.into_uuid is not None and write.into_kind is not None:
            return self._home_title(write.into_kind, write.into_uuid, item_titles)
        if write.inbox:
            return "Inbox"
        if write.anytime:
            return "Anytime"
        if write.into_kind is None and write.kind == "project":
            return "Anytime"
        return self._home_title(write.into_kind, write.into_uuid, item_titles)

    def _record_home_title(self, item: Record) -> str:
        if item.parent_uuid:
            parent = self._library.records.get(item.parent_uuid)
            return parent.title if parent is not None else "Project"
        if item.area_uuid:
            area = self._library.records.get(item.area_uuid)
            return area.title if area is not None else "Area"
        return "Anytime"

    def _home_title(
        self,
        kind: str | None,
        uuid: str | None,
        item_titles: dict[str, str] | None = None,
    ) -> str:
        if uuid is None:
            return "Anytime" if kind == "project" else kind or "home"
        if item_titles is not None and uuid in item_titles:
            return item_titles[uuid]
        item = self._library.records.get(uuid)
        return item.title if item is not None else kind or "home"

    def _stage(self, record: IntentRecord, prepared: _Prepared) -> Result:
        expires = self._clock() + timedelta(minutes=_PLAN_MINUTES)
        plan_id = f"plan_{token_urlsafe(12)}"
        try:
            summary, sections = self._plan_manifest(prepared)
        except _Abort as error:
            return error.result
        result = Result(
            next="approve",
            status="needs_approval",
            instruction=(
                "Show one exact before-and-after manifest for the visible change. "
                "Name every permanent deletion, Trash move, cancellation, note "
                "replacement, and date change in the plan. Ask one short, natural "
                "confirmation about that complete manifest. Keep plan IDs and control "
                "fields private. Call things_approve only after a clear yes."
            ),
            sections=sections,
            plan=PlanFact(
                id=plan_id,
                expires_at=expires.isoformat(),
                summary=summary,
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
        already_correct = self._already_correct_from_plan(record.plan)
        if not writes and self._preconditions_changed(record.plan):
            result = self._stale(
                "Relevant Things data changed. Read it before a new intent."
            )
            self._save_result(record, "stale", result)
            return result
        if self._writes_match(writes):
            result = self._settled(
                record.intent_id,
                writes,
                unchanged=True,
                already_correct=already_correct,
            )
            self._save_result(record, "unchanged", result)
            return result
        if self._preconditions_changed(record.plan):
            result = self._stale(
                "Relevant Things data changed. Read it before a new intent."
            )
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
                return self._unsettled_outcome(
                    record,
                    writes,
                    "The Cloud outcome is not proven. Retry only this same receipt.",
                )
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
            return self._unsettled_outcome(
                record,
                writes,
                "Cloud accepted the request, but read-back is still pending.",
            )
        result = self._settled(
            record.intent_id,
            writes,
            unchanged=False,
            already_correct=already_correct,
        )
        self._save_result(record, "applied", result)
        return result

    def _resume(self, record: IntentRecord, *, allow_apply: bool) -> Result:
        if (
            record.state in {"applied", "unchanged", "stale"}
            and record.result is not None
        ):
            return Result.model_validate(record.result)
        if record.state == "needs_approval" and record.result is not None:
            return Result.model_validate(record.result)
        failed = self._refresh(force=True)
        if failed is not None:
            return failed
        writes = self._writes_from_plan(record.plan)
        already_correct = self._already_correct_from_plan(record.plan)
        if not writes and self._preconditions_changed(record.plan):
            result = self._stale(
                "Relevant Things data changed. Read it before a new intent."
            )
            self._save_result(record, "stale", result)
            return result
        if self._writes_match(writes):
            result = self._settled(
                record.intent_id,
                writes,
                unchanged=False,
                already_correct=already_correct,
            )
            self._save_result(record, "applied", result)
            return result
        if allow_apply:
            return self._apply(record)
        return self._unsettled_outcome(
            record,
            writes,
            "The Cloud outcome is still unknown. Retry only this same receipt.",
        )

    def _unsettled_outcome(
        self,
        record: IntentRecord,
        writes: list[Write],
        instruction: str,
    ) -> Result:
        matched = [write for write in writes if self._writes_match([write])]
        if matched and len(matched) < len(writes):
            applied = self._change_titles(matched)
            not_applied = self._change_titles(
                [write for write in writes if write not in matched]
            )
            result = Result(
                next="stop",
                status="partial",
                instruction=(
                    f"Cloud read-back verified {len(matched)} of {len(writes)} "
                    f"requested changes. Applied: {applied}. Not applied: "
                    f"{not_applied}. Do not retry this receipt. Report both lists. "
                    "Read current facts only if the owner asks to continue."
                ),
                signals=[
                    f"partial_applied:{len(matched)}",
                    f"partial_not_applied:{len(writes) - len(matched)}",
                ],
                receipt=record.plan_id or record.intent_id,
            )
            self._save_result(record, "pending", result)
            return result
        return self._pending_outcome(record, instruction)

    def _change_titles(self, writes: list[Write]) -> str:
        titles: list[str] = []
        for write in writes:
            record = self._library.records.get(
                write.checklist_parent_uuid or write.uuid
            )
            title = (
                record.title
                if record is not None
                else write.title or f"{write.kind}:{write.uuid}"
            )
            compact = _bounded_title(title)
            compact = compact if len(compact) <= 80 else compact[:79] + "…"
            if compact not in titles:
                titles.append(compact)
        shown = titles[:5]
        if len(titles) > len(shown):
            shown.append(f"and {len(titles) - len(shown)} more")
        return ", ".join(shown)

    def _pending_outcome(self, record: IntentRecord, instruction: str) -> Result:
        attempts = _pending_attempts(record) + 1
        receipt = record.plan_id or record.intent_id
        if attempts >= _PENDING_RETRY_LIMIT:
            result = Result(
                next="stop",
                status="unavailable",
                instruction=(
                    "Cloud read-back did not settle after "
                    f"{_PENDING_RETRY_LIMIT} attempts. Do not retry or defer this "
                    "receipt. Report the unresolved outcome. Read current facts "
                    "only if the owner asks to continue."
                ),
                receipt=receipt,
            )
        else:
            result = Result(
                next="retry_same",
                status="pending",
                instruction=instruction,
                receipt=receipt,
            )
        counted = replace(
            record,
            plan={**record.plan, "pending_attempts": attempts},
        )
        self._save_result(counted, "pending", result)
        return result

    def _settled(
        self,
        intent_id: str,
        writes: list[Write],
        *,
        unchanged: bool,
        already_correct: Sequence[str] = (),
    ) -> Result:
        ids: list[str] = []
        missing_ids: list[str] = []
        tags: list[TagFact] = []
        assigned: dict[str, list[str]] = {}

        def add_id(uuid: str | None) -> None:
            if uuid and uuid not in ids:
                ids.append(uuid)

        def add_tag(uuid: str | None) -> None:
            if uuid is None or any(tag.id == f"tag:{uuid}" for tag in tags):
                return
            tags.append(self._tag_fact(uuid))

        for write in writes:
            if write.action == "ensure_tag":
                title = write.title or ""
                add_tag(self._library.tag_uuid(title) or write.uuid)
                continue
            if write.action in {"rename_tag", "reparent_tag"}:
                add_tag(write.uuid)
                continue
            if write.action == "delete_tag":
                missing_ids.append(f"tag:{write.uuid}")
                continue
            if write.action == "checklist":
                parent = write.checklist_parent_uuid
                if parent:
                    add_id(parent)
                    continue
                for record in self._library.records.values():
                    if any(row.uuid == write.uuid for row in record.checklists):
                        add_id(record.uuid)
                        break
                continue
            if write.action in {"permanent_delete", "delete_area"}:
                if self._library.records.get(write.uuid) is None:
                    kind = "heading" if write.heading else write.kind
                    missing_ids.append(f"{kind}:{write.uuid}")
                else:
                    add_id(write.uuid)
                continue
            add_id(write.uuid)
            if write.tag_uuids:
                assigned[write.uuid] = list(write.tag_uuids)
                for tag_uuid in write.tag_uuids:
                    add_tag(tag_uuid)

        items: list[ItemFact] = []
        ordered_uuids = {
            write.uuid for write in writes if write.sort_index is not None
        }
        for uuid in ids:
            item = self._library.records.get(uuid)
            if item is None:
                continue
            fact = self._fact(item, full=False)
            if uuid in ordered_uuids:
                fact = fact.model_copy(update={"order": item.sort_index})
            tag_ids = assigned.get(uuid)
            if tag_ids:
                fact = fact.model_copy(
                    update={
                        "direct_tag_ids": [f"tag:{tag_uuid}" for tag_uuid in tag_ids]
                    }
                )
            items.append(fact)
            if len(items) >= _CONTEXT_LIMIT:
                break
        changed_item_count = sum(
            self._library.records.get(uuid) is not None for uuid in ids
        )
        changed_items_omitted = max(changed_item_count - len(items), 0)
        all_missing_ids = list(dict.fromkeys(missing_ids))
        missing_ids = all_missing_ids[:_CONTEXT_LIMIT]
        missing_ids_omitted = max(len(all_missing_ids) - len(missing_ids), 0)
        unchanged_items: list[ReceiptItemFact] = []
        for item_id in already_correct:
            item = self._exact_item(item_id)
            if item is not None:
                unchanged_items.append(
                    ReceiptItemFact(id=item.id, title=_bounded_title(item.title))
                )
            if len(unchanged_items) >= _CONTEXT_LIMIT:
                break
        unchanged_items_omitted = max(len(already_correct) - len(unchanged_items), 0)
        created = {
            write.uuid
            for write in writes
            if write.action in {"create", "create_heading"}
        }
        instruction = (
            "The requested state was already true."
            if unchanged
            else self._applied_instruction(items, created, writes)
        )
        if already_correct:
            instruction = (
                instruction.rstrip(".")
                + f". {len(already_correct)} requested item(s) were already correct; "
                "already_correct identifies them by exact ID and title."
            )
        receipt_signals = []
        if changed_items_omitted:
            receipt_signals.append(f"items_truncated:{changed_items_omitted}")
        if missing_ids_omitted:
            receipt_signals.append(f"missing_ids_truncated:{missing_ids_omitted}")
        if unchanged_items_omitted:
            receipt_signals.append(
                f"unchanged_items_truncated:{unchanged_items_omitted}"
            )
        receipt_truncated = any(
            (changed_items_omitted, missing_ids_omitted, unchanged_items_omitted)
        )
        return Result(
            next="done",
            status="unchanged" if unchanged else "applied",
            instruction=instruction,
            items=items,
            already_correct=unchanged_items,
            tags=tags,
            signals=receipt_signals,
            receipt=intent_id,
            missing_ids=missing_ids,
            truncated=receipt_truncated,
        )

    def _applied_instruction(
        self, items: list[ItemFact], created: set[str], writes: list[Write]
    ) -> str:
        registry_instruction = self._registry_applied_instruction(writes)
        if registry_instruction is not None:
            return registry_instruction
        project_creates = [
            write
            for write in writes
            if write.action == "create" and write.kind == "project"
        ]
        if len(project_creates) == 1:
            project = project_creates[0]
            tasks = [
                write
                for write in writes
                if write.action == "create"
                and write.kind == "task"
                and write.into_uuid == project.uuid
            ]
            headings = [
                write
                for write in writes
                if write.action == "create_heading" and write.into_uuid == project.uuid
            ]
            document_uuids = {
                project.uuid,
                *[task.uuid for task in tasks],
                *[heading.uuid for heading in headings],
            }
            document_only = all(
                write.uuid in document_uuids
                or (
                    write.action == "checklist"
                    and write.checklist_parent_uuid in document_uuids
                )
                for write in writes
            )
            if tasks and document_only:
                created_project = self._library.records.get(project.uuid)
                home = (
                    self._record_home_title(created_project)
                    if created_project is not None
                    else self._home_title(project.into_kind, project.into_uuid)
                )
                note_count = sum(bool((task.notes or "").strip()) for task in tasks)
                detail = (
                    f'Created "{project.title}" in {home} with {len(tasks)} Tasks'
                )
                if headings:
                    detail += f" under {len(headings)} headings"
                detail += "."
                if project.notes and note_count == len(tasks):
                    detail += (
                        f" Project notes and all {note_count} Task notes passed read-back."
                    )
                detail += f' First Task: "{tasks[0].title}". Report this result and stop.'
                if len(detail) <= 1000:
                    return detail
        broad_instruction = self._broad_applied_instruction(writes, items)
        if broad_instruction is not None:
            return broad_instruction
        named: list[tuple[str, str, bool]] = []
        homes: list[str] = []
        for fact in items:
            uuid = parse_id(fact.id)[1]
            record = self._library.records.get(uuid)
            if record is None or record.inbox or record.kind == "area":
                continue
            home = self._record_home_title(record)
            named.append((fact.title, home, uuid in created))
            if home not in homes:
                homes.append(home)
        if not homes:
            return "Cloud read-back matched the requested state."
        if len(named) == 1:
            title, home, is_create = named[0]
            verb = "Created" if is_create else "Updated"
            text = f"{verb} {title} in {home}."
        else:
            joined = " and ".join(homes)
            if all(is_create for _title, _home, is_create in named):
                text = f"Created in {joined}."
            elif not any(is_create for _title, _home, is_create in named):
                text = f"Updated in {joined}."
            else:
                text = f"Applied in {joined}."
        if len(text) > 1000:
            return "Cloud read-back matched the requested state."
        return text

    def _broad_applied_instruction(
        self, writes: list[Write], items: list[ItemFact]
    ) -> str | None:
        changed = {
            write.uuid
            for write in writes
            if write.kind in {"task", "project"}
            and write.action not in {"checklist", "repeat", "repeat_link"}
        }
        if len(changed) <= 1:
            return None
        if len(changed) <= 5 and any(
            write.uuid in changed and write.action in {"create", "create_heading"}
            for write in writes
        ):
            return None
        tasks = len(
            {
                write.uuid
                for write in writes
                if write.uuid in changed and write.kind == "task"
            }
        )
        projects = len(
            {
                write.uuid
                for write in writes
                if write.uuid in changed and write.kind == "project"
            }
        )
        instruction = (
            f"Read-back verified {tasks} Task changes and {projects} Project changes. "
            "All requested changes now match Things Cloud. The items array reports "
            "changed Tasks and Projects. A truncation signal gives any omitted count. "
            "missing_ids confirms permanent removals. Report "
            "this result and stop."
        )
        return " ".join(instruction.split())

    def _registry_applied_instruction(self, writes: list[Write]) -> str | None:
        area_uuids = {
            write.uuid
            for write in writes
            if write.kind == "area"
            and write.action in {"create", "update", "rename_area", "delete_area"}
        }
        tag_actions = {"ensure_tag", "rename_tag", "reparent_tag", "delete_tag"}
        tag_uuids = {write.uuid for write in writes if write.action in tag_actions}
        tag_registry_changed = any(
            write.action in {"rename_tag", "reparent_tag", "delete_tag"}
            for write in writes
        )
        if not area_uuids and not tag_registry_changed:
            return None

        def count_phrase(count: int, label: str) -> str:
            suffix = "" if count == 1 else "s"
            return f"{count} {label}{suffix} applied."

        parts: list[str] = []
        if area_uuids:
            parts.append(count_phrase(len(area_uuids), "Area change"))
        if tag_uuids:
            parts.append(count_phrase(len(tag_uuids), "tag change"))

        item_uuids = {
            write.uuid
            for write in writes
            if write.kind in {"task", "project"}
            and write.action
            not in {"checklist", "repeat", "repeat_link", *tag_actions}
        }
        if item_uuids:
            parts.append(count_phrase(len(item_uuids), "Task or Project change"))

        if area_uuids:
            areas = sorted(
                (
                    record
                    for record in self._library.records.values()
                    if record.kind == "area" and record.is_open()
                ),
                key=lambda record: (record.sort_index, record.uuid),
            )
            parts.append(
                "Final Area order: " + ", ".join(area.title for area in areas) + "."
            )
        if tag_uuids:
            tag_titles = sorted(self._library.tags.values(), key=str.casefold)
            parts.append("Final tag catalog: " + ", ".join(tag_titles) + ".")

        parts.append("All requested changes passed read-back. Report this result and stop.")
        instruction = " ".join(parts)
        return (
            instruction
            if len(instruction) <= 1000
            else "All requested registry changes passed read-back. Report this result and stop."
        )

    def _plan_payload(self, prepared: _Prepared) -> JsonDict:
        return {
            "writes": [_write_json(write) for write in prepared.writes],
            "preconditions": dict(prepared.preconditions),
            "summary": list(prepared.summary),
            "warnings": list(prepared.warnings),
            "already_correct": list(prepared.already_correct),
        }

    def _writes_from_plan(self, plan: JsonDict) -> list[Write]:
        raw = cast(list[object], plan.get("writes", []))
        return [_write_from_json(cast(dict[str, object], value)) for value in raw]

    @staticmethod
    def _already_correct_from_plan(plan: JsonDict) -> list[str]:
        raw = cast(list[object], plan.get("already_correct", []))
        return [str(value) for value in raw[:120]]

    def _preconditions_changed(self, plan: JsonDict) -> bool:
        raw = cast(dict[str, object], plan.get("preconditions", {}))
        for item_id, expected in raw.items():
            if item_id == "scope:areas":
                if self._area_scope_revision() != expected:
                    return True
                continue
            if item_id == "scope:tags":
                if self._tag_revision() != expected:
                    return True
                continue
            if item_id == "scope:today":
                if self._today_scope_revision() != expected:
                    return True
                continue
            if item_id.startswith("scope:list:"):
                encoded_scope = item_id.removeprefix("scope:list:")
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
            if item_id.startswith("scope:project:"):
                uuid = item_id.removeprefix("scope:project:")
                if self._project_scope_revision(uuid) != expected:
                    return True
                continue
            if item_id.startswith("scope:repeat:"):
                uuid = item_id.removeprefix("scope:repeat:")
                if self._recurrence_scope_revision(uuid) != expected:
                    return True
                continue
            item = self._exact_item(item_id)
            if item is None or self._revision(item) != expected:
                return True
        return False

    def _writes_match(self, writes: list[Write]) -> bool:
        return self._library.matches(writes)

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
            "completed_at": item.completed_at.isoformat()
            if item.completed_at
            else None,
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
                for row in sorted(
                    item.checklists, key=lambda row: (row.sort_index, row.uuid)
                )
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
    def _list_scope_key(kind: Kind, scope: tuple[str, str | None]) -> str:
        return "scope:list:" + json.dumps(
            [kind, scope[0], scope[1]], separators=(",", ":")
        )

    def _list_scope_revision(self, kind: Kind, scope: tuple[str, str | None]) -> str:
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

    def _project_membership_changes(
        self, project_uuid: str, writes: list[Write]
    ) -> bool:
        for write in writes:
            current = self._library.records.get(write.uuid)
            if write.into_kind == "project" and write.into_uuid == project_uuid:
                if current is None or current.parent_uuid != project_uuid:
                    return True
                if (
                    write.sort_index is not None
                    and write.sort_index != current.sort_index
                ):
                    return True
                if write.clear_heading and current.heading_uuid:
                    return True
                if (
                    write.heading_uuid is not None
                    and write.heading_uuid != current.heading_uuid
                ):
                    return True
            if current is None or current.parent_uuid != project_uuid:
                continue
            if write.action in {"trash", "permanent_delete", "restore"}:
                return True
            if write.inbox or write.anytime:
                return True
            if write.into_kind == "area":
                return True
            if write.into_uuid is not None and write.into_uuid != project_uuid:
                return True
        return False

    def _project_scope_revision(self, uuid: str) -> str:
        project = self._library.records.get(uuid)
        rows = [] if project is None else [[project.id, self._revision(project)]]
        rows.extend(
            [item.id, self._revision(item)]
            for item in sorted(
                self._project_descendants(uuid), key=lambda item: item.id
            )
        )
        return "s_" + _digest(rows)

    def _project_descendants(self, uuid: str) -> list[Record]:
        """Return the complete Project subtree with the deepest records first."""
        by_parent: dict[str, list[Record]] = {}
        for item in self._library.records.values():
            if item.parent_uuid is not None:
                by_parent.setdefault(item.parent_uuid, []).append(item)
        ordered: list[Record] = []

        def visit(parent_uuid: str, path: frozenset[str]) -> None:
            for child in sorted(
                by_parent.get(parent_uuid, []),
                key=lambda item: (item.sort_index, item.uuid),
            ):
                if child.uuid in path:
                    raise _Abort(
                        self._rejected("The Project structure contains a cycle.")
                    )
                visit(child.uuid, path | {child.uuid})
                ordered.append(child)

        visit(uuid, frozenset({uuid}))
        return ordered

    def _recurrence_scope_revision(self, uuid: str) -> str:
        items: list[Record] = []
        template = self._library.records.get(uuid)
        if template is not None:
            items.append(template)
        items.extend(self._library.recurrence_instances(uuid))
        return "s_" + _digest([[item.id, self._revision(item)] for item in items])

    def _recurrence_relationship_is_valid(self, target: Record) -> bool:
        """Check the native one-way link before exposing repeat mutation facts."""
        recurrence = target.recurrence
        if recurrence.role == "none":
            return not self._library.recurrence_instances(target.uuid)
        if recurrence.role == "template":
            return all(
                candidate.recurrence.role == "instance"
                for candidate in self._library.recurrence_instances(target.uuid)
            )
        template_uuid = template_uuid_of(target)
        if recurrence.role != "instance" or template_uuid is None:
            return False
        template = self._library.records.get(template_uuid)
        return (
            template is not None
            and template.recurrence.role == "template"
            and template.recurrence.rule is not None
        )

    def _recurrence_kind(self, item: Record) -> RecurrenceKind:
        if item.recurrence.role == "template":
            return "template"
        if item.recurrence.role == "instance":
            repeat_type = item.recurrence.repeat_type
            if repeat_type not in {"fixed", "after_completion"}:
                template = self._library.records.get(template_uuid_of(item) or "")
                if template is not None:
                    repeat_type = template.recurrence.repeat_type
            if repeat_type == "fixed":
                return "fixed_instance"
            if repeat_type == "after_completion":
                return "after_completion_instance"
            return "unknown"
        return "none"

    @staticmethod
    def _needs_input(instruction: str) -> Result:
        return Result(next="ask", status="needs_input", instruction=instruction)

    @staticmethod
    def _revise(instruction: str) -> Result:
        return Result(next="revise", status="rejected", instruction=instruction)

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
    return (
        "sha256:"
        + sha256(
            json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode()
        ).hexdigest()
    )


def _bounded_tag_title(title: str) -> str:
    return title if len(title) <= 1000 else title[:999] + "…"


def _tag_cost(tag: TagFact) -> int:
    return (
        len(tag.id)
        + len(tag.title)
        + sum(len(parent) for parent in tag.parent_ids)
        + len(tag.from_id or "")
    )


def _take_budget[T](
    rows: list[T], remain: int, cost: Callable[[T], int]
) -> tuple[list[T], int, bool]:
    kept: list[T] = []
    for row in rows:
        need = cost(row)
        if remain < need:
            return kept, remain, True
        kept.append(row)
        remain -= need
    return kept, remain, False


def _bound_bulk_text(facts: list[ItemFact]) -> list[ItemFact]:
    remain = _BULK_TEXT_BUDGET
    notes = [fact.notes_markdown or "" for fact in facts]
    prefixes = [""] * len(facts)
    for index, text in enumerate(notes):
        take = min(len(text), _NOTE_RESERVE, remain)
        prefixes[index] = text[:take]
        remain -= take

    kept_checks: list[list[ChecklistFact]] = []
    cut_checks: list[bool] = []
    for fact in facts:
        kept, remain, cut = _take_budget(
            list(fact.checklist), remain, lambda row: len(row.title)
        )
        kept_checks.append(kept)
        cut_checks.append(cut)

    kept_direct: list[list[str]] = []
    kept_inherited: list[list[str]] = []
    cut_tags: list[bool] = []
    for fact in facts:
        direct_ids = list(fact.direct_tag_ids) or [tag.id for tag in fact.direct_tags]
        inherited_ids = list(fact.inherited_tag_ids) or [
            tag.id for tag in fact.inherited_tags
        ]
        direct, remain, direct_cut = _take_budget(direct_ids, remain, len)
        inherited: list[str] = []
        inherited_cut = False
        if not direct_cut:
            inherited, remain, inherited_cut = _take_budget(
                inherited_ids, remain, len
            )
        kept_direct.append(direct)
        kept_inherited.append(inherited)
        cut_tags.append(direct_cut or inherited_cut)

    kept_notes: list[str] = []
    cut_notes: list[bool] = []
    for index, text in enumerate(notes):
        rest = text[len(prefixes[index]) :]
        if remain < len(rest):
            kept_notes.append(prefixes[index] + rest[:remain])
            cut_notes.append(True)
            remain = 0
        else:
            kept_notes.append(prefixes[index] + rest)
            cut_notes.append(len(prefixes[index]) + len(rest) < len(text))
            remain -= len(rest)

    return [
        _with_completeness(
            fact,
            notes=kept_notes[index] or None,
            checklist=kept_checks[index],
            direct=[],
            inherited=[],
            direct_ids=kept_direct[index],
            inherited_ids=kept_inherited[index],
            truncated=[
                *fact.truncated_fields,
                *(["checklist"] if cut_checks[index] else []),
                *(["tags"] if cut_tags[index] else []),
                *(["notes"] if cut_notes[index] else []),
            ],
        )
        for index, fact in enumerate(facts)
    ]


def _with_completeness(
    fact: ItemFact,
    *,
    notes: str | None,
    checklist: list[ChecklistFact],
    direct: list[TagFact],
    inherited: list[TagFact],
    truncated: Sequence[str],
    linked_item_ids: list[str] | None = None,
    direct_ids: list[str] | None = None,
    inherited_ids: list[str] | None = None,
) -> ItemFact:
    truncated_fields = _truncated_fields(
        notes="notes" in truncated,
        checklist="checklist" in truncated,
        tags="tags" in truncated,
        recurrence="recurrence" in truncated,
    )
    recurrence = fact.recurrence
    if linked_item_ids is not None and recurrence is not None:
        recurrence = recurrence.model_copy(update={"linked_item_ids": linked_item_ids})
    extras = (
        ["recurrence_links_truncated"] if "recurrence" in truncated_fields else []
    )
    return fact.model_copy(
        update={
            "notes_markdown": notes,
            "checklist": checklist,
            "direct_tags": direct,
            "inherited_tags": inherited,
            "direct_tag_ids": (
                list(fact.direct_tag_ids) if direct_ids is None else direct_ids
            ),
            "inherited_tag_ids": (
                list(fact.inherited_tag_ids)
                if inherited_ids is None
                else inherited_ids
            ),
            "recurrence": recurrence,
            "truncated_fields": truncated_fields,
            "signals": _signals_with_truncation(
                list(fact.signals), truncated_fields, *extras
            ),
        }
    )


def _slim_tag(tag: TagFact) -> TagFact:
    return tag.model_copy(update={"parent_ids": [], "parents_truncated": False})


def _hoist_bulk_tags(facts: list[ItemFact]) -> tuple[list[ItemFact], list[TagFact]]:
    registry: dict[str, TagFact] = {}
    for fact in facts:
        for tag in (*fact.direct_tags, *fact.inherited_tags):
            current = registry.get(tag.id)
            if current is None or len(tag.parent_ids) > len(current.parent_ids):
                registry[tag.id] = tag.model_copy(update={"from_id": None})
    hoisted = [registry[key] for key in sorted(registry)]
    overflow = {tag.id for tag in hoisted[_TAG_REGISTRY_LIMIT:]}
    hoisted = hoisted[:_TAG_REGISTRY_LIMIT]
    kept = {tag.id for tag in hoisted}
    slim = [
        _with_completeness(
            fact,
            notes=fact.notes_markdown,
            checklist=list(fact.checklist),
            direct=[
                _slim_tag(tag) for tag in fact.direct_tags if tag.id in kept
            ],
            inherited=[
                _slim_tag(tag) for tag in fact.inherited_tags if tag.id in kept
            ],
            truncated=[
                *fact.truncated_fields,
                *(["tags"] if overflow.intersection(
                    tag.id for tag in (*fact.direct_tags, *fact.inherited_tags)
                ) else []),
            ],
        )
        for fact in facts
    ]
    return slim, hoisted


def _normalize_bulk_tag_ids(facts: list[ItemFact]) -> list[ItemFact]:
    return [
        fact.model_copy(
            update={
                "direct_tag_ids": [tag.id for tag in fact.direct_tags],
                "inherited_tag_ids": [tag.id for tag in fact.inherited_tags],
                "direct_tags": [],
                "inherited_tags": [],
            }
        )
        for fact in facts
    ]


def _result_bytes(result: Result) -> int:
    payload = dump_result(result)
    return len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    )


def _replace_item(facts: list[ItemFact], index: int, fact: ItemFact) -> list[ItemFact]:
    updated = list(facts)
    updated[index] = fact
    return updated


def _trim_optional_detail(result: Result) -> Result | None:
    facts = list(result.items)
    tags = list(result.tags)
    if any(tag.parent_ids for tag in tags):
        return result.model_copy(
            update={
                "tags": [
                    tag.model_copy(
                        update={"parent_ids": [], "parents_truncated": True}
                    )
                    if tag.parent_ids
                    else tag
                    for tag in tags
                ]
            }
        )
    for index in range(len(facts) - 1, -1, -1):
        fact = facts[index]
        notes = fact.notes_markdown or ""
        links = (
            list(fact.recurrence.linked_item_ids)
            if fact.recurrence is not None
            else []
        )
        if len(notes) > _NOTE_RESERVE:
            next_fact = _with_completeness(
                fact,
                notes=notes[:_NOTE_RESERVE],
                checklist=list(fact.checklist),
                direct=list(fact.direct_tags),
                inherited=list(fact.inherited_tags),
                truncated=[*fact.truncated_fields, "notes"],
            )
        elif fact.checklist:
            next_fact = _with_completeness(
                fact,
                notes=fact.notes_markdown,
                checklist=[],
                direct=list(fact.direct_tags),
                inherited=list(fact.inherited_tags),
                truncated=[*fact.truncated_fields, "checklist"],
            )
        elif links:
            next_fact = _with_completeness(
                fact,
                notes=fact.notes_markdown,
                checklist=list(fact.checklist),
                direct=list(fact.direct_tags),
                inherited=list(fact.inherited_tags),
                truncated=[*fact.truncated_fields, "recurrence"],
                linked_item_ids=[],
            )
        elif notes:
            next_fact = _with_completeness(
                fact,
                notes=None,
                checklist=list(fact.checklist),
                direct=list(fact.direct_tags),
                inherited=list(fact.inherited_tags),
                truncated=[*fact.truncated_fields, "notes"],
            )
        elif fact.inherited_tag_ids:
            next_fact = _with_completeness(
                fact,
                notes=fact.notes_markdown,
                checklist=list(fact.checklist),
                direct=list(fact.direct_tags),
                inherited=[],
                inherited_ids=[],
                truncated=[*fact.truncated_fields, "tags"],
            )
        elif fact.direct_tag_ids:
            next_fact = _with_completeness(
                fact,
                notes=fact.notes_markdown,
                checklist=list(fact.checklist),
                direct=[],
                inherited=list(fact.inherited_tags),
                direct_ids=[],
                truncated=[*fact.truncated_fields, "tags"],
            )
        else:
            continue
        facts = _replace_item(facts, index, next_fact)
        return result.model_copy(
            update={
                "items": facts,
                "tags": _prune_unused_tags(facts, tags),
            }
        )
    return None


def _prune_unused_tags(
    facts: list[ItemFact], tags: list[TagFact]
) -> list[TagFact]:
    used = {
        tag_id
        for fact in facts
        for tag_id in (
            *fact.direct_tag_ids,
            *fact.inherited_tag_ids,
            *(tag.id for tag in fact.direct_tags),
            *(tag.id for tag in fact.inherited_tags),
        )
    }
    return [tag for tag in tags if tag.id in used]


def _truncated_fields(
    *,
    notes: bool = False,
    checklist: bool = False,
    tags: bool = False,
    recurrence: bool = False,
) -> list[TruncatedField]:
    fields: list[TruncatedField] = []
    if notes:
        fields.append("notes")
    if checklist:
        fields.append("checklist")
    if tags:
        fields.append("tags")
    if recurrence:
        fields.append("recurrence")
    return fields


def _signals_with_truncation(
    signals: list[str], truncated_fields: Sequence[str], *extra: str
) -> list[str]:
    extras = [_TRUNCATION_SIGNALS[field] for field in truncated_fields]
    extras.extend(extra)
    extras = list(dict.fromkeys(extras))
    skip = set(extras)
    ordinary = [name for name in dict.fromkeys(signals) if name not in skip]
    return [*extras, *ordinary[: max(20 - len(extras), 0)]]


def _diagnostics_digest(conflicts: list[Conflict], titles: list[str]) -> str:
    return "s_" + _digest(
        [
            [
                row.item_id,
                title,
                list(row.signals),
                row.repair_kind,
                list(row.repairs),
            ]
            for row, title in zip(conflicts, titles, strict=True)
        ]
    )


def _bounded_id_list(ids: list[str]) -> str:
    text = ", ".join(ids)
    if len(text) <= 800:
        return text
    kept: list[str] = []
    used = 0
    for item_id in ids:
        extra = len(item_id) + (2 if kept else 0)
        if used + extra > 760:
            kept.append("and more")
            break
        kept.append(item_id)
        used += extra
    return ", ".join(kept)


def _bounded_title(title: str) -> str:
    cleaned = title.strip() if title else ""
    if not cleaned:
        return "(untitled)"
    return cleaned if len(cleaned) <= _TITLE_LIMIT else cleaned[: _TITLE_LIMIT - 1] + "…"


def _bounded_order(value: int) -> int:
    return max(_ORDER_MIN, min(value, _ORDER_MAX))


def _digest(value: object) -> str:
    return sha256(
        json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
    ).hexdigest()[:24]


def _public_status(status: Status) -> PublicStatus:
    return (
        "completed"
        if status == "done"
        else "canceled"
        if status == "dropped"
        else "open"
    )


def _internal_status(status: str | None) -> Status | None:
    if status is None:
        return None
    return (
        "done"
        if status == "completed"
        else "dropped"
        if status == "canceled"
        else "open"
    )


def _pending_attempts(record: IntentRecord) -> int:
    value = record.plan.get("pending_attempts")
    return value if isinstance(value, int) and value >= 0 else 0


def _outcome_unknown(error: CloudError) -> bool:
    text = str(error).casefold()
    return any(
        term in text for term in ("timed out", "unknown", "read-back", "read back")
    )


def _write_public_id(write: Write) -> str:
    kind: PublicKind = "heading" if write.heading else write.kind
    return public_id(kind, write.uuid)


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


def _legacy_recovery_plan_is_complete(plan: JsonDict) -> bool:
    raw = plan.get("writes")
    if not isinstance(raw, list) or not raw:
        return False
    for value in raw:
        if not isinstance(value, dict):
            return False
        action = value.get("action")
        uuid = value.get("uuid")
        kind = value.get("kind")
        if (
            action not in _LEGACY_WRITE_ACTIONS
            or kind not in _LEGACY_WRITE_KINDS
            or not isinstance(uuid, str)
            or not uuid
        ):
            return False
        try:
            write = _write_from_json(cast(dict[str, object], value))
        except (TypeError, ValueError):
            return False
        action = write.action
        if (write.into_uuid is None) != (write.into_kind is None):
            return False
        if write.into_kind not in {None, "project", "area"}:
            return False
        if action == "move" and write.kind not in {"task", "project"}:
            return False
        if action == "move" and write.kind == "project" and write.into_kind == "project":
            return False
        if action in {"rename_area", "delete_area"} and write.kind != "area":
            return False
        if action == "create_heading" and write.kind != "task":
            return False
        if action in {"create", "create_heading", "rename_area", "ensure_tag", "rename_tag"} and not write.title:
            return False
        if action == "update" and not _legacy_has_effective_field(write, (
            "title", "notes", "status", "into_uuid", "start", "clear_start",
            "deadline", "clear_deadline", "remind", "clear_remind", "tag_uuids",
            "tonight", "someday", "inbox", "anytime", "heading_uuid",
            "clear_heading", "sort_index", "today_index",
        )):
            return False
        if action == "move" and not (
            (write.into_uuid is not None and write.into_kind is not None)
            or _legacy_has_effective_field(write, (
                "start", "clear_start", "tonight", "someday", "inbox", "anytime",
                "heading_uuid", "clear_heading", "sort_index", "today_index",
            ))
        ):
            return False
        if action == "tags" and write.tag_uuids is None:
            return False
        if action == "reparent_tag" and write.tag_parent_uuids is None:
            return False
        if action == "checklist" and not (
            write.title
            or _legacy_has_effective_field(write, (
                "checklist_parent_uuid", "checklist_status", "checklist_index",
                "checklist_remove",
            ))
        ):
            return False
        if action == "repeat" and not write.recurrence_rule:
            return False
        if action == "repeat_link" and write.recurrence_links is None:
            return False
    return True


def _legacy_has_effective_field(write: Write, names: tuple[str, ...]) -> bool:
    return any(
        (value := getattr(write, name)) is not None and value is not False
        for name in names
    )


_LEGACY_WRITE_ACTIONS = frozenset({
    "create", "create_heading", "update", "complete", "cancel", "move", "tags",
    "rename_area", "delete_area", "trash", "restore", "permanent_delete",
    "ensure_tag", "rename_tag", "reparent_tag", "delete_tag", "checklist",
    "repeat", "repeat_link",
})
_LEGACY_WRITE_KINDS = frozenset({"task", "project", "area"})


def _legacy_plan_digest(plan: JsonDict) -> str:
    canonical = json.dumps(plan, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:v1:" + sha256(canonical.encode()).hexdigest()


def _taint_things_text(value: object) -> object:
    """Mark Things-origin title/note values, including nested receipt snapshots."""

    if isinstance(value, list):
        return [_taint_things_text(item) for item in value]
    if not isinstance(value, dict):
        return value
    projected: dict[str, object] = {}
    for key, item in value.items():
        if key in {"title", "notes", "notes_markdown"} and isinstance(item, str):
            projected[key] = {
                "value": item,
                "source": "things_cloud",
                "trust": "untrusted",
            }
        else:
            projected[key] = _taint_things_text(item)
    return projected


def _delete_write(record: Record) -> Write:
    return Write(
        action="permanent_delete",
        uuid=record.uuid,
        kind=record.kind,
        heading=record.heading,
    )


def _diagnostics_instruction(diagnostics: list[DiagnosticFact]) -> str:
    if not diagnostics:
        return "No native-state conflicts are visible."
    instruction = (
        "These records have native-state conflicts. Use diagnostics and repairs."
    )
    if any("test_residue" in row.conflicts for row in diagnostics):
        instruction += " Trash test_residue with this context and short refs."
    return instruction


def _result_json(result: Result) -> JsonDict:
    return cast(JsonDict, dump_result(result))
