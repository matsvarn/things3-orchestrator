"""In-memory Things library. Cloud and tests share this seam."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, Protocol, cast
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


@dataclass(frozen=True)
class _EditMutation:
    write: Write
    action: Literal["update", "move", "tags", "rename_area"]


@dataclass(frozen=True)
class _LifecycleMutation:
    write: Write
    action: Literal[
        "complete", "cancel", "delete_area", "trash", "restore", "permanent_delete"
    ]


@dataclass(frozen=True)
class _TagMutation:
    write: Write
    action: Literal["ensure_tag", "rename_tag", "reparent_tag", "delete_tag"]


@dataclass(frozen=True)
class _ChecklistMutation:
    write: Write


@dataclass(frozen=True)
class _RecurrenceMutation:
    write: Write
    action: Literal["repeat", "repeat_link"]


_Mutation = (
    _CreateMutation
    | _EditMutation
    | _LifecycleMutation
    | _TagMutation
    | _ChecklistMutation
    | _RecurrenceMutation
)


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
    def find(self, text: str, limit: int = 10, into: str | None = None) -> list[Record]: ...
    def today(self, *, waiting_tag: str, today: date) -> list[Record]: ...
    def inbox(self, limit: int = 15) -> list[Record]: ...
    def week(self, *, today: date, limit: int = 15) -> list[Record]: ...
    def trash(self) -> list[Record]: ...
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
    def apply(self, writes: list[Write]) -> ApplyResult: ...
    def matches(self, writes: list[Write]) -> bool: ...


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
                hits = [item for item in hits if item.area_uuid == home.uuid or item.uuid == home.uuid]
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
        hits.sort(key=lambda item: (item.deadline or item.start or date.max, item.sort_index))
        return hits[:limit]

    def trash(self) -> list[Record]:
        hits = [item for item in self.records.values() if item.trashed]
        return sorted(hits, key=lambda item: (item.kind, item.sort_index, item.title))

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
                else item.sort_index if item.heading else -1,
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
            if item.heading or item.recurrence.role == "template" or item.uuid == write.uuid:
                continue
            if write.into_kind == "project" and item.parent_uuid == write.into_uuid:
                siblings.append(item)
            elif write.kind == "area" and item.kind == "area":
                siblings.append(item)
            elif write.into_kind == "area" and item.area_uuid == write.into_uuid and not item.parent_uuid:
                siblings.append(item)
            elif write.kind == "project" and write.into_uuid is None and write.into_kind is None:
                if (
                    item.kind == "project"
                    and not item.parent_uuid
                    and not item.area_uuid
                    and not item.inbox
                    and not item.someday
                ):
                    siblings.append(item)
            elif write.kind == "task" and write.into_uuid is None and write.into_kind is None:
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
        projects = [
            item
            for item in self._open()
            if item.kind == "project"
        ]
        projects.sort(key=lambda item: (item.sort_index, item.title))
        return [*areas, *projects]

    def areas(self) -> list[Record]:
        return sorted(
            [item for item in self._open() if item.kind == "area"],
            key=lambda item: (item.sort_index, item.title),
        )

    def children_in_area(self, uuid: str) -> list[Record]:
        return sorted([
            item
            for item in self.records.values()
            if item.kind != "area"
            and item.area_uuid == uuid
            and not item.parent_uuid
            and not item.trashed
            and item.status == "open"
            and not item.heading
        ], key=lambda item: (item.sort_index, item.title))

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
        mutation = _compile_mutation(write)
        if isinstance(mutation, _TagMutation) and mutation.action == "ensure_tag":
            return self.tags.get(write.uuid) == (write.title or "") or self.tag_uuid(
                write.title or ""
            ) is not None
        if isinstance(mutation, _TagMutation) and mutation.action == "rename_tag":
            return self.tags.get(write.uuid) == (write.title or "")
        if isinstance(mutation, _TagMutation) and mutation.action == "reparent_tag":
            return self.tag_parents.get(write.uuid, []) == (
                write.tag_parent_uuids or []
            )
        if isinstance(mutation, _TagMutation) and mutation.action == "delete_tag":
            return write.uuid not in self.tags
        if isinstance(mutation, _ChecklistMutation):
            parent, row = self._find_checklist(write.uuid)
            if write.checklist_remove:
                return row is None
            return row is not None and parent is not None and all(
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
        item = self.records.get(write.uuid)
        if isinstance(mutation, _LifecycleMutation) and mutation.action in {
            "delete_area",
            "permanent_delete",
        }:
            return item is None
        if item is None:
            return False
        if isinstance(mutation, _LifecycleMutation) and mutation.action == "trash":
            return item.trashed
        if isinstance(mutation, _LifecycleMutation) and mutation.action == "restore":
            return not item.trashed
        if isinstance(mutation, _RecurrenceMutation) and mutation.action == "repeat":
            return item.recurrence.rule == write.recurrence_rule
        if isinstance(mutation, _RecurrenceMutation) and mutation.action == "repeat_link":
            return list(item.recurrence.links) == (write.recurrence_links or [])
        if isinstance(mutation, _LifecycleMutation) and mutation.action == "complete":
            return item.status == "done"
        if isinstance(mutation, _LifecycleMutation) and mutation.action == "cancel":
            return item.status == "dropped"
        if isinstance(mutation, _EditMutation) and mutation.action == "rename_area":
            return item.title == write.title
        if isinstance(mutation, _EditMutation) and mutation.action == "tags":
            return item.tag_uuids == (write.tag_uuids or [])
        if isinstance(mutation, _EditMutation) and mutation.action == "move":
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
            isinstance(mutation, _CreateMutation)
            or write.into_uuid is not None
            or write.into_kind is not None
            or write.inbox
            or write.anytime
        ):
            checks.append(self._placement_matches(item, write))
        if isinstance(mutation, _CreateMutation):
            checks.append(item.kind == write.kind)
        if isinstance(mutation, _CreateMutation) and mutation.heading:
            checks.append(item.heading)
        return all(checks)

    @staticmethod
    def _placement_matches(item: Record, write: Write) -> bool:
        if write.kind == "area":
            return not item.inbox and item.parent_uuid is None and item.area_uuid is None
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
            return not item.inbox and item.parent_uuid is None and item.area_uuid is None
        return item.inbox and item.parent_uuid is None and item.area_uuid is None

    def _apply_unchecked(self, writes: list[Write]) -> ApplyResult:
        created: dict[str, str] = {}
        verified: list[str] = []
        tag_aliases: dict[str, str] = {}
        for write in writes:
            mutation = _compile_mutation(write)
            if (
                isinstance(mutation, _RecurrenceMutation)
                and mutation.action == "repeat"
            ):
                current = self.records.get(write.uuid)
                if current is None or write.recurrence_rule is None:
                    raise ValueError("Repeat changes need an exact repeating Task template")
                current.recurrence.validate_interval_template(kind=current.kind)
            if write.tag_uuids is not None:
                write = replace(
                    write,
                    tag_uuids=[tag_aliases.get(uuid, uuid) for uuid in write.tag_uuids],
                )
            if write.heading_uuid is not None:
                current = self.records.get(write.uuid)
                project_uuid = write.into_uuid or (current.parent_uuid if current else None)
                heading = self.records.get(write.heading_uuid)
                if (
                    heading is None
                    or not heading.heading
                    or not project_uuid
                    or heading.parent_uuid != project_uuid
                ):
                    raise ValueError("The heading must belong to the destination Project")
            if isinstance(mutation, _TagMutation) and mutation.action == "ensure_tag":
                existing = self.tag_uuid(write.title or "")
                parents = [
                    tag_aliases.get(parent, parent)
                    for parent in (write.tag_parent_uuids or [])
                ]
                if existing is None:
                    uuid = write.uuid
                    self.tags[uuid] = write.title or ""
                    self.tag_parents[uuid] = parents
                    tag_aliases[write.uuid] = uuid
                    created[write.title or uuid] = uuid
                else:
                    tag_aliases[write.uuid] = existing
                    if write.tag_parent_uuids is not None:
                        self.tag_parents[existing] = parents
                    created[write.title or existing] = existing
                continue
            if isinstance(mutation, _TagMutation) and mutation.action == "rename_tag":
                tag_uuid = tag_aliases.get(write.uuid, write.uuid)
                if tag_uuid not in self.tags:
                    raise ValueError("Tag does not exist")
                if not write.title or not write.title.strip():
                    raise ValueError("Tag rename needs a title")
                self.tags[tag_uuid] = write.title.strip()
                verified.append(self.tags[tag_uuid])
                continue
            if isinstance(mutation, _TagMutation) and mutation.action == "reparent_tag":
                tag_uuid = tag_aliases.get(write.uuid, write.uuid)
                if tag_uuid not in self.tags:
                    raise ValueError("Tag does not exist")
                parents = [
                    tag_aliases.get(parent, parent)
                    for parent in (write.tag_parent_uuids or [])
                ]
                if tag_uuid in parents:
                    raise ValueError("A tag cannot be its own parent")
                if any(parent not in self.tags for parent in parents):
                    raise ValueError("Tag parent does not exist")
                self.tag_parents[tag_uuid] = parents
                verified.append(self.tags[tag_uuid])
                continue
            if isinstance(mutation, _TagMutation) and mutation.action == "delete_tag":
                tag_uuid = tag_aliases.get(write.uuid, write.uuid)
                title = self.tags.pop(tag_uuid, write.title or tag_uuid)
                self.tag_parents.pop(tag_uuid, None)
                for tagged_item in self.records.values():
                    tagged_item.tag_uuids = [
                        tag for tag in tagged_item.tag_uuids if tag != tag_uuid
                    ]
                for tag, parents in self.tag_parents.items():
                    self.tag_parents[tag] = [
                        parent for parent in parents if parent != tag_uuid
                    ]
                verified.append(title)
                continue
            if isinstance(mutation, _ChecklistMutation):
                parent, line = self._find_checklist(write.uuid)
                if write.checklist_remove:
                    if parent is not None:
                        parent.checklists = [item for item in parent.checklists if item.uuid != write.uuid]
                    verified.append(write.title or (line.title if line else write.uuid))
                    continue
                destination = self.records.get(write.checklist_parent_uuid or "") or parent
                if destination is None or destination.kind != "task":
                    raise ValueError("A checklist row needs a task parent")
                status = write.checklist_status or (line.status if line else "open")
                index = write.checklist_index if write.checklist_index is not None else write.sort_index
                if index is None:
                    index = (
                        line.sort_index
                        if line
                        else max(
                            (item.sort_index for item in destination.checklists),
                            default=-1,
                        )
                        + 1
                    )
                replacement = ChecklistLine(
                    uuid=write.uuid,
                    title=write.title if write.title is not None else (line.title if line else ""),
                    status=status,
                    sort_index=index,
                )
                if parent is not None and parent is not destination:
                    parent.checklists = [item for item in parent.checklists if item.uuid != write.uuid]
                destination.checklists = [item for item in destination.checklists if item.uuid != write.uuid]
                destination.checklists.append(replacement)
                destination.checklists.sort(key=lambda item: (item.sort_index, item.uuid))
                verified.append(replacement.title)
                continue
            if isinstance(mutation, _CreateMutation):
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
                    parent_uuid = None
                    area_uuid = None
                elif write.inbox:
                    if write.kind == "project":
                        raise ValueError("Projects cannot enter Inbox")
                    inbox = True
                    parent_uuid = None
                    area_uuid = None
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
                        else self.next_index(write)
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
                self.records[record.uuid] = record
                created[record.title] = record.id
                verified.append(record.title)
                continue
            item = self.records.get(write.uuid)
            if item is None:
                continue
            if (
                isinstance(mutation, _LifecycleMutation)
                and mutation.action == "complete"
            ):
                item.status = "done"
                item.completed_at = datetime.now(timezone.utc)
            elif (
                isinstance(mutation, _LifecycleMutation)
                and mutation.action == "cancel"
            ):
                item.status = "dropped"
                item.completed_at = datetime.now(timezone.utc)
            elif (
                isinstance(mutation, _LifecycleMutation)
                and mutation.action == "delete_area"
            ):
                item.trashed = True
                del self.records[item.uuid]
            elif isinstance(mutation, _LifecycleMutation) and mutation.action == "trash":
                item.trashed = True
            elif isinstance(mutation, _LifecycleMutation) and mutation.action == "restore":
                item.trashed = False
            elif (
                isinstance(mutation, _LifecycleMutation)
                and mutation.action == "permanent_delete"
            ):
                for child in self.records.values():
                    if child.parent_uuid == item.uuid:
                        child.parent_uuid = None
                    if child.area_uuid == item.uuid:
                        child.area_uuid = None
                    if child.heading_uuid == item.uuid:
                        child.heading_uuid = None
                del self.records[item.uuid]
            elif (
                isinstance(mutation, _RecurrenceMutation)
                and mutation.action == "repeat"
            ):
                assert write.recurrence_rule is not None
                item.recurrence = item.recurrence.fold_rule(write.recurrence_rule)
            elif (
                isinstance(mutation, _RecurrenceMutation)
                and mutation.action == "repeat_link"
            ):
                item.recurrence = item.recurrence.fold_links(
                    write.recurrence_links or []
                )
            elif (
                isinstance(mutation, _EditMutation)
                and mutation.action == "rename_area"
                and write.title
            ):
                item.title = write.title
            elif isinstance(mutation, _EditMutation) and mutation.action == "move":
                item.heading_uuid = write.heading_uuid
                if write.into_kind == "project":
                    item.parent_uuid = write.into_uuid
                    item.area_uuid = None
                    item.inbox = False
                elif write.into_kind == "area":
                    item.area_uuid = write.into_uuid
                    item.parent_uuid = None
                    item.inbox = False
                else:
                    if item.kind == "project":
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
            elif (
                isinstance(mutation, _EditMutation)
                and mutation.action == "tags"
                and write.tag_uuids is not None
            ):
                item.tag_uuids = list(write.tag_uuids)
            elif isinstance(mutation, _EditMutation) and mutation.action == "update":
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
                    self._apply_unchecked(
                        [
                            Write(
                                action="move",
                                uuid=item.uuid,
                                kind=item.kind,
                                into_uuid=write.into_uuid,
                                into_kind=write.into_kind,
                                inbox=write.inbox,
                                anytime=write.anytime,
                                heading_uuid=write.heading_uuid,
                            )
                        ]
                    )
            verified.append(item.title)
        for record in self.records.values():
            if record.recurrence.role != "instance" or not record.recurrence.template_uuid:
                continue
            template = self.records.get(record.recurrence.template_uuid)
            record.recurrence = record.recurrence.resolve_instance_type(
                template.recurrence.repeat_type if template else None
            )
        return ApplyResult(verified=list(dict.fromkeys(verified)), created=created)

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
