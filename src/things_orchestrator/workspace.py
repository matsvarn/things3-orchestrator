"""Things reads, bounded v2 mutations, and retained legacy recovery."""

from __future__ import annotations

import json
import re
from base64 import b32encode
from calendar import monthrange
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from secrets import token_urlsafe
from typing import Any, Callable, Literal, cast

from .cloud import CloudError, CloudWriteRejected
from .config import Preferences
from .consistency import Conflict, diagnose, item_conflicts
from .context import (
    CompletenessFact,
    ContextConflict,
    ContextRef,
    ContextStore,
    MemoryContextStore,
    ReadContext,
    ReadIncludeSelector,
    ReadSelector,
)
from .interface import (
    DETAIL_FIELDS,
    ChecklistFact,
    ContextFact,
    DiagnosticFact,
    DiagnosticRepair,
    ItemFact,
    LayoutFact,
    LayoutSectionFact,
    ReadCall,
    RecoveryFact,
    RecurrenceFact,
    RecurrenceKind,
    RepeatOnFact,
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
    AmbiguousV2Request,
    Journal,
    JsonDict,
    MemoryJournal,
    V2ApplySession,
    V2ApplyState,
    V2Operation,
    V2State,
    same_account_id,
    v2_manifest_is_valid,
)
from .library import (
    MAX_RECURRENCE_INSTANCE_COUNT,
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
from .recurrence import RecurrenceReadError, RecurrenceState, RepeatMode, new_rule

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
_LOGBOOK_DAYS = 14
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
_REPEAT_NEVER = 64_092_211_200


def _after_completion_next(anchor: date, unit: str, interval: int) -> date:
    if unit == "day":
        return anchor + timedelta(days=interval)
    if unit == "week":
        return anchor + timedelta(days=interval * 7)
    if unit == "month":
        month_index = anchor.year * 12 + anchor.month - 1 + interval
        year, zero_month = divmod(month_index, 12)
        month = zero_month + 1
        return date(year, month, min(anchor.day, monthrange(year, month)[1]))
    if unit == "year":
        year = anchor.year + interval
        return date(
            year,
            anchor.month,
            min(anchor.day, monthrange(year, anchor.month)[1]),
        )
    raise ValueError("After-completion repeat has an unsupported unit")


def _repeat_offsets(
    repeat: dict[str, object], unit: str
) -> list[dict[str, object]] | None:
    raw = repeat.get("on")
    if not isinstance(raw, list):
        return None
    offsets: list[dict[str, object]] = []
    for value in raw:
        if not isinstance(value, dict):
            raise ValueError("repeat on entries need selected-date objects")
        offset: dict[str, object] = {}
        month = value.get("month")
        day = value.get("day")
        weekday = value.get("weekday")
        ordinal = value.get("ordinal")
        if unit == "year" and isinstance(month, int):
            offset["mo"] = month - 1
        if isinstance(day, int):
            offset["dy"] = -1 if day == -1 else day - 1
        elif isinstance(weekday, str):
            offset["wd"] = _WEEKDAY_CODES[cast(Weekday, weekday)]
            if unit in {"month", "year"} and isinstance(ordinal, int):
                offset["wdo"] = ordinal
        offsets.append(offset)
    return offsets


def _public_repeat_on(rule: RecurrenceState) -> list[RepeatOnFact]:
    raw_offsets = rule.rule.get("of") if rule.rule is not None else None
    if not isinstance(raw_offsets, list):
        return []
    values: list[RepeatOnFact] = []
    for raw in raw_offsets:
        if not isinstance(raw, dict):
            continue
        month = raw.get("mo")
        day = raw.get("dy")
        weekday = raw.get("wd")
        ordinal = raw.get("wdo")
        fact = _safe_repeat_on_fact(
            month=month,
            day=day,
            weekday=weekday,
            ordinal=ordinal,
        )
        if fact is not None:
            values.append(fact)
    return values


def _safe_repeat_on_fact(
    *, month: object, day: object, weekday: object, ordinal: object
) -> RepeatOnFact | None:
    """Translate one native zero-based selector without trusting persisted data."""
    if isinstance(month, bool) or (
        month is not None and (not isinstance(month, int) or not 0 <= month <= 11)
    ):
        return None
    if isinstance(day, bool) or (
        day is not None and (not isinstance(day, int) or day not in {-1, *range(31)})
    ):
        return None
    if isinstance(weekday, bool) or (
        weekday is not None
        and (not isinstance(weekday, int) or weekday not in _WEEKDAY_NAMES)
    ):
        return None
    if isinstance(ordinal, bool) or (
        ordinal is not None
        and (not isinstance(ordinal, int) or ordinal not in {-1, 1, 2, 3, 4, 5})
    ):
        return None
    if (day is None) == (weekday is None):
        return None
    if ordinal is not None and weekday is None:
        return None
    return RepeatOnFact(
        month=month + 1 if isinstance(month, int) else None,
        day=-1 if day == -1 else day + 1 if isinstance(day, int) else None,
        weekday=(
            cast(Weekday, _WEEKDAY_NAMES[weekday])
            if isinstance(weekday, int)
            else None
        ),
        ordinal=ordinal if isinstance(ordinal, int) else None,
    )


def _repeat_timestamp_date(raw: object) -> str | None:
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not 0 < raw < _REPEAT_NEVER
    ):
        return None
    try:
        return datetime.fromtimestamp(raw, timezone.utc).date().isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _repeat_end(rule: RecurrenceState) -> str | None:
    if rule.rule is None:
        return None
    return _repeat_timestamp_date(rule.rule.get("ed"))


def _rt2_fact(item: Record) -> RecurrenceFact | None:
    raw: object = item.repeater
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return RecurrenceFact(kind="unknown", engine="rt2")
    if not isinstance(raw, dict):
        return None
    version = raw.get("v")
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        return RecurrenceFact(kind="unknown", engine="rt2")
    mode_code = raw.get("t")
    unit_code = raw.get("pfu")
    mode: RepeatMode | None = (
        "fixed"
        if isinstance(mode_code, int)
        and not isinstance(mode_code, bool)
        and mode_code == 0
        else "after_completion"
        if isinstance(mode_code, int)
        and not isinstance(mode_code, bool)
        and mode_code == 1
        else None
    )
    unit = (
        {0: "day", 1: "week", 2: "month", 3: "year"}.get(unit_code)
        if isinstance(unit_code, int) and not isinstance(unit_code, bool)
        else None
    )
    raw_interval = raw.get("pfa")
    interval = (
        raw_interval
        if isinstance(raw_interval, int)
        and not isinstance(raw_interval, bool)
        and 1 <= raw_interval <= 366
        else None
    )
    semantic_on: list[RepeatOnFact] = []
    offsets = raw.get("po")
    if mode == "fixed" and isinstance(offsets, list):
        for offset in offsets:
            if not isinstance(offset, dict):
                continue
            month = offset.get("m")
            day = offset.get("d")
            weekday = offset.get("wd")
            ordinal = offset.get("wo")
            fact = _safe_repeat_on_fact(
                month=month,
                day=day,
                weekday=weekday,
                ordinal=ordinal,
            )
            if fact is not None:
                semantic_on.append(fact)
    until = _repeat_timestamp_date(raw.get("ead"))
    alarm = raw.get("aa")
    reminder_time = None
    if (
        isinstance(alarm, int)
        and not isinstance(alarm, bool)
        and 0 <= alarm < 86_400
    ):
        hours, remainder = divmod(alarm, 3_600)
        reminder_time = f"{hours:02d}:{remainder // 60:02d}"
    return RecurrenceFact(
        kind=(
            "template"
            if mode is not None and unit is not None and interval is not None
            else "unknown"
        ),
        engine="rt2",
        mode=mode,
        unit=cast(Any, unit),
        interval=interval,
        on=semantic_on,
        until=until,
        start_early_days=(
            raw["os"]
            if isinstance(raw.get("os"), int)
            and not isinstance(raw.get("os"), bool)
            and 0 <= raw["os"] <= 366
            else None
        ),
        reminder_time=reminder_time,
        adds_deadline=raw.get("ad") is True,
    )
_SEARCH_ARTICLES = frozenset({"a", "an", "the"})
_SEARCH_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)
_NON_STARTABLE_ACTION_ROW = re.compile(
    r"(?i)^\s*(audit|consider|decide|explore|figure out|handle|investigate|"
    r"look into|plan|research|think about|work on)\b"
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


def _normalize_search_text(text: str) -> _NormalizedSearchText:
    folded = text.casefold()
    return _NormalizedSearchText(
        folded=folded,
        tokens=tuple(_SEARCH_TOKEN_PATTERN.findall(folded)),
    )


@dataclass(frozen=True)
class _RepeatStopPlan:
    replacement: Write
    writes: list[Write]
    preconditions: dict[str, str]


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
class _ItemCursor:
    ids: list[str]
    offset: int
    snapshot_revision: str
    public_scope_revision: str
    full: bool
    view: View | None
    detail: tuple[str, ...]
    expires_at: datetime
    signals_any: tuple[str, ...] = ()
    membership_revision: str | None = None


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
            return self._diagnostics_page(call.limit)
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
        return result

    def _weekly_review_page(
        self,
        call: ReadCall,
        *,
        offset: int = 0,
        expected_ids: list[str] | None = None,
        expected_snapshot: str | None = None,
        expected_membership: str | None = None,
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
        return result

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
        """Read one item and verify its repeat template/copy relationship."""
        assert call.id is not None
        target = self._exact_item(call.id)
        if target is None or target.kind not in {"task", "project"} or target.heading:
            return self._unsupported(
                "Recurrence inspection needs one exact Task or Project, not a heading."
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
        try:
            existing = journal.get_v2_request(
                self._account_id, draft.api_version, draft.request_id
            )
        except AmbiguousV2Request:
            return self._ambiguous_v2_request()
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
                "next_action": "read_receipt",
                "instruction": "An unresolved operation blocks writes. Read its receipt and retry only its exact pending request.",
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
        initial_state: V2State = "pending"
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
        try:
            ownership = journal.create_apply_session_v2(
                operation,
                claim_fence=True,
            )
            with ownership as start:
                outcome = start.outcome
                stored = start.operation
                blockers = start.blockers
                if outcome == "blocked":
                    return {
                        "state": "rejected",
                        "code": "write_fenced",
                        "next_action": "read_receipt",
                        "instruction": "An unresolved operation blocks writes. Read its receipt and retry only its exact pending request.",
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
                    return (
                        self._resume_v2_session(stored.operation_id, start.session)
                        if stored.state == "pending"
                        else self._resume_v2(stored)
                    )
                result = self._apply_v2_session(
                    stored.operation_id, start.session
                )
        except AmbiguousV2Request:
            return self._ambiguous_v2_request()
        if result.get("item_ids"):
            return {**result, "_fresh_items": True}
        return result

    @staticmethod
    def _ambiguous_v2_request() -> JsonDict:
        return {
            "state": "rejected",
            "code": "request_conflict",
            "next_action": "correct_request",
            "instruction": (
                "Conflicting stored operations share this request_id. "
                "No Cloud write was attempted."
            ),
        }

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
        result_ids: list[str] = []

        def append_planned(
            write: Write,
            *,
            prior: JsonDict | None,
            fields: list[str],
            title: str,
        ) -> None:
            writes.append(write)
            before.append(prior)
            touched.append(fields)
            display_titles.append(title)

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
                start, someday, tonight = self._start(start_value)
                write = Write(
                    action="create",
                    uuid=uuid,
                    kind=kind,
                    title=cast(str, capture_item["title"]),
                    notes=cast(str | None, capture_item.get("notes")),
                    into_uuid=parent_uuid or area_uuid,
                    into_kind="project"
                    if parent_uuid
                    else "area"
                    if area_uuid
                    else None,
                    inbox=kind == "task" and into_id is None,
                    start=start,
                    tonight=tonight,
                    someday=someday,
                    deadline=date.fromisoformat(cast(str, capture_item["deadline"]))
                    if capture_item.get("deadline")
                    else None,
                )
                repeat = capture_item.get("repeat")
                children = cast(
                    list[dict[str, object]], capture_item.get("tasks", [])
                )
                if isinstance(repeat, dict):
                    template_uuid = new_uuid()
                    try:
                        rule = new_rule(
                            mode=cast(RepeatMode, repeat.get("mode", "fixed")),
                            unit=cast(Any, repeat["unit"]),
                            interval=cast(int, repeat.get("interval", 1)),
                            anchor=start or self._clock().date(),
                            weekday_codes=[
                                _WEEKDAY_CODES[cast(Any, weekday)]
                                for weekday in cast(
                                    list[object], repeat.get("weekdays", [])
                                )
                            ]
                            if "on" not in repeat
                            else None,
                            offsets=cast(
                                Any,
                                _repeat_offsets(repeat, cast(str, repeat["unit"])),
                            ),
                            until=(
                                date.fromisoformat(cast(str, repeat["until"]))
                                if repeat.get("until")
                                else None
                            ),
                        )
                    except ValueError as error:
                        return {
                            "state": "rejected",
                            "code": "validation_error",
                            "next_action": "correct_request",
                            "instruction": str(error),
                        }
                    template_write = replace(
                        write,
                        uuid=template_uuid,
                        recurrence_rule=rule,
                        recurrence_paused=cast(bool, repeat.get("paused", False)),
                        recurrence_created_through=(start or self._clock().date())
                        + timedelta(days=1),
                        recurrence_instance_count=1,
                        start=None,
                        remind=None,
                        tonight=False,
                        someday=True,
                        inbox=False,
                        anytime=False,
                        today_index=None,
                    )
                    append_planned(
                        template_write,
                        prior=None,
                        fields=[
                            "title",
                            "notes",
                            "start",
                            "deadline",
                            "into",
                            "recurrence",
                        ],
                        title=cast(str, capture_item["title"]),
                    )
                    if kind == "project":
                        for child in children:
                            template_child = Write(
                                action="create",
                                uuid=new_uuid(),
                                kind="task",
                                title=cast(str, child["title"]),
                                notes=cast(str | None, child.get("notes")),
                                into_uuid=template_uuid,
                                into_kind="project",
                            )
                            append_planned(
                                template_child,
                                prior=None,
                                fields=["title", "notes", "into"],
                                title=cast(str, child["title"]),
                            )
                    write = replace(
                        write,
                        recurrence_links=[template_uuid],
                    )
                append_planned(
                    write,
                    prior=None,
                    fields=[
                        "title",
                        "notes",
                        "start",
                        "deadline",
                        "into",
                        *(["recurrence"] if isinstance(repeat, dict) else []),
                    ],
                    title=cast(str, capture_item["title"]),
                )
                result_ids.append(_write_public_id(write))
                if kind == "project":
                    for child in children:
                        child_write = Write(
                            action="create",
                            uuid=new_uuid(),
                            kind="task",
                            title=cast(str, child["title"]),
                            notes=cast(str | None, child.get("notes")),
                            into_uuid=uuid,
                            into_kind="project",
                        )
                        append_planned(
                            child_write,
                            prior=None,
                            fields=["title", "notes", "into"],
                            title=cast(str, child["title"]),
                        )
                        result_ids.append(_write_public_id(child_write))
        else:
            ids = cast(
                list[str],
                payload["ids"]
                if "ids" in payload
                else [
                    row["id"] for row in cast(list[dict[str, object]], payload["items"])
                ],
            )
            result_ids.extend(ids)
            expanded_ids: set[str] = set()
            targets: list[Record] = []
            for item_id in ids:
                target = self._exact_item(item_id)
                if (
                    target is None
                    or target.kind not in {"task", "project"}
                    or target.heading
                ):
                    return {
                        "state": "rejected",
                        "code": "missing_target",
                        "next_action": "correct_request",
                        "instruction": "Mutation targets must be exact Tasks or Projects.",
                    }
                if draft.tool in {"things_complete", "things_trash"} and target.kind == "project":
                    preconditions[f"scope:project:{target.uuid}"] = (
                        self._project_scope_revision(target.uuid)
                    )
                candidates = (
                    [
                        *(
                            child
                            for child in self._project_descendants(target.uuid)
                            if draft.tool != "things_complete"
                            or (
                                child.status == "open"
                                and not child.trashed
                                and not child.heading
                                and child.recurrence.role != "template"
                            )
                        ),
                        target,
                    ]
                    if draft.tool in {"things_complete", "things_trash"}
                    and target.kind == "project"
                    else [target]
                )
                for candidate in candidates:
                    if candidate.repeater is not None and draft.tool in {
                        "things_complete",
                        "things_trash",
                    }:
                        return {
                            "state": "rejected",
                            "code": "validation_error",
                            "next_action": "correct_request",
                            "instruction": "This item uses the newer Things repeater engine; its lifecycle must remain native until that account exposes verified write deltas.",
                        }
                    if candidate.recurrence.role == "template" and draft.tool in {
                        "things_complete",
                        "things_trash",
                    }:
                        return {
                            "state": "rejected",
                            "code": "validation_error",
                            "next_action": "correct_request",
                            "instruction": "Complete or trash a generated copy; use repeat editing to change or stop its template.",
                        }
                    if candidate.id not in expanded_ids:
                        expanded_ids.add(candidate.id)
                        targets.append(candidate)
            if len(targets) > 120:
                return {"state": "rejected", "code": "expanded_write_limit", "next_action": "correct_request", "instruction": "The operation expands beyond 120 writes."}
            progressed_templates: set[str] = set()
            for target in targets:
                item_id = target.id
                preconditions[target.id] = self._revision(target)
                if target.parent_uuid:
                    parent = self._library.records.get(target.parent_uuid)
                    if parent is not None:
                        preconditions[parent.id] = self._revision(parent)
                if draft.tool == "things_complete":
                    if target.kind == "project":
                        preconditions[f"scope:project:{target.uuid}"] = (
                            self._project_scope_revision(target.uuid)
                        )
                    writes.append(Write(action="complete", uuid=target.uuid, kind=target.kind, status="done"))
                    touched.append(["status"])
                    before.append(self._v2_observed(target, ("status",)))
                    display_titles.append(target.title)
                    if (
                        target.status == "open"
                        and target.recurrence.role == "instance"
                        and target.recurrence.repeat_type == "after_completion"
                        and target.recurrence.template_uuid is not None
                        and target.recurrence.template_uuid not in progressed_templates
                    ):
                        template = self._library.records.get(
                            target.recurrence.template_uuid
                        )
                        if (
                            template is None
                            or template.recurrence.role != "template"
                            or template.recurrence.repeat_type != "after_completion"
                            or template.recurrence.unit is None
                            or template.recurrence.interval is None
                        ):
                            return {
                                "state": "rejected",
                                "code": "validation_error",
                                "next_action": "read_fresh",
                                "instruction": "The after-completion template is incomplete; read the series again.",
                            }
                        completed_on = self._clock().date()
                        next_on = _after_completion_next(
                            completed_on,
                            template.recurrence.unit,
                            template.recurrence.interval,
                        )
                        preconditions[template.id] = self._revision(template)
                        preconditions[f"scope:repeat:{template.uuid}"] = (
                            self._recurrence_scope_revision(template.uuid)
                        )
                        writes.append(
                            Write(
                                action="repeat_progress",
                                uuid=template.uuid,
                                kind=template.kind,
                                recurrence_completed_on=completed_on,
                                recurrence_next_on=next_on,
                            )
                        )
                        touched.append(["recurrence"])
                        before.append(self._v2_observed(template, ("recurrence",)))
                        display_titles.append(template.title)
                        progressed_templates.add(template.uuid)
                elif draft.tool == "things_trash":
                    writes.append(Write(action="trash", uuid=target.uuid, kind=target.kind, heading=target.heading))
                    touched.append(["trashed"])
                    before.append(self._v2_observed(target, ("trashed",)))
                    display_titles.append(target.title)
                else:
                    update_rows = cast(list[dict[str, object]], payload["items"])
                    row_index, row = next(
                        (index, entry)
                        for index, entry in enumerate(update_rows)
                        if entry["id"] == item_id
                    )
                    fields = cast(dict[str, object], row["set"])
                    projected_parent_uuid = target.parent_uuid
                    projected_area_uuid = target.area_uuid
                    projected_heading_uuid = target.heading_uuid
                    projected_tag_uuids = list(target.tag_uuids)
                    projected_checklists = list(target.checklists)
                    into_id = fields.get("into_id")
                    if isinstance(into_id, str):
                        destination = self._exact_item(into_id)
                        if destination is None or destination.kind not in {"project", "area"}:
                            return {"state": "rejected", "code": "invalid_destination", "next_action": "correct_request", "instruction": "The exact move destination was not found.", "issues": [{"path": f"items.{row_index}.set.into_id", "code": "invalid_destination", "hint": "Use an exact active Project or Area ID.", "item_index": row_index, "item_id": item_id}]}
                        if destination.status != "open" or destination.trashed or destination.recurrence.role != "none":
                            return {"state": "rejected", "code": "inactive_destination", "next_action": "correct_request", "instruction": "The move destination is not an active ordinary container.", "issues": [{"path": f"items.{row_index}.set.into_id", "code": "inactive_destination", "hint": "Choose an active ordinary container.", "item_index": row_index, "item_id": item_id}]}
                        if target.kind == "project" and destination.kind != "area":
                            return {"state": "rejected", "code": "invalid_destination", "next_action": "correct_request", "instruction": "Projects may only move to Areas.", "issues": [{"path": f"items.{row_index}.set.into_id", "code": "invalid_destination", "hint": "Move a Project only to an exact Area ID.", "item_index": row_index, "item_id": item_id}]}
                        preconditions[destination.id] = self._revision(destination)
                        source = self._library.records.get(target.parent_uuid or target.area_uuid or "")
                        if source is not None:
                            preconditions[source.id] = self._revision(source)
                        heading = self._library.records.get(target.heading_uuid or "")
                        keep_heading = (
                            destination.kind == "project"
                            and heading is not None
                            and heading.heading
                            and heading.parent_uuid == destination.uuid
                        )
                        move_write = Write(
                            action="move",
                            uuid=target.uuid,
                            kind=target.kind,
                            into_uuid=destination.uuid,
                            into_kind=destination.kind,
                            heading_uuid=target.heading_uuid if keep_heading else None,
                            clear_heading=target.heading_uuid is not None and not keep_heading,
                        )
                        writes.append(move_write)
                        touched.append(["into"])
                        before.append(self._v2_observed(target, ("into",)))
                        display_titles.append(target.title)
                        projected_parent_uuid = (
                            destination.uuid if destination.kind == "project" else None
                        )
                        projected_area_uuid = (
                            destination.uuid if destination.kind == "area" else None
                        )
                        projected_heading_uuid = (
                            target.heading_uuid if keep_heading else None
                        )

                    tag_delta = fields.get("tags")
                    if isinstance(tag_delta, dict):
                        add_ids = cast(list[str], tag_delta.get("add", []))
                        remove_ids = cast(list[str], tag_delta.get("remove", []))
                        requested = [*add_ids, *remove_ids]
                        unknown = [tag_id for tag_id in requested if tag_id.removeprefix("tag:") not in self._library.tags]
                        if unknown:
                            return {"state": "rejected", "code": "validation_error", "next_action": "correct_request", "instruction": "One or more exact tag IDs are unknown.", "issues": [{"path": f"items.{row_index}.set.tags", "code": "unknown_tag", "hint": "Use exact tag IDs from a fresh tag catalog read.", "item_index": row_index, "item_id": item_id}]}
                        preconditions["scope:tags"] = self._tag_revision()
                        direct = list(target.tag_uuids)
                        removed = {tag_id.removeprefix("tag:") for tag_id in remove_ids}
                        final_tags = [uuid for uuid in direct if uuid not in removed]
                        for tag_id in add_ids:
                            uuid = tag_id.removeprefix("tag:")
                            if uuid not in final_tags:
                                final_tags.append(uuid)
                        projected_tag_uuids = final_tags
                        writes.append(Write(action="tags", uuid=target.uuid, kind=target.kind, tag_uuids=final_tags))
                        touched.append(["tags"])
                        before.append(self._v2_observed(target, ("tags",)))
                        display_titles.append(target.title)

                    checklist_patch = fields.get("checklist")
                    if isinstance(checklist_patch, dict):
                        if target.kind != "task":
                            return {"state": "rejected", "code": "validation_error", "next_action": "correct_request", "instruction": "Only Tasks can have checklist rows.", "issues": [{"path": f"items.{row_index}.set.checklist", "code": "invalid_checklist_target", "hint": "Patch checklists only on exact Task IDs.", "item_index": row_index, "item_id": item_id}]}
                        rows_by_id = {f"check:{entry.uuid}": entry for entry in target.checklists}
                        named_ids = [
                            *[cast(str, entry["id"]) for entry in cast(list[dict[str, object]], checklist_patch.get("update", []))],
                            *cast(list[str], checklist_patch.get("remove", [])),
                        ]
                        missing_rows = [row_id for row_id in named_ids if row_id not in rows_by_id]
                        if missing_rows:
                            return {"state": "rejected", "code": "validation_error", "next_action": "correct_request", "instruction": "One or more checklist rows do not belong to the target Task.", "issues": [{"path": f"items.{row_index}.set.checklist", "code": "missing_checklist_row", "hint": "Use exact checklist IDs from a fresh read of this Task.", "item_index": row_index, "item_id": item_id}]}
                        for row_id in named_ids:
                            preconditions[row_id] = self._checklist_revision(rows_by_id[row_id])
                        for entry in cast(list[dict[str, object]], checklist_patch.get("update", [])):
                            existing = rows_by_id[cast(str, entry["id"])]
                            patch = cast(dict[str, object], entry["set"])
                            replacement = replace(
                                existing,
                                title=cast(str, patch.get("title", existing.title)),
                                status=(
                                    cast(Status, _internal_status(cast(str, patch["status"])))
                                    if "status" in patch
                                    else existing.status
                                ),
                            )
                            projected_checklists = [
                                replacement if row.uuid == existing.uuid else row
                                for row in projected_checklists
                            ]
                            writes.append(Write(
                                action="checklist", uuid=existing.uuid,
                                title=replacement.title,
                                checklist_parent_uuid=target.uuid,
                                checklist_status=replacement.status,
                                checklist_index=existing.sort_index,
                            ))
                            touched.append(["checklist"])
                            before.append({"id": f"check:{existing.uuid}", "title": existing.title, "status": _public_status(existing.status), "parent_id": target.id, "order": existing.sort_index})
                            display_titles.append(existing.title)
                        for row_id in cast(list[str], checklist_patch.get("remove", [])):
                            existing = rows_by_id[row_id]
                            projected_checklists = [
                                row for row in projected_checklists
                                if row.uuid != existing.uuid
                            ]
                            writes.append(Write(action="checklist", uuid=existing.uuid, title=existing.title, checklist_parent_uuid=target.uuid, checklist_remove=True))
                            touched.append(["checklist"])
                            before.append({"id": row_id, "title": existing.title, "status": _public_status(existing.status), "parent_id": target.id, "order": existing.sort_index})
                            display_titles.append(existing.title)
                        next_index = max((entry.sort_index for entry in target.checklists), default=-1) + 1
                        for offset, entry in enumerate(cast(list[dict[str, object]], checklist_patch.get("add", []))):
                            row_uuid = new_uuid()
                            row_status = cast(
                                Status,
                                _internal_status(cast(str, entry.get("status", "open"))),
                            )
                            projected_checklists.append(ChecklistLine(
                                uuid=row_uuid,
                                title=cast(str, entry["title"]),
                                status=row_status,
                                sort_index=next_index + offset,
                            ))
                            writes.append(Write(
                                action="checklist", uuid=row_uuid, title=cast(str, entry["title"]),
                                checklist_parent_uuid=target.uuid,
                                checklist_status=row_status,
                                checklist_index=next_index + offset,
                            ))
                            touched.append(["checklist"])
                            before.append(None)
                            display_titles.append(cast(str, entry["title"]))
                    if target.repeater is not None and {
                        "repeat",
                        "start",
                        "deadline",
                        "remind_at",
                        "into_id",
                        "tags",
                        "checklist",
                    }.intersection(fields):
                        return {
                            "state": "rejected",
                            "code": "validation_error",
                            "next_action": "correct_request",
                            "instruction": "This item uses the newer Things repeater engine; only title and note edits are currently lossless.",
                        }
                    if "notes" in fields and target.notes_format == "unavailable":
                        return {
                            "state": "rejected",
                            "code": "validation_error",
                            "next_action": "correct_request",
                            "instruction": "The current note could not be reconstructed completely from Things Cloud. Preserve it and retry after a fresh read; do not replace it from partial text.",
                        }
                    start_set = "start" in fields
                    start_value = cast(str | None, fields.get("start"))
                    start, someday, tonight = self._start(start_value) if start_set else (None, False, False)
                    remind_set = "remind_at" in fields
                    if (
                        start_set
                        and (
                            fields.get("start") is None
                            or fields.get("start") == "anytime"
                            or someday
                        )
                        and target.remind is not None
                        and not remind_set
                    ):
                        return {
                            "state": "rejected",
                            "code": "validation_error",
                            "next_action": "correct_request",
                            "instruction": "Clearing a start or moving it to Someday with an existing reminder also requires remind_at=null.",
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
                            return {
                                "state": "rejected",
                                "code": "validation_error",
                                "next_action": "correct_request",
                                "instruction": "start and remind_at must use the same local date.",
                            }
                        if not start_set:
                            if target.start != reminder_date:
                                return {
                                    "state": "rejected",
                                    "code": "validation_error",
                                    "next_action": "correct_request",
                                    "instruction": "remind_at may omit start only when the existing start uses the same local date.",
                                }
                            start = reminder_date
                            tonight = target.tonight
                    ordinary_fields = {
                        key: value for key, value in fields.items()
                        if key not in {"repeat", "into_id", "tags", "checklist"}
                    }
                    anytime_to_top_level = (
                        start_value == "anytime"
                        and target.inbox
                        and not isinstance(into_id, str)
                    )
                    anytime_in_home = (
                        start_value == "anytime" and not anytime_to_top_level
                    )
                    public_start_anytime = (
                        start_set
                        and start is None
                        and not someday
                        and (
                            start_value == "anytime"
                            or not target.inbox
                            or isinstance(into_id, str)
                        )
                    )
                    ordinary_write = Write(
                        action="update",
                        uuid=target.uuid,
                        kind=target.kind,
                        title=cast(str | None, fields.get("title")),
                        notes=cast(str | None, fields.get("notes")),
                        start=start,
                        clear_start=start_set and (fields.get("start") is None or anytime_in_home),
                        tonight=tonight,
                        someday=someday,
                        deadline=date.fromisoformat(cast(str, fields["deadline"]))
                        if fields.get("deadline")
                        else None,
                        clear_deadline="deadline" in fields
                        and fields.get("deadline") is None,
                        remind=reminder,
                        clear_remind="remind_at" in fields
                        and fields.get("remind_at") is None,
                        anytime=anytime_to_top_level,
                        public_start_anytime=public_start_anytime,
                    )
                    repeat = fields.get("repeat")
                    if isinstance(repeat, dict):
                        projected = replace(
                            target,
                            title=cast(str, fields.get("title", target.title)),
                            notes=cast(str, fields.get("notes", target.notes)),
                            start=(start if start_set else target.start),
                            someday=someday if start_set else target.someday,
                            tonight=tonight if start_set else target.tonight,
                            deadline=(
                                date.fromisoformat(cast(str, fields["deadline"]))
                                if fields.get("deadline")
                                else None
                                if "deadline" in fields
                                else target.deadline
                            ),
                            remind=(
                                reminder if remind_set or start_set else target.remind
                            ),
                            parent_uuid=projected_parent_uuid,
                            area_uuid=projected_area_uuid,
                            heading_uuid=projected_heading_uuid,
                            inbox=False if isinstance(into_id, str) else target.inbox,
                            tag_uuids=projected_tag_uuids,
                            checklists=projected_checklists,
                        )
                        failure = self._append_v2_repeat_change(
                            target=target,
                            projected=projected,
                            repeat=repeat,
                            writes=writes,
                            before=before,
                            touched=touched,
                            display_titles=display_titles,
                            result_ids=result_ids,
                            preconditions=preconditions,
                        )
                        if failure is not None:
                            return failure
                    if ordinary_fields:
                        writes.append(ordinary_write)
                        selected = tuple(sorted(ordinary_fields))
                        touched.append(list(selected))
                        before.append(self._v2_observed(target, selected))
                        display_titles.append(target.title)
        permanently_deleted = [
            write.uuid for write in writes if write.action == "permanent_delete"
        ]
        permanently_deleted_set = set(permanently_deleted)
        repeat_series_mutations = [
            write.uuid
            for write in writes
            if write.action in {"repeat", "repeat_next", "permanent_delete"}
        ]
        if (
            len(repeat_series_mutations) != len(set(repeat_series_mutations))
            or any(
                write.uuid in permanently_deleted_set
                and write.action != "permanent_delete"
                for write in writes
            )
        ):
            return {
                "state": "rejected",
                "code": "validation_error",
                "next_action": "correct_request",
                "instruction": (
                    "A batch cannot change an item that it permanently deletes or "
                    "change the same repeat series more than once."
                ),
            }
        if len(writes) > 120:
            return {
                "state": "rejected",
                "code": "expanded_write_limit",
                "next_action": "correct_request",
                "instruction": "The operation expands beyond 120 writes.",
            }
        if not writes:
            return {
                "state": "rejected",
                "code": "validation_error",
                "next_action": "correct_request",
                "instruction": "The operation must compile to at least one explicit write.",
            }
        manifest = OperationManifest.build(
            account_id=self._account_id,
            draft=draft,
            preconditions=preconditions,
            writes=[_write_json(write) for write in writes],
            touched=touched,
            before=before,
            display_titles=display_titles,
            result_ids=result_ids,
            requires_owner=False,
            clock=self._clock(),
        )
        return manifest, writes, before

    def _append_v2_repeat_change(
        self,
        *,
        target: Record,
        projected: Record,
        repeat: dict[str, object],
        writes: list[Write],
        before: list[JsonDict | None],
        touched: list[list[str]],
        display_titles: list[str],
        result_ids: list[str],
        preconditions: dict[str, str],
    ) -> JsonDict | None:
        if target.kind not in {"task", "project"} or target.heading:
            return {
                "state": "rejected",
                "code": "validation_error",
                "next_action": "correct_request",
                "instruction": "The repeat protocol applies to Tasks and Projects.",
            }
        remove = repeat.get("remove") is True
        if target.recurrence.role == "none":
            if remove or repeat.get("create_next") is True:
                return {
                    "state": "rejected",
                    "code": "validation_error",
                    "next_action": "correct_request",
                    "instruction": "That item is not repeating.",
                }
            if target.status != "open" or target.trashed:
                return {
                    "state": "rejected",
                    "code": "validation_error",
                    "next_action": "correct_request",
                    "instruction": "Only an open Task or Project outside Trash can start repeating.",
                }
            unit = repeat.get("unit")
            if not isinstance(unit, str):
                return {
                    "state": "rejected",
                    "code": "validation_error",
                    "next_action": "correct_request",
                    "instruction": "Starting repetition needs a unit.",
                }
            template_uuid = new_uuid()
            try:
                rule = new_rule(
                    mode=cast(RepeatMode, repeat.get("mode", "fixed")),
                    unit=cast(Any, unit),
                    interval=cast(int, repeat.get("interval", 1)),
                    anchor=projected.start or self._clock().date(),
                    weekday_codes=[
                        _WEEKDAY_CODES[cast(Any, weekday)]
                        for weekday in cast(list[object], repeat.get("weekdays", []))
                    ]
                    if "on" not in repeat
                    else None,
                    offsets=cast(Any, _repeat_offsets(repeat, unit)),
                    until=(
                        date.fromisoformat(cast(str, repeat["until"]))
                        if repeat.get("until")
                        else None
                    ),
                )
            except ValueError as error:
                return {
                    "state": "rejected",
                    "code": "validation_error",
                    "next_action": "correct_request",
                    "instruction": str(error),
                }
            template_write = Write(
                action="create",
                uuid=template_uuid,
                kind=target.kind,
                title=projected.title,
                notes=projected.notes,
                into_uuid=projected.parent_uuid or projected.area_uuid,
                into_kind=(
                    "project"
                    if projected.parent_uuid
                    else "area"
                    if projected.area_uuid
                    else None
                ),
                inbox=False,
                anytime=False,
                deadline=projected.deadline,
                tag_uuids=list(projected.tag_uuids),
                heading_uuid=projected.heading_uuid,
                sort_index=projected.sort_index,
                today_index=None,
                owner_today=self._clock().date(),
                recurrence_rule=rule,
                recurrence_paused=cast(bool, repeat.get("paused", False)),
                recurrence_created_through=(
                    projected.start or self._clock().date()
                )
                + timedelta(days=1),
                recurrence_instance_count=1,
                start=None,
                remind=None,
                tonight=False,
                someday=True,
            )
            writes.append(template_write)
            before.append(None)
            touched.append(
                ["title", "notes", "start", "deadline", "into", "recurrence"]
            )
            display_titles.append(projected.title)
            if projected.kind == "project":
                preconditions[f"scope:project:{target.uuid}"] = (
                    self._project_scope_revision(target.uuid)
                )
                try:
                    graph_writes = self._clone_project_graph_writes(
                        target.uuid,
                        template_uuid,
                        leavable=False,
                    )
                except ValueError as error:
                    return {
                        "state": "rejected",
                        "code": "validation_error",
                        "next_action": "correct_request",
                        "instruction": str(error),
                    }
                for graph_write in graph_writes:
                    writes.append(graph_write)
                    before.append(None)
                    touched.append(
                        []
                        if graph_write.action == "checklist"
                        else ["title", "notes", "status", "into"]
                    )
                    display_titles.append(graph_write.title or projected.title)
            else:
                for row in projected.checklists:
                    writes.append(
                        Write(
                            action="checklist",
                            uuid=new_uuid(),
                            title=row.title,
                            checklist_parent_uuid=template_uuid,
                            checklist_status="open",
                            checklist_index=row.sort_index,
                        )
                    )
                    before.append(None)
                    touched.append([])
                    display_titles.append(row.title)
            writes.append(
                Write(
                    action="repeat_link",
                    uuid=target.uuid,
                    kind=target.kind,
                    recurrence_links=[template_uuid],
                )
            )
            before.append(self._v2_observed(target, ("recurrence",)))
            touched.append(["recurrence"])
            display_titles.append(target.title)
            preconditions[f"scope:repeat:{template_uuid}"] = (
                self._recurrence_scope_revision(template_uuid)
            )
            return None

        template = (
            self._library.records.get(template_uuid_of(target) or "")
            if target.recurrence.role == "instance"
            else target
        )
        if template is None:
            return {
                "state": "rejected",
                "code": "validation_error",
                "next_action": "read_fresh",
                "instruction": "The repeat template is unavailable; read the item again.",
            }
        if "paused" in repeat and not template.recurrence_paused_known:
            return {
                "state": "rejected",
                "code": "validation_error",
                "next_action": "read_fresh",
                "instruction": (
                    "The native repeat pause state is unavailable; read the series "
                    "again before changing it."
                ),
            }
        try:
            template.recurrence.validate_interval_template(kind=template.kind)
        except ValueError as error:
            return {
                "state": "rejected",
                "code": "validation_error",
                "next_action": "correct_request",
                "instruction": str(error),
            }
        preconditions[template.id] = self._revision(template)
        preconditions[f"scope:repeat:{template.uuid}"] = (
            self._recurrence_scope_revision(template.uuid)
        )
        if (
            repeat.get("create_next") is True or remove
        ) and template.recurrence_next_on is None:
            return {
                "state": "rejected",
                "code": "validation_error",
                "next_action": "read_fresh",
                "instruction": (
                    "The repeat template has no native next date; read the series "
                    "again before changing its lifecycle."
                ),
            }
        if repeat.get("create_next") is True:
            if (
                not template.recurrence_instance_count_known
                or template.recurrence_instance_count
                >= MAX_RECURRENCE_INSTANCE_COUNT
            ):
                return {
                    "state": "rejected",
                    "code": "validation_error",
                    "next_action": "read_fresh",
                    "instruction": (
                        "The native generated-copy count is unavailable; read the "
                        "series again before creating another copy."
                    ),
                }
            instances = self._library.recurrence_instances(template.uuid)
            if any(
                not instance.recurrence_generated_on_known
                for instance in instances
            ):
                return {
                    "state": "rejected",
                    "code": "validation_error",
                    "next_action": "read_fresh",
                    "instruction": (
                        "A linked copy has no trustworthy native occurrence date; "
                        "read the series again before creating another copy."
                    ),
                }
            if any(
                instance.recurrence_generated_on == template.recurrence_next_on
                for instance in instances
            ):
                return {
                    "state": "rejected",
                    "code": "validation_error",
                    "next_action": "read_fresh",
                    "instruction": (
                        "The native next date is already materialized; read the "
                        "series again before creating another copy."
                    ),
                }
            next_uuid = new_uuid()
            next_on = template.recurrence_next_on
            assert next_on is not None
            mapped_heading = template.heading_uuid
            current_write = Write(
                action="create",
                uuid=next_uuid,
                kind=template.kind,
                title=template.title,
                notes=template.notes,
                status="open",
                into_uuid=(
                    None
                    if mapped_heading
                    else template.parent_uuid or template.area_uuid
                ),
                into_kind=(
                    None
                    if mapped_heading
                    else "project"
                    if template.parent_uuid
                    else "area"
                    if template.area_uuid
                    else None
                ),
                start=next_on,
                deadline=template.deadline,
                remind=template.remind,
                tag_uuids=list(template.tag_uuids),
                heading_uuid=mapped_heading,
                sort_index=template.sort_index,
                today_index=template.today_index,
                owner_today=self._clock().date(),
                recurrence_links=[template.uuid],
                leavable=True,
            )
            writes.append(current_write)
            before.append(None)
            touched.append(
                ["title", "notes", "start", "deadline", "into", "recurrence"]
            )
            display_titles.append(template.title)
            result_ids.append(_write_public_id(current_write))
            if template.kind == "project":
                try:
                    graph_writes = self._clone_project_graph_writes(
                        template.uuid,
                        next_uuid,
                        leavable=True,
                    )
                except ValueError as error:
                    return {
                        "state": "rejected",
                        "code": "validation_error",
                        "next_action": "correct_request",
                        "instruction": str(error),
                    }
                for graph_write in graph_writes:
                    writes.append(graph_write)
                    before.append(None)
                    touched.append(
                        []
                        if graph_write.action == "checklist"
                        else ["title", "notes", "status", "into"]
                    )
                    display_titles.append(graph_write.title or template.title)
            else:
                for row in template.checklists:
                    writes.append(
                        Write(
                            action="checklist",
                            uuid=new_uuid(),
                            title=row.title,
                            checklist_parent_uuid=next_uuid,
                            checklist_status="open",
                            checklist_index=row.sort_index,
                        )
                    )
                    before.append(None)
                    touched.append([])
                    display_titles.append(row.title)
            writes.append(
                Write(
                    action="repeat_next",
                    uuid=template.uuid,
                    kind=template.kind,
                    recurrence_instance_count=template.recurrence_instance_count + 1,
                )
            )
            before.append(self._v2_observed(template, ("recurrence",)))
            touched.append(["recurrence"])
            display_titles.append(template.title)
            return None
        if remove:
            try:
                plan = self._repeat_stop_plan(template)
            except ValueError as error:
                return {
                    "state": "rejected",
                    "code": "validation_error",
                    "next_action": "correct_request",
                    "instruction": str(error),
                }
            preconditions.update(plan.preconditions)
            for write in plan.writes:
                current = self._library.records.get(write.uuid)
                writes.append(write)
                before.append(
                    self._v2_observed(current, ("recurrence",))
                    if current is not None
                    and write.action in {"repeat_link", "permanent_delete"}
                    else None
                )
                touched.append(
                    ["title", "notes", "start", "deadline", "into", "recurrence"]
                    if write.uuid == plan.replacement.uuid
                    else []
                    if write.action == "checklist"
                    else ["title", "notes", "status", "into"]
                    if write.action in {"create", "create_heading"}
                    else ["recurrence"]
                )
                display_titles.append(
                    current.title
                    if current is not None
                    else write.title or template.title
                )
            result_ids[:] = [item_id for item_id in result_ids if item_id != template.id]
            result_ids.append(_write_public_id(plan.replacement))
            return None

        rule_fields = {
            "mode",
            "unit",
            "interval",
            "weekdays",
            "on",
            "until",
        }.intersection(repeat)
        recurrence = template.recurrence
        if rule_fields:
            try:
                recurrence = template.recurrence.transition(
                    kind=template.kind,
                    mode=cast(RepeatMode | None, repeat.get("mode")),
                    unit=cast(Any, repeat.get("unit")),
                    interval=cast(int | None, repeat.get("interval")),
                    weekday_codes=(
                        [
                            _WEEKDAY_CODES[cast(Any, weekday)]
                            for weekday in cast(list[object], repeat["weekdays"])
                        ]
                        if "weekdays" in repeat
                        else None
                    ),
                    offsets=(
                        cast(
                            Any,
                            _repeat_offsets(
                                repeat,
                                cast(
                                    str,
                                    repeat.get("unit") or template.recurrence.unit,
                                ),
                            ),
                        )
                        if "on" in repeat
                        else None
                    ),
                    until=(
                        date.fromisoformat(cast(str, repeat["until"]))
                        if repeat.get("until")
                        else None
                    ),
                    until_set="until" in repeat,
                )
            except RecurrenceReadError as error:
                return {
                    "state": "rejected",
                    "code": "validation_error",
                    "next_action": "read_fresh",
                    "instruction": str(error),
                }
            except ValueError as error:
                return {
                    "state": "rejected",
                    "code": "validation_error",
                    "next_action": "correct_request",
                    "instruction": str(error),
                }
        writes.append(
            Write(
                action="repeat",
                uuid=template.uuid,
                kind=template.kind,
                recurrence_rule=recurrence.rule if rule_fields else None,
                recurrence_paused=cast(bool | None, repeat.get("paused")),
            )
        )
        before.append(self._v2_observed(template, ("recurrence",)))
        touched.append(["recurrence"])
        display_titles.append(template.title)
        return None

    def _apply_v2(
        self,
        operation: V2Operation,
        *,
        writes: list[Write] | None = None,
        before: list[JsonDict | None] | None = None,
    ) -> JsonDict:
        del writes, before
        with self._journal.apply_session_v2(operation.operation_id) as session:
            return self._apply_v2_session(operation.operation_id, session)

    def _apply_v2_session(
        self, operation_id: str, session: V2ApplySession | None
    ) -> JsonDict:
        if session is None:
            try:
                current = self._journal.get_v2_operation(operation_id)
                if current is not None:
                    if not v2_manifest_is_valid(current):
                        return self._invalid_v2_manifest(current.operation_id)
                    resolved = self._journal.get_v2_request(
                        current.account_id,
                        current.api_version,
                        current.request_id,
                    )
                    if resolved is None or resolved.operation_id != current.operation_id:
                        return self._ambiguous_v2_request()
            except AmbiguousV2Request:
                return self._ambiguous_v2_request()
            return self._persisted_v2_outcome(operation_id)
        operation = session.operation
        if not v2_manifest_is_valid(operation):
            return self._invalid_v2_manifest(operation.operation_id)
        if operation.dispatch_started:
            return self._resume_v2_session(operation_id, session)
        writes = [
            _write_from_json(cast(dict[str, object], row))
            for row in cast(list[object], operation.manifest["writes"])
        ]
        before = cast(
            list[JsonDict | None],
            operation.manifest.get("before", [None] * len(writes)),
        )
        failed = self._refresh(force=True)
        if failed is not None:
            return {"state": "pending", "code": "pending_unknown", "next_action": "retry_same", "instruction": "Retry this exact request to force read-back; the stored operation is never reposted.", "operation_id": operation.operation_id}
        if not self._v2_preconditions_match(operation):
            response: JsonDict = {"state": "not_applied", "code": "not_applied_precondition", "next_action": "read_receipt", "instruction": "A frozen precondition changed before the Cloud write.", "operation_id": operation.operation_id}
            rows = self._v2_receipt_rows(operation, writes, before, "not_applied")
            settled = session.settle(
                state="not_applied", response=response, rows=rows
            )
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
            settled = session.settle(
                state="unchanged", response=response, rows=rows
            )
            return response if settled else self._persisted_v2_outcome(
                operation.operation_id
            )
        if not session.mark_dispatched():
            return self._persisted_v2_outcome(operation.operation_id)
        operation = session.operation
        try:
            applied = self._library.apply(writes)
        except CloudWriteRejected:
            response = {
                "state": "not_applied",
                "code": "not_applied_precondition",
                "next_action": "read_receipt",
                "instruction": (
                    "Things Cloud rejected the frozen write before commit; "
                    "nothing was replayed."
                ),
                "operation_id": operation.operation_id,
            }
            rows = self._v2_rejected_receipt_rows(operation, writes, before)
            settled = session.settle_rejected(response=response, rows=rows)
            return response if settled else self._persisted_v2_outcome(
                operation.operation_id
            )
        except CloudError:
            failed = self._refresh(force=True)
            if failed is not None:
                return {"state": "pending", "code": "pending_unknown", "next_action": "retry_same", "instruction": "Retry this exact request to force read-back; the stored operation is never reposted.", "operation_id": operation.operation_id}
            return self._reconcile_v2(
                operation,
                writes,
                before,
                session=session,
            )
        if not applied.read_back_verified:
            failed = self._refresh(force=True)
            if failed is not None:
                return {"state": "pending", "code": "pending_unknown", "next_action": "retry_same", "instruction": "Retry this exact request to force read-back; the stored operation is never reposted.", "operation_id": operation.operation_id}
        return self._reconcile_v2(
            operation,
            writes,
            before,
            session=session,
            provider_readback_final=applied.read_back_verified,
        )

    def _reconcile_v2(
        self,
        operation: V2Operation,
        writes: list[Write],
        before: list[JsonDict | None],
        *,
        session: V2ApplySession | None = None,
        provider_readback_final: bool = False,
    ) -> JsonDict:
        if not v2_manifest_is_valid(operation):
            return self._invalid_v2_manifest(operation.operation_id)
        matched = [self._writes_match([write]) for write in writes]
        state: V2ApplyState
        if all(matched):
            state = "applied"
        elif any(matched) and (
            not operation.dispatch_started or provider_readback_final
        ):
            state = "partial"
        elif not operation.dispatch_started and self._v2_current_equals_before(
            operation, writes, before
        ):
            state = "not_applied"
        else:
            return {
                "state": "pending",
                "code": "pending_unknown",
                "next_action": "retry_same",
                "instruction": "Retry this exact request to force read-back; the stored operation is never reposted.",
                "operation_id": operation.operation_id,
            }
        raw_result_ids = operation.manifest.get("result_ids")
        item_ids = (
            [str(item_id) for item_id in raw_result_ids]
            if isinstance(raw_result_ids, list)
            else [_write_public_id(write) for write in writes]
        )
        response: JsonDict = {
            "state": state,
            "code": (
                "not_applied_precondition" if state == "not_applied" else state
            ),
            "next_action": "read_receipt",
            "instruction": (
                "Cloud read-back recorded every frozen write in the immutable receipt. "
                "Correct a partial outcome only with a fresh current-state request; "
                "the stored operation is never replayed."
                if state == "partial"
                else "Forced read-back proved that no frozen write landed; nothing was replayed."
                if state == "not_applied"
                else "Cloud read-back recorded the operation outcome."
            ),
            "operation_id": operation.operation_id,
            "item_ids": item_ids,
        }
        rows = self._v2_receipt_rows(operation, writes, before, state)
        settled = (
            session.settle(state=state, response=response, rows=rows)
            if session is not None
            else self._journal.settle_v2(
                operation.operation_id,
                expected="pending",
                state=state,
                response=response,
                rows=rows,
            )
        )
        return (
            response if settled else self._persisted_v2_outcome(operation.operation_id)
        )

    def _persisted_v2_outcome(self, operation_id: str) -> JsonDict:
        current = self._journal.get_v2_operation(operation_id)
        if current is not None and current.response is not None:
            return current.response
        return {
            "state": "pending",
            "code": "pending_unknown",
            "next_action": "retry_same",
            "instruction": "Retry this exact request to read the concurrent result; the stored operation is never reposted.",
            "operation_id": operation_id,
        }

    def _v2_preconditions_match(self, operation: V2Operation) -> bool:
        preconditions = cast(
            dict[str, object], operation.manifest.get("preconditions", {})
        )
        for item_id, expected in preconditions.items():
            if item_id == "scope:tags":
                if self._tag_revision() != expected:
                    return False
                continue
            if item_id.startswith("check:"):
                _parent, row = self._library._find_checklist(
                    item_id.removeprefix("check:")
                )
                if row is None or self._checklist_revision(row) != expected:
                    return False
                continue
            if item_id.startswith("scope:project:"):
                uuid = item_id.removeprefix("scope:project:")
                if self._project_scope_revision(uuid) != expected:
                    return False
                continue
            if item_id.startswith("scope:repeat:"):
                uuid = item_id.removeprefix("scope:repeat:")
                if self._recurrence_scope_revision(uuid) != expected:
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
            with self._journal.apply_session_v2(operation.operation_id) as session:
                return self._resume_v2_session(operation.operation_id, session)
        if operation.state == "awaiting_owner":
            return {
                "state": "awaiting_owner",
                "code": "awaiting_owner",
                "next_action": "run_cli",
                "instruction": (
                    "This immutable operation still awaits CLI-only owner review. "
                    "It does not block unrelated writes; do not replay it."
                ),
                "operation_id": operation.operation_id,
            }
        return {"state": operation.state, "instruction": "This immutable operation is unchanged.", "operation_id": operation.operation_id}

    def _resume_v2_session(
        self, operation_id: str, session: V2ApplySession | None
    ) -> JsonDict:
        if session is None:
            return self._persisted_v2_outcome(operation_id)
        operation = session.operation
        if not v2_manifest_is_valid(operation):
            return self._invalid_v2_manifest(operation.operation_id)
        failed = self._refresh(force=True)
        if failed is not None:
            return {"state": "pending", "code": "pending_unknown", "next_action": "retry_same", "instruction": "Retry this exact request to force read-back; the stored operation is never reposted.", "operation_id": operation.operation_id}
        writes = [
            _write_from_json(cast(dict[str, object], row))
            for row in cast(list[object], operation.manifest["writes"])
        ]
        before = cast(
            list[JsonDict | None],
            operation.manifest.get("before", [None] * len(writes)),
        )
        return self._reconcile_v2(
            operation, writes, before, session=session
        )

    def host_get_operation_v2(self, operation_id: str) -> V2Operation | None:
        """Return an operation only when it belongs to this workspace account."""

        try:
            return self._unambiguous_host_operation_v2(operation_id)
        except AmbiguousV2Request:
            return None

    def _unambiguous_host_operation_v2(
        self, operation_id: str
    ) -> V2Operation | None:
        operation = self._journal.get_v2_operation(operation_id)
        if (
            operation is None
            or not same_account_id(operation.account_id, self._account_id)
            or not v2_manifest_is_valid(operation)
        ):
            return None
        resolved = self._journal.get_v2_request(
            operation.account_id, operation.api_version, operation.request_id
        )
        if resolved is None or resolved.operation_id != operation.operation_id:
            raise AmbiguousV2Request()
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
        with self._journal.apply_session_v2(operation_id) as session:
            return self._resume_v2_session(operation_id, session)

    def host_settle_not_applied_v2(self, operation_id: str, authorization: object) -> JsonDict:
        """Settle pending only when forced evidence proves no frozen write landed."""

        try:
            operation = self._unambiguous_host_operation_v2(operation_id)
        except AmbiguousV2Request:
            operation = None
        if operation is None or operation.state != "pending":
            return self._missing_pending_v2_target()
        with self._journal.apply_session_v2(operation_id) as session:
            if session is None:
                return self._missing_pending_v2_target()
            try:
                guarded = self._unambiguous_host_operation_v2(operation_id)
            except AmbiguousV2Request:
                guarded = None
            if (
                guarded is None
                or guarded.state != "pending"
                or guarded.operation_id != session.operation.operation_id
            ):
                return self._missing_pending_v2_target()
            operation = session.operation
            if self._journal.verify_v2_authorization(operation, "settle_not_applied", authorization) is None:
                return {"state": "rejected", "code": "validation_error", "next_action": "run_cli", "instruction": "Verified CLI authorization is required.", "operation_id": operation_id}
            failed = self._refresh(force=True)
            if failed is not None:
                return {"state": "pending", "code": "pending_unknown", "next_action": "run_cli", "instruction": "Cloud evidence is unavailable.", "operation_id": operation_id}
            writes = [_write_from_json(cast(dict[str, object], row)) for row in cast(list[object], operation.manifest["writes"])]
            before = cast(list[JsonDict | None], operation.manifest.get("before", [None] * len(writes)))
            if any(self._writes_match([write]) for write in writes):
                return self._reconcile_v2(
                    operation, writes, before, session=session
                )
            if not self._v2_current_equals_before(operation, writes, before):
                return {"state": "pending", "code": "pending_unknown", "next_action": "run_cli", "instruction": "Current touched fields differ from both the frozen before and desired observations; nothing was replayed.", "operation_id": operation_id}
            response: JsonDict = {"state": "not_applied", "code": "not_applied_precondition", "next_action": "read_receipt", "instruction": "Forced read-back proved that no frozen write landed; nothing was replayed.", "operation_id": operation_id}
            rows = self._v2_receipt_rows(operation, writes, before, "not_applied")
            settled = session.settle(
                state="not_applied", response=response, rows=rows,
                authorization=authorization, action="settle_not_applied",
            )
            return response if settled else self._persisted_v2_outcome(operation_id)

    @staticmethod
    def _missing_pending_v2_target() -> JsonDict:
        return {
            "state": "rejected",
            "code": "missing_target",
            "next_action": "correct_request",
            "instruction": "That pending operation does not belong to this account.",
        }

    def _v2_current_equals_before(
        self,
        operation: V2Operation,
        writes: list[Write],
        before: list[JsonDict | None],
    ) -> bool:
        touched = cast(list[list[str]], operation.manifest["touched"])
        for index, write in enumerate(writes):
            observed = self._v2_observed_write(write, touched[index])
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
        try:
            operation = self._unambiguous_host_operation_v2(operation_id)
        except AmbiguousV2Request:
            return {
                "state": "rejected",
                "instruction": "Conflicting stored operations share this request_id.",
            }
        if operation is None:
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
        with self._journal.authorize_apply_session_v2(
            operation_id, authorization, now=self._clock
        ) as start:
            if start.authorized:
                return self._apply_v2_session(operation_id, start.session)
            if start.session is not None:
                return self._resume_v2_session(operation_id, start.session)
            current = self._journal.get_v2_operation(operation_id)
            if current is not None and current.state != "awaiting_owner":
                return self._resume_v2(current)
            return {"state": "rejected", "instruction": "Another unresolved operation blocks approval.", "operation_id": operation_id, "blocking_operation_ids": start.blockers}

    def host_decline_v2(self, operation_id: str, authorization: object) -> bool:
        try:
            operation = self._unambiguous_host_operation_v2(operation_id)
        except AmbiguousV2Request:
            return False
        return bool(
            operation is not None
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
        from .v2 import SAFETY_POLICY_DIGEST

        try:
            operation = self._unambiguous_host_operation_v2(operation_id)
        except AmbiguousV2Request:
            return False
        return bool(
            operation is not None
            and operation.safety_policy_digest != SAFETY_POLICY_DIGEST
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
            observed = self._v2_observed_write(write, touched[index - 1])
            result = outcome
            if outcome == "partial":
                result = "applied" if self._writes_match([write]) else "not_applied"
            desired = self._v2_desired(write, touched[index - 1])
            rows.append({"sequence": index, "action": write.action, "target_id": _write_public_id(write), "before": _taint_things_text(before[index - 1]), "desired": desired, "observed": _taint_things_text(observed), "result": result})
        return rows

    def _v2_rejected_receipt_rows(
        self,
        operation: V2Operation,
        writes: list[Write],
        before: list[JsonDict | None],
    ) -> list[JsonDict]:
        touched = cast(list[list[str]], operation.manifest["touched"])
        return [
            {
                "sequence": index,
                "action": write.action,
                "target_id": _write_public_id(write),
                "before": _taint_things_text(before[index - 1]),
                "desired": self._v2_desired(write, touched[index - 1]),
                "observed": None,
                "result": "not_applied",
                "proof": "provider_rejected",
            }
            for index, write in enumerate(writes, start=1)
        ]

    def _v2_desired(self, write: Write, fields: Sequence[str]) -> JsonDict:
        selected = set(fields)
        desired: JsonDict = {
            "action": write.action,
            "uuid": write.uuid,
            "kind": "heading" if write.heading else write.kind,
        }
        if "title" in selected:
            desired["title"] = write.title
        if "notes" in selected:
            desired["notes"] = write.notes
        if "status" in selected:
            desired["status"] = _public_status(write.status or "open")
        if "trashed" in selected:
            desired["trashed"] = write.action == "trash"
        if "start" in selected:
            desired["start"] = self._v2_write_start(write)
        if "deadline" in selected:
            desired["deadline"] = (
                None
                if write.clear_deadline or write.deadline is None
                else write.deadline.isoformat()
            )
        if "remind_at" in selected:
            desired["remind_at"] = (
                None if write.clear_remind else self._reminder_from_write(write)
            )
        if "into" in selected:
            desired["into_id"] = (
                f"{write.into_kind}:{write.into_uuid}"
                if write.into_kind is not None and write.into_uuid is not None
                else None
            )
        if "tags" in selected:
            desired["direct_tag_ids"] = [
                f"tag:{uuid}" for uuid in (write.tag_uuids or [])
            ]
        if "checklist" in selected:
            desired.update(
                {
                    "id": f"check:{write.uuid}",
                    "title": write.title,
                    "status": _public_status(write.checklist_status or "open"),
                    "parent_id": f"task:{write.checklist_parent_uuid}"
                    if write.checklist_parent_uuid
                    else None,
                    "order": write.checklist_index,
                }
            )
        if "recurrence" in selected:
            if write.action == "repeat":
                current = self._library.records.get(write.uuid)
                recurrence = (
                    current.recurrence
                    if write.recurrence_rule is None
                    and not write.clear_recurrence_rule
                    and current is not None
                    else RecurrenceState().fold_rule(write.recurrence_rule)
                ).fold_paused(write.recurrence_paused)
                desired["recurrence"] = self._v2_recurrence_value(
                    recurrence,
                    item=(
                        replace(current, recurrence=recurrence)
                        if current is not None
                        else None
                    ),
                )
            elif write.action == "repeat_link":
                desired["recurrence"] = self._v2_recurrence_from_write(write)
            elif write.action in {"repeat_progress", "repeat_next"}:
                current = self._library.records.get(write.uuid)
                desired["recurrence"] = (
                    self._v2_recurrence_value(current.recurrence, item=current)
                    if current is not None
                    else None
                )
            elif write.action == "create":
                desired["recurrence"] = self._v2_recurrence_from_write(write)
            else:
                desired["recurrence"] = None
        if write.action == "checklist" and write.checklist_remove:
            desired["exists"] = False
        return desired

    def _v2_observed_write(
        self, write: Write, fields: Sequence[str]
    ) -> JsonDict | None:
        if write.action == "checklist":
            parent, row = self._library._find_checklist(write.uuid)
            if parent is None or row is None:
                return None
            return {
                "id": f"check:{row.uuid}",
                "title": row.title,
                "status": _public_status(row.status),
                "parent_id": parent.id,
                "order": row.sort_index,
            }
        item = self._library.records.get(write.uuid)
        return self._v2_observed(item, fields) if item is not None else None

    def _v2_recurrence_from_write(self, write: Write) -> JsonDict | None:
        recurrence = RecurrenceState()
        if write.recurrence_rule is not None:
            recurrence = recurrence.fold_rule(write.recurrence_rule).fold_paused(
                write.recurrence_paused
            )
        elif write.recurrence_links:
            recurrence = recurrence.fold_links(write.recurrence_links)
        else:
            return None
        projected = Record(
            uuid=write.uuid,
            kind=write.kind,
            title=write.title or "",
            recurrence=recurrence,
            recurrence_created_through=write.recurrence_created_through,
            recurrence_instance_count=write.recurrence_instance_count or 0,
            recurrence_completed_on=write.recurrence_completed_on,
            recurrence_next_on=write.recurrence_next_on,
        )
        return self._v2_recurrence_value(recurrence, item=projected)

    @staticmethod
    def _v2_write_start(write: Write) -> str | None:
        if write.public_start_anytime:
            return "anytime"
        if write.clear_start:
            return None
        if write.tonight:
            return "evening"
        if write.someday:
            return "someday"
        return write.start.isoformat() if write.start is not None else None

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
            values["status"] = _public_status(item.status)
        if "trashed" in selected:
            values["trashed"] = item.trashed
        if "start" in selected:
            values["start"] = _public_start(item)
        if "deadline" in selected:
            values["deadline"] = item.deadline.isoformat() if item.deadline else None
        if "remind_at" in selected:
            values["remind_at"] = self._reminder(item)
        if "into" in selected:
            values["into_id"] = (
                f"project:{item.parent_uuid}"
                if item.parent_uuid
                else f"area:{item.area_uuid}"
                if item.area_uuid
                else None
            )
        if "tags" in selected:
            values["direct_tag_ids"] = [
                f"tag:{uuid}" for uuid in item.tag_uuids if uuid in self._library.tags
            ]
        if "recurrence" in selected:
            rt2 = _rt2_fact(item)
            values["recurrence"] = (
                rt2.model_dump()
                if rt2 is not None
                else self._v2_recurrence_value(item.recurrence, item=item)
            )
        return values

    def _v2_recurrence_value(
        self, recurrence: RecurrenceState, *, item: Record | None = None
    ) -> JsonDict | None:
        if recurrence.role == "none":
            return None
        resolved = recurrence
        bookkeeping = item
        template_kind: PublicKind = item.public_kind if item is not None else "task"
        if recurrence.role == "instance" and recurrence.template_uuid is not None:
            template = self._library.records.get(recurrence.template_uuid)
            if template is not None and template.recurrence.role == "template":
                resolved = template.recurrence
                bookkeeping = template
                template_kind = template.public_kind
        kind = (
            "template"
            if recurrence.role == "template"
            else "fixed_instance"
            if resolved.repeat_type == "fixed"
            else "after_completion_instance"
            if resolved.repeat_type == "after_completion"
            else "unknown"
        )
        until = _repeat_end(resolved)
        return {
            "kind": kind,
            "template_id": (
                f"{template_kind}:{recurrence.template_uuid}"
                if recurrence.template_uuid is not None
                else None
            ),
            "mode": (
                resolved.repeat_type
                if resolved.repeat_type in {"fixed", "after_completion"}
                else None
            ),
            "unit": resolved.unit,
            "interval": resolved.interval,
            "weekdays": [
                _WEEKDAY_NAMES[code]
                for code in resolved.weekday_codes
                if code in _WEEKDAY_NAMES
            ],
            "paused": (
                resolved.paused
                if bookkeeping is not None and bookkeeping.recurrence_paused_known
                else None
            ),
            "created_through": (
                bookkeeping.recurrence_created_through.isoformat()
                if bookkeeping is not None
                and bookkeeping.recurrence_created_through is not None
                else None
            ),
            "generated_count": (
                bookkeeping.recurrence_instance_count
                if bookkeeping is not None
                and bookkeeping.recurrence_instance_count_known
                else None
            ),
            "completed_on": (
                bookkeeping.recurrence_completed_on.isoformat()
                if bookkeeping is not None
                and bookkeeping.recurrence_completed_on is not None
                else None
            ),
            "next_on": (
                bookkeeping.recurrence_next_on.isoformat()
                if bookkeeping is not None
                and bookkeeping.recurrence_next_on is not None
                else None
            ),
            "on": [value.model_dump() for value in _public_repeat_on(resolved)],
            "until": until,
        }

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

    def _view_items(self, call: ReadCall) -> list[Record] | Result:
        view = call.view or "today"
        today = self._clock().date()
        if view == "today":
            return self._library.today(today=today)
        if view == "inbox":
            return self._library.inbox(limit=10_000)
        if view == "week":
            return self._library.week(today=today, limit=10_000)
        if view == "repeating":
            return sorted(
                [
                    item
                    for item in self._library.records.values()
                    if (
                        item.recurrence.role == "template"
                        or item.repeater is not None
                    )
                    and not item.trashed
                ],
                key=lambda item: (
                    item.start or date.max,
                    item.sort_index,
                    item.title,
                ),
            )
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
        return self._follow_cursor(result)

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
            return self._diagnostics_page(
                limit,
                offset=saved.offset,
                expected_ids=saved.ids,
                expected_digest=saved.snapshot_revision,
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
        return self._follow_cursor(result)

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
        compact_inherited_tag_ids: list[str] = []
        if full and want_tags:
            compact_direct_tag_ids = [tag.id for tag in direct_tags]
            compact_inherited_tag_ids = [tag.id for tag in inherited_tags]
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
        recurrence = _rt2_fact(item)
        if recurrence is None and recurrence_kind != "none":
            rule = item.recurrence
            bookkeeping = item
            template_id: str | None = None
            if item.recurrence.role == "instance":
                template_record = self._library.records.get(
                    template_uuid_of(item) or ""
                )
                if template_record is not None and template_record.recurrence.rule:
                    rule = template_record.recurrence
                    bookkeeping = template_record
                    template_id = template_record.id
            until = _repeat_end(rule)
            recurrence = RecurrenceFact(
                kind=recurrence_kind,
                template_id=template_id,
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
                paused=(
                    rule.paused if bookkeeping.recurrence_paused_known else None
                ),
                created_through=(
                    bookkeeping.recurrence_created_through.isoformat()
                    if bookkeeping.recurrence_created_through
                    else None
                ),
                generated_count=(
                    bookkeeping.recurrence_instance_count
                    if bookkeeping.recurrence_instance_count_known
                    else None
                ),
                completed_on=(
                    bookkeeping.recurrence_completed_on.isoformat()
                    if bookkeeping.recurrence_completed_on
                    else None
                ),
                next_on=(
                    bookkeeping.recurrence_next_on.isoformat()
                    if bookkeeping.recurrence_next_on
                    else None
                ),
                on=_public_repeat_on(rule),
                until=until,
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
            inherited_tag_ids=compact_inherited_tag_ids,
            start="evening"
            if item.tonight
            else "someday"
            if item.someday
            else item.start.isoformat()
            if item.start
            else None,
            deadline=item.deadline.isoformat() if item.deadline else None,
            remind_at=self._reminder(item),
            recurrence=recurrence,
            order=_bounded_order(item.sort_index) if full else None,
            today_order=(
                _bounded_order(item.today_index)
                if self._is_today_member(
                    start=item.start,
                    deadline=item.deadline,
                    tonight=item.tonight,
                    today=self._clock().date(),
                )
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

    def _start(self, value: str | None) -> tuple[date | None, bool, bool]:
        if value is None or value == "anytime":
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

    @staticmethod
    def _is_today_member(
        *,
        start: date | None,
        deadline: date | None,
        tonight: bool,
        today: date,
    ) -> bool:
        return (
            tonight
            or (start is not None and start <= today)
            or (deadline is not None and deadline <= today)
        )

    def _writes_from_plan(self, plan: JsonDict) -> list[Write]:
        raw = cast(list[object], plan.get("writes", []))
        return [_write_from_json(cast(dict[str, object], value)) for value in raw]

    def _writes_match(self, writes: list[Write]) -> bool:
        return self._library.matches(writes)

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
            "repeater": item.repeater,
            "recurrence_created_through": (
                item.recurrence_created_through.isoformat()
                if item.recurrence_created_through
                else None
            ),
            "recurrence_instance_count": item.recurrence_instance_count,
            "recurrence_instance_count_known": (
                item.recurrence_instance_count_known
            ),
            "recurrence_paused_known": item.recurrence_paused_known,
            "recurrence_completed_on": (
                item.recurrence_completed_on.isoformat()
                if item.recurrence_completed_on
                else None
            ),
            "recurrence_next_on": (
                item.recurrence_next_on.isoformat()
                if item.recurrence_next_on
                else None
            ),
            "recurrence_generated_on": (
                item.recurrence_generated_on.isoformat()
                if item.recurrence_generated_on
                else None
            ),
            "recurrence_generated_on_known": (
                item.recurrence_generated_on_known
            ),
            "leavable": item.leavable,
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
        unique = {item.id: item for item in items}
        return "s_" + _digest(
            [
                [item_id, self._revision(item)]
                for item_id, item in sorted(unique.items())
            ]
        )

    def _workspace_revision(self) -> str:
        items = sorted(self._library.records.values(), key=lambda item: item.id)
        return self._scope_revision(items)

    def _tag_revision(self) -> str:
        rows = [
            [uuid, title, *self._library.tag_parents.get(uuid, [])]
            for uuid, title in sorted(self._library.tags.items())
        ]
        return "s_" + _digest(rows)

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
            elif item.heading_uuid is not None:
                # Native Things stores a task placed under a heading through
                # ``agr`` alone; it does not duplicate the Project in ``pr``.
                by_parent.setdefault(item.heading_uuid, []).append(item)
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

    def _clone_project_graph_writes(
        self,
        source_project_uuid: str,
        destination_project_uuid: str,
        *,
        leavable: bool,
    ) -> list[Write]:
        """Clone one native Project descendant graph with fresh identities."""
        all_descendants = self._project_descendants(source_project_uuid)
        by_uuid = {item.uuid: item for item in all_descendants}

        def cloneable(item: Record) -> bool:
            current = item
            seen = {item.uuid}
            while True:
                if current.trashed:
                    return False
                parent_uuid = current.parent_uuid or current.heading_uuid
                if parent_uuid == source_project_uuid:
                    return True
                if parent_uuid is None or parent_uuid in seen:
                    return False
                parent = by_uuid.get(parent_uuid)
                if parent is None:
                    return False
                seen.add(parent_uuid)
                current = parent

        descendants = [item for item in all_descendants if cloneable(item)]
        if any(item.kind == "project" for item in descendants):
            raise ValueError(
                "A repeating Project cannot contain another Project."
            )
        if any(
            item.recurrence.role != "none" or item.repeater is not None
            for item in descendants
        ):
            raise ValueError(
                "A repeating Project cannot contain another repeating item."
            )
        headings = sorted(
            (item for item in descendants if item.heading),
            key=lambda item: (item.sort_index, item.uuid),
        )
        tasks = sorted(
            (item for item in descendants if item.kind == "task" and not item.heading),
            key=lambda item: (item.sort_index, item.uuid),
        )
        uuid_map = {item.uuid: new_uuid() for item in [*headings, *tasks]}
        writes: list[Write] = []
        for heading in headings:
            writes.append(
                Write(
                    action="create_heading",
                    uuid=uuid_map[heading.uuid],
                    kind="task",
                    title=heading.title,
                    into_uuid=destination_project_uuid,
                    into_kind="project",
                    status="open",
                    anytime=True,
                    sort_index=heading.sort_index,
                    leavable=leavable,
                )
            )
        for task in tasks:
            mapped_heading = (
                uuid_map.get(task.heading_uuid) if task.heading_uuid else None
            )
            writes.append(
                Write(
                    action="create",
                    uuid=uuid_map[task.uuid],
                    kind="task",
                    title=task.title,
                    notes=task.notes,
                    status="open",
                    into_uuid=None if mapped_heading else destination_project_uuid,
                    into_kind=None if mapped_heading else "project",
                    start=task.start,
                    deadline=task.deadline,
                    remind=task.remind,
                    tonight=task.tonight,
                    someday=task.someday,
                    anytime=task.start is None and not task.someday,
                    tag_uuids=list(task.tag_uuids),
                    heading_uuid=mapped_heading,
                    sort_index=task.sort_index,
                    today_index=task.today_index,
                    owner_today=self._clock().date(),
                    leavable=leavable,
                )
            )
        for task in tasks:
            parent_uuid = uuid_map[task.uuid]
            for row in sorted(
                task.checklists, key=lambda item: (item.sort_index, item.uuid)
            ):
                writes.append(
                    Write(
                        action="checklist",
                        uuid=new_uuid(),
                        title=row.title,
                        checklist_parent_uuid=parent_uuid,
                        checklist_status="open",
                        checklist_index=row.sort_index,
                    )
                )
        return writes

    def _repeat_stop_plan(self, template: Record) -> _RepeatStopPlan:
        template.recurrence.validate_interval_template(kind=template.kind)
        if template.recurrence_next_on is None:
            raise ValueError(
                "The repeat template has no native next date; read the series again"
            )
        replacement = Write(
            action="create",
            uuid=new_uuid(),
            kind=template.kind,
            title=template.title,
            notes=template.notes,
            status="open",
            into_uuid=(
                None
                if template.heading_uuid
                else template.parent_uuid or template.area_uuid
            ),
            into_kind=(
                None
                if template.heading_uuid
                else "project"
                if template.parent_uuid
                else "area"
                if template.area_uuid
                else None
            ),
            start=template.recurrence_next_on,
            deadline=template.deadline,
            remind=template.remind,
            tag_uuids=list(template.tag_uuids),
            heading_uuid=template.heading_uuid,
            sort_index=template.sort_index,
            today_index=template.today_index,
            owner_today=self._clock().date(),
            leavable=True,
        )
        writes: list[Write] = []
        preconditions = {
            template.id: self._revision(template),
            f"scope:repeat:{template.uuid}": self._recurrence_scope_revision(
                template.uuid
            ),
        }
        for current in self._library.recurrence_instances(template.uuid):
            preconditions[current.id] = self._revision(current)
            writes.append(
                Write(
                    action="repeat_link",
                    uuid=current.uuid,
                    kind=current.kind,
                    recurrence_links=[],
                )
            )
        writes.append(replacement)
        if template.kind == "project":
            preconditions[f"scope:project:{template.uuid}"] = (
                self._project_scope_revision(template.uuid)
            )
            writes.extend(
                self._clone_project_graph_writes(
                    template.uuid,
                    replacement.uuid,
                    leavable=True,
                )
            )
            for descendant in self._project_descendants(template.uuid):
                preconditions[descendant.id] = self._revision(descendant)
                for row in sorted(
                    descendant.checklists,
                    key=lambda item: (item.sort_index, item.uuid),
                ):
                    writes.append(
                        Write(
                            action="checklist",
                            uuid=row.uuid,
                            title=row.title,
                            checklist_parent_uuid=descendant.uuid,
                            checklist_remove=True,
                        )
                    )
                writes.append(
                    Write(
                        action="permanent_delete",
                        uuid=descendant.uuid,
                        kind=descendant.kind,
                        heading=descendant.heading,
                    )
                )
        else:
            for row in sorted(
                template.checklists,
                key=lambda item: (item.sort_index, item.uuid),
            ):
                writes.append(
                    Write(
                        action="checklist",
                        uuid=new_uuid(),
                        title=row.title,
                        checklist_parent_uuid=replacement.uuid,
                        checklist_status="open",
                        checklist_index=row.sort_index,
                    )
                )
        for row in sorted(
            template.checklists,
            key=lambda item: (item.sort_index, item.uuid),
        ):
            writes.append(
                Write(
                    action="checklist",
                    uuid=row.uuid,
                    title=row.title,
                    checklist_parent_uuid=template.uuid,
                    checklist_remove=True,
                )
            )
        writes.append(
            Write(
                action="permanent_delete",
                uuid=template.uuid,
                kind=template.kind,
            )
        )
        return _RepeatStopPlan(
            replacement=replacement,
            writes=writes,
            preconditions=preconditions,
        )

    def _recurrence_scope_revision(self, uuid: str) -> str:
        items: list[Record] = []
        template = self._library.records.get(uuid)
        if template is not None:
            items.append(template)
            if template.kind == "project":
                items.extend(self._project_descendants(template.uuid))
        current = self._library.recurrence_instances(uuid)
        items.extend(current)
        for instance in current:
            if instance.kind == "project":
                items.extend(self._project_descendants(instance.uuid))
        return self._scope_revision(items)

    def _recurrence_relationship_is_valid(self, target: Record) -> bool:
        """Check the native one-way link before exposing repeat mutation facts."""
        recurrence = target.recurrence
        if recurrence.role == "none":
            return not self._library.recurrence_instances(target.uuid)
        if recurrence.role == "template":
            return all(
                candidate.recurrence.role == "instance"
                and candidate.kind == target.kind
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
            and template.kind == target.kind
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
    def _stale(instruction: str) -> Result:
        return Result(next="read", status="stale", instruction=instruction)

    @staticmethod
    def _rejected(instruction: str) -> Result:
        return Result(next="stop", status="rejected", instruction=instruction)

    @staticmethod
    def _unsupported(instruction: str) -> Result:
        return Result(next="stop", status="unsupported", instruction=instruction)


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


def _public_start(item: Record) -> str | None:
    if item.tonight:
        return "evening"
    if item.someday:
        return "someday"
    if item.start is not None:
        return item.start.isoformat()
    if item.kind in {"task", "project"} and not item.heading and not item.inbox:
        return "anytime"
    return None


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


def _write_public_id(write: Write) -> str:
    if write.action == "checklist":
        return f"check:{write.uuid}"
    kind: PublicKind = "heading" if write.heading else write.kind
    return public_id(kind, write.uuid)


def _write_json(write: Write) -> JsonDict:
    payload = cast(JsonDict, asdict(write))
    for name in (
        "start",
        "deadline",
        "owner_today",
        "recurrence_created_through",
        "recurrence_completed_on",
        "recurrence_next_on",
    ):
        value = payload.get(name)
        if isinstance(value, date):
            payload[name] = value.isoformat()
    return payload


def _write_from_json(payload: dict[str, object]) -> Write:
    allowed = {field.name for field in fields(Write)}
    values = {key: value for key, value in payload.items() if key in allowed}
    for name in (
        "start",
        "deadline",
        "owner_today",
        "recurrence_created_through",
        "recurrence_completed_on",
        "recurrence_next_on",
    ):
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
        if (
            action == "repeat"
            and not write.recurrence_rule
            and not write.clear_recurrence_rule
            and write.recurrence_paused is None
        ):
            return False
        if action == "repeat_link" and write.recurrence_links is None:
            return False
        if action == "repeat_progress" and (
            write.recurrence_completed_on is None
            or write.recurrence_next_on is None
        ):
            return False
        if action == "repeat_next" and write.recurrence_instance_count is None:
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
    "repeat", "repeat_link", "repeat_progress", "repeat_next",
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


def _diagnostics_instruction(diagnostics: list[DiagnosticFact]) -> str:
    if not diagnostics:
        return "No native-state conflicts are visible."
    instruction = (
        "These records have native-state conflicts. Use diagnostics and repairs."
    )
    if any("test_residue" in row.conflicts for row in diagnostics):
        instruction += " Trash test_residue with this context and short refs."
    return instruction
