"""Internal workspace models retained behind the bounded v2 interface."""

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
Next = Literal["done", "ask", "approve", "read", "revise", "retry_same", "stop"]
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
    "repeating",
    "weekly_review",
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
START_PATTERN = r"^(today|evening|tomorrow|someday|[0-9]{4}-[0-9]{2}-[0-9]{2})$"
Purpose = Literal["review", "change", "organize", "recurrence"]
WeeklyCategory = Literal[
    "inbox",
    "stale_start",
    "overdue",
    "today",
    "upcoming",
    "possible_duplicate",
    "waiting",
    "project_without_candidate_task",
    "project_review",
    "active_task_in_someday_project",
    "open_task_with_finished_checklist",
    "recently_completed_project",
    "someday",
    "weekly_candidate",
]
RecurrenceKind = Literal[
    "none", "fixed_instance", "after_completion_instance", "template", "unknown"
]

_ITEM_ID = r"^(task|project|area|heading):[^\s:]+$"
_DIAGNOSTIC_ID = r"^(task|project|area|heading|tag):[^\s:]+$"
_CONTAINER_ID = r"^(trash|(project|area):[^\s:]+)$"
_CHECK_ID = r"^check:[^\s:]+$"
_TAG_ID = r"^tag:[^\s:]+$"
_HOME_REFERENCE = (
    r"^(inbox|anytime|\$[A-Za-z][A-Za-z0-9_-]{0,79}|"
    r"(project|area):[^\s:]+)$"
)
_AFTER_REFERENCE = (
    r"^(\$[A-Za-z][A-Za-z0-9_-]{0,79}|"
    r"(task|project|area|heading):[^\s:]+)$"
)
_AREA_ID = r"^area:[^\s:]+$"
_HEADING_ID = r"^heading:[^\s:]+$"
_HEADING_REFERENCE = r"^(\$[A-Za-z][A-Za-z0-9_-]{0,79}|heading:[^\s:]+)$"
_ORDER_MIN = -(2**63)
_ORDER_MAX = 2**63 - 1
_CONTEXT_ID = r"^ctx_[A-Za-z0-9_-]{8,120}$"
_SHORT_REF = r"^[a-z][a-z0-9]{0,11}$"
def _validate_date(value: str | None, *, name: str) -> str | None:
    if value is None:
        return None
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO date") from error
    return value


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
        if self.within == "trash":
            raise ValueError("include within must identify an Area or Project")
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
    category: WeeklyCategory | None = None

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
                self.id,
                self.find,
                self.within,
                self.from_date,
                self.to_date,
            )
        ):
            raise ValueError("cursor cannot combine with another item selector")
        if self.cursor is not None and (
            self.include or self.ids or self.signals_any or self.fields or self.category
        ):
            raise ValueError(
                "cursor cannot combine with include, ids, fields, signals_any, or category"
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
        if self.category is not None and self.view != "weekly_review":
            raise ValueError("category needs view weekly_review")
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
        if self.within == "trash":
            if self.find is None:
                raise ValueError("within trash needs find")
            if self.view is not None:
                raise ValueError("within trash cannot combine with view")
        elif self.within is not None and self.find is None and self.view not in {
            "project",
            "area",
        }:
            raise ValueError("within needs find, view project, or view area")
        if self.view == "project":
            container = self.within or (
                self.id if self.id is not None and self.id.startswith("project:") else None
            )
            if container is None or not container.startswith("project:"):
                raise ValueError(
                    "view project needs id or within as an exact Project id"
                )
        if self.view == "area":
            container = self.within or (
                self.id if self.id is not None and self.id.startswith("area:") else None
            )
            if container is None or not container.startswith("area:"):
                raise ValueError("view area needs id or within as an exact Area id")
        has_range = self.from_date is not None or self.to_date is not None
        if has_range and self.view != "logbook":
            raise ValueError("from and to need view logbook")
        if self.view == "logbook" and (self.from_date is None) != (self.to_date is None):
            raise ValueError("view logbook needs both from and to, or neither")
        if self.from_date is not None and self.to_date is not None:
            if self.from_date > self.to_date:
                raise ValueError("from must not be after to")
        if self.purpose == "change" and self.id is None and self.find is None:
            raise ValueError("change purpose needs an exact id or unique find")
        if self.include and self.purpose not in {"review", "change", "organize"}:
            raise ValueError("include is only available for review, change, or organize")
        if self.purpose == "recurrence" and self.id is None:
            raise ValueError("recurrence purpose needs an exact Task or Project id")
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
            raise ValueError(
                "recurrence purpose accepts only an exact Task or Project id"
            )
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


class RepeatOnFact(StrictModel):
    month: int | None = Field(default=None, ge=1, le=12)
    day: int | None = None
    weekday: Weekday | None = None
    ordinal: int | None = None


class RecurrenceFact(StrictModel):
    engine: Literal["rt1", "rt2"] = "rt1"
    kind: RecurrenceKind
    template_id: str | None = Field(default=None, pattern=_ITEM_ID, max_length=512)
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
    complete: bool

    @field_validator("expires_at")
    @classmethod
    def valid_expiry(cls, value: str) -> str:
        checked = _validate_reminder(value)
        assert checked is not None
        return checked


class LayoutSectionFact(StrictModel):
    heading_ref: str | None = Field(default=None, pattern=_SHORT_REF, max_length=12)
    task_refs: list[str] = Field(default_factory=list, max_length=120)
    hidden_count: int = Field(default=0, ge=0, le=10_000)
    hidden_signals: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("task_refs")
    @classmethod
    def unique_task_refs(cls, value: list[str]) -> list[str]:
        if _duplicates(value) or any(
            re.fullmatch(_SHORT_REF, item) is None for item in value
        ):
            raise ValueError("layout task_refs need unique context refs")
        return value

    @field_validator("hidden_signals")
    @classmethod
    def unique_hidden_signals(cls, value: list[str]) -> list[str]:
        if _duplicates(value):
            raise ValueError("hidden_signals cannot contain duplicates")
        return value


class LayoutFact(StrictModel):
    project_ref: str = Field(pattern=_SHORT_REF, max_length=12)
    sections: list[LayoutSectionFact] = Field(default_factory=list, max_length=120)
    complete: bool

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


class ReceiptItemFact(StrictModel):
    id: str = Field(pattern=_ITEM_ID, max_length=512)
    title: str = Field(min_length=1, max_length=1000)


class Result(StrictModel):
    next: Next
    status: ResultStatus
    instruction: str = Field(min_length=1, max_length=1000)
    items: list[ItemFact] = Field(default_factory=list, max_length=120)
    already_correct: list[ReceiptItemFact] = Field(default_factory=list, max_length=120)
    tags: list[TagFact] = Field(default_factory=list, max_length=400)
    diagnostics: list[DiagnosticFact] = Field(default_factory=list, max_length=40)
    sections: list[ReviewSection] = Field(default_factory=list, max_length=40)
    layouts: list[LayoutFact] = Field(default_factory=list, max_length=120)
    signals: list[str] = Field(default_factory=list, max_length=160)
    context: ContextFact | None = None
    recovery: RecoveryFact | None = None
    plan: PlanFact | None = None
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

    @model_validator(mode="after")
    def contextual_facts_have_context(self) -> Self:
        refs = [item.ref for item in self.items if item.ref is not None]
        if _duplicates(refs):
            raise ValueError("item context refs must be unique")
        if (refs or self.layouts) and self.context is None:
            raise ValueError("context refs and layouts need context")
        return self


def dump_result(result: Result) -> dict[str, Any]:
    """Compact JSON for MCP and the wire budget. Required fields still emit."""

    return result.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
