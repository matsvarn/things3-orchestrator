"""One deep workspace Module behind the three model tools."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime, time, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from typing import Callable, Literal, cast

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
    DiagnosticFact,
    DiagnosticRepair,
    ItemFact,
    LayoutFact,
    LayoutSectionFact,
    PlanFact,
    ReadCall,
    RecoveryFact,
    RecurrenceFact,
    RecurrenceKind,
    Result,
    ResultStatus,
    ReviewSection,
    TagFact,
    TruncatedField,
    Weekday,
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
    template_uuid_of,
)
from .recurrence import RepeatMode, new_rule

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
_CHANGE_FIND_LIMIT = 40
_NOTES_LIMIT = 50_000
_TITLE_LIMIT = 1000
_ORDER_MIN = -(2**63)
_ORDER_MAX = 2**63 - 1
_PLAN_MINUTES = 30
_PENDING_RETRY_LIMIT = 3
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


@dataclass(frozen=True)
class _NormalizedSearchText:
    """Keep the old substring search and provide exact token fallback."""

    folded: str
    tokens: tuple[str, ...]


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


@dataclass
class _PreparationContext:
    """Mutable planning state shared by cohesive preparation branches."""

    local: dict[str, tuple[str, Kind | str]]
    writes: list[Write]
    preconditions: dict[str, str]
    summary: list[str]
    warnings: list[str]
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
class _ItemCursor:
    ids: list[str]
    offset: int
    snapshot_revision: str
    public_scope_revision: str
    full: bool
    view: str | None
    detail: tuple[str, ...]
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
        context_store: ContextStore | None = None,
        account_id: str | None = None,
    ) -> None:
        self._library = library
        self._journal = journal or MemoryJournal()
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._context_store = context_store or MemoryContextStore(
            clock=self._clock,
            token_factory=lambda: token_urlsafe(18),
        )
        self._account_id = account_id or f"workspace:{token_urlsafe(18)}"
        self._contextual_compiler = ContextualCommitCompiler()
        self._cursors: dict[str, _ItemCursor] = {}
        self._tag_cursors: dict[str, _TagCursor] = {}
        self._detail_cursors: dict[str, _DetailCursor] = {}

    def read(self, call: ReadCall) -> Result:
        failed = self._refresh()
        if failed is not None:
            return failed
        if call.cursor is not None:
            return self._continue(call.cursor, call.limit)

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
            return self._detail_page(
                item,
                row_offset=0,
                note_offset=0,
                limit=call.limit,
            )

        if call.find is not None:
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
                    instruction="Use an exact ID for a change.",
                )
            closed = [
                item
                for item in self._search(call.find, within, closed=True)
                if item.trashed or item.status != "open"
            ]
            if closed:
                return self._page(
                    closed,
                    call.limit,
                    full=False,
                    instruction=(
                        "These matches are not active. "
                        "Use purpose=change to restore, or view=trash."
                    ),
                )
            return self._page(
                matches,
                call.limit,
                full=False,
                instruction="Use an exact ID for a change.",
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
        visible = self._view_items(call)
        if isinstance(visible, Result):
            return visible
        if view == "audit" and call.signals_any:
            wanted = set(call.signals_any)
            visible = [
                item
                for item in visible
                if wanted.intersection(
                    self._item_signals(
                        item,
                        checklist_truncated=False,
                        tags_truncated=False,
                        notes_truncated=False,
                    )
                )
            ]
        instruction = "Use this review as current evidence."
        if view == "audit":
            instruction = (
                "This audit lists each active item once. Continue the cursor for the rest."
            )
        elif view == "area":
            instruction = (
                "This Area, its loose tasks, and its Projects. "
                "Read a Project for its children."
            )
        return self._page(
            visible,
            call.limit,
            full=False,
            instruction=instruction,
            view=view,
            public_scope=(self._area_scope_revision() if view == "system" else None),
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
        items = [
            self._fact(item, full=False)
            for row in page
            if (item := self._exact_item(row.item_id)) is not None
        ]
        return self._follow_cursor(
            Result(
                next="done",
                status="ok",
                instruction=(
                    "These records have native-state conflicts. "
                    "Use diagnostics and repairs."
                    if diagnostics
                    else "No native-state conflicts are visible."
                ),
                items=items,
                diagnostics=diagnostics,
                cursor=cursor,
                truncated=cursor is not None,
            )
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
        neighborhood = self._neighborhood_collect(target)
        self._neighborhood_include(neighborhood, call)
        if len(neighborhood.records) > _CONTEXT_LIMIT:
            return self._oversized_context(call, len(neighborhood.records))
        refs, by_id = self._context_refs(neighborhood.records)
        context = self._create_context(
            call,
            refs,
            scope=f"change:{target.id}",
        )
        facts = [
            self._fact(
                record,
                full=record.uuid == target.uuid,
                include_revision=record.uuid in neighborhood.placement_ids,
            ).model_copy(update={"ref": by_id[record.id]})
            for record in neighborhood.records
        ]
        instruction = (
            "Use context_id and short refs for one coherent change. "
            "Omitted item fields remain unchanged. Include a destination to move."
        )
        if neighborhood.include_note:
            instruction = f"{instruction} {neighborhood.include_note}"
        return Result(
            next="done",
            status="ok",
            instruction=instruction,
            items=facts,
            signals=neighborhood.include_signals,
            context=self._public_context(context),
            scope_revision=(
                self._area_scope_revision()
                if target.kind == "area"
                else self._detail_revision(target)
            ),
            missing_ids=neighborhood.missing_ids,
        )

    def _neighborhood_collect(self, target: Record) -> _Neighborhood:
        """Collect the local neighborhood for one change target."""
        neighborhood = _Neighborhood()

        def place(item: Record) -> None:
            neighborhood.add(self._library.records.get(item.parent_uuid or ""))
            neighborhood.add(self._library.records.get(item.area_uuid or ""))
            neighborhood.add(self._library.records.get(item.heading_uuid or ""))

        neighborhood.add(target)
        place(target)
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
            if (
                project is None
                or project.kind != "project"
                or not project.is_open()
            ):
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
                        return self._organize_unavailable(closed_projects[0])
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
            if (
                project is None
                or project.kind != "project"
                or not project.is_open()
            ):
                return self._organize_unavailable(project)
        else:
            raise AssertionError("organize selector must identify a Project")
        assert project is not None
        source_records = self._library.project(project.id)
        neighborhood = _Neighborhood()
        for record in source_records:
            neighborhood.add(record)
        self._neighborhood_include(neighborhood, call)
        if len(neighborhood.records) > _CONTEXT_LIMIT:
            return self._oversized_context(call, len(neighborhood.records))
        refs, by_id = self._context_refs(neighborhood.records)
        context = self._create_context(call, refs, scope=project.id)
        facts = [
            self._fact(
                record,
                full=False,
                include_revision=record.uuid in neighborhood.placement_ids,
            ).model_copy(update={"ref": by_id[record.id]})
            for record in neighborhood.records
        ]
        instruction = (
            "Use one organize draft with this context. Listed work can move; "
            "unlisted work stays unchanged. Name new headings from the groups "
            "the tasks already form. Include a destination Project to merge."
        )
        if neighborhood.include_note:
            instruction = f"{instruction} {neighborhood.include_note}"
        return Result(
            next="done",
            status="ok",
            instruction=instruction,
            items=facts,
            layouts=[self._project_layout(project, source_records, by_id)],
            signals=neighborhood.include_signals,
            context=self._public_context(context),
            scope_revision=self._project_scope_revision(project.uuid),
            missing_ids=neighborhood.missing_ids,
        )

    def _organize_unavailable(self, project: Record | None) -> Result:
        """Point organize recovery at a live look, never the same dead selector."""
        if project is not None and project.kind == "project" and project.trashed:
            return Result(
                next="read",
                status="needs_input",
                instruction=(
                    "This Project is in Trash. "
                    "Restore it with purpose=change, or read view=trash."
                ),
                items=[self._fact(project, full=False, include_revision=False)],
                recovery=RecoveryFact(
                    code="context_required",
                    retry="read",
                    read={"purpose": "change", "id": project.id},
                ),
            )
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
        self, records: list[Record]
    ) -> tuple[list[ContextRef], dict[str, str]]:
        counters = {"task": 0, "project": 0, "area": 0, "heading": 0}
        prefixes = {"task": "t", "project": "p", "area": "a", "heading": "h"}
        refs: list[ContextRef] = []
        by_id: dict[str, str] = {}
        for record in records:
            kind = record.public_kind
            counters[kind] += 1
            short = f"{prefixes[kind]}{counters[kind]}"
            refs.append(
                ContextRef(
                    ref=short,
                    exact_id=record.id,
                    revision=self._revision(record),
                )
            )
            by_id[record.id] = short
        return refs, by_id

    def _create_context(
        self, call: ReadCall, refs: list[ContextRef], *, scope: str
    ) -> ReadContext:
        return self._context_store.create(
            account_id=self._account_id,
            selector=ReadSelector(
                purpose=call.purpose,
                view=call.view,
                item_id=call.id,
                find=call.find,
                within=call.within,
                from_date=call.from_date,
                to_date=call.to_date,
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
            completeness=(
                CompletenessFact(
                    scope=scope,
                    seen=len(refs),
                    total=len(refs),
                    complete=True,
                ),
            ),
        )

    @staticmethod
    def _public_context(context: ReadContext) -> ContextFact:
        return ContextFact(
            id=context.id,
            purpose=context.selector.purpose,
            expires_at=context.expires_at.isoformat(),
            complete=context.complete,
        )

    @staticmethod
    def _project_layout(
        project: Record, records: list[Record], by_id: dict[str, str]
    ) -> LayoutFact:
        headings = sorted(
            [record for record in records if record.heading],
            key=lambda item: (item.sort_index, item.uuid),
        )
        tasks = [
            record for record in records if record.kind == "task" and not record.heading
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
                ],
            )
            for heading in headings
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
        if call.organize or context.is_complete("system"):
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
            assert call.within is not None
            area = self._exact_item(call.within)
            if area is None or area.kind != "area":
                return self._needs_input("I could not find that exact Area.")
            return self._library.area(area.id)
        if view == "audit":
            return self._library.audit()
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

    def _page(
        self,
        items: list[Record],
        limit: int,
        *,
        full: bool,
        instruction: str,
        view: str | None = None,
        public_scope: str | None = None,
        result_signals: list[str] | None = None,
        extra_truncated: bool = False,
        missing_ids: list[str] | None = None,
        detail: tuple[str, ...] = DETAIL_FIELDS,
    ) -> Result:
        limit = min(limit, _READ_LIMIT)
        facts = [
            self._fact(item, full=full, detail=detail) for item in items[:limit]
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
        }.get(view or "", "No matching work is visible. Search with find and one title token.")
        visible = bool(facts or result_signals)
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
            return self._tag_page(tag_saved.rows, offset=tag_saved.offset, limit=limit)
        saved = self._cursors.get(cursor)
        if saved is None:
            return self._stale("That cursor is invalid. Start the read again.")
        if saved.expires_at <= self._clock():
            return self._stale("That cursor expired. Start the read again.")
        if saved.view == "diagnostics":
            return self._diagnostics_page(
                limit,
                offset=saved.offset,
                expected_ids=saved.ids,
                expected_digest=saved.snapshot_revision,
            )
        items = [
            item for value in saved.ids if (item := self._exact_item(value)) is not None
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
                saved.detail,
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

    def _encode_cursor(
        self,
        ids: list[str],
        offset: int,
        snapshot_revision: str,
        public_scope_revision: str,
        full: bool,
        view: str | None,
        detail: tuple[str, ...] = (),
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
                instruction="Send this scope_revision as tags_revision with change_tags. Use exact tag IDs.",
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
            "area": "Area",
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
                item_ids=[item.id for item in items],
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
        view: str | None,
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
        rule = item.recurrence
        if item.recurrence.role == "instance":
            template_record = self._library.records.get(template_uuid_of(item) or "")
            if template_record is not None and template_record.recurrence.rule:
                rule = template_record.recurrence
        recurrence = RecurrenceFact(
            kind=self._recurrence_kind(item),
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
        return ItemFact(
            id=item.id,
            revision=self._revision(item) if include_revision else None,
            kind=item.public_kind,
            title=_bounded_title(item.title),
            status=_public_status(item.status),
            into_id=(
                f"project:{item.parent_uuid}"
                if item.parent_uuid
                else f"area:{item.area_uuid}"
                if item.area_uuid
                else None
            ),
            heading_id=(f"heading:{item.heading_uuid}" if item.heading_uuid else None),
            notes_markdown=(
                item.notes[note_offset : note_offset + _NOTES_LIMIT]
                if full and include_notes and want_notes
                else None
            ),
            checklist=checklist,
            direct_tags=direct_tags,
            inherited_tags=inherited_tags,
            start=item.start.isoformat()
            if item.start
            else "someday"
            if item.someday
            else None,
            deadline=item.deadline.isoformat() if item.deadline else None,
            remind_at=self._reminder(item),
            recurrence=recurrence,
            order=_bounded_order(item.sort_index),
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
        if waiting and waiting in item.tag_uuids:
            ordinary.append("waiting")
        if item.someday:
            ordinary.append("someday")
        if item.trashed:
            ordinary.append("trashed")
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
        context = self._preparation_context(
            call, contextual_commit=contextual_commit
        )
        self._prepare_tag_registry(call, context)
        self._prepare_items(call, context)
        self._finish_preparation(call, context)
        return context.result()

    def _prepare_items(self, call: CommitCall, context: _PreparationContext) -> None:
        # Pre-index heading moves. A Task that follows its heading in the same
        # merge must retain that heading UUID after both records enter the
        # destination Project.
        for change in call.change:
            if change.id is None or "into" not in change.model_fields_set:
                continue
            item = self._library.records.get(parse_id(change.id)[1])
            if item is None or not item.heading:
                continue
            context.project_heading_moves[item.uuid] = (
                self._require_heading_destination(item, change, call, context)
            )
        self._prepare_creates(call, context)
        self._prepare_changes(call, context)

    def _prepare_creates(self, call: CommitCall, context: _PreparationContext) -> None:
        """Plan all create entries, including generated copies and children."""
        local = context.local
        writes = context.writes
        preconditions = context.preconditions
        summary = context.summary
        warnings = context.warnings
        for entry in call.create:
            uuid = local[entry.key][0] if entry.key else new_uuid()
            if entry.kind == "task":
                twins = [
                    item
                    for item in self._library.records.values()
                    if item.kind == "task"
                    and not item.heading
                    and item.is_open()
                    and item.title.casefold() == entry.title.casefold()
                ]
                if len(twins) == 1:
                    raise _Abort(
                        self._needs_input(
                            f"{twins[0].title} already exists. Change that Task. "
                            "If this is a reminder, ask for the clock time."
                        )
                    )
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
            home = self._home(entry.into, entry.kind, local, new_item=True)
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
            for row_index, title in enumerate(entry.checklist):
                if repeat_template_uuid is not None:
                    writes.append(
                        Write(
                            action="checklist",
                            uuid=new_uuid(),
                            title=title,
                            checklist_parent_uuid=repeat_template_uuid,
                            checklist_status="open",
                            checklist_index=row_index * 1024,
                        )
                    )
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
            summary.append(
                f"Create repeating {entry.kind}: {entry.title}"
                if entry.repeat is not None
                else f"Create {entry.kind}: {entry.title}"
            )
            if entry.next_actions:
                summary.append(
                    f"Add {len(entry.next_actions)} next actions to {entry.title}"
                )
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
                writes.append(
                    Write(
                        action="permanent_delete",
                        uuid=target.uuid,
                        kind="task",
                    )
                )
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
            self._home(change.into, item.kind, local, new_item=False)
            if "into" in change.model_fields_set
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
        placement_clears_schedule = "into" in change.model_fields_set and (
            home[2] or home[3]
        )
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

        tags = list(item.tag_uuids)
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
            if "into" in change.model_fields_set:
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
                for child in assigned:
                    child_home = self._record_home(child)
                    preconditions[child.id] = self._revision(child)
                    writes.append(
                        Write(
                            action="update",
                            uuid=child.uuid,
                            kind=child.kind,
                            into_uuid=child_home[0],
                            into_kind=child_home[1],
                            inbox=child_home[2],
                            anytime=child_home[3],
                            clear_heading=True,
                        )
                    )
                writes.append(
                    Write(action="permanent_delete", uuid=item.uuid, kind="task")
                )
                summary.append(f"Permanently delete heading: {item.title}")
                if assigned:
                    summary.append(
                        f"Clear the heading from {len(assigned)} assigned Tasks"
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
                    writes.append(
                        Write(
                            action="permanent_delete",
                            uuid=descendant.uuid,
                            kind=descendant.kind,
                        )
                    )
            for item_row in item.checklists:
                writes.append(
                    Write(
                        action="checklist",
                        uuid=item_row.uuid,
                        checklist_parent_uuid=item.uuid,
                        checklist_remove=True,
                    )
                )
            writes.append(
                Write(action="permanent_delete", uuid=item.uuid, kind=item.kind)
            )
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
            elif change.replace_rich_note:
                raise _Abort(
                    self._rejected(
                        "replace_rich_note is only for an existing rich note."
                    )
                )
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
        if not context.writes:
            raise _Abort(self._rejected("The request did not produce a change."))
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
                if write.into_kind == "project" and write.into_uuid is not None
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
            context.writes.append(
                Write(
                    action="tags",
                    uuid=item.uuid,
                    kind=item.kind,
                    tag_uuids=[tag for tag in item.tag_uuids if tag != uuid],
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
        if change.into is None:
            return None
        if change.into.startswith("$"):
            resolved = context.local.get(change.into)
            if resolved is None or resolved[1] != "project":
                return None
            return resolved[0]
        if change.into in {"inbox", "anytime"}:
            return None
        destination = self._required_exact(change.into)
        if destination.kind != "project":
            return None
        return destination.uuid

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
            companion = next(
                (
                    write
                    for write in reversed(planned)
                    if write.uuid == item.uuid
                    and write.action == "update"
                    and write.kind == kind
                    and self._write_scope(write) == wanted_scope
                ),
                None,
            )
            if item.kind != kind or self._record_scope(item) != wanted_scope:
                if companion is None:
                    raise _Abort(
                        self._rejected("An after reference must be in the same list.")
                    )
                preconditions[item.id] = self._revision(item)
                anchor_uuid = companion.uuid
                anchor_index = (
                    companion.sort_index
                    if companion.sort_index is not None
                    else item.sort_index
                )
            else:
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
        preconditions[f"scope:project:{project_uuid}"] = self._project_scope_revision(
            project_uuid
        )
        writes: list[Write] = []
        for index, row in enumerate(ordered):
            wanted = index * 1024
            if row.record is not None:
                preconditions[row.record.id] = self._revision(row.record)
                if row.sort_index == wanted:
                    continue
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
            [(item.sort_index, item.uuid, "existing", item) for item in existing]
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
            if write.action in {"delete_tag", "rename_tag", "reparent_tag", "ensure_tag"}:
                item_id = None
            elif item is not None:
                item_id = item.id
            else:
                item_id = f"{write.kind}:{write.uuid}"
            if write.action == "create":
                add(f"create_{write.kind}", write.title or write.kind, item_id)
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
                else f"{write.into_kind}:{write.into_uuid}"
                if write.into_uuid and write.into_kind
                else None
            )
            if item is not None and dest is not None:
                source = (
                    "Inbox"
                    if item.inbox
                    else f"project:{item.parent_uuid}"
                    if item.parent_uuid
                    else f"area:{item.area_uuid}"
                    if item.area_uuid
                    else "Anytime"
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
        review = [
            ReviewSection(
                key=key,
                title=titles.get(key, key),
                item_ids=ids,
            )
            for key, ids in sections.items()
            if ids
        ][:20]
        return summary, review

    def _stage(self, record: IntentRecord, prepared: _Prepared) -> Result:
        expires = self._clock() + timedelta(minutes=_PLAN_MINUTES)
        plan_id = f"plan_{token_urlsafe(12)}"
        summary, sections = self._plan_manifest(prepared)
        result = Result(
            next="approve",
            status="needs_approval",
            instruction=(
                "Ask one short, natural confirmation about the visible change and its "
                "important consequence. Keep plan IDs and control fields private. "
                "Call things_approve only after a clear yes."
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
        if self._writes_match(writes):
            result = self._settled(record.intent_id, writes, unchanged=True)
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
                return self._pending_outcome(
                    record,
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
            return self._pending_outcome(
                record,
                "Cloud accepted the request, but read-back is still pending.",
            )
        result = self._settled(record.intent_id, writes, unchanged=False)
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
        if self._writes_match(writes):
            result = self._settled(record.intent_id, writes, unchanged=False)
            self._save_result(record, "applied", result)
            return result
        if allow_apply:
            return self._apply(record)
        return self._pending_outcome(
            record,
            "The Cloud outcome is still unknown. Retry only this same receipt.",
        )

    def _pending_outcome(self, record: IntentRecord, instruction: str) -> Result:
        attempts = _pending_attempts(record) + 1
        receipt = record.plan_id or record.intent_id
        if attempts > _PENDING_RETRY_LIMIT:
            result = Result(
                next="stop",
                status="unavailable",
                instruction=(
                    "Cloud read-back did not settle after "
                    f"{_PENDING_RETRY_LIMIT} attempts. Do not retry this receipt. "
                    "Read current facts and start a new intent if the work is "
                    "still needed."
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
        self, intent_id: str, writes: list[Write], *, unchanged: bool
    ) -> Result:
        ids: list[str] = []
        missing_ids: list[str] = []
        tags: list[TagFact] = []

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
                    missing_ids.append(f"{write.kind}:{write.uuid}")
                else:
                    add_id(write.uuid)
                continue
            add_id(write.uuid)
            if write.tag_uuids is not None:
                for tag_uuid in write.tag_uuids:
                    add_tag(tag_uuid)

        items = [
            self._fact(item, full=False)
            for uuid in ids
            if (item := self._library.records.get(uuid)) is not None
        ][:_CONTEXT_LIMIT]
        missing_ids = list(dict.fromkeys(missing_ids))[:10]
        return Result(
            next="done",
            status="unchanged" if unchanged else "applied",
            instruction="The requested state was already true."
            if unchanged
            else "Cloud read-back matched the requested state.",
            items=items,
            tags=tags,
            receipt=intent_id,
            missing_ids=missing_ids,
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
    payload = result.model_dump(
        mode="json", exclude_none=True, exclude_defaults=True
    )
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
    return cast(
        JsonDict,
        result.model_dump(mode="json", exclude_defaults=True, exclude_none=True),
    )
