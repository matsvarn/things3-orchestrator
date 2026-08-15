"""Model-facing interface for the three Things tools."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    """Reject values that are not part of the model interface."""

    model_config = ConfigDict(extra="forbid", strict=True)


Kind = Literal["task", "project", "area", "heading"]
Status = Literal["open", "completed", "canceled"]
Next = Literal["done", "ask", "approve", "read", "retry_same", "stop"]
ResultStatus = Literal[
    "ok",
    "applied",
    "unchanged",
    "needs_input",
    "needs_approval",
    "stale",
    "pending",
    "partial",
    "rejected",
    "unsupported",
    "unavailable",
    "internal_error",
]
View = Literal["today", "inbox", "week", "system", "project", "logbook", "tags"]
RecurrenceKind = Literal[
    "none", "fixed_instance", "after_completion_instance", "template", "unknown"
]

_ITEM_ID = r"^(?:task|project|area|heading):[^\s:][^\s]*$"
_CONTAINER_ID = r"^(?:project|area):[^\s:][^\s]*$"
_CHECK_ID = r"^check:[^\s:][^\s]*$"
_TAG_ID = r"^tag:[^\s:][^\s]*$"
_LOCAL_KEY = r"^\$[A-Za-z][A-Za-z0-9_-]{0,79}$"
_TAG_REFERENCE = (
    r"^(?:\$[A-Za-z][A-Za-z0-9_-]{0,79}|tag:[^\s:][^\s]*)$"
)
_HOME_REFERENCE = (
    r"^(?:inbox|anytime|\$[A-Za-z][A-Za-z0-9_-]{0,79}|"
    r"(?:project|area):[^\s:][^\s]*)$"
)
_AFTER_REFERENCE = (
    r"^(?:\$[A-Za-z][A-Za-z0-9_-]{0,79}|"
    r"(?:task|project|area):[^\s:][^\s]*)$"
)
_CHECK_REFERENCE = (
    r"^(?:\$[A-Za-z][A-Za-z0-9_-]{0,79}|check:[^\s:][^\s]*)$"
)
_AREA_ID = r"^area:[^\s:][^\s]*$"
_HEADING_ID = r"^heading:[^\s:][^\s]*$"
_HEADING_REFERENCE = (
    r"^(?:\$[A-Za-z][A-Za-z0-9_-]{0,79}|heading:[^\s:][^\s]*)$"
)
_ORDER_MIN = -(2**63)
_ORDER_MAX = 2**63 - 1


def _validate_date(value: str | None, *, name: str) -> str | None:
    if value is None:
        return None
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO date") from error
    return value


def _validate_start(value: str | None) -> str | None:
    if value is None or value in {"today", "evening", "someday"}:
        return value
    return _validate_date(value, name="start")


def _validate_reminder(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("remind_at must be an ISO date-time") from error
    if parsed.utcoffset() is None:
        raise ValueError("remind_at needs a UTC offset")
    return value


def _duplicates(values: list[str]) -> bool:
    return len(values) != len(set(values))


class ReadCall(StrictModel):
    """Select one ordered Things read. An empty call selects Today."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        validate_by_alias=True,
        validate_by_name=False,
        serialize_by_alias=True,
    )

    view: View | None = None
    id: str | None = Field(default=None, pattern=_ITEM_ID, max_length=512)
    find: str | None = Field(default=None, min_length=1, max_length=500)
    within: str | None = Field(default=None, pattern=_CONTAINER_ID, max_length=512)
    from_date: str | None = Field(default=None, alias="from", max_length=10)
    to_date: str | None = Field(default=None, alias="to", max_length=10)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)
    limit: int = Field(default=20, ge=1, le=40)

    @field_validator("from_date")
    @classmethod
    def valid_from_date(cls, value: str | None) -> str | None:
        return _validate_date(value, name="from")

    @field_validator("to_date")
    @classmethod
    def valid_to_date(cls, value: str | None) -> str | None:
        return _validate_date(value, name="to")

    @model_validator(mode="after")
    def valid_selector(self) -> Self:
        if self.cursor is not None and any(
            value is not None
            for value in (
                self.view,
                self.id,
                self.find,
                self.within,
                self.from_date,
                self.to_date,
            )
        ):
            raise ValueError("cursor cannot combine with another selector")
        selectors = sum(value is not None for value in (self.view, self.id, self.find))
        if selectors > 1:
            raise ValueError("use only one of view, id, or find")
        if self.within is not None and self.find is None and self.view != "project":
            raise ValueError("within needs find or view project")
        if self.view == "project" and self.within is None:
            raise ValueError("view project needs within")
        has_range = self.from_date is not None or self.to_date is not None
        if has_range and self.view != "logbook":
            raise ValueError("from and to need view logbook")
        if self.view == "logbook" and (self.from_date is None or self.to_date is None):
            raise ValueError("view logbook needs from and to")
        if self.from_date is not None and self.to_date is not None:
            if self.from_date > self.to_date:
                raise ValueError("from must not be after to")
        return self


class CreateEntry(StrictModel):
    key: str | None = Field(default=None, pattern=_LOCAL_KEY)
    kind: Kind = "task"
    title: str = Field(min_length=1, max_length=1000)
    notes_markdown: str | None = Field(default=None, max_length=50_000)
    checklist: list[str] = Field(default_factory=list, max_length=100)
    next_actions: list[str] = Field(default_factory=list, max_length=20)
    into: str | None = Field(default=None, pattern=_HOME_REFERENCE, max_length=512)
    start: str | None = Field(default=None, max_length=32)
    deadline: str | None = Field(default=None, max_length=10)
    remind_at: str | None = Field(default=None, max_length=40)
    waiting: bool | None = None
    tag_ids: list[str] = Field(default_factory=list, max_length=20)
    after: str | None = Field(default=None, pattern=_AFTER_REFERENCE, max_length=512)
    today_after: str | None = Field(
        default=None, pattern=_AFTER_REFERENCE, max_length=512
    )
    heading_id: str | None = Field(
        default=None, pattern=_HEADING_REFERENCE, max_length=512
    )

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title cannot be blank")
        return value

    @field_validator("checklist", "next_actions")
    @classmethod
    def valid_checklist(cls, value: list[str]) -> list[str]:
        if any(not title.strip() for title in value):
            raise ValueError("titles cannot be blank")
        return value

    @field_validator("tag_ids")
    @classmethod
    def valid_tag_ids(cls, value: list[str]) -> list[str]:
        if any(re.fullmatch(_TAG_REFERENCE, item) is None for item in value):
            raise ValueError("tag_ids need exact tag IDs or local tag keys")
        if _duplicates(value):
            raise ValueError("tag_ids cannot contain duplicates")
        return value

    @field_validator("start")
    @classmethod
    def valid_start(cls, value: str | None) -> str | None:
        return _validate_start(value)

    @field_validator("deadline")
    @classmethod
    def valid_deadline(cls, value: str | None) -> str | None:
        return _validate_date(value, name="deadline")

    @field_validator("remind_at")
    @classmethod
    def valid_remind_at(cls, value: str | None) -> str | None:
        return _validate_reminder(value)

    @model_validator(mode="after")
    def valid_kind_fields(self) -> Self:
        if self.kind == "heading":
            allowed = {"key", "kind", "title", "into"}
            if self.into is None:
                raise ValueError("a heading needs a Project")
            if not self.into.startswith(("project:", "$")):
                raise ValueError("a heading needs a Project")
            if self.model_fields_set - allowed:
                raise ValueError("a heading accepts only key, kind, title, and into")
            return self
        if self.checklist and self.kind != "task":
            raise ValueError("only a task can have a checklist")
        if self.next_actions and self.kind != "project":
            raise ValueError("only a Project can have next_actions")
        if self.heading_id is not None and self.kind != "task":
            raise ValueError("only a Task can use a heading")
        if self.into in {"inbox", "anytime"} and (
            self.start is not None or self.remind_at is not None
        ):
            raise ValueError("Inbox or Anytime cannot combine with a schedule")
        return self


class ChecklistAdd(StrictModel):
    key: str | None = Field(default=None, pattern=_LOCAL_KEY)
    title: str = Field(min_length=1, max_length=1000)
    after: str | None = Field(default=None, pattern=_CHECK_REFERENCE, max_length=512)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("checklist title cannot be blank")
        return value


class ChecklistChange(StrictModel):
    id: str = Field(pattern=_CHECK_ID, max_length=512)
    title: str | None = Field(default=None, min_length=1, max_length=1000)
    status: Status | None = None
    after: str | None = Field(default=None, pattern=_CHECK_REFERENCE, max_length=512)

    @model_validator(mode="after")
    def has_change(self) -> Self:
        if (
            self.title is None
            and self.status is None
            and "after" not in self.model_fields_set
        ):
            raise ValueError("checklist_change needs a change")
        if self.title is not None and not self.title.strip():
            raise ValueError("checklist title cannot be blank")
        return self


class ChangeEntry(StrictModel):
    id: str = Field(pattern=_ITEM_ID, max_length=512)
    if_revision: str = Field(min_length=1, max_length=512)
    title: str | None = Field(default=None, min_length=1, max_length=1000)
    status: Status | None = None
    notes_markdown: str | None = Field(default=None, max_length=50_000)
    checklist_add: list[ChecklistAdd] = Field(default_factory=list, max_length=500)
    checklist_change: list[ChecklistChange] = Field(default_factory=list, max_length=500)
    checklist_remove: list[str] = Field(default_factory=list, max_length=500)
    checklist_order: list[str] | None = Field(default=None, max_length=500)
    into: str | None = Field(default=None, pattern=_HOME_REFERENCE, max_length=512)
    start: str | None = Field(default=None, max_length=32)
    deadline: str | None = Field(default=None, max_length=10)
    remind_at: str | None = Field(default=None, max_length=40)
    waiting: bool | None = None
    tags_add: list[str] = Field(default_factory=list, max_length=20)
    tags_remove: list[str] = Field(default_factory=list, max_length=20)
    after: str | None = Field(default=None, pattern=_AFTER_REFERENCE, max_length=512)
    today_after: str | None = Field(
        default=None, pattern=_AFTER_REFERENCE, max_length=512
    )
    move_contents_to: str | None = Field(default=None, pattern=_AREA_ID, max_length=512)
    remove_if_empty: Literal[True] | None = None
    trash: Literal[True] | None = None
    heading_id: str | None = Field(default=None, pattern=_HEADING_ID, max_length=512)
    repeat_interval: int | None = Field(default=None, ge=1, le=366)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("title cannot be blank")
        return value

    @field_validator("start")
    @classmethod
    def valid_start(cls, value: str | None) -> str | None:
        return _validate_start(value)

    @field_validator("deadline")
    @classmethod
    def valid_deadline(cls, value: str | None) -> str | None:
        return _validate_date(value, name="deadline")

    @field_validator("remind_at")
    @classmethod
    def valid_remind_at(cls, value: str | None) -> str | None:
        return _validate_reminder(value)

    @field_validator("checklist_remove")
    @classmethod
    def valid_checklist_remove(cls, value: list[str]) -> list[str]:
        if any(re.fullmatch(_CHECK_ID, item) is None for item in value):
            raise ValueError("checklist_remove needs exact checklist IDs")
        if _duplicates(value):
            raise ValueError("checklist_remove cannot contain duplicates")
        return value

    @field_validator("checklist_order")
    @classmethod
    def valid_checklist_order(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if any(re.fullmatch(_CHECK_REFERENCE, item) is None for item in value):
            raise ValueError("checklist_order needs exact or local checklist IDs")
        if _duplicates(value):
            raise ValueError("checklist_order cannot contain duplicates")
        return value

    @field_validator("tags_add")
    @classmethod
    def valid_tag_add(cls, value: list[str]) -> list[str]:
        if any(re.fullmatch(_TAG_REFERENCE, item) is None for item in value):
            raise ValueError("tags_add needs exact tag IDs or local tag keys")
        if _duplicates(value):
            raise ValueError("tags_add cannot contain duplicates")
        return value

    @field_validator("tags_remove")
    @classmethod
    def valid_tag_remove(cls, value: list[str]) -> list[str]:
        if any(re.fullmatch(_TAG_ID, item) is None for item in value):
            raise ValueError("tags_remove needs exact tag IDs")
        if _duplicates(value):
            raise ValueError("tags_remove cannot contain duplicates")
        return value

    @model_validator(mode="after")
    def valid_change(self) -> Self:
        meaningful = any(
            (
                self.title is not None,
                self.status is not None,
                "notes_markdown" in self.model_fields_set,
                bool(self.checklist_add),
                bool(self.checklist_change),
                bool(self.checklist_remove),
                self.checklist_order is not None and bool(self.checklist_order),
                "into" in self.model_fields_set,
                "start" in self.model_fields_set,
                "deadline" in self.model_fields_set,
                "remind_at" in self.model_fields_set,
                self.waiting is not None,
                bool(self.tags_add),
                bool(self.tags_remove),
                "after" in self.model_fields_set,
                "today_after" in self.model_fields_set,
                self.move_contents_to is not None,
                self.remove_if_empty is True,
                self.trash is True,
                "heading_id" in self.model_fields_set,
                self.repeat_interval is not None,
            )
        )
        if not meaningful:
            raise ValueError("an existing item needs a change")
        changed_rows = [row.id for row in self.checklist_change]
        if _duplicates(changed_rows):
            raise ValueError("each checklist row can change once")
        removed = set(self.checklist_remove)
        if removed.intersection(changed_rows):
            raise ValueError("a checklist row cannot change and be removed")
        if self.checklist_order is not None and removed.intersection(self.checklist_order):
            raise ValueError("checklist_order cannot contain a removed row")
        if self.checklist_order is not None and any(
            "after" in row.model_fields_set for row in self.checklist_change
        ):
            raise ValueError("use checklist_order or checklist_change.after, not both")
        if set(self.tags_add).intersection(self.tags_remove):
            raise ValueError("a tag cannot be added and removed")
        if self.into in {"inbox", "anytime"} and (
            self.start is not None or self.remind_at is not None
        ):
            raise ValueError("Inbox or Anytime cannot combine with a schedule")
        if self.move_contents_to is not None or self.remove_if_empty:
            allowed = {
                "id",
                "if_revision",
                "move_contents_to",
                "remove_if_empty",
            }
            if self.model_fields_set - allowed:
                raise ValueError(
                    "an Area removal cannot combine with source-item changes"
                )
        if self.trash:
            allowed = {"id", "if_revision", "trash"}
            if self.model_fields_set - allowed:
                raise ValueError("Trash cannot combine with other changes")
        if self.id.startswith("heading:"):
            allowed = {"id", "if_revision", "title"}
            if self.model_fields_set - allowed or self.title is None:
                raise ValueError("a heading change can only rename the heading")
        if self.repeat_interval is not None:
            allowed = {"id", "if_revision", "repeat_interval"}
            if self.model_fields_set - allowed:
                raise ValueError("a repeat interval cannot combine with other changes")
        if "heading_id" in self.model_fields_set and "into" in self.model_fields_set:
            raise ValueError("change the Project or heading in separate requests")
        return self


class EnsureTag(StrictModel):
    key: str = Field(pattern=_LOCAL_KEY)
    title: str = Field(min_length=1, max_length=1000)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tag title cannot be blank")
        return value.strip()


class CommitCall(StrictModel):
    intent_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{8,120}$")
    scope_revision: str | None = Field(default=None, min_length=1, max_length=512)
    ensure_tags: list[EnsureTag] = Field(default_factory=list, max_length=20)
    create: list[CreateEntry] = Field(default_factory=list, max_length=40)
    change: list[ChangeEntry] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def valid_commit(self) -> Self:
        ids = [entry.id for entry in self.change]
        if _duplicates(ids):
            raise ValueError("each existing item can change once")

        changes_areas = any(entry.kind == "area" for entry in self.create) or any(
            entry.id.startswith("area:") for entry in self.change
        )
        if changes_areas and self.scope_revision is None:
            raise ValueError("Area changes need a system scope_revision")

        keys = [entry.key for entry in self.ensure_tags]
        keys.extend(entry.key for entry in self.create if entry.key is not None)
        keys.extend(
            row.key
            for entry in self.change
            for row in entry.checklist_add
            if row.key is not None
        )
        if _duplicates(keys):
            raise ValueError("local keys must be unique")

        tag_titles = [entry.title.strip().casefold() for entry in self.ensure_tags]
        if _duplicates(tag_titles):
            raise ValueError("ensure_tags titles must be unique")

        known = set(keys)
        refs: list[str | None] = []
        for created in self.create:
            refs.extend(
                (created.into, created.after, created.today_after, created.heading_id)
            )
        for changed in self.change:
            refs.extend(
                (
                    changed.into,
                    changed.after,
                    changed.today_after,
                    changed.move_contents_to,
                )
            )
            refs.extend(row.after for row in changed.checklist_add)
            refs.extend(row.after for row in changed.checklist_change)
            refs.extend(changed.checklist_order or [])
        unknown = sorted({ref for ref in refs if ref and ref.startswith("$")} - known)
        if unknown:
            raise ValueError(f"unknown local keys: {', '.join(unknown)}")

        tag_keys = {entry.key for entry in self.ensure_tags}
        tag_refs = [
            reference
            for entry in self.create
            for reference in entry.tag_ids
            if reference.startswith("$")
        ]
        tag_refs.extend(
            reference
            for entry in self.change
            for reference in entry.tags_add
            if reference.startswith("$")
        )
        invalid_tag_refs = sorted(set(tag_refs) - tag_keys)
        if invalid_tag_refs:
            raise ValueError(
                "local tag references need ensure_tags keys: "
                + ", ".join(invalid_tag_refs)
            )

        create_by_key = {
            entry.key: entry for entry in self.create if entry.key is not None
        }
        create_keys = set(create_by_key)

        def create_scope(entry: CreateEntry) -> tuple[str, str | None]:
            if entry.kind == "area":
                return "areas", None
            home = entry.into
            if home is not None and home.startswith("$"):
                target = create_by_key.get(home)
                if target is not None:
                    return target.kind, home
            if home is not None and home.startswith("project:"):
                return "project", home
            if home is not None and home.startswith("area:"):
                return "area", home
            if entry.kind == "project":
                return "projects", None
            if home is None and (
                entry.start is not None or entry.remind_at is not None
            ):
                return "root", None
            return ("root", None) if home == "anytime" else ("inbox", None)

        seen_create: set[str] = set()
        for entry in self.create:
            for anchor in (entry.after, entry.today_after):
                if anchor is not None and anchor.startswith("$") and anchor not in seen_create:
                    raise ValueError("local after anchors must be earlier create entries")
            if entry.into is not None and entry.into.startswith("$"):
                target = create_by_key.get(entry.into)
                if target is None or target.kind not in {"area", "project"}:
                    raise ValueError("a local home must be an Area or Project")
                if entry.kind == "project" and target.kind != "area":
                    raise ValueError("a Project local home must be an Area")
                if entry.kind == "heading" and target.kind != "project":
                    raise ValueError("a heading local home must be a Project")
            if entry.heading_id is not None and entry.heading_id.startswith("$"):
                target = create_by_key.get(entry.heading_id)
                if (
                    target is None
                    or target.kind != "heading"
                    or entry.heading_id not in seen_create
                ):
                    raise ValueError(
                        "a local heading must be an earlier heading create entry"
                    )
            if entry.after is not None and entry.after.startswith("$"):
                anchor_entry = create_by_key[entry.after]
                if (
                    anchor_entry.kind != entry.kind
                    or create_scope(anchor_entry) != create_scope(entry)
                ):
                    raise ValueError("a local after anchor must be in the same list")
            if entry.key is not None:
                seen_create.add(entry.key)

        for changed in self.change:
            for anchor in (changed.after, changed.today_after):
                if anchor is not None and anchor.startswith("$") and anchor not in create_keys:
                    raise ValueError("an item after anchor must be a created item")
            if changed.into is not None and changed.into.startswith("$"):
                target = create_by_key.get(changed.into)
                if target is None or target.kind not in {"area", "project"}:
                    raise ValueError("a local home must be a created Area or Project")
            row_keys = {
                row.key for row in changed.checklist_add if row.key is not None
            }
            seen_rows: set[str] = set()
            for row in changed.checklist_add:
                if row.after is not None and row.after.startswith("$") and row.after not in seen_rows:
                    raise ValueError("local checklist after anchors must be earlier rows")
                if row.key is not None:
                    seen_rows.add(row.key)
            row_refs = [row.after for row in changed.checklist_change]
            row_refs.extend(changed.checklist_order or [])
            if any(
                ref is not None and ref.startswith("$") and ref not in row_keys
                for ref in row_refs
            ):
                raise ValueError("local checklist references must belong to the same item")
        return self


class ApproveCall(StrictModel):
    plan_id: str = Field(pattern=r"^plan_[A-Za-z0-9_-]{8,120}$")


class TagFact(StrictModel):
    id: str = Field(pattern=_TAG_ID, max_length=512)
    title: str = Field(min_length=1, max_length=1000)
    from_id: str | None = Field(default=None, pattern=_ITEM_ID, max_length=512)


class ChecklistFact(StrictModel):
    id: str = Field(pattern=_CHECK_ID, max_length=512)
    revision: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=1000)
    status: Status
    order: int = Field(ge=_ORDER_MIN, le=_ORDER_MAX)


class RecurrenceFact(StrictModel):
    kind: RecurrenceKind
    template_id: str | None = Field(default=None, pattern=_ITEM_ID, max_length=512)
    rule: str | None = Field(default=None, max_length=2000)
    unit: Literal["day", "week", "month", "year"] | None = None
    interval: int | None = Field(default=None, ge=1, le=366)


class ItemFact(StrictModel):
    id: str = Field(pattern=_ITEM_ID, max_length=512)
    revision: str = Field(min_length=1, max_length=512)
    kind: Kind
    title: str = Field(min_length=1, max_length=1000)
    status: Status
    into_id: str | None = Field(default=None, pattern=_ITEM_ID, max_length=512)
    notes_markdown: str | None = Field(default=None, max_length=50_000)
    checklist: list[ChecklistFact] = Field(default_factory=list, max_length=100)
    direct_tags: list[TagFact] = Field(default_factory=list, max_length=40)
    inherited_tags: list[TagFact] = Field(default_factory=list, max_length=40)
    start: str | None = Field(default=None, max_length=32)
    deadline: str | None = Field(default=None, max_length=10)
    remind_at: str | None = Field(default=None, max_length=40)
    recurrence: RecurrenceFact | None = None
    order: int = Field(ge=_ORDER_MIN, le=_ORDER_MAX)
    today_order: int | None = Field(default=None, ge=_ORDER_MIN, le=_ORDER_MAX)
    signals: list[str] = Field(default_factory=list, max_length=20)


class ReviewSection(StrictModel):
    key: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    item_ids: list[str] = Field(default_factory=list, max_length=40)
    signals: list[str] = Field(default_factory=list, max_length=40)

    @field_validator("signals")
    @classmethod
    def bounded_signals(cls, value: list[str]) -> list[str]:
        if any(len(signal) > 1600 for signal in value):
            raise ValueError("section signals must be 1600 characters or less")
        return value

    @field_validator("item_ids")
    @classmethod
    def valid_item_ids(cls, value: list[str]) -> list[str]:
        if any(re.fullmatch(_ITEM_ID, item) is None for item in value):
            raise ValueError("item_ids need exact item IDs")
        if _duplicates(value):
            raise ValueError("item_ids cannot contain duplicates")
        return value


class PlanFact(StrictModel):
    id: str = Field(pattern=r"^plan_[A-Za-z0-9_-]{8,120}$")
    expires_at: str = Field(max_length=40)
    summary: list[str] = Field(min_length=1, max_length=40)
    preserves: list[str] = Field(default_factory=list, max_length=40)
    warnings: list[str] = Field(default_factory=list, max_length=40)

    @field_validator("expires_at")
    @classmethod
    def valid_expiry(cls, value: str) -> str:
        checked = _validate_reminder(value)
        assert checked is not None
        return checked


class Result(StrictModel):
    next: Next
    status: ResultStatus
    instruction: str = Field(min_length=1, max_length=1000)
    items: list[ItemFact] = Field(default_factory=list, max_length=40)
    tags: list[TagFact] = Field(default_factory=list, max_length=40)
    sections: list[ReviewSection] = Field(default_factory=list, max_length=20)
    signals: list[str] = Field(default_factory=list, max_length=40)
    plan: PlanFact | None = None
    receipt: str | None = Field(default=None, min_length=1, max_length=512)
    scope_revision: str | None = Field(default=None, min_length=1, max_length=512)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)
    truncated: bool = False


_STRING: dict[str, Any] = {"type": "string", "minLength": 1, "maxLength": 512}
_NULLABLE_STRING: dict[str, Any] = {"type": ["string", "null"], "maxLength": 512}
_EXACT_ITEM: dict[str, Any] = {**_STRING, "pattern": _ITEM_ID}
_EXACT_TAG: dict[str, Any] = {**_STRING, "pattern": _TAG_ID}
_TAG_REFERENCE_SCHEMA: dict[str, Any] = {
    **_STRING,
    "pattern": _TAG_REFERENCE,
}
_EXACT_CHECK: dict[str, Any] = {**_STRING, "pattern": _CHECK_ID}
_HOME_SCHEMA: dict[str, Any] = {**_NULLABLE_STRING, "pattern": _HOME_REFERENCE}
_AFTER_SCHEMA: dict[str, Any] = {**_NULLABLE_STRING, "pattern": _AFTER_REFERENCE}
_CHECK_REFERENCE_SCHEMA: dict[str, Any] = {
    **_NULLABLE_STRING,
    "pattern": _CHECK_REFERENCE,
}
_AREA_SCHEMA: dict[str, Any] = {**_STRING, "pattern": _AREA_ID}
_DATE: dict[str, Any] = {"type": ["string", "null"], "format": "date"}
_DATE_TIME: dict[str, Any] = {"type": ["string", "null"], "format": "date-time"}

READ_IN: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "view": {"enum": ["today", "inbox", "week", "system", "project", "logbook", "tags"]},
        "id": _EXACT_ITEM,
        "find": {"type": "string", "minLength": 1, "maxLength": 500},
        "within": {**_STRING, "pattern": _CONTAINER_ID},
        "from": {"type": "string", "format": "date"},
        "to": {"type": "string", "format": "date"},
        "cursor": _STRING,
        "limit": {"type": "integer", "minimum": 1, "maximum": 40, "default": 20},
    },
}

_CHECKLIST_ADD: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title"],
    "properties": {
        "key": {"type": "string", "pattern": _LOCAL_KEY},
        "title": {"type": "string"},
        "after": _CHECK_REFERENCE_SCHEMA,
    },
}

_CHECKLIST_CHANGE: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id"],
    "properties": {
        "id": _EXACT_CHECK,
        "title": {"type": "string"},
        "status": {"enum": ["open", "completed", "canceled"]},
        "after": _CHECK_REFERENCE_SCHEMA,
    },
}

_CREATE: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title"],
    "properties": {
        "key": {"type": "string", "pattern": _LOCAL_KEY},
        "kind": {"enum": ["task", "project", "area", "heading"], "default": "task"},
        "title": {"type": "string", "minLength": 1, "maxLength": 1000},
        "notes_markdown": {"type": ["string", "null"], "maxLength": 50000},
        "checklist": {
            "type": "array",
            "maxItems": 100,
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
        "next_actions": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
        "into": _HOME_SCHEMA,
        "start": {"type": ["string", "null"], "maxLength": 32},
        "deadline": _DATE,
        "remind_at": _DATE_TIME,
        "waiting": {"type": ["boolean", "null"]},
        "tag_ids": {
            "type": "array",
            "maxItems": 20,
            "uniqueItems": True,
            "items": _TAG_REFERENCE_SCHEMA,
        },
        "after": _AFTER_SCHEMA,
        "today_after": _AFTER_SCHEMA,
        "heading_id": {
            "type": "string",
            "pattern": _HEADING_REFERENCE,
            "maxLength": 512,
        },
    },
}

_CHANGE: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "if_revision"],
    "properties": {
        "id": _EXACT_ITEM,
        "if_revision": _STRING,
        "title": {"type": "string", "minLength": 1, "maxLength": 1000},
        "status": {"enum": ["open", "completed", "canceled"]},
        "notes_markdown": {"type": ["string", "null"], "maxLength": 50000},
        "checklist_add": {"type": "array", "minItems": 1, "maxItems": 500, "items": _CHECKLIST_ADD},
        "checklist_change": {"type": "array", "minItems": 1, "maxItems": 500, "items": _CHECKLIST_CHANGE},
        "checklist_remove": {"type": "array", "minItems": 1, "maxItems": 500, "items": _EXACT_CHECK},
        "checklist_order": {
            "type": ["array", "null"],
            "minItems": 1,
            "maxItems": 500,
            "items": {**_STRING, "pattern": _CHECK_REFERENCE},
        },
        "into": _HOME_SCHEMA,
        "start": {"type": ["string", "null"], "maxLength": 32},
        "deadline": _DATE,
        "remind_at": _DATE_TIME,
        "waiting": {"type": "boolean"},
        "tags_add": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "uniqueItems": True,
            "items": _TAG_REFERENCE_SCHEMA,
        },
        "tags_remove": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "uniqueItems": True,
            "items": _EXACT_TAG,
        },
        "after": _AFTER_SCHEMA,
        "today_after": _AFTER_SCHEMA,
        "move_contents_to": _AREA_SCHEMA,
        "remove_if_empty": {"const": True},
        "trash": {"const": True},
        "heading_id": {"type": ["string", "null"], "pattern": _HEADING_ID},
        "repeat_interval": {"type": "integer", "minimum": 1, "maximum": 366},
    },
}

_ENSURE_TAG: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["key", "title"],
    "properties": {
        "key": {"type": "string", "pattern": _LOCAL_KEY},
        "title": {"type": "string", "minLength": 1, "maxLength": 1000},
    },
}

COMMIT_IN: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intent_id"],
    "properties": {
        "intent_id": {"type": "string", "pattern": r"^[A-Za-z0-9._:-]{8,120}$"},
        "scope_revision": _STRING,
        "ensure_tags": {"type": "array", "maxItems": 20, "items": _ENSURE_TAG},
        "create": {"type": "array", "maxItems": 40, "items": _CREATE},
        "change": {"type": "array", "maxItems": 100, "items": _CHANGE},
    },
}

APPROVE_IN: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["plan_id"],
    "properties": {
        "plan_id": {"type": "string", "pattern": r"^plan_[A-Za-z0-9_-]{8,120}$"},
    },
}

_TAG_FACT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "title"],
    "properties": {
        "id": _EXACT_TAG,
        "title": {"type": "string", "minLength": 1, "maxLength": 1000},
        "from_id": _EXACT_ITEM,
    },
}

_CHECKLIST_FACT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "revision", "title", "status", "order"],
    "properties": {
        "id": _EXACT_CHECK,
        "revision": _STRING,
        "title": {"type": "string", "minLength": 1, "maxLength": 1000},
        "status": {"enum": ["open", "completed", "canceled"]},
        "order": {"type": "integer"},
    },
}

_RECURRENCE: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind"],
    "properties": {
        "kind": {
            "enum": ["none", "fixed_instance", "after_completion_instance", "template", "unknown"]
        },
        "template_id": _EXACT_ITEM,
        "rule": {"type": "string"},
        "unit": {"enum": ["day", "week", "month", "year"]},
        "interval": {"type": "integer", "minimum": 1, "maximum": 366},
    },
}

_ITEM_FACT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "revision", "kind", "title", "status"],
    "properties": {
        "id": _EXACT_ITEM,
        "revision": _STRING,
        "kind": {"enum": ["task", "project", "area", "heading"]},
        "title": {"type": "string", "minLength": 1, "maxLength": 1000},
        "status": {"enum": ["open", "completed", "canceled"]},
        "into_id": _EXACT_ITEM,
        "notes_markdown": {"type": ["string", "null"]},
        "checklist": {"type": "array", "maxItems": 100, "items": _CHECKLIST_FACT},
        "direct_tags": {"type": "array", "maxItems": 40, "items": _TAG_FACT},
        "inherited_tags": {"type": "array", "maxItems": 40, "items": _TAG_FACT},
        "start": {"type": ["string", "null"]},
        "deadline": _DATE,
        "remind_at": _DATE_TIME,
        "recurrence": _RECURRENCE,
        "order": {"type": "integer"},
        "today_order": {"type": "integer"},
        "signals": {
            "type": "array",
            "maxItems": 40,
            "items": {"type": "string", "maxLength": 1600},
        },
    },
}

_SECTION: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["key", "title"],
    "properties": {
        "key": {"type": "string"},
        "title": {"type": "string"},
        "item_ids": {
            "type": "array",
            "maxItems": 40,
            "uniqueItems": True,
            "items": _EXACT_ITEM,
        },
        "signals": {"type": "array", "maxItems": 20, "items": {"type": "string"}},
    },
}

_PLAN: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "expires_at", "summary"],
    "properties": {
        "id": APPROVE_IN["properties"]["plan_id"],
        "expires_at": {"type": "string", "format": "date-time"},
        "summary": {"type": "array", "minItems": 1, "maxItems": 40, "items": {"type": "string"}},
        "preserves": {"type": "array", "maxItems": 40, "items": {"type": "string"}},
        "warnings": {"type": "array", "maxItems": 40, "items": {"type": "string"}},
    },
}

RESULT_OUT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["next", "status", "instruction"],
    "properties": {
        "next": {"enum": ["done", "ask", "approve", "read", "retry_same", "stop"]},
        "status": {
            "enum": [
                "ok",
                "applied",
                "unchanged",
                "needs_input",
                "needs_approval",
                "stale",
                "pending",
                "partial",
                "rejected",
                "unsupported",
                "unavailable",
                "internal_error",
            ]
        },
        "instruction": {"type": "string", "minLength": 1, "maxLength": 1000},
        "items": {"type": "array", "maxItems": 40, "items": _ITEM_FACT},
        "tags": {"type": "array", "maxItems": 40, "items": _TAG_FACT},
        "sections": {"type": "array", "maxItems": 20, "items": _SECTION},
        "signals": {"type": "array", "maxItems": 40, "items": {"type": "string"}},
        "plan": _PLAN,
        "receipt": _STRING,
        "scope_revision": _STRING,
        "cursor": _STRING,
        "truncated": {"type": "boolean"},
    },
}

_ITEM_SUMMARY: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "revision", "kind", "title", "status"],
    "properties": {
        "id": _EXACT_ITEM,
        "revision": _STRING,
        "kind": {"enum": ["task", "project", "area", "heading"]},
        "title": {"type": "string", "minLength": 1, "maxLength": 1000},
        "status": {"enum": ["open", "completed", "canceled"]},
    },
}
_CONTROL_PROPERTIES: dict[str, Any] = {
    "next": RESULT_OUT["properties"]["next"],
    "status": RESULT_OUT["properties"]["status"],
    "instruction": RESULT_OUT["properties"]["instruction"],
}
_SUMMARY_ITEMS: dict[str, Any] = {
    "type": "array",
    "maxItems": 40,
    "items": _ITEM_SUMMARY,
}
_READ_ITEMS: dict[str, Any] = {
    "type": "array",
    "maxItems": 40,
    "items": {**_ITEM_SUMMARY, "additionalProperties": True},
}

READ_OUT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["next", "status", "instruction"],
    "properties": {
        **_CONTROL_PROPERTIES,
        "items": _READ_ITEMS,
        "tags": RESULT_OUT["properties"]["tags"],
        "sections": {
            "type": "array",
            "maxItems": 20,
            "items": _SECTION,
        },
        "signals": RESULT_OUT["properties"]["signals"],
        "scope_revision": _STRING,
        "cursor": _STRING,
        "truncated": {"type": "boolean"},
    },
}

COMMIT_OUT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["next", "status", "instruction"],
    "properties": {
        **_CONTROL_PROPERTIES,
        "items": _SUMMARY_ITEMS,
        "tags": RESULT_OUT["properties"]["tags"],
        "signals": RESULT_OUT["properties"]["signals"],
        "plan": _PLAN,
        "receipt": _STRING,
    },
}

APPROVE_OUT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["next", "status", "instruction"],
    "properties": {
        **_CONTROL_PROPERTIES,
        "items": _SUMMARY_ITEMS,
        "tags": RESULT_OUT["properties"]["tags"],
        "signals": RESULT_OUT["properties"]["signals"],
        "receipt": _STRING,
    },
}

READ_DESC = (
    "Read Things. Empty input reads Today. Select one view, exact id, or find query. "
    "Project needs within. Logbook needs from and to. Send a cursor without selectors. "
    "Exact reads add notes_markdown, checklist, direct_tags, inherited_tags, start, deadline, "
    "remind_at, recurrence, order, today_order, and signals. "
    "Use returned IDs and revisions for changes. Follow next and instruction."
)
COMMIT_DESC = (
    "Commit decided work with a durable intent_id, optional ensure_tags, and create or change rows. "
    "An ensured tag key can be used in tag_ids or tags_add in the same commit. "
    "Changes need an exact id and if_revision. After pending, retry the exact payload. "
    "Moving a Task or Project to Trash and other high-impact work returns a plan without writes. "
    "Ask one natural confirmation and keep its "
    "control fields private. Follow next and instruction."
)
APPROVE_DESC = (
    "After clear owner confirmation, apply the exact returned plan. Send only plan_id and keep "
    "that ID private. "
    "If it is stale, read and prepare again. Follow next and instruction."
)
