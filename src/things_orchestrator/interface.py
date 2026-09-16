"""Internal workspace models retained behind the bounded v2 interface."""

from __future__ import annotations

import re
from collections.abc import Hashable, Sequence
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .tools import ITEM_ID


class StrictModel(BaseModel):
    """Reject values that are not part of the model interface."""

    model_config = ConfigDict(extra="forbid", strict=True)


Kind = Literal["task", "project", "area", "heading"]
Status = Literal["open", "completed", "canceled"]
TruncatedField = Literal["notes", "checklist", "tags", "recurrence"]
DetailField = Literal["notes", "checklist", "tags", "recurrence"]
DETAIL_FIELDS: tuple[DetailField, ...] = ("notes", "checklist", "tags", "recurrence")
Next = Literal["done", "ask", "read", "stop"]
ResultStatus = Literal["ok", "needs_input", "stale", "rejected", "unavailable"]
View = Literal[
    "today",
    "inbox",
    "week",
    "repeating",
    "logbook",
    "trash",
    "tags",
]
RegistryView = Literal["projects", "areas"]
CursorView = View | RegistryView
BULK_ID_LIMIT = 50
START_PATTERN = r"^(today|evening|tomorrow|someday|[0-9]{4}-[0-9]{2}-[0-9]{2})$"
RecurrenceKind = Literal[
    "none", "fixed_instance", "after_completion_instance", "template", "unknown"
]

_DIAGNOSTIC_ID = r"^(task|project|area|heading|tag):[^\s:]+$"
_CONTAINER_ID = r"^(trash|(project|area):[^\s:]+)$"
_CHECK_ID = r"^check:[^\s:]+$"
_TAG_ID = r"^tag:[^\s:]+$"
_HEADING_ID = r"^heading:[^\s:]+$"
_ORDER_MIN = -(2**63)
_ORDER_MAX = 2**63 - 1


def _duplicates(values: Sequence[Hashable]) -> bool:
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
    id: str | None = Field(default=None, pattern=ITEM_ID, max_length=512)
    find: str | None = Field(default=None, min_length=1, max_length=500)
    within: str | None = Field(default=None, pattern=_CONTAINER_ID, max_length=512)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)
    limit: int = Field(default=20, ge=1, le=40)
    ids: list[str] = Field(default_factory=list, max_length=BULK_ID_LIMIT)

    @field_validator("ids")
    @classmethod
    def valid_ids(cls, value: list[str]) -> list[str]:
        if any(re.fullmatch(ITEM_ID, item) is None for item in value):
            raise ValueError("ids need exact item IDs")
        if _duplicates(value):
            raise ValueError("ids cannot contain duplicates")
        return value

    @model_validator(mode="after")
    def valid_selector(self) -> Self:
        if self.cursor is not None and any(
            value is not None for value in (self.id, self.find, self.within)
        ):
            raise ValueError("cursor cannot combine with another item selector")
        if self.cursor is not None and self.ids:
            raise ValueError("cursor cannot combine with ids")
        selectors = sum(value is not None for value in (self.view, self.id, self.find))
        if self.ids:
            selectors += 1
        if selectors > 1:
            raise ValueError("use only one of view, id, find, or ids")
        if "ids" in self.model_fields_set and not self.ids:
            raise ValueError("ids needs at least one exact item ID")
        if self.ids and self.within is not None:
            raise ValueError("ids cannot combine with another selector")
        if self.within == "trash":
            if self.find is None:
                raise ValueError("within trash needs find")
            if self.view is not None:
                raise ValueError("within trash cannot combine with view")
        elif self.within is not None and self.find is None:
            raise ValueError("within needs find")
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


class TagFact(StrictModel):
    id: str = Field(pattern=_TAG_ID, max_length=512)
    title: str = Field(min_length=1, max_length=1000)
    parent_ids: list[str] = Field(default_factory=list, max_length=20)
    parents_truncated: bool = False
    from_id: str | None = Field(default=None, pattern=ITEM_ID, max_length=512)

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


class RepeatOnFact(StrictModel):
    month: int | None = Field(default=None, ge=1, le=12)
    day: int | None = None
    weekday: Weekday | None = None
    ordinal: int | None = None


class RecurrenceFact(StrictModel):
    engine: Literal["rt1", "rt2"] = "rt1"
    kind: RecurrenceKind
    template_id: str | None = Field(default=None, pattern=ITEM_ID, max_length=512)
    mode: Literal["fixed", "after_completion"] | None = None
    unit: Literal["day", "week", "month", "year"] | None = None
    interval: int | None = Field(default=None, ge=1, le=366)
    weekdays: list[Weekday] = Field(default_factory=list, max_length=7)
    linked_item_ids: list[str] = Field(default_factory=list, max_length=40)
    paused: bool | None = None
    created_through: str | None = Field(default=None, max_length=10)
    generated_count: int | None = Field(default=None, ge=0)
    completed_on: str | None = Field(default=None, max_length=10)
    next_on: str | None = Field(default=None, max_length=10)
    on: list[RepeatOnFact] = Field(default_factory=list, max_length=64)
    until: str | None = Field(default=None, max_length=10)
    start_early_days: int | None = Field(default=None, ge=0, le=366)
    reminder_time: str | None = None
    adds_deadline: bool = False

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
            re.fullmatch(ITEM_ID, item) is None for item in value
        ):
            raise ValueError("linked_item_ids need unique exact item IDs")
        return value


class ItemFact(StrictModel):
    id: str = Field(pattern=ITEM_ID, max_length=512)
    revision: str | None = Field(default=None, min_length=1, max_length=512)
    kind: Kind
    title: str = Field(min_length=1, max_length=1000)
    status: Status
    into_id: str | None = Field(default=None, pattern=ITEM_ID, max_length=512)
    into_title: str | None = Field(default=None, min_length=1, max_length=1000)
    heading_id: str | None = Field(default=None, pattern=_HEADING_ID, max_length=512)
    heading_title: str | None = Field(default=None, min_length=1, max_length=1000)
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
    order: int | None = Field(default=None, ge=_ORDER_MIN, le=_ORDER_MAX)
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


class Result(StrictModel):
    next: Next
    status: ResultStatus
    instruction: str = Field(min_length=1, max_length=1000)
    items: list[ItemFact] = Field(default_factory=list, max_length=120)
    tags: list[TagFact] = Field(default_factory=list, max_length=400)
    signals: list[str] = Field(default_factory=list, max_length=160)
    scope_revision: str | None = Field(default=None, min_length=1, max_length=512)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)
    missing_ids: list[str] = Field(default_factory=list, max_length=120)
    truncated: bool = False

    @field_validator("missing_ids")
    @classmethod
    def valid_missing_ids(cls, value: list[str]) -> list[str]:
        if _duplicates(value) or any(
            re.fullmatch(_DIAGNOSTIC_ID, item) is None for item in value
        ):
            raise ValueError("missing_ids need unique exact item or tag IDs")
        return value


def dump_result(result: Result) -> dict[str, Any]:
    """Compact JSON for MCP and the wire budget. Required fields still emit."""

    return result.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
