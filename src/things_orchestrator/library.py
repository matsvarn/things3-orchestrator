"""In-memory Things library. Cloud and tests share this seam."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, Protocol, TypeVar, cast
from uuid import uuid4

from .recurrence import JsonValue, RecurrenceState

Kind = Literal["task", "project", "area"]
PublicKind = Literal["task", "project", "area", "heading"]
Status = Literal["open", "done", "dropped"]


def public_id(kind: PublicKind, uuid: str) -> str:
    return f"{kind}:{uuid}"


def parse_id(value: str) -> tuple[PublicKind | None, str]:
    prefix, _, rest = value.partition(":")
    if rest and prefix in {"task", "project", "area", "heading"}:
        return prefix, rest  # type: ignore[return-value]
    return None, value


def new_uuid() -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = int.from_bytes(uuid4().bytes, "big")
    if number == 0:
        return alphabet[0]
    chars: list[str] = []
    while number:
        number, rem = divmod(number, 58)
        chars.append(alphabet[rem])
    return "".join(reversed(chars))


def day_ts(day: date) -> int:
    return int(datetime.combine(day, time.min, tzinfo=timezone.utc).timestamp())


def from_ts(value: int | float | None) -> date | None:
    if value is None or value <= 0:
        return None
    return datetime.fromtimestamp(value, timezone.utc).date()


def remind_from_offset(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    hours, rem = divmod(int(seconds), 3600)
    minutes = rem // 60
    return f"{hours:02d}:{minutes:02d}"


def offset_from_remind(value: str) -> int:
    hours, minutes = value.split(":", 1)
    return int(hours) * 3600 + int(minutes) * 60


@dataclass
class ChecklistLine:
    uuid: str
    title: str
    done: bool = False
    status: Status = "open"
    sort_index: int = 0

    def __post_init__(self) -> None:
        if self.done and self.status == "open":
            self.status = "done"
        self.done = self.status == "done"


@dataclass
class Record:
    uuid: str
    kind: Kind
    title: str
    notes: str = ""
    notes_source: Literal["none", "legacy", "structured"] = "none"
    notes_format: Literal["plain", "markdown", "rich"] = "markdown"
    status: Status = "open"
    completed_at: datetime | None = None
    trashed: bool = False
    inbox: bool = False
    start: date | None = None
    deadline: date | None = None
    remind: str | None = None
    tonight: bool = False
    parent_uuid: str | None = None
    area_uuid: str | None = None
    tag_uuids: list[str] = field(default_factory=list)
    recurrence: RecurrenceState = field(default_factory=RecurrenceState)
    heading: bool = False
    heading_uuid: str | None = None
    someday: bool = False
    sort_index: int = 0
    today_index: int = 0
    entity: str = ""
    checklists: list[ChecklistLine] = field(default_factory=list)

    @property
    def id(self) -> str:
        return public_id(self.public_kind, self.uuid)

    @property
    def public_kind(self) -> PublicKind:
        return "heading" if self.heading else self.kind

    def is_open(self) -> bool:
        return (
            self.status == "open"
            and not self.trashed
            and self.recurrence.role != "template"
            and not self.heading
        )


@dataclass(frozen=True)
class Write:
    """Stable journal form for one mutation.

    Adapters compile this compatibility record to a typed mutation family
    before they execute it. Old pending journal entries therefore keep their
    wire shape while adapter code does not depend on one flat action union.
    """

    action: Literal[
        "create",
        "create_heading",
        "update",
        "complete",
        "cancel",
        "move",
        "tags",
        "rename_area",
        "delete_area",
        "trash",
        "restore",
        "permanent_delete",
        "ensure_tag",
        "rename_tag",
        "reparent_tag",
        "delete_tag",
        "checklist",
        "repeat",
        "repeat_link",
    ]
    uuid: str
    kind: Kind = "task"
    title: str | None = None
    notes: str | None = None
    status: Status | None = None
    into_uuid: str | None = None
    into_kind: Kind | None = None
    start: date | None = None
    clear_start: bool = False
    deadline: date | None = None
    clear_deadline: bool = False
    remind: str | None = None
    clear_remind: bool = False
    tag_uuids: list[str] | None = None
    tonight: bool = False
    someday: bool = False
    inbox: bool = False
    anytime: bool = False
    heading_uuid: str | None = None
    clear_heading: bool = False
    sort_index: int | None = None
    today_index: int | None = None
    owner_today: date | None = None
    checklist_parent_uuid: str | None = None
    checklist_status: Status | None = None
    checklist_index: int | None = None
    checklist_remove: bool = False
    recurrence_rule: dict[str, JsonValue] | None = None
    recurrence_links: list[str] | None = None
    recurrence_generated: bool = False
    tag_parent_uuids: list[str] | None = None


@dataclass(frozen=True)
class _CreateMutation:
    write: Write
    heading: bool

    def dispatch(self, handler: _MutationHandler[_Result]) -> _Result:
        return handler.create(self)


@dataclass(frozen=True)
class _EditMutation:
    write: Write
    action: Literal["update", "move", "tags", "rename_area"]

    def dispatch(self, handler: _MutationHandler[_Result]) -> _Result:
        return handler.edit(self)


@dataclass(frozen=True)
class _LifecycleMutation:
    write: Write
    action: Literal[
        "complete", "cancel", "delete_area", "trash", "restore", "permanent_delete"
    ]

    def dispatch(self, handler: _MutationHandler[_Result]) -> _Result:
        return handler.lifecycle(self)


@dataclass(frozen=True)
class _TagMutation:
    write: Write
    action: Literal["ensure_tag", "rename_tag", "reparent_tag", "delete_tag"]

    def dispatch(self, handler: _MutationHandler[_Result]) -> _Result:
        return handler.tag(self)


@dataclass(frozen=True)
class _ChecklistMutation:
    write: Write

    def dispatch(self, handler: _MutationHandler[_Result]) -> _Result:
        return handler.checklist(self)


@dataclass(frozen=True)
class _RecurrenceMutation:
    write: Write
    action: Literal["repeat", "repeat_link"]

    def dispatch(self, handler: _MutationHandler[_Result]) -> _Result:
        return handler.recurrence(self)


_Mutation = (
    _CreateMutation
    | _EditMutation
    | _LifecycleMutation
    | _TagMutation
    | _ChecklistMutation
    | _RecurrenceMutation
)

_Result = TypeVar("_Result", covariant=True)


class _MutationHandler(Protocol[_Result]):
    """Visitor interface implemented by each mutation adapter."""

    def create(self, mutation: _CreateMutation) -> _Result: ...
    def edit(self, mutation: _EditMutation) -> _Result: ...
    def lifecycle(self, mutation: _LifecycleMutation) -> _Result: ...
    def tag(self, mutation: _TagMutation) -> _Result: ...
    def checklist(self, mutation: _ChecklistMutation) -> _Result: ...
    def recurrence(self, mutation: _RecurrenceMutation) -> _Result: ...


def _compile_mutation(write: Write) -> _Mutation:
    """Compile the journal form to the small internal mutation interface."""
    action = write.action
    if action in {"create", "create_heading"}:
        return _CreateMutation(write, heading=action == "create_heading")
    if action in {"update", "move", "tags", "rename_area"}:
        return _EditMutation(
            write, cast(Literal["update", "move", "tags", "rename_area"], action)
        )
    if action in {
        "complete",
        "cancel",
        "delete_area",
        "trash",
        "restore",
        "permanent_delete",
    }:
        return _LifecycleMutation(
            write,
            cast(
                Literal[
                    "complete",
                    "cancel",
                    "delete_area",
                    "trash",
                    "restore",
                    "permanent_delete",
                ],
                action,
            ),
        )
    if action in {"ensure_tag", "rename_tag", "reparent_tag", "delete_tag"}:
        return _TagMutation(
            write,
            cast(
                Literal["ensure_tag", "rename_tag", "reparent_tag", "delete_tag"],
                action,
            ),
        )
    if action == "checklist":
        return _ChecklistMutation(write)
    if action in {"repeat", "repeat_link"}:
        return _RecurrenceMutation(
            write, cast(Literal["repeat", "repeat_link"], action)
        )
    raise ValueError(f"Unknown mutation action: {action}")


@dataclass
class ApplyResult:
    verified: list[str]
    created: dict[str, str]


class Library(Protocol):
    def refresh(self, *, force: bool = False) -> None: ...
    def get(self, value: str) -> Record | None: ...
    def find(
        self, text: str, limit: int = 10, into: str | None = None
    ) -> list[Record]: ...
    def today(self, *, waiting_tag: str, today: date) -> list[Record]: ...
    def inbox(self, limit: int = 15) -> list[Record]: ...
    def week(self, *, today: date, limit: int = 15) -> list[Record]: ...
    def trash(self) -> list[Record]: ...
    def area(self, value: str) -> list[Record]: ...
    def audit(self) -> list[Record]: ...
    def project(self, value: str) -> list[Record]: ...
    def heading_title(self, item: Record) -> str | None: ...
    def next_index(self, write: Write) -> int: ...
    def system(self) -> list[Record]: ...
    def areas(self) -> list[Record]: ...
    def children_in_area(self, uuid: str) -> list[Record]: ...
    def parent_title(self, item: Record) -> str | None: ...
    def resolve_into(self, value: str) -> Record | None | list[Record]: ...
    def tag_uuid(self, title: str) -> str | None: ...
    def waiting_tag(self) -> str: ...
    def recurrence_instances(self, template_uuid: str) -> list[Record]: ...
    def apply(self, writes: list[Write]) -> ApplyResult: ...
    def matches(self, writes: list[Write]) -> bool: ...


def template_uuid_of(item: Record) -> str | None:
    """Return the stored template UUID for an instance, from either representation."""

    if item.recurrence.template_uuid:
        return item.recurrence.template_uuid
    if item.recurrence.links:
        return item.recurrence.links[0]
    return None


def linked_to_template(item: Record, template_uuid: str) -> bool:
    """Return whether a record is a recurrence instance of this template."""

    if item.uuid == template_uuid:
        return False
    return template_uuid_of(item) == template_uuid or template_uuid in item.recurrence.links


class MemoryLibrary:
    def __init__(self, records: list[Record] | None = None) -> None:
        self.records: dict[str, Record] = {item.uuid: item for item in records or []}
        self.tags: dict[str, str] = {}
        self.tag_parents: dict[str, list[str]] = {}

    def refresh(self, *, force: bool = False) -> None:
        return

    def _open(self) -> list[Record]:
        return [item for item in self.records.values() if item.is_open()]

    def get(self, value: str) -> Record | None:
        kind, uuid = parse_id(value)
        item = self.records.get(uuid)
        if item is None:
            matches = [
                candidate
                for candidate in self.records.values()
                if candidate.uuid.startswith(value) or candidate.id == value
            ]
            return matches[0] if len(matches) == 1 else None
        if kind is not None and item.public_kind != kind:
            return None
        return item

    def find(self, text: str, limit: int = 10, into: str | None = None) -> list[Record]:
        needle = text.casefold()
        hits = [
            item
            for item in self._open()
            if needle in item.title.casefold()
            or needle in item.notes.casefold()
            or any(needle in line.title.casefold() for line in item.checklists)
        ]
        if into:
            home = self.resolve_into(into)
            if isinstance(home, list) or home is None:
                return []
            if home.kind == "area":
                hits = [
                    item
                    for item in hits
                    if item.area_uuid == home.uuid or item.uuid == home.uuid
                ]
            else:
                hits = [
                    item
                    for item in hits
                    if item.parent_uuid == home.uuid or item.uuid == home.uuid
                ]
        hits.sort(key=lambda item: (item.sort_index, item.title))
        return hits[:limit]

    def today(self, *, waiting_tag: str, today: date) -> list[Record]:
        waiting = self.tag_uuid(waiting_tag)
        ranked: list[tuple[int, Record]] = []
        for item in self._open():
            if item.kind == "area":
                continue
            issue = 9
            if item.deadline is not None and item.deadline <= today:
                issue = 0
            elif item.tonight:
                issue = 1
            elif item.start == today and not item.inbox:
                issue = 2
            elif item.inbox:
                issue = 3
            elif waiting is not None and waiting in item.tag_uuids:
                issue = 4
            else:
                continue
            ranked.append((issue, item))
        ranked.sort(
            key=lambda pair: (
                pair[0],
                pair[1].deadline or date.max,
                pair[1].today_index,
                pair[1].sort_index,
                pair[1].title,
            )
        )
        return [item for _, item in ranked]

    def inbox(self, limit: int = 15) -> list[Record]:
        hits = [item for item in self._open() if item.inbox and item.kind != "area"]
        hits.sort(key=lambda item: (item.sort_index, item.title))
        return hits[:limit]

    def week(self, *, today: date, limit: int = 15) -> list[Record]:
        end = today + timedelta(days=7)
        hits = [
            item
            for item in self._open()
            if item.kind != "area"
            and (
                (item.deadline is not None and today <= item.deadline <= end)
                or (item.start is not None and today < item.start <= end)
            )
        ]
        hits.sort(
            key=lambda item: (item.deadline or item.start or date.max, item.sort_index)
        )
        return hits[:limit]

    def trash(self) -> list[Record]:
        hits = [item for item in self.records.values() if item.trashed]
        return sorted(hits, key=lambda item: (item.kind, item.sort_index, item.title))

    def area(self, value: str) -> list[Record]:
        root = self.get(value)
        if root is None or root.kind != "area":
            return []
        return [root, *self.children_in_area(root.uuid)]

    def audit(self) -> list[Record]:
        kind_order = {"area": 0, "project": 1, "heading": 2, "task": 3}
        items = [
            item
            for item in self.records.values()
            if not item.trashed
            and item.status == "open"
            and item.recurrence.role != "template"
        ]
        items.sort(
            key=lambda item: (
                kind_order.get(item.public_kind, 9),
                item.sort_index,
                item.title.casefold(),
                item.uuid,
            )
        )
        return items

    def project(self, value: str) -> list[Record]:
        root = self.get(value)
        if root is None or root.kind != "project":
            return []
        children = [
            item
            for item in self.records.values()
            if item.parent_uuid == root.uuid
            and not item.trashed
            and item.status == "open"
            and item.recurrence.role != "template"
        ]
        children.sort(
            key=lambda item: (
                self.records[item.heading_uuid].sort_index
                if item.heading_uuid and item.heading_uuid in self.records
                else item.sort_index
                if item.heading
                else -1,
                0 if item.heading else 1,
                item.sort_index,
                item.title,
            )
        )
        return [root, *children]

    def heading_title(self, item: Record) -> str | None:
        if item.heading_uuid and item.heading_uuid in self.records:
            return self.records[item.heading_uuid].title
        return None

    def next_index(self, write: Write) -> int:
        siblings: list[Record] = []
        for item in self.records.values():
            if (
                item.heading
                or item.recurrence.role == "template"
                or item.uuid == write.uuid
            ):
                continue
            if write.into_kind == "project" and item.parent_uuid == write.into_uuid:
                siblings.append(item)
            elif write.kind == "area" and item.kind == "area":
                siblings.append(item)
            elif (
                write.into_kind == "area"
                and item.area_uuid == write.into_uuid
                and not item.parent_uuid
            ):
                siblings.append(item)
            elif (
                write.kind == "project"
                and write.into_uuid is None
                and write.into_kind is None
            ):
                if (
                    item.kind == "project"
                    and not item.parent_uuid
                    and not item.area_uuid
                    and not item.inbox
                    and not item.someday
                ):
                    siblings.append(item)
            elif (
                write.kind == "task"
                and write.into_uuid is None
                and write.into_kind is None
            ):
                if write.anytime:
                    if (
                        item.kind == "task"
                        and not item.parent_uuid
                        and not item.area_uuid
                        and not item.inbox
                        and not item.someday
                    ):
                        siblings.append(item)
                elif item.inbox:
                    siblings.append(item)
        if not siblings:
            return 0
        return max(item.sort_index for item in siblings) + 1024

    def system(self) -> list[Record]:
        areas = self.areas()
        projects = [item for item in self._open() if item.kind == "project"]
        projects.sort(key=lambda item: (item.sort_index, item.title))
        return [*areas, *projects]

    def areas(self) -> list[Record]:
        return sorted(
            [item for item in self._open() if item.kind == "area"],
            key=lambda item: (item.sort_index, item.title),
        )

    def children_in_area(self, uuid: str) -> list[Record]:
        return sorted(
            [
                item
                for item in self.records.values()
                if item.kind != "area"
                and item.area_uuid == uuid
                and not item.parent_uuid
                and not item.trashed
                and item.status == "open"
                and not item.heading
            ],
            key=lambda item: (item.sort_index, item.title),
        )

    def resolve_into(self, value: str) -> Record | None | list[Record]:
        exact = self.get(value)
        if exact is not None and exact.kind in {"area", "project"}:
            return exact
        needle = value.casefold()
        matches = [
            item
            for item in self._open()
            if item.kind in {"area", "project"} and item.title.casefold() == needle
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            return None
        return matches

    def tag_uuid(self, title: str) -> str | None:
        for uuid, name in self.tags.items():
            if name.casefold() == title.casefold():
                return uuid
        return None

    def waiting_tag(self) -> str:
        names = {name.casefold(): name for name in self.tags.values() if name}
        if "warten" in names:
            return names["warten"]
        if "waiting" in names:
            return names["waiting"]
        return "Waiting"

    def recurrence_instances(self, template_uuid: str) -> list[Record]:
        return sorted(
            (
                item
                for item in self.records.values()
                if linked_to_template(item, template_uuid)
            ),
            key=lambda item: (item.sort_index, item.uuid),
        )

    def resolve_instance_types(self) -> None:
        for record in self.records.values():
            template_uuid = template_uuid_of(record)
            if record.recurrence.role != "instance" or template_uuid is None:
                continue
            template = self.records.get(template_uuid)
            record.recurrence = record.recurrence.resolve_instance_type(
                template.recurrence.repeat_type if template else None
            )

    def apply(self, writes: list[Write]) -> ApplyResult:
        snapshot = (
            deepcopy(self.records),
            deepcopy(self.tags),
            deepcopy(self.tag_parents),
        )
        try:
            return self._apply_unchecked(writes)
        except Exception:
            self.records, self.tags, self.tag_parents = snapshot
            raise

    def matches(self, writes: list[Write]) -> bool:
        """Verify a mutation batch against the current materialized state.

        This is the shared read-back seam for both library adapters. It keeps
        journal recovery independent from adapter storage details.
        """
        tag_aliases = {
            write.uuid: actual
            for write in writes
            if write.action == "ensure_tag"
            and (actual := self.tag_uuid(write.title or "")) is not None
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
        return _compile_mutation(write).dispatch(_MutationVerifier(self))

    @staticmethod
    def _placement_matches(item: Record, write: Write) -> bool:
        if write.kind == "area":
            return (
                not item.inbox and item.parent_uuid is None and item.area_uuid is None
            )
        if write.into_kind == "project":
            return (
                item.parent_uuid == write.into_uuid
                and item.area_uuid is None
                and not item.inbox
                and item.heading_uuid == write.heading_uuid
            )
        if write.into_kind == "area":
            return (
                item.area_uuid == write.into_uuid
                and item.parent_uuid is None
                and not item.inbox
            )
        if (
            write.kind == "project"
            or write.anytime
            or write.start is not None
            or write.someday
            or write.tonight
        ):
            return (
                not item.inbox and item.parent_uuid is None and item.area_uuid is None
            )
        return item.inbox and item.parent_uuid is None and item.area_uuid is None

    def _apply_unchecked(self, writes: list[Write]) -> ApplyResult:
        handler = _MemoryApplyHandler(self)
        for write in writes:
            handler.apply(write)
        handler.finish()
        return handler.result()

    def _find_checklist(self, uuid: str) -> tuple[Record | None, ChecklistLine | None]:
        for parent in self.records.values():
            for line in parent.checklists:
                if line.uuid == uuid:
                    return parent, line
        return None, None

    def parent_title(self, item: Record) -> str | None:
        if item.parent_uuid and item.parent_uuid in self.records:
            return self.records[item.parent_uuid].title
        if item.area_uuid and item.area_uuid in self.records:
            return self.records[item.area_uuid].title
        if item.inbox:
            return "Inbox"
        return None


class _MemoryApplyHandler(_MutationHandler[None]):
    """Apply each typed mutation family to one in-memory library."""

    def __init__(self, library: MemoryLibrary) -> None:
        self.library = library
        self.created: dict[str, str] = {}
        self.verified: list[str] = []
        self.tag_aliases: dict[str, str] = {}

    def result(self) -> ApplyResult:
        return ApplyResult(
            verified=list(dict.fromkeys(self.verified)),
            created=self.created,
        )

    def apply(self, write: Write) -> None:
        mutation = _compile_mutation(write)
        if write.tag_uuids is not None:
            write = replace(
                write,
                tag_uuids=[
                    self.tag_aliases.get(uuid, uuid) for uuid in write.tag_uuids
                ],
            )
            mutation = _compile_mutation(write)
        self._validate_heading(write)
        mutation.dispatch(self)

    def _validate_heading(self, write: Write) -> None:
        if write.heading_uuid is None:
            return
        current = self.library.records.get(write.uuid)
        project_uuid = write.into_uuid or (current.parent_uuid if current else None)
        heading = self.library.records.get(write.heading_uuid)
        if (
            heading is None
            or not heading.heading
            or not project_uuid
            or heading.parent_uuid != project_uuid
        ):
            raise ValueError("The heading must belong to the destination Project")

    def create(self, mutation: _CreateMutation) -> None:
        write = mutation.write
        parent_uuid: str | None = None
        area_uuid: str | None = None
        inbox = (
            write.into_uuid is None
            and write.kind == "task"
            and not write.someday
            and not write.tonight
            and write.start is None
            and not write.anytime
            and not write.inbox
        )
        if write.kind == "area":
            inbox = False
        elif write.inbox:
            if write.kind == "project":
                raise ValueError("Projects cannot enter Inbox")
            inbox = True
        elif write.into_kind == "project":
            parent_uuid = write.into_uuid
            inbox = False
        elif write.into_kind == "area":
            area_uuid = write.into_uuid
            inbox = False
        record = Record(
            uuid=write.uuid,
            kind=write.kind,
            title=write.title or "",
            notes=write.notes or "",
            notes_source="structured" if write.notes is not None else "none",
            notes_format="markdown",
            status=write.status or "open",
            completed_at=(
                datetime.now(timezone.utc)
                if write.status in {"done", "dropped"}
                else None
            ),
            start=write.start,
            deadline=write.deadline,
            remind=write.remind,
            tonight=write.tonight,
            someday=write.someday,
            parent_uuid=parent_uuid,
            area_uuid=area_uuid,
            inbox=inbox and not write.someday and not write.tonight,
            tag_uuids=list(write.tag_uuids or []),
            heading_uuid=write.heading_uuid,
            sort_index=(
                write.sort_index
                if write.sort_index is not None
                else self.library.next_index(write)
            ),
            today_index=write.today_index or 0,
            heading=mutation.heading,
            recurrence=(
                RecurrenceState()
                .fold_rule(write.recurrence_rule)
                .fold_links(write.recurrence_links)
                if write.recurrence_links
                else RecurrenceState().fold_rule(write.recurrence_rule)
            ),
        )
        self.library.records[record.uuid] = record
        self.created[record.title] = record.id
        self.verified.append(record.title)

    def edit(self, mutation: _EditMutation) -> None:
        write = mutation.write
        item = self.library.records.get(write.uuid)
        if item is None:
            return
        if mutation.action == "rename_area":
            if write.title:
                item.title = write.title
        elif mutation.action == "move":
            item.heading_uuid = write.heading_uuid
            if write.into_kind == "project":
                item.parent_uuid = write.into_uuid
                item.area_uuid = None
                item.inbox = False
            elif write.into_kind == "area":
                item.area_uuid = write.into_uuid
                item.parent_uuid = None
                item.inbox = False
            elif item.kind == "project":
                if write.inbox:
                    raise ValueError("Projects cannot enter Inbox")
                item.inbox = False
                item.someday = False
                item.parent_uuid = None
                item.area_uuid = None
            else:
                item.parent_uuid = None
                item.area_uuid = None
                item.inbox = not write.anytime
                item.someday = False
                item.tonight = False
                item.start = None
                item.remind = None
        elif mutation.action == "tags":
            if write.tag_uuids is not None:
                item.tag_uuids = list(write.tag_uuids)
        else:
            if write.clear_heading:
                item.heading_uuid = None
            if write.status is not None:
                item.status = write.status
                item.completed_at = (
                    datetime.now(timezone.utc)
                    if write.status in {"done", "dropped"}
                    else None
                )
            if write.title is not None:
                item.title = write.title
            if write.notes is not None:
                item.notes = write.notes
                item.notes_source = "structured"
                item.notes_format = "markdown"
            if write.tag_uuids is not None:
                item.tag_uuids = list(write.tag_uuids)
            if write.sort_index is not None:
                item.sort_index = write.sort_index
            if write.today_index is not None:
                item.today_index = write.today_index
            if write.clear_start:
                item.start = None
                item.remind = None
                item.tonight = False
                item.someday = False
            elif write.someday:
                item.start = None
                item.remind = None
                item.someday = True
                item.tonight = False
                item.inbox = False
            elif write.start is not None:
                item.start = write.start
                item.someday = False
                item.inbox = False
                item.tonight = write.tonight
            if write.tonight and not write.someday:
                item.tonight = True
                item.inbox = False
            if write.anytime:
                item.start = None
                item.remind = None
                item.someday = False
                item.tonight = False
                item.inbox = False
            if write.clear_deadline:
                item.deadline = None
            elif write.deadline is not None:
                item.deadline = write.deadline
            if write.clear_remind:
                item.remind = None
            elif write.remind is not None:
                item.remind = write.remind
            if (
                write.into_uuid is not None
                or write.into_kind is not None
                or write.inbox
                or write.anytime
                or write.heading_uuid is not None
            ):
                self.edit(
                    _EditMutation(
                        Write(
                            action="move",
                            uuid=item.uuid,
                            kind=item.kind,
                            into_uuid=write.into_uuid,
                            into_kind=write.into_kind,
                            inbox=write.inbox,
                            anytime=write.anytime,
                            heading_uuid=write.heading_uuid,
                        ),
                        action="move",
                    )
                )
        self.verified.append(item.title)

    def lifecycle(self, mutation: _LifecycleMutation) -> None:
        write = mutation.write
        item = self.library.records.get(write.uuid)
        if item is None:
            return
        if mutation.action == "complete":
            item.status = "done"
            item.completed_at = datetime.now(timezone.utc)
        elif mutation.action == "cancel":
            item.status = "dropped"
            item.completed_at = datetime.now(timezone.utc)
        elif mutation.action == "delete_area":
            item.trashed = True
            del self.library.records[item.uuid]
        elif mutation.action == "trash":
            item.trashed = True
        elif mutation.action == "restore":
            item.trashed = False
        else:
            for child in self.library.records.values():
                if child.parent_uuid == item.uuid:
                    child.parent_uuid = None
                if child.area_uuid == item.uuid:
                    child.area_uuid = None
                if child.heading_uuid == item.uuid:
                    child.heading_uuid = None
            del self.library.records[item.uuid]
        self.verified.append(item.title)

    def tag(self, mutation: _TagMutation) -> None:
        write = mutation.write
        if mutation.action == "ensure_tag":
            existing = self.library.tag_uuid(write.title or "")
            parents = [
                self.tag_aliases.get(parent, parent)
                for parent in (write.tag_parent_uuids or [])
            ]
            if existing is None:
                self.library.tags[write.uuid] = write.title or ""
                self.library.tag_parents[write.uuid] = parents
                self.tag_aliases[write.uuid] = write.uuid
                self.created[write.title or write.uuid] = write.uuid
            else:
                self.tag_aliases[write.uuid] = existing
                if write.tag_parent_uuids is not None:
                    self.library.tag_parents[existing] = parents
                self.created[write.title or existing] = existing
            return
        tag_uuid = self.tag_aliases.get(write.uuid, write.uuid)
        if mutation.action == "rename_tag":
            if tag_uuid not in self.library.tags:
                raise ValueError("Tag does not exist")
            if not write.title or not write.title.strip():
                raise ValueError("Tag rename needs a title")
            self.library.tags[tag_uuid] = write.title.strip()
            self.verified.append(self.library.tags[tag_uuid])
            return
        if mutation.action == "reparent_tag":
            if tag_uuid not in self.library.tags:
                raise ValueError("Tag does not exist")
            parents = [
                self.tag_aliases.get(parent, parent)
                for parent in (write.tag_parent_uuids or [])
            ]
            if tag_uuid in parents:
                raise ValueError("A tag cannot be its own parent")
            if any(parent not in self.library.tags for parent in parents):
                raise ValueError("Tag parent does not exist")
            self.library.tag_parents[tag_uuid] = parents
            self.verified.append(self.library.tags[tag_uuid])
            return
        title = self.library.tags.pop(tag_uuid, write.title or tag_uuid)
        self.library.tag_parents.pop(tag_uuid, None)
        for item in self.library.records.values():
            item.tag_uuids = [tag for tag in item.tag_uuids if tag != tag_uuid]
        for tag, parents in self.library.tag_parents.items():
            self.library.tag_parents[tag] = [
                parent for parent in parents if parent != tag_uuid
            ]
        self.verified.append(title)

    def checklist(self, mutation: _ChecklistMutation) -> None:
        write = mutation.write
        parent, line = self.library._find_checklist(write.uuid)
        if write.checklist_remove:
            if parent is not None:
                parent.checklists = [
                    item for item in parent.checklists if item.uuid != write.uuid
                ]
            self.verified.append(write.title or (line.title if line else write.uuid))
            return
        destination = (
            self.library.records.get(write.checklist_parent_uuid or "") or parent
        )
        if destination is None or destination.kind != "task":
            raise ValueError("A checklist row needs a task parent")
        status = write.checklist_status or (line.status if line else "open")
        index = (
            write.checklist_index
            if write.checklist_index is not None
            else write.sort_index
        )
        if index is None:
            index = (
                line.sort_index
                if line
                else max(
                    (item.sort_index for item in destination.checklists), default=-1
                )
                + 1
            )
        replacement = ChecklistLine(
            uuid=write.uuid,
            title=write.title
            if write.title is not None
            else (line.title if line else ""),
            status=status,
            sort_index=index,
        )
        if parent is not None and parent is not destination:
            parent.checklists = [
                item for item in parent.checklists if item.uuid != write.uuid
            ]
        destination.checklists = [
            item for item in destination.checklists if item.uuid != write.uuid
        ]
        destination.checklists.append(replacement)
        destination.checklists.sort(key=lambda item: (item.sort_index, item.uuid))
        self.verified.append(replacement.title)

    def recurrence(self, mutation: _RecurrenceMutation) -> None:
        item = self.library.records.get(mutation.write.uuid)
        if mutation.action == "repeat" and (
            item is None or mutation.write.recurrence_rule is None
        ):
            raise ValueError("Repeat changes need an exact repeating Task template")
        if item is None:
            return
        if mutation.action == "repeat":
            item.recurrence.validate_interval_template(kind=item.kind)
            assert mutation.write.recurrence_rule is not None
            item.recurrence = item.recurrence.fold_rule(mutation.write.recurrence_rule)
        else:
            item.recurrence = item.recurrence.fold_links(
                mutation.write.recurrence_links or []
            )
        self.verified.append(item.title)

    def finish(self) -> None:
        self.library.resolve_instance_types()


class _MutationVerifier(_MutationHandler[bool]):
    """Verify semantic mutations against one materialized library."""

    def __init__(self, library: MemoryLibrary) -> None:
        self.library = library

    def create(self, mutation: _CreateMutation) -> bool:
        item = self.library.records.get(mutation.write.uuid)
        return (
            item is not None
            and item.kind == mutation.write.kind
            and (not mutation.heading or item.heading)
            and self._patch(item, mutation.write, placement=True)
        )

    def edit(self, mutation: _EditMutation) -> bool:
        write = mutation.write
        item = self.library.records.get(write.uuid)
        if item is None:
            return False
        if mutation.action == "move":
            return self.library._placement_matches(item, write)
        if mutation.action == "tags":
            return item.tag_uuids == (write.tag_uuids or [])
        if mutation.action == "rename_area":
            return item.title == write.title
        return self._patch(item, write)

    def lifecycle(self, mutation: _LifecycleMutation) -> bool:
        item = self.library.records.get(mutation.write.uuid)
        checks: dict[str, bool] = {
            "delete_area": item is None,
            "permanent_delete": item is None,
            "trash": item is not None and item.trashed,
            "restore": item is not None and not item.trashed,
            "complete": item is not None and item.status == "done",
            "cancel": item is not None and item.status == "dropped",
        }
        return checks[mutation.action]

    def tag(self, mutation: _TagMutation) -> bool:
        write = mutation.write
        if mutation.action == "ensure_tag":
            return (
                self.library.tags.get(write.uuid) == (write.title or "")
                or self.library.tag_uuid(write.title or "") is not None
            )
        if mutation.action == "rename_tag":
            return self.library.tags.get(write.uuid) == (write.title or "")
        if mutation.action == "reparent_tag":
            return self.library.tag_parents.get(write.uuid, []) == (
                write.tag_parent_uuids or []
            )
        return write.uuid not in self.library.tags

    def checklist(self, mutation: _ChecklistMutation) -> bool:
        write = mutation.write
        parent, row = self.library._find_checklist(write.uuid)
        if write.checklist_remove:
            return row is None
        return (
            row is not None
            and parent is not None
            and all(
                (
                    write.title is None or row.title == write.title,
                    write.checklist_status is None
                    or row.status == write.checklist_status,
                    write.checklist_parent_uuid is None
                    or parent.uuid == write.checklist_parent_uuid,
                    write.checklist_index is None
                    or row.sort_index == write.checklist_index,
                )
            )
        )

    def recurrence(self, mutation: _RecurrenceMutation) -> bool:
        write = mutation.write
        item = self.library.records.get(write.uuid)
        if item is None:
            return False
        if mutation.action == "repeat":
            return item.recurrence.rule == write.recurrence_rule
        return list(item.recurrence.links) == (write.recurrence_links or [])

    def _patch(self, item: Record, write: Write, *, placement: bool = False) -> bool:
        checks = [
            write.title is None or item.title == write.title,
            write.notes is None or item.notes == write.notes,
            write.status is None or item.status == write.status,
            write.tag_uuids is None or item.tag_uuids == write.tag_uuids,
            write.deadline is None or item.deadline == write.deadline,
            not write.clear_deadline or item.deadline is None,
            write.start is None or item.start == write.start,
            write.start is None or item.tonight == write.tonight,
            not write.clear_start
            or (
                item.start is None
                and not item.someday
                and not item.tonight
                and item.remind is None
            ),
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
        if placement or any(
            (
                write.into_uuid is not None,
                write.into_kind is not None,
                write.inbox,
                write.anytime,
            )
        ):
            checks.append(self.library._placement_matches(item, write))
        return all(checks)
