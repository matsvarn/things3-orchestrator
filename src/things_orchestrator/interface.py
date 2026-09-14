"""Internal workspace models retained behind the bounded v2 interface."""

from __future__ import annotations

import re
from collections.abc import Hashable, Sequence
from datetime import date
from typing import Any, Literal, Self, get_args

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
Next = Literal["done", "ask", "read", "retry_same", "stop"]
ResultStatus = Literal[
    "ok",
    "applied",
    "unchanged",
    "needs_input",
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
    "repeating",
    "system",
    "project",
    "area",
    "audit",
    "logbook",
    "trash",
    "tags",
]
BULK_ID_LIMIT = 10
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


def _validate_date(value: str | None, *, name: str) -> str | None:
    if value is None:
        return None
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO date") from error
    return value


def _duplicates(values: Sequence[Hashable]) -> bool:
    return len(values) != len(set(values))


def validate_read_selector(
    *,
    view: View | None,
    item_id: str | None,
    find: str | None,
    within: str | None,
    from_date: str | None,
    to_date: str | None,
) -> None:
    """Shared view/within/logbook rules for ReadCall."""

    if view is not None and view not in get_args(View):
        raise ValueError("invalid selector view")
    if within is not None and find is None and view not in {"project", "area"}:
        raise ValueError("within needs find, view project, or view area")
    if view == "project":
        container = within or (
            item_id if item_id is not None and item_id.startswith("project:") else None
        )
        if container is None or not container.startswith("project:"):
            raise ValueError(
                "view project needs id or within as an exact Project id"
            )
    if view == "area":
        container = within or (
            item_id if item_id is not None and item_id.startswith("area:") else None
        )
        if container is None or not container.startswith("area:"):
            raise ValueError("view area needs id or within as an exact Area id")
    has_range = from_date is not None or to_date is not None
    if has_range and view != "logbook":
        raise ValueError("from and to need view logbook")
    if view == "logbook" and (from_date is None) != (to_date is None):
        raise ValueError("view logbook needs both from and to, or neither")
    if (
        from_date is not None
        and to_date is not None
        and date.fromisoformat(from_date) > date.fromisoformat(to_date)
    ):
        raise ValueError("from must not be after to")


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
    from_date: str | None = Field(default=None, alias="from", max_length=10)
    to_date: str | None = Field(default=None, alias="to", max_length=10)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)
    limit: int = Field(default=20, ge=1, le=40)
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
            value is not None
            for value in (
                self.id,
                self.find,
                self.within,
                self.from_date,
                self.to_date,
            )
        ):
            raise ValueError("cursor cannot combine with another item selector")
        if self.cursor is not None and (
            self.ids or self.signals_any or self.fields
        ):
            raise ValueError(
                "cursor cannot combine with ids, fields, or signals_any"
            )
        selectors = sum(value is not None for value in (self.view, self.id, self.find))
        if self.ids:
            selectors += 1
        container_address = (
            self.view in {"area", "project"}
            and self.id is not None
            and self.id.startswith(f"{self.view}:")
            and self.find is None
            and not self.ids
        )
        if selectors > 1 and not container_address:
            raise ValueError("use only one of view, id, find, or ids")
        if "ids" in self.model_fields_set and not self.ids:
            raise ValueError("ids needs at least one exact item ID")
        if self.signals_any and self.view != "audit":
            raise ValueError("signals_any needs view audit")
        if any(not 1 <= len(signal) <= 80 for signal in self.signals_any):
            raise ValueError("signals_any values need 1 to 80 characters")
        if _duplicates(self.signals_any):
            raise ValueError("signals_any cannot contain duplicates")
        if "fields" in self.model_fields_set and not self.ids:
            raise ValueError("fields needs ids")
        if _duplicates(self.fields):
            raise ValueError("fields cannot contain duplicates")
        if self.ids and (
            self.within is not None
            or self.from_date is not None
            or self.to_date is not None
        ):
            raise ValueError("ids cannot combine with another selector")
        if self.within == "trash":
            if self.find is None:
                raise ValueError("within trash needs find")
            if self.view is not None:
                raise ValueError("within trash cannot combine with view")
        validate_read_selector(
            view=self.view,
            item_id=self.id,
            find=self.find,
            within=self.within,
            from_date=self.from_date,
            to_date=self.to_date,
        )
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
        if any(re.fullmatch(ITEM_ID, item) is None for item in value):
            raise ValueError("item_ids need exact item IDs")
        if _duplicates(value):
            raise ValueError("item_ids cannot contain duplicates")
        return value


class Result(StrictModel):
    next: Next
    status: ResultStatus
    instruction: str = Field(min_length=1, max_length=1000)
    items: list[ItemFact] = Field(default_factory=list, max_length=120)
    tags: list[TagFact] = Field(default_factory=list, max_length=400)
    sections: list[ReviewSection] = Field(default_factory=list, max_length=40)
    signals: list[str] = Field(default_factory=list, max_length=160)
    receipt: str | None = Field(default=None, min_length=1, max_length=512)
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
