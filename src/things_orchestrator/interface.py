"""Model-facing interface for the three Things tools."""

from __future__ import annotations

import re
from collections.abc import Hashable, Sequence
from datetime import date, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    """Reject values that are not part of the model interface."""

    model_config = ConfigDict(extra="forbid", strict=True)


Kind = Literal["task", "project", "area", "heading"]
Status = Literal["open", "completed", "canceled"]
TruncatedField = Literal["notes", "checklist", "tags", "recurrence"]
DetailField = Literal["notes", "checklist", "tags", "recurrence"]
DETAIL_FIELDS: tuple[DetailField, ...] = ("notes", "checklist", "tags", "recurrence")
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
View = Literal[
    "today",
    "inbox",
    "week",
    "system",
    "project",
    "area",
    "audit",
    "diagnostics",
    "logbook",
    "trash",
    "tags",
]
INCLUDE_LIMIT = 40
BULK_ID_LIMIT = 10
START_PATTERN = r"^(today|evening|someday|[0-9]{4}-[0-9]{2}-[0-9]{2})$"
Purpose = Literal["review", "change", "organize", "recurrence"]
RecurrenceKind = Literal[
    "none", "fixed_instance", "after_completion_instance", "template", "unknown"
]

_ITEM_ID = r"^(task|project|area|heading):[^\s:]+$"
_DIAGNOSTIC_ID = r"^(task|project|area|heading|tag):[^\s:]+$"
_CONTAINER_ID = r"^(project|area):[^\s:]+$"
_CHECK_ID = r"^check:[^\s:]+$"
_TAG_ID = r"^tag:[^\s:]+$"
_LOCAL_KEY = r"^\$[A-Za-z][A-Za-z0-9_-]{0,79}$"
_TAG_REFERENCE = r"^(\$[A-Za-z][A-Za-z0-9_-]{0,79}|tag:[^\s:]\S*)$"
_HOME_REFERENCE = (
    r"^(inbox|anytime|\$[A-Za-z][A-Za-z0-9_-]{0,79}|"
    r"(project|area):[^\s:]+)$"
)
_CONTEXT_HOME_REFERENCE = (
    r"^(inbox|anytime|\$[A-Za-z][A-Za-z0-9_-]{0,79}|"
    r"(project|area):[^\s:]+|[a-z][a-z0-9]{0,11})$"
)
_CONTEXT_AFTER_REFERENCE = (
    r"^(\$[A-Za-z][A-Za-z0-9_-]{0,79}|"
    r"(task|project|area|heading):[^\s:]+|[a-z][a-z0-9]{0,11})$"
)
_CONTEXT_AREA_REFERENCE = (
    r"^(\$[A-Za-z][A-Za-z0-9_-]{0,79}|"
    r"area:[^\s:]+|[a-z][a-z0-9]{0,11})$"
)
_CONTEXT_HEADING_REFERENCE = (
    r"^(\$[A-Za-z][A-Za-z0-9_-]{0,79}|"
    r"heading:[^\s:]+|[a-z][a-z0-9]{0,11})$"
)
_AFTER_REFERENCE = (
    r"^(\$[A-Za-z][A-Za-z0-9_-]{0,79}|"
    r"(task|project|area|heading):[^\s:]+)$"
)
_CHECK_REFERENCE = r"^(\$[A-Za-z][A-Za-z0-9_-]{0,79}|check:[^\s:]\S*)$"
_AREA_ID = r"^area:[^\s:]+$"
_HEADING_ID = r"^heading:[^\s:]+$"
_HEADING_REFERENCE = r"^(\$[A-Za-z][A-Za-z0-9_-]{0,79}|heading:[^\s:]+)$"
_ORDER_MIN = -(2**63)
_ORDER_MAX = 2**63 - 1
_CONTEXT_ID = r"^ctx_[A-Za-z0-9_-]{8,120}$"
_SHORT_REF = r"^[a-z][a-z0-9]{0,11}$"
_SHORT_OR_LOCAL_REF = r"^([a-z][a-z0-9]{0,11}|\$[A-Za-z][A-Za-z0-9_-]{0,79})$"


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


def _duplicates(values: Sequence[Hashable]) -> bool:
    return len(values) != len(set(values))


def _reject_cleared_start_with_reminder(
    fields_set: set[str], start: str | None, remind_at: str | None
) -> None:
    if "start" in fields_set and start is None and remind_at is not None:
        raise ValueError("start=null cannot combine with remind_at")


class ReadInclude(StrictModel):
    """One bounded item lookup to add to a change context."""

    id: str | None = Field(default=None, pattern=_ITEM_ID, max_length=512)
    find: str | None = Field(default=None, min_length=1, max_length=500)
    within: str | None = Field(default=None, pattern=_CONTAINER_ID, max_length=512)

    @model_validator(mode="after")
    def valid_include(self) -> Self:
        if (self.id is None) == (self.find is None):
            raise ValueError("include needs exactly one id or find")
        if self.within is not None and self.find is None:
            raise ValueError("include within needs find")
        return self


class ReadCall(StrictModel):
    """Select one ordered Things read. An empty call selects Today."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        validate_by_alias=True,
        validate_by_name=False,
        serialize_by_alias=True,
    )

    purpose: Purpose = "review"
    view: View | None = None
    id: str | None = Field(default=None, pattern=_ITEM_ID, max_length=512)
    find: str | None = Field(default=None, min_length=1, max_length=500)
    within: str | None = Field(default=None, pattern=_CONTAINER_ID, max_length=512)
    from_date: str | None = Field(default=None, alias="from", max_length=10)
    to_date: str | None = Field(default=None, alias="to", max_length=10)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)
    limit: int = Field(default=20, ge=1, le=40)
    include: list[ReadInclude] = Field(default_factory=list, max_length=INCLUDE_LIMIT)
    ids: list[str] = Field(default_factory=list, max_length=BULK_ID_LIMIT)
    fields: list[DetailField] = Field(default_factory=list, max_length=4)
    signals_any: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("from_date")
    @classmethod
    def valid_from_date(cls, value: str | None) -> str | None:
        return _validate_date(value, name="from")

    @field_validator("to_date")
    @classmethod
    def valid_to_date(cls, value: str | None) -> str | None:
        return _validate_date(value, name="to")

    @field_validator("include")
    @classmethod
    def unique_includes(cls, value: list[ReadInclude]) -> list[ReadInclude]:
        keys = [(row.id, row.find, row.within) for row in value]
        if _duplicates(keys):
            raise ValueError("include lookups must be unique")
        return value

    @field_validator("ids")
    @classmethod
    def valid_ids(cls, value: list[str]) -> list[str]:
        if any(re.fullmatch(_ITEM_ID, item) is None for item in value):
            raise ValueError("ids need exact item IDs")
        if _duplicates(value):
            raise ValueError("ids cannot contain duplicates")
        return value

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
        if self.cursor is not None and (
            self.include or self.ids or self.signals_any or self.fields
        ):
            raise ValueError(
                "cursor cannot combine with include, ids, fields, or signals_any"
            )
        selectors = sum(value is not None for value in (self.view, self.id, self.find))
        if self.ids:
            selectors += 1
        if selectors > 1:
            raise ValueError("use only one of view, id, find, or ids")
        if "ids" in self.model_fields_set and not self.ids:
            raise ValueError("ids needs at least one exact item ID")
        if self.signals_any and self.view != "audit":
            raise ValueError("signals_any needs view audit")
        if _duplicates(self.signals_any):
            raise ValueError("signals_any cannot contain duplicates")
        if "fields" in self.model_fields_set and not self.ids:
            raise ValueError("fields needs ids")
        if _duplicates(self.fields):
            raise ValueError("fields cannot contain duplicates")
        if self.ids and self.purpose != "review":
            raise ValueError("ids is only available for review purpose")
        if self.ids and (
            self.within is not None
            or self.from_date is not None
            or self.to_date is not None
            or self.include
        ):
            raise ValueError("ids cannot combine with another selector")
        if self.within is not None and self.find is None and self.view not in {
            "project",
            "area",
        }:
            raise ValueError("within needs find, view project, or view area")
        if self.view == "project" and (
            self.within is None or not self.within.startswith("project:")
        ):
            raise ValueError("view project needs within as an exact Project id")
        if self.view == "area" and (
            self.within is None or not self.within.startswith("area:")
        ):
            raise ValueError("view area needs within as an exact Area id")
        has_range = self.from_date is not None or self.to_date is not None
        if has_range and self.view != "logbook":
            raise ValueError("from and to need view logbook")
        if self.view == "logbook" and (self.from_date is None or self.to_date is None):
            raise ValueError("view logbook needs from and to")
        if self.from_date is not None and self.to_date is not None:
            if self.from_date > self.to_date:
                raise ValueError("from must not be after to")
        if self.purpose == "change" and self.id is None and self.find is None:
            raise ValueError("change purpose needs an exact id or unique find")
        if self.include and self.purpose not in {"change", "organize"}:
            raise ValueError("include is only available for change or organize")
        if self.purpose == "recurrence" and self.id is None:
            raise ValueError("recurrence purpose needs an exact Task id")
        if self.purpose == "recurrence" and any(
            value is not None
            for value in (
                self.view,
                self.find,
                self.within,
                self.from_date,
                self.to_date,
            )
        ):
            raise ValueError("recurrence purpose accepts only an exact Task id")
        if self.purpose == "organize" and not (
            self.id is not None
            or self.find is not None
            or (self.view == "project" and self.within is not None)
        ):
            raise ValueError(
                "organize purpose needs an exact Project id, Project find, or Project read"
            )
        if self.purpose == "organize" and self.id is not None:
            if not self.id.startswith("project:"):
                raise ValueError("organize purpose needs an exact Project id")
        if self.purpose == "organize" and self.view == "project":
            if self.within is None or not self.within.startswith("project:"):
                raise ValueError("organize Project view needs a Project scope")
        if self.cursor is not None and "purpose" in self.model_fields_set:
            raise ValueError("cursor cannot combine with purpose")
        return self


Weekday = Literal[
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


class RepeatCreate(StrictModel):
    """A complete semantic repeat rule for a new Task."""

    unit: Literal["day", "week", "month", "year"]
    mode: Literal["fixed", "after_completion"] = "fixed"
    interval: int = Field(default=1, ge=1, le=366)
    weekdays: list[Weekday] = Field(default_factory=list, max_length=7)

    @field_validator("weekdays")
    @classmethod
    def valid_weekdays(cls, value: list[Weekday]) -> list[Weekday]:
        if _duplicates(value):
            raise ValueError("weekdays cannot contain duplicates")
        return value

    @model_validator(mode="after")
    def valid_pattern(self) -> Self:
        if self.weekdays and self.unit != "week":
            raise ValueError("weekdays need a weekly repeat rule")
        if self.weekdays and self.mode != "fixed":
            raise ValueError("weekdays need fixed repeat mode")
        return self


class CreateEntry(StrictModel):
    key: str | None = Field(default=None, pattern=_LOCAL_KEY)
    kind: Kind = "task"
    title: str = Field(min_length=1, max_length=1000)
    notes_markdown: str | None = Field(default=None, max_length=50_000)
    checklist: list[str] = Field(default_factory=list, max_length=100)
    next_actions: list[str] = Field(default_factory=list, max_length=20)
    into: str | None = Field(
        default=None, pattern=_CONTEXT_HOME_REFERENCE, max_length=512
    )
    start: str | None = Field(default=None, max_length=32, pattern=START_PATTERN)
    deadline: str | None = Field(default=None, max_length=10)
    remind_at: str | None = Field(default=None, max_length=40)
    waiting: bool | None = None
    tag_ids: list[str] = Field(default_factory=list, max_length=20)
    after: str | None = Field(
        default=None, pattern=_CONTEXT_AFTER_REFERENCE, max_length=512
    )
    today_after: str | None = Field(
        default=None, pattern=_CONTEXT_AFTER_REFERENCE, max_length=512
    )
    heading_id: str | None = Field(
        default=None, pattern=_CONTEXT_HEADING_REFERENCE, max_length=512
    )
    repeat: RepeatCreate | None = None

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
            allowed = {"key", "kind", "title", "into", "after"}
            if self.into is None:
                raise ValueError("a heading needs a Project")
            if not (
                self.into.startswith(("project:", "$"))
                or re.fullmatch(_SHORT_REF, self.into) is not None
            ):
                raise ValueError("a heading needs a Project")
            if self.model_fields_set - allowed:
                raise ValueError(
                    "a heading accepts only key, kind, title, into, and after"
                )
            return self
        if self.checklist and self.kind != "task":
            raise ValueError("only a task can have a checklist")
        if self.next_actions and self.kind != "project":
            raise ValueError("only a Project can have next_actions")
        if self.heading_id is not None and self.kind != "task":
            raise ValueError("only a Task can use a heading")
        if self.repeat is not None and self.kind != "task":
            raise ValueError("only a Task can repeat")
        if self.into in {"inbox", "anytime"} and (
            self.start is not None or self.remind_at is not None
        ):
            raise ValueError("Inbox or Anytime cannot combine with a schedule")
        _reject_cleared_start_with_reminder(
            self.model_fields_set, self.start, self.remind_at
        )
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


class RepeatEdit(StrictModel):
    """Start repetition or change one existing repeat template.

    ``unit`` is required when the target is an ordinary Task. Existing
    templates can change only the fields that are present.
    """

    mode: Literal["fixed", "after_completion"] | None = None
    unit: Literal["day", "week", "month", "year"] | None = None
    interval: int | None = Field(default=None, ge=1, le=366)
    weekdays: list[Weekday] | None = Field(default=None, max_length=7)
    remove: Literal[True] | None = None

    @model_validator(mode="after")
    def valid_edit(self) -> Self:
        if self.remove:
            if self.model_fields_set != {"remove"}:
                raise ValueError("repeat removal cannot combine with rule fields")
            return self
        if not self.model_fields_set:
            raise ValueError("repeat needs a mode, unit, interval, or remove")
        if self.weekdays is not None and _duplicates(self.weekdays):
            raise ValueError("weekdays cannot contain duplicates")
        if self.unit is not None and self.unit != "week" and self.weekdays:
            raise ValueError("weekdays need a weekly repeat rule")
        if self.mode == "after_completion" and self.weekdays:
            raise ValueError("weekdays need fixed repeat mode")
        return self


class ChangeEntry(StrictModel):
    id: str | None = Field(default=None, pattern=_ITEM_ID, max_length=512)
    if_revision: str | None = Field(default=None, min_length=1, max_length=512)
    ref: str | None = Field(default=None, pattern=_SHORT_REF, max_length=12)
    title: str | None = Field(default=None, min_length=1, max_length=1000)
    status: Status | None = None
    notes_markdown: str | None = Field(default=None, max_length=50_000)
    checklist_add: list[ChecklistAdd] = Field(default_factory=list, max_length=500)
    checklist_change: list[ChecklistChange] = Field(
        default_factory=list, max_length=500
    )
    checklist_remove: list[str] = Field(default_factory=list, max_length=500)
    checklist_order: list[str] | None = Field(default=None, max_length=500)
    into: str | None = Field(
        default=None, pattern=_CONTEXT_HOME_REFERENCE, max_length=512
    )
    start: str | None = Field(default=None, max_length=32, pattern=START_PATTERN)
    deadline: str | None = Field(default=None, max_length=10)
    remind_at: str | None = Field(default=None, max_length=40)
    waiting: bool | None = None
    tags_add: list[str] = Field(default_factory=list, max_length=20)
    tags_remove: list[str] = Field(default_factory=list, max_length=20)
    after: str | None = Field(
        default=None, pattern=_CONTEXT_AFTER_REFERENCE, max_length=512
    )
    today_after: str | None = Field(
        default=None, pattern=_CONTEXT_AFTER_REFERENCE, max_length=512
    )
    move_contents_to: str | None = Field(
        default=None, pattern=_CONTEXT_AREA_REFERENCE, max_length=512
    )
    remove_if_empty: Literal[True] | None = None
    trash: Literal[True] | None = None
    lifecycle: Literal["trash", "restore", "delete_permanently"] | None = None
    delete_contents: Literal[True] | None = None
    heading_id: str | None = Field(
        default=None, pattern=_CONTEXT_HEADING_REFERENCE, max_length=512
    )
    repeat_interval: int | None = Field(default=None, ge=1, le=366)
    repeat: RepeatEdit | None = None
    replace_rich_note: Literal[True] | None = None

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
        exact = self.id is not None or self.if_revision is not None
        if self.ref is None and exact and (self.id is None or self.if_revision is None):
            raise ValueError("an exact change needs id and if_revision")
        # A model can copy the exact identity from the contextual result while
        # also sending its short ref.  Keep that request valid, then let the
        # contextual compiler prove that both identities match.  This keeps
        # ref-only changes the normal path without turning a mismatch into a
        # silent overwrite.
        if self.ref is None and not exact:
            raise ValueError("a change needs an exact id or context ref")
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
                self.lifecycle is not None,
                self.delete_contents is True,
                "heading_id" in self.model_fields_set,
                self.repeat_interval is not None,
                self.repeat is not None,
                self.replace_rich_note is True,
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
        if self.checklist_order is not None and removed.intersection(
            self.checklist_order
        ):
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
        _reject_cleared_start_with_reminder(
            self.model_fields_set, self.start, self.remind_at
        )
        if self.move_contents_to is not None or self.remove_if_empty:
            allowed = {
                "id",
                "if_revision",
                "ref",
                "move_contents_to",
                "remove_if_empty",
            }
            if self.model_fields_set - allowed:
                raise ValueError(
                    "an Area removal cannot combine with source-item changes"
                )
        if self.trash:
            allowed = {"id", "if_revision", "ref", "trash"}
            if self.model_fields_set - allowed:
                raise ValueError("Trash cannot combine with other changes")
        if self.lifecycle is not None:
            allowed = {"id", "if_revision", "ref", "lifecycle", "delete_contents"}
            if self.model_fields_set - allowed:
                raise ValueError("a lifecycle change cannot combine with other changes")
        if self.delete_contents and self.lifecycle != "delete_permanently":
            raise ValueError("delete_contents needs permanent deletion")
        if self.id is not None and self.id.startswith("heading:"):
            if "into" in self.model_fields_set:
                # A heading can use into only to follow its source Project
                # during an atomic merge. Workspace rejects a heading move
                # into a different Project unless that merge is present.
                if self.into is None or self.into in {"inbox", "anytime"}:
                    raise ValueError("a heading needs a destination Project")
                if self.into.startswith("area:"):
                    raise ValueError("a heading needs a destination Project")
                allowed = {"id", "if_revision", "ref", "into"}
                if self.model_fields_set - allowed:
                    raise ValueError(
                        "a heading move can only specify its destination Project"
                    )
                return self
            allowed = {"id", "if_revision", "ref", "title", "after", "lifecycle"}
            if self.model_fields_set - allowed:
                raise ValueError("a heading can only rename, reorder, or delete")
            if self.lifecycle not in {None, "trash", "restore", "delete_permanently"}:
                raise ValueError(
                    "a heading supports trash, restore, or permanent deletion"
                )
        if self.repeat_interval is not None:
            allowed = {"id", "if_revision", "ref", "repeat_interval"}
            if self.model_fields_set - allowed:
                raise ValueError("a repeat interval cannot combine with other changes")
        if self.repeat is not None:
            allowed = {
                "id",
                "if_revision",
                "ref",
                "repeat",
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
                "into",
                "start",
                "deadline",
                "remind_at",
                "after",
                "today_after",
                "heading_id",
            }
            if self.repeat.remove:
                allowed = {"id", "if_revision", "ref", "repeat"}
            if self.model_fields_set - allowed:
                raise ValueError("a repeat change cannot combine with that item change")
        if self.replace_rich_note and "notes_markdown" not in self.model_fields_set:
            raise ValueError("replace_rich_note needs notes_markdown")
        return self


class EnsureTag(StrictModel):
    key: str = Field(pattern=_LOCAL_KEY)
    title: str = Field(min_length=1, max_length=1000)
    parent_id: str | None = Field(default=None, pattern=_TAG_REFERENCE, max_length=512)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tag title cannot be blank")
        return value.strip()


class ChangeTag(StrictModel):
    id: str = Field(pattern=_TAG_ID, max_length=512)
    title: str | None = Field(default=None, min_length=1, max_length=1000)
    parent_id: str | None = Field(default=None, pattern=_TAG_ID, max_length=512)
    delete_permanently: Literal[True] | None = None

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("tag title cannot be blank")
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def valid_change(self) -> Self:
        if self.delete_permanently:
            if self.model_fields_set != {"id", "delete_permanently"}:
                raise ValueError("tag deletion cannot combine with other changes")
            return self
        if self.model_fields_set == {"id"}:
            raise ValueError("a tag needs a change")
        if self.parent_id == self.id:
            raise ValueError("a tag cannot reference itself")
        return self


class OrganizeSection(StrictModel):
    """One ordered Project section in a contextual organization draft."""

    heading_ref: str | None = Field(default=None, pattern=_SHORT_REF, max_length=12)
    heading_key: str | None = Field(default=None, pattern=_LOCAL_KEY)
    heading_title: str | None = Field(default=None, min_length=1, max_length=1000)
    task_refs: list[str] = Field(default_factory=list, max_length=120)

    @field_validator("task_refs")
    @classmethod
    def valid_task_refs(cls, value: list[str]) -> list[str]:
        if _duplicates(value) or any(
            re.fullmatch(_SHORT_OR_LOCAL_REF, item) is None for item in value
        ):
            raise ValueError("task_refs need unique context refs or local keys")
        return value

    @model_validator(mode="after")
    def valid_heading(self) -> Self:
        if self.heading_ref is not None and self.heading_key is not None:
            raise ValueError("a section uses an existing or new heading, not both")
        if self.heading_key is not None and self.heading_title is None:
            raise ValueError("a new heading needs heading_title")
        if self.heading_key is None and self.heading_ref is None:
            if self.heading_title is not None:
                raise ValueError("heading_title needs an existing or new heading")
        return self


class OrganizeDraft(StrictModel):
    project_ref: str = Field(pattern=_SHORT_REF, max_length=12)
    sections: list[OrganizeSection] = Field(min_length=1, max_length=120)
    delete_headings: list[str] = Field(default_factory=list, max_length=120)
    unlisted: Literal["keep"] = "keep"

    @field_validator("delete_headings")
    @classmethod
    def valid_deleted_headings(cls, value: list[str]) -> list[str]:
        if _duplicates(value) or any(
            re.fullmatch(_SHORT_REF, item) is None for item in value
        ):
            raise ValueError("delete_headings need unique context refs")
        return value

    @model_validator(mode="after")
    def unique_layout_refs(self) -> Self:
        tasks = [ref for section in self.sections for ref in section.task_refs]
        headings = [
            section.heading_ref
            for section in self.sections
            if section.heading_ref is not None
        ]
        keys = [
            section.heading_key
            for section in self.sections
            if section.heading_key is not None
        ]
        if _duplicates(tasks):
            raise ValueError("each task can appear in one organize section")
        if _duplicates(headings) or _duplicates(keys):
            raise ValueError("each heading can appear in one organize section")
        if set(headings).intersection(self.delete_headings):
            raise ValueError("a heading cannot be kept and deleted")
        return self


class CommitCall(StrictModel):
    intent_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{8,120}$")
    context_id: str | None = Field(default=None, pattern=_CONTEXT_ID, max_length=124)
    scope_revision: str | None = Field(default=None, min_length=1, max_length=512)
    tags_revision: str | None = Field(default=None, min_length=1, max_length=512)
    ensure_tags: list[EnsureTag] = Field(default_factory=list, max_length=20)
    change_tags: list[ChangeTag] = Field(default_factory=list, max_length=40)
    create: list[CreateEntry] = Field(default_factory=list, max_length=40)
    change: list[ChangeEntry] = Field(default_factory=list, max_length=120)
    organize: list[OrganizeDraft] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def valid_commit(self) -> Self:
        ids = [entry.id for entry in self.change if entry.id is not None]
        if _duplicates(ids):
            raise ValueError("each existing item can change once")
        context_refs = [entry.ref for entry in self.change if entry.ref is not None]
        if _duplicates(context_refs):
            raise ValueError("each context item can change once")
        if (context_refs or self.organize) and self.context_id is None:
            raise ValueError("context refs need context_id")
        project_refs = [draft.project_ref for draft in self.organize]
        if _duplicates(project_refs):
            raise ValueError("each Project can have one organize draft")

        changes_areas = any(entry.kind == "area" for entry in self.create) or any(
            entry.id is not None and entry.id.startswith("area:")
            for entry in self.change
        )
        short_context_refs = [
            reference
            for entry in self.create
            for reference in (
                entry.into,
                entry.after,
                entry.today_after,
                entry.heading_id,
            )
            if reference is not None
            and reference not in {"inbox", "anytime"}
            and re.fullmatch(_SHORT_REF, reference) is not None
        ]
        short_context_refs.extend(
            reference
            for entry in self.change
            for reference in (
                entry.into,
                entry.after,
                entry.today_after,
                entry.move_contents_to,
                entry.heading_id,
            )
            if reference is not None
            and reference not in {"inbox", "anytime"}
            and re.fullmatch(_SHORT_REF, reference) is not None
        )
        if short_context_refs and self.context_id is None:
            raise ValueError(
                "short relationship refs need context_id; this includes short Area refs"
            )
        if changes_areas and self.scope_revision is None:
            raise ValueError("Area changes need a system scope_revision")
        if self.change_tags and self.tags_revision is None:
            if self.scope_revision is None:
                raise ValueError("tag changes need a tags_revision from a tags read")
            self.tags_revision = self.scope_revision
        tag_change_ids = [entry.id for entry in self.change_tags]
        if _duplicates(tag_change_ids):
            raise ValueError("each existing tag can change once")

        keys = [entry.key for entry in self.ensure_tags]
        keys.extend(entry.key for entry in self.create if entry.key is not None)
        keys.extend(
            section.heading_key
            for draft in self.organize
            for section in draft.sections
            if section.heading_key is not None
        )
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
        local_refs: list[str | None] = []
        for created in self.create:
            local_refs.extend(
                (created.into, created.after, created.today_after, created.heading_id)
            )
        for changed in self.change:
            local_refs.extend(
                (
                    changed.into,
                    changed.after,
                    changed.today_after,
                    changed.move_contents_to,
                    changed.heading_id,
                )
            )
            local_refs.extend(row.after for row in changed.checklist_add)
            local_refs.extend(row.after for row in changed.checklist_change)
            local_refs.extend(changed.checklist_order or [])
        unknown = sorted(
            {ref for ref in local_refs if ref and ref.startswith("$")} - known
        )
        if unknown:
            raise ValueError(f"unknown local keys: {', '.join(unknown)}")
        organize_task_refs = {
            ref
            for draft in self.organize
            for section in draft.sections
            for ref in section.task_refs
            if ref.startswith("$")
        }
        create_keys_for_organize = {
            entry.key for entry in self.create if entry.key is not None
        }
        unknown_organize = sorted(organize_task_refs - create_keys_for_organize)
        if unknown_organize:
            raise ValueError(
                "organize local task refs need create keys: "
                + ", ".join(unknown_organize)
            )
        non_task_organize = sorted(
            ref
            for ref in organize_task_refs
            if any(entry.key == ref and entry.kind != "task" for entry in self.create)
        )
        if non_task_organize:
            raise ValueError("organize local task refs must identify Tasks")

        tag_keys = {entry.key for entry in self.ensure_tags}
        seen_tag_keys: set[str] = set()
        for entry in self.ensure_tags:
            if (
                entry.parent_id is not None
                and entry.parent_id.startswith("$")
                and entry.parent_id not in seen_tag_keys
            ):
                raise ValueError("a local tag parent must be an earlier ensured tag")
            seen_tag_keys.add(entry.key)
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
        for create_entry in self.create:
            for anchor in (create_entry.after, create_entry.today_after):
                if (
                    anchor is not None
                    and anchor.startswith("$")
                    and anchor not in seen_create
                ):
                    raise ValueError(
                        "local after anchors must be earlier create entries"
                    )
            if create_entry.into is not None and create_entry.into.startswith("$"):
                target = create_by_key.get(create_entry.into)
                if target is None or target.kind not in {"area", "project"}:
                    raise ValueError("a local home must be an Area or Project")
                if create_entry.kind == "project" and target.kind != "area":
                    raise ValueError("a Project local home must be an Area")
                if create_entry.kind == "heading" and target.kind != "project":
                    raise ValueError("a heading local home must be a Project")
            if (
                create_entry.heading_id is not None
                and create_entry.heading_id.startswith("$")
            ):
                target = create_by_key.get(create_entry.heading_id)
                if (
                    target is None
                    or target.kind != "heading"
                    or create_entry.heading_id not in seen_create
                ):
                    raise ValueError(
                        "a local heading must be an earlier heading create entry"
                    )
            if create_entry.after is not None and create_entry.after.startswith("$"):
                anchor_entry = create_by_key[create_entry.after]
                if anchor_entry.kind != create_entry.kind or create_scope(
                    anchor_entry
                ) != create_scope(create_entry):
                    raise ValueError("a local after anchor must be in the same list")
            if create_entry.key is not None:
                seen_create.add(create_entry.key)

        for changed in self.change:
            for anchor in (changed.after, changed.today_after):
                if (
                    anchor is not None
                    and anchor.startswith("$")
                    and anchor not in create_keys
                ):
                    raise ValueError("an item after anchor must be a created item")
            if changed.into is not None and changed.into.startswith("$"):
                target = create_by_key.get(changed.into)
                if target is None or target.kind not in {"area", "project"}:
                    raise ValueError("a local home must be a created Area or Project")
            if changed.heading_id is not None and changed.heading_id.startswith("$"):
                target = create_by_key.get(changed.heading_id)
                if target is None or target.kind != "heading":
                    raise ValueError("a local heading must be a created heading")
            row_keys = {row.key for row in changed.checklist_add if row.key is not None}
            seen_rows: set[str] = set()
            for row in changed.checklist_add:
                if (
                    row.after is not None
                    and row.after.startswith("$")
                    and row.after not in seen_rows
                ):
                    raise ValueError(
                        "local checklist after anchors must be earlier rows"
                    )
                if row.key is not None:
                    seen_rows.add(row.key)
            row_refs = [row.after for row in changed.checklist_change]
            row_refs.extend(changed.checklist_order or [])
            if any(
                ref is not None and ref.startswith("$") and ref not in row_keys
                for ref in row_refs
            ):
                raise ValueError(
                    "local checklist references must belong to the same item"
                )
        return self


class ApproveCall(StrictModel):
    plan_id: str = Field(pattern=r"^plan_[A-Za-z0-9_-]{8,120}$")


class TagFact(StrictModel):
    id: str = Field(pattern=_TAG_ID, max_length=512)
    title: str = Field(min_length=1, max_length=1000)
    parent_ids: list[str] = Field(default_factory=list, max_length=20)
    parents_truncated: bool = False
    from_id: str | None = Field(default=None, pattern=_ITEM_ID, max_length=512)

    @field_validator("parent_ids")
    @classmethod
    def valid_parent_ids(cls, value: list[str]) -> list[str]:
        if _duplicates(value) or any(
            re.fullmatch(_TAG_ID, item) is None for item in value
        ):
            raise ValueError("parent_ids need unique exact tag IDs")
        return value


class ChecklistFact(StrictModel):
    id: str = Field(pattern=_CHECK_ID, max_length=512)
    revision: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=1000)
    status: Status
    order: int = Field(ge=_ORDER_MIN, le=_ORDER_MAX)


class RecurrenceFact(StrictModel):
    kind: RecurrenceKind
    template_id: str | None = Field(default=None, pattern=_ITEM_ID, max_length=512)
    mode: Literal["fixed", "after_completion"] | None = None
    unit: Literal["day", "week", "month", "year"] | None = None
    interval: int | None = Field(default=None, ge=1, le=366)
    weekdays: list[Weekday] = Field(default_factory=list, max_length=7)
    linked_item_ids: list[str] = Field(default_factory=list, max_length=40)

    @field_validator("weekdays")
    @classmethod
    def unique_weekdays(cls, value: list[Weekday]) -> list[Weekday]:
        if _duplicates(value):
            raise ValueError("weekdays cannot contain duplicates")
        return value

    @field_validator("linked_item_ids")
    @classmethod
    def valid_linked_items(cls, value: list[str]) -> list[str]:
        if _duplicates(value) or any(
            re.fullmatch(_ITEM_ID, item) is None for item in value
        ):
            raise ValueError("linked_item_ids need unique exact item IDs")
        return value


class ItemFact(StrictModel):
    ref: str | None = Field(default=None, pattern=_SHORT_REF, max_length=12)
    id: str = Field(pattern=_ITEM_ID, max_length=512)
    revision: str | None = Field(default=None, min_length=1, max_length=512)
    kind: Kind
    title: str = Field(min_length=1, max_length=1000)
    status: Status
    into_id: str | None = Field(default=None, pattern=_ITEM_ID, max_length=512)
    heading_id: str | None = Field(default=None, pattern=_HEADING_ID, max_length=512)
    notes_markdown: str | None = Field(default=None, max_length=50_000)
    checklist: list[ChecklistFact] = Field(default_factory=list, max_length=100)
    direct_tags: list[TagFact] = Field(default_factory=list, max_length=40)
    inherited_tags: list[TagFact] = Field(default_factory=list, max_length=40)
    direct_tag_ids: list[str] = Field(default_factory=list, max_length=40)
    inherited_tag_ids: list[str] = Field(default_factory=list, max_length=40)
    start: str | None = Field(default=None, max_length=32, pattern=START_PATTERN)
    deadline: str | None = Field(default=None, max_length=10)
    remind_at: str | None = Field(default=None, max_length=40)
    recurrence: RecurrenceFact | None = None
    order: int = Field(ge=_ORDER_MIN, le=_ORDER_MAX)
    today_order: int | None = Field(default=None, ge=_ORDER_MIN, le=_ORDER_MAX)
    signals: list[str] = Field(default_factory=list, max_length=20)
    truncated_fields: list[TruncatedField] = Field(default_factory=list, max_length=4)

    @field_validator("truncated_fields")
    @classmethod
    def unique_truncated_fields(
        cls, value: list[TruncatedField]
    ) -> list[TruncatedField]:
        if _duplicates(value):
            raise ValueError("truncated_fields cannot contain duplicates")
        return value

    @field_validator("direct_tag_ids", "inherited_tag_ids")
    @classmethod
    def valid_tag_id_lists(cls, value: list[str]) -> list[str]:
        if _duplicates(value) or any(
            re.fullmatch(_TAG_ID, item) is None for item in value
        ):
            raise ValueError("tag id lists need unique exact tag IDs")
        return value


class DiagnosticRepair(StrictModel):
    conflict: str = Field(min_length=1, max_length=80)
    repair_kind: str = Field(min_length=1, max_length=80)


class DiagnosticFact(StrictModel):
    """One native-state conflict, including tag conflicts."""

    id: str = Field(pattern=_DIAGNOSTIC_ID, max_length=512)
    kind: Literal["task", "project", "area", "heading", "tag"]
    title: str = Field(min_length=1, max_length=1000)
    conflicts: list[str] = Field(min_length=1, max_length=20)
    repair: str | None = Field(default=None, max_length=400)
    repair_kind: str | None = Field(default=None, max_length=80)
    repairs: list[DiagnosticRepair] = Field(default_factory=list, max_length=20)


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


class ContextFact(StrictModel):
    id: str = Field(pattern=_CONTEXT_ID, max_length=124)
    purpose: Purpose
    expires_at: str = Field(max_length=40)
    complete: bool = False

    @field_validator("expires_at")
    @classmethod
    def valid_expiry(cls, value: str) -> str:
        checked = _validate_reminder(value)
        assert checked is not None
        return checked


class LayoutSectionFact(StrictModel):
    heading_ref: str | None = Field(default=None, pattern=_SHORT_REF, max_length=12)
    task_refs: list[str] = Field(default_factory=list, max_length=120)

    @field_validator("task_refs")
    @classmethod
    def unique_task_refs(cls, value: list[str]) -> list[str]:
        if _duplicates(value) or any(
            re.fullmatch(_SHORT_REF, item) is None for item in value
        ):
            raise ValueError("layout task_refs need unique context refs")
        return value


class LayoutFact(StrictModel):
    project_ref: str = Field(pattern=_SHORT_REF, max_length=12)
    sections: list[LayoutSectionFact] = Field(default_factory=list, max_length=120)
    complete: bool = False

    @model_validator(mode="after")
    def unique_refs(self) -> Self:
        tasks = [ref for section in self.sections for ref in section.task_refs]
        headings = [
            section.heading_ref
            for section in self.sections
            if section.heading_ref is not None
        ]
        if _duplicates(tasks) or _duplicates(headings):
            raise ValueError("layout refs must appear once")
        return self


class RecoveryFact(StrictModel):
    code: Literal[
        "context_required",
        "context_expired",
        "context_incomplete",
        "context_conflict",
        "context_corrupt",
    ]
    retry: Literal["read", "same", "rebuild"]
    read: dict[str, Any] | None = None

    @field_validator("read")
    @classmethod
    def valid_read(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if value is not None:
            ReadCall.model_validate(value)
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
    items: list[ItemFact] = Field(default_factory=list, max_length=120)
    tags: list[TagFact] = Field(default_factory=list, max_length=400)
    diagnostics: list[DiagnosticFact] = Field(default_factory=list, max_length=40)
    sections: list[ReviewSection] = Field(default_factory=list, max_length=40)
    layouts: list[LayoutFact] = Field(default_factory=list, max_length=10)
    signals: list[str] = Field(default_factory=list, max_length=40)
    context: ContextFact | None = None
    recovery: RecoveryFact | None = None
    plan: PlanFact | None = None
    receipt: str | None = Field(default=None, min_length=1, max_length=512)
    scope_revision: str | None = Field(default=None, min_length=1, max_length=512)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)
    missing_ids: list[str] = Field(default_factory=list, max_length=10)
    truncated: bool = False

    @field_validator("missing_ids")
    @classmethod
    def valid_missing_ids(cls, value: list[str]) -> list[str]:
        if _duplicates(value) or any(
            re.fullmatch(_ITEM_ID, item) is None for item in value
        ):
            raise ValueError("missing_ids need unique exact item IDs")
        return value

    @model_validator(mode="after")
    def contextual_facts_have_context(self) -> Self:
        refs = [item.ref for item in self.items if item.ref is not None]
        if _duplicates(refs):
            raise ValueError("item context refs must be unique")
        if (refs or self.layouts) and self.context is None:
            raise ValueError("context refs and layouts need context")
        return self


# MCP discovery needs flat schemas without refs or unions. The contract-parity test
# compares every mirrored model, property, required field, and scalar constraint.
_STRING: dict[str, Any] = {"type": "string", "minLength": 1, "maxLength": 512}
_NULLABLE_STRING: dict[str, Any] = {"type": ["string", "null"], "maxLength": 512}
_EXACT_ITEM: dict[str, Any] = {**_STRING, "pattern": _ITEM_ID}
_EXACT_DIAGNOSTIC: dict[str, Any] = {**_STRING, "pattern": _DIAGNOSTIC_ID}
_EXACT_TAG: dict[str, Any] = {**_STRING, "pattern": _TAG_ID}
_TAG_REFERENCE_SCHEMA: dict[str, Any] = {
    **_STRING,
    "pattern": _TAG_REFERENCE,
}
_EXACT_CHECK: dict[str, Any] = {**_STRING, "pattern": _CHECK_ID}
_HOME_SCHEMA: dict[str, Any] = {**_NULLABLE_STRING, "pattern": _HOME_REFERENCE}
_CONTEXT_HOME_SCHEMA: dict[str, Any] = {
    **_NULLABLE_STRING,
    "pattern": _CONTEXT_HOME_REFERENCE,
}
_CONTEXT_AFTER_SCHEMA: dict[str, Any] = {
    **_NULLABLE_STRING,
    "pattern": _CONTEXT_AFTER_REFERENCE,
}
_CONTEXT_AREA_SCHEMA: dict[str, Any] = {
    **_STRING,
    "pattern": _CONTEXT_AREA_REFERENCE,
}
_CONTEXT_HEADING_SCHEMA: dict[str, Any] = {
    "type": ["string", "null"],
    "pattern": _CONTEXT_HEADING_REFERENCE,
    "maxLength": 512,
}
_READ_INCLUDE: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "minProperties": 1,
    # ReadInclude keeps the length bounds at the runtime boundary. Avoid
    # repeating them in this bounded, model-facing selector schema.
    "properties": {
        "id": {"type": "string", "pattern": _ITEM_ID},
        "find": {"type": "string", "minLength": 1},
        "within": {
            "type": "string",
            "pattern": _CONTAINER_ID,
        },
    },
    # An exact id is the only one-property selector. A find may add within.
    "not": {"required": ["id"], "minProperties": 2},
}
_AFTER_SCHEMA: dict[str, Any] = {**_NULLABLE_STRING, "pattern": _AFTER_REFERENCE}
_CHECK_REFERENCE_SCHEMA: dict[str, Any] = {
    **_NULLABLE_STRING,
    "pattern": _CHECK_REFERENCE,
}
_AREA_SCHEMA: dict[str, Any] = {**_STRING, "pattern": _AREA_ID}
_DATE: dict[str, Any] = {
    "type": ["string", "null"],
    "format": "date",
    "maxLength": 10,
}
_DATE_TIME: dict[str, Any] = {
    "type": ["string", "null"],
    "format": "date-time",
    "maxLength": 40,
}
_WEEKDAYS: dict[str, Any] = {
    "type": "array",
    "maxItems": 7,
    "uniqueItems": True,
    "items": {
        "enum": [
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ]
    },
}

READ_IN: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "purpose": {
            "enum": ["review", "change", "organize", "recurrence"],
            "default": "review",
        },
        "view": {
            "enum": [
                "today",
                "inbox",
                "week",
                "system",
                "project",
                "area",
                "audit",
                "diagnostics",
                "logbook",
                "trash",
                "tags",
            ]
        },
        "id": _EXACT_ITEM,
        "find": {"type": "string", "minLength": 1, "maxLength": 500},
        "within": {**_STRING, "pattern": _CONTAINER_ID},
        "from": {"type": "string", "format": "date", "maxLength": 10},
        "to": {"type": "string", "format": "date", "maxLength": 10},
        "cursor": _STRING,
        "limit": {"type": "integer", "minimum": 1, "maximum": 40, "default": 20},
        "include": {
            "type": "array",
            "maxItems": INCLUDE_LIMIT,
            "uniqueItems": True,
            "items": _READ_INCLUDE,
        },
        "ids": {
            "type": "array",
            "minItems": 1,
            "maxItems": BULK_ID_LIMIT,
            "uniqueItems": True,
            "items": _EXACT_ITEM,
        },
        "fields": {
            "type": "array",
            "maxItems": 4,
            "uniqueItems": True,
            "items": {"enum": ["notes", "checklist", "tags", "recurrence"]},
        },
        "signals_any": {
            "type": "array",
            "maxItems": 8,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 80},
        },
    },
}

_CHECKLIST_ADD: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title"],
    "properties": {
        "key": {"type": "string", "pattern": _LOCAL_KEY},
        "title": {"type": "string", "minLength": 1, "maxLength": 1000},
        "after": _CHECK_REFERENCE_SCHEMA,
    },
}

_CHECKLIST_CHANGE: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id"],
    "properties": {
        "id": _EXACT_CHECK,
        "title": {"type": "string", "minLength": 1, "maxLength": 1000},
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
        "into": _CONTEXT_HOME_SCHEMA,
        "start": {
            "type": ["string", "null"],
            "maxLength": 32,
            "pattern": START_PATTERN,
        },
        "deadline": _DATE,
        "remind_at": _DATE_TIME,
        "waiting": {"type": ["boolean", "null"]},
        "tag_ids": {
            "type": "array",
            "maxItems": 20,
            "uniqueItems": True,
            "items": _TAG_REFERENCE_SCHEMA,
        },
        "after": _CONTEXT_AFTER_SCHEMA,
        "today_after": _CONTEXT_AFTER_SCHEMA,
        "heading_id": _CONTEXT_HEADING_SCHEMA,
        "repeat": {
            "type": "object",
            "additionalProperties": False,
            "required": ["unit"],
            "properties": {
                "unit": {"enum": ["day", "week", "month", "year"]},
                "mode": {
                    "enum": ["fixed", "after_completion"],
                    "default": "fixed",
                },
                "interval": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 366,
                    "default": 1,
                },
                "weekdays": _WEEKDAYS,
            },
        },
    },
}

_CHANGE: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "id": _EXACT_ITEM,
        "if_revision": _STRING,
        "ref": {"type": "string", "pattern": _SHORT_REF, "maxLength": 12},
        "title": {"type": "string", "minLength": 1, "maxLength": 1000},
        "status": {"enum": ["open", "completed", "canceled"]},
        "notes_markdown": {"type": ["string", "null"], "maxLength": 50000},
        "checklist_add": {
            "type": "array",
            "minItems": 1,
            "maxItems": 500,
            "items": _CHECKLIST_ADD,
        },
        "checklist_change": {
            "type": "array",
            "minItems": 1,
            "maxItems": 500,
            "items": _CHECKLIST_CHANGE,
        },
        "checklist_remove": {
            "type": "array",
            "minItems": 1,
            "maxItems": 500,
            "items": _EXACT_CHECK,
        },
        "checklist_order": {
            "type": ["array", "null"],
            "minItems": 1,
            "maxItems": 500,
            "items": {**_STRING, "pattern": _CHECK_REFERENCE},
        },
        "into": _CONTEXT_HOME_SCHEMA,
        "start": {
            "type": ["string", "null"],
            "maxLength": 32,
            "pattern": START_PATTERN,
        },
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
        "after": _CONTEXT_AFTER_SCHEMA,
        "today_after": _CONTEXT_AFTER_SCHEMA,
        "move_contents_to": _CONTEXT_AREA_SCHEMA,
        "remove_if_empty": {"const": True},
        "trash": {"const": True},
        "lifecycle": {"enum": ["trash", "restore", "delete_permanently"]},
        "delete_contents": {"const": True},
        "heading_id": _CONTEXT_HEADING_SCHEMA,
        "repeat_interval": {"type": "integer", "minimum": 1, "maximum": 366},
        "repeat": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "mode": {"enum": ["fixed", "after_completion"]},
                "unit": {"enum": ["day", "week", "month", "year"]},
                "interval": {"type": "integer", "minimum": 1, "maximum": 366},
                "weekdays": _WEEKDAYS,
                "remove": {"const": True},
            },
        },
        "replace_rich_note": {"const": True},
    },
}

_ENSURE_TAG: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["key", "title"],
    "properties": {
        "key": {"type": "string", "pattern": _LOCAL_KEY},
        "title": {"type": "string", "minLength": 1, "maxLength": 1000},
        "parent_id": {**_NULLABLE_STRING, "pattern": _TAG_REFERENCE},
    },
}

_CHANGE_TAG: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id"],
    "properties": {
        "id": _EXACT_TAG,
        "title": {"type": "string", "minLength": 1, "maxLength": 1000},
        "parent_id": {**_NULLABLE_STRING, "pattern": _TAG_ID},
        "delete_permanently": {"const": True},
    },
}

_ORGANIZE_SECTION: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "heading_ref": {
            "type": "string",
            "pattern": _SHORT_REF,
            "maxLength": 12,
        },
        "heading_key": {"type": "string", "pattern": _LOCAL_KEY},
        "heading_title": {"type": "string", "minLength": 1, "maxLength": 1000},
        "task_refs": {
            "type": "array",
            "maxItems": 120,
            "items": {"type": "string", "pattern": _SHORT_OR_LOCAL_REF},
        },
    },
}

_ORGANIZE: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["project_ref", "sections"],
    "properties": {
        "project_ref": {
            "type": "string",
            "pattern": _SHORT_REF,
            "maxLength": 12,
        },
        "sections": {
            "type": "array",
            "minItems": 1,
            "maxItems": 120,
            "items": _ORGANIZE_SECTION,
        },
        "delete_headings": {
            "type": "array",
            "maxItems": 120,
            "items": {"type": "string", "pattern": _SHORT_REF},
        },
        "unlisted": {"const": "keep", "default": "keep"},
    },
}

COMMIT_IN: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intent_id"],
    "properties": {
        "intent_id": {"type": "string", "pattern": r"^[A-Za-z0-9._:-]{8,120}$"},
        "context_id": {
            "type": "string",
            "pattern": _CONTEXT_ID,
            "maxLength": 124,
        },
        "scope_revision": _STRING,
        "tags_revision": _STRING,
        "ensure_tags": {"type": "array", "maxItems": 20, "items": _ENSURE_TAG},
        "change_tags": {"type": "array", "maxItems": 40, "items": _CHANGE_TAG},
        "create": {"type": "array", "maxItems": 40, "items": _CREATE},
        "change": {"type": "array", "maxItems": 120, "items": _CHANGE},
        "organize": {"type": "array", "maxItems": 10, "items": _ORGANIZE},
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
        "parent_ids": {
            "type": "array",
            "maxItems": 20,
            "uniqueItems": True,
            "items": _EXACT_TAG,
        },
        "parents_truncated": {"type": "boolean", "default": False},
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
        "order": {"type": "integer", "minimum": _ORDER_MIN, "maximum": _ORDER_MAX},
    },
}

_RECURRENCE: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind"],
    "properties": {
        "kind": {
            "enum": [
                "none",
                "fixed_instance",
                "after_completion_instance",
                "template",
                "unknown",
            ]
        },
        "template_id": _EXACT_ITEM,
        "mode": {"enum": ["fixed", "after_completion"]},
        "unit": {"enum": ["day", "week", "month", "year"]},
        "interval": {"type": "integer", "minimum": 1, "maximum": 366},
        "weekdays": {
            "type": "array",
            "maxItems": 7,
            "uniqueItems": True,
            "items": {
                "enum": [
                    "monday",
                    "tuesday",
                    "wednesday",
                    "thursday",
                    "friday",
                    "saturday",
                    "sunday",
                ]
            },
        },
        "linked_item_ids": {
            "type": "array",
            "maxItems": 40,
            "uniqueItems": True,
            "items": _EXACT_ITEM,
        },
    },
}

_ITEM_FACT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "kind", "title", "status", "order"],
    "properties": {
        "ref": {"type": "string", "pattern": _SHORT_REF, "maxLength": 12},
        "id": _EXACT_ITEM,
        "revision": _STRING,
        "kind": {"enum": ["task", "project", "area", "heading"]},
        "title": {"type": "string", "minLength": 1, "maxLength": 1000},
        "status": {"enum": ["open", "completed", "canceled"]},
        "into_id": _EXACT_ITEM,
        "heading_id": {**_STRING, "pattern": _HEADING_ID},
        "notes_markdown": {"type": ["string", "null"], "maxLength": 50000},
        "checklist": {"type": "array", "maxItems": 100, "items": _CHECKLIST_FACT},
        "direct_tags": {"type": "array", "maxItems": 40, "items": _TAG_FACT},
        "inherited_tags": {"type": "array", "maxItems": 40, "items": _TAG_FACT},
        "direct_tag_ids": {
            "type": "array",
            "maxItems": 40,
            "uniqueItems": True,
            "items": _EXACT_TAG,
        },
        "inherited_tag_ids": {
            "type": "array",
            "maxItems": 40,
            "uniqueItems": True,
            "items": _EXACT_TAG,
        },
        "start": {
            "type": ["string", "null"],
            "maxLength": 32,
            "pattern": START_PATTERN,
        },
        "deadline": _DATE,
        "remind_at": _DATE_TIME,
        "recurrence": _RECURRENCE,
        "order": {"type": "integer", "minimum": _ORDER_MIN, "maximum": _ORDER_MAX},
        "today_order": {
            "type": "integer",
            "minimum": _ORDER_MIN,
            "maximum": _ORDER_MAX,
        },
        "signals": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "maxLength": 1600},
        },
        "truncated_fields": {
            "type": "array",
            "maxItems": 4,
            "uniqueItems": True,
            "items": {"enum": ["notes", "checklist", "tags", "recurrence"]},
        },
    },
}

_SECTION: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["key", "title"],
    "properties": {
        "key": {"type": "string", "minLength": 1, "maxLength": 80},
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "item_ids": {
            "type": "array",
            "maxItems": 40,
            "uniqueItems": True,
            "items": _EXACT_ITEM,
        },
        "signals": {"type": "array", "maxItems": 40, "items": {"type": "string"}},
    },
}

_DIAGNOSTIC: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "kind", "title", "conflicts"],
    "properties": {
        "id": _EXACT_DIAGNOSTIC,
        "kind": {"enum": ["task", "project", "area", "heading", "tag"]},
        "title": {"type": "string", "minLength": 1, "maxLength": 1000},
        "conflicts": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": {"type": "string", "minLength": 1, "maxLength": 80},
        },
        "repair": {"type": "string", "maxLength": 400},
        "repair_kind": {"type": "string", "maxLength": 80},
        "repairs": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["conflict", "repair_kind"],
                "properties": {
                    "conflict": {"type": "string", "minLength": 1, "maxLength": 80},
                    "repair_kind": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 80,
                    },
                },
            },
        },
    },
}

_CONTEXT_FACT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "purpose", "expires_at"],
    "properties": {
        "id": {"type": "string", "pattern": _CONTEXT_ID},
        "purpose": {"enum": ["review", "change", "organize", "recurrence"]},
        "expires_at": {"type": "string"},
        "complete": {"type": "boolean", "default": False},
    },
}

_LAYOUT_SECTION: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "heading_ref": {
            "type": "string",
            "pattern": _SHORT_REF,
            "maxLength": 12,
        },
        "task_refs": {
            "type": "array",
            "maxItems": 120,
            "items": {"type": "string", "pattern": _SHORT_REF},
        },
    },
}

_LAYOUT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["project_ref"],
    "properties": {
        "project_ref": {
            "type": "string",
            "pattern": _SHORT_REF,
            "maxLength": 12,
        },
        "sections": {"type": "array", "maxItems": 120, "items": _LAYOUT_SECTION},
        "complete": {"type": "boolean", "default": False},
    },
}

_RECOVERY: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["code", "retry"],
    "properties": {
        "code": {
            "enum": [
                "context_required",
                "context_expired",
                "context_incomplete",
                "context_conflict",
                "context_corrupt",
            ]
        },
        "retry": {"enum": ["read", "same", "rebuild"]},
        "read": {"type": "object"},
    },
}

_PLAN: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "expires_at", "summary"],
    "properties": {
        "id": APPROVE_IN["properties"]["plan_id"],
        "expires_at": {
            "type": "string",
            "format": "date-time",
            "maxLength": 40,
        },
        "summary": {
            "type": "array",
            "minItems": 1,
            "maxItems": 40,
            "items": {"type": "string"},
        },
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
        "items": {"type": "array", "maxItems": 120, "items": _ITEM_FACT},
        "tags": {"type": "array", "maxItems": 400, "items": _TAG_FACT},
        "diagnostics": {"type": "array", "maxItems": 40, "items": _DIAGNOSTIC},
        "sections": {"type": "array", "maxItems": 40, "items": _SECTION},
        "layouts": {"type": "array", "maxItems": 10, "items": _LAYOUT},
        "signals": {"type": "array", "maxItems": 40, "items": {"type": "string"}},
        "context": _CONTEXT_FACT,
        "recovery": _RECOVERY,
        "plan": _PLAN,
        "receipt": _STRING,
        "scope_revision": _STRING,
        "cursor": _STRING,
        "missing_ids": {
            "type": "array",
            "maxItems": 10,
            "uniqueItems": True,
            "items": _EXACT_ITEM,
        },
        "truncated": {"type": "boolean", "default": False},
    },
}

_ITEM_SUMMARY: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "kind", "title", "status"],
    "properties": {
        "ref": {"type": "string", "pattern": _SHORT_REF, "maxLength": 12},
        "id": _EXACT_ITEM,
        "revision": _STRING,
        "kind": {"enum": ["task", "project", "area", "heading"]},
        "title": {"type": "string", "minLength": 1, "maxLength": 1000},
        "status": {"enum": ["open", "completed", "canceled"]},
        "into_id": _EXACT_ITEM,
        "heading_id": {**_STRING, "pattern": _HEADING_ID},
        "start": {"type": "string", "maxLength": 32, "pattern": START_PATTERN},
        "signals": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "maxLength": 1600},
        },
    },
}
_CONTROL_PROPERTIES: dict[str, Any] = {
    "next": RESULT_OUT["properties"]["next"],
    "status": RESULT_OUT["properties"]["status"],
    "instruction": RESULT_OUT["properties"]["instruction"],
}
_SUMMARY_ITEMS: dict[str, Any] = {
    "type": "array",
    "maxItems": 120,
    "items": _ITEM_SUMMARY,
}
_READ_ITEM: dict[str, Any] = {
    **_ITEM_SUMMARY,
    "additionalProperties": True,
    "properties": {
        **_ITEM_SUMMARY["properties"],
        "signals": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "maxLength": 1600},
        },
        "truncated_fields": {
            "type": "array",
            "maxItems": 4,
            "uniqueItems": True,
            "items": {"enum": ["notes", "checklist", "tags", "recurrence"]},
        },
        "direct_tag_ids": {
            "type": "array",
            "maxItems": 40,
            "uniqueItems": True,
            "items": _EXACT_TAG,
        },
        "inherited_tag_ids": {
            "type": "array",
            "maxItems": 40,
            "uniqueItems": True,
            "items": _EXACT_TAG,
        },
    },
}
_READ_ITEMS: dict[str, Any] = {
    "type": "array",
    "maxItems": 120,
    "items": _READ_ITEM,
}

READ_OUT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["next", "status", "instruction"],
    "properties": {
        **_CONTROL_PROPERTIES,
        "items": _READ_ITEMS,
        "tags": RESULT_OUT["properties"]["tags"],
        "diagnostics": RESULT_OUT["properties"]["diagnostics"],
        "sections": {
            "type": "array",
            "maxItems": 40,
            "items": _SECTION,
        },
        "layouts": {"type": "array", "maxItems": 10, "items": _LAYOUT},
        "signals": RESULT_OUT["properties"]["signals"],
        "context": _CONTEXT_FACT,
        "scope_revision": _STRING,
        "cursor": _STRING,
        "missing_ids": RESULT_OUT["properties"]["missing_ids"],
        "truncated": {"type": "boolean"},
        "recovery": _RECOVERY,
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
        "sections": RESULT_OUT["properties"]["sections"],
        "signals": RESULT_OUT["properties"]["signals"],
        "recovery": _RECOVERY,
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
    "Read Things. Empty input reviews Today. Use purpose change for one exact item or one unique active find match, organize for one exact Project id or one unique Project find, and recurrence for one exact Task before repeat changes. Use view system with the default review purpose for the Area and Project registry. "
    "Select exactly one view, exact id, find query, or a non-empty ids list. A view stands alone; project view needs within as project:<id>, never an Area; area view needs within as area:<id>, never a Project. Never combine view with id, find, or ids. Logbook needs from and to. "
    "ids is review-only. purpose=change cannot use ids. include lookups must be unique and are only for purpose=change or organize. fields is ids-only and selects notes, checklist, tags, and/or recurrence links; omit it for all four, or send [] for core facts only. Recurrence kind stays on the item. "
    "view=audit lists every active item once; add signals_any to keep only matching signals. view=diagnostics pages item and tag conflicts in diagnostics with repairs; repair_kind is set only when one repair exists. ids returns bounded full-detail facts for 1 to 10 exact items. Bulk ids hoist unique tags to tags and put only direct_tag_ids and inherited_tag_ids on items; join those IDs to the registry for titles and parents. Every item first gets a 400-character note prefix, then remaining budget is spent in request order on checklist titles, tag ids and titles, then the rest of each note. The complete structured result stays under 256 KB. If still large, parent graphs go first, then extra notes, checklists, recurrence links, then tag membership; if core metadata still overflows, fewer items return with a cursor. truncated_fields and signals name omitted notes, checklist, tags, or recurrence; read that exact id for the rest. Trash returns recoverable items, including untitled or malformed records. Send a cursor without selectors. "
    "start is today, evening, someday, an ISO date, or null. start=null cannot combine with remind_at. into=anytime moves to root Anytime; start=null clears scheduling and keeps the current Project or Area. "
    "For repeat changes, search first, then use recurrence with the exact Task id, then change only when editable context is needed. "
    "Exact reads add notes_markdown, checklist, direct_tags, inherited_tags, start, deadline, "
    "remind_at, recurrence, order, today_order, and signals. Compact reviews add has_notes and has_checklist when those exist. "
    "A change or organize read returns the local neighborhood, not the whole registry. Include a destination Project or Area to move or merge. "
    "For a Task or Project after anchor, or a heading order anchor, outside the target's returned facts, add up to 40 compact include lookups to the same change or organize read. Use today_after for a Task that is on Today now or is moved to Today earlier in the same commit. Each include uses one exact id or one unique active find; use within only with find. An unresolved include stays out of the context and does not block the target. "
    "Use the returned context_id and item ref in change; use an Area ref as into for a Project move. Do not send id or if_revision with ref. "
    "Review reads can use returned IDs and revisions for legacy changes. Follow next and instruction."
)
COMMIT_DESC = (
    "Commit decided work with a durable intent_id and one coherent batch. "
    "Prefer context_id with short refs from a change or organize read. An organize draft orders listed work and preserves unlisted work. "
    "It supports repeat rules, headings, tag structure, rich-note replacement, Trash, restore, and permanent deletion. "
    "A complete repeat rule on an exact ordinary Task keeps it as the current copy and creates its future template. "
    "Batch requested metadata, schedule, placement, order, and checklist changes into that conversion; both copies get the desired future content. "
    "An ensured tag key can be used in tag_ids or tags_add in the same commit. Define local refs before use and parent tags before children. Use start=evening for evening work. "
    "Use organize.delete_headings to delete Project headings. lifecycle=trash on a heading or Project is recoverable teardown. Use change_tags.delete_permanently for tag deletion. "
    "For a context change, send context_id and ref only; ref is authoritative and the context supplies the revision. "
    "For a Project move, send the Project ref and the destination Area ref as into; do not use an organize draft. "
    "Ordinary Task or Project Trash uses only lifecycle='trash'. Project trash also moves remaining descendants to Trash. delete_contents is only for permanent Project deletion with lifecycle='delete_permanently'. remove_if_empty and move_contents_to are Area-only. Every permanent Task or Project deletion target must already be in Trash, including Tasks and empty Projects. Permanent deletion of a non-empty Project additionally requires a complete Project read, lifecycle='delete_permanently' with delete_contents=true, and approval. "
    "For a Project merge, organize the source and include the destination. Move the children you want to keep, then set the source Project to lifecycle='trash'. Remaining descendants go to Trash with it. A heading can use into only to follow its source Project during that merge; do not move a heading into a different Project by itself. "
    "If you also send id and if_revision, they must exactly match the context. After pending, retry the exact payload. "
    "If the client loses the response or the outcome is pending or unknown, retry the exact same intent_id and byte-equivalent semantic payload. "
    "Do not read first, add scope_revision, or rebuild. Use a fresh read only for stale or expired context recovery. "
    "Moving a Task or Project to Trash and other high-impact work returns a plan without writes. "
    "Ask one natural confirmation and keep its "
    "control fields private. Follow next and instruction."
)
APPROVE_DESC = (
    "After clear owner confirmation, apply the exact returned plan. Send only plan_id and keep "
    "that ID private. "
    "If it is stale, read and prepare again. Follow next and instruction."
)
