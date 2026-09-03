"""Bounded v2 caller models, immutable drafts, and taint projection."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from typing import Annotated, Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from .interface import ReadCall, TruncatedField
from .journal import same_account_id

API_VERSION = "2"
SCHEMA_VERSION = "v2.0"
MANIFEST_VERSION = "v1"
SAFETY_POLICY_DIGEST = "sha256:v1:" + sha256(
    b"preserve-omitted-fields;bounded-v2-writes-apply-immediately;"
    b"pre-post-recheck;read-back-receipt;never-replay-stored-operation"
).hexdigest()
REQUEST_ID = r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}|[0-9A-HJKMNP-TV-Z]{26})$"
ITEM_ID = r"^(task|project|area|heading):[^\s:]+$"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True, slots=True)
class OperationDraft:
    api_version: str
    schema_version: str
    tool: str
    request_id: str
    request_hash: str
    payload_json: str

    @classmethod
    def build(cls, tool: str, request_id: str, payload: dict[str, object]) -> OperationDraft:
        canonical = _canonical(payload)
        digest = "sha256:v1:" + sha256(_canonical({"tool": tool, "schema_version": SCHEMA_VERSION, "arguments": json.loads(canonical)}).encode()).hexdigest()
        return cls(API_VERSION, SCHEMA_VERSION, tool, request_id, digest, canonical)

    @property
    def payload(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.payload_json))


@dataclass(frozen=True, slots=True)
class OperationManifest:
    account_id: str
    draft: OperationDraft
    preconditions: tuple[tuple[str, str], ...]
    write_json: tuple[str, ...]
    touched: tuple[tuple[str, ...], ...]
    before_json: tuple[str | None, ...]
    display_titles: tuple[str, ...]
    result_ids: tuple[str, ...]
    requires_owner: bool
    safety_policy_digest: str
    expires_at: str | None
    manifest_hash: str

    @classmethod
    def build(
        cls,
        *,
        account_id: str,
        draft: OperationDraft,
        preconditions: dict[str, str],
        writes: list[dict[str, object]],
        touched: list[list[str]],
        before: list[dict[str, object] | None],
        display_titles: list[str],
        result_ids: list[str],
        requires_owner: bool,
        clock: datetime,
    ) -> OperationManifest:
        expires_at = (clock + timedelta(minutes=30)).isoformat() if requires_owner else None
        body = {
            "version": MANIFEST_VERSION,
            "account_id": account_id,
            "api_version": draft.api_version,
            "schema_version": draft.schema_version,
            "request_hash": draft.request_hash,
            "tool": draft.tool,
            "preconditions": dict(sorted(preconditions.items())),
            "writes": writes,
            "touched": touched,
            "before": before,
            "display_titles": display_titles,
            "result_ids": result_ids,
            "requires_owner": requires_owner,
            "safety_policy_digest": SAFETY_POLICY_DIGEST,
            "expires_at": expires_at,
        }
        return cls(
            account_id=account_id,
            draft=draft,
            preconditions=tuple(sorted(preconditions.items())),
            write_json=tuple(_canonical(row) for row in writes),
            touched=tuple(tuple(row) for row in touched),
            before_json=tuple(
                _canonical(row) if row is not None else None for row in before
            ),
            display_titles=tuple(display_titles),
            result_ids=tuple(result_ids),
            requires_owner=requires_owner,
            safety_policy_digest=SAFETY_POLICY_DIGEST,
            expires_at=expires_at,
            manifest_hash="sha256:v1:" + sha256(_canonical(body).encode()).hexdigest(),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "version": MANIFEST_VERSION,
            "account_id": self.account_id,
            "api_version": self.draft.api_version,
            "schema_version": self.draft.schema_version,
            "request_hash": self.draft.request_hash,
            "tool": self.draft.tool,
            "preconditions": dict(self.preconditions),
            "writes": [json.loads(row) for row in self.write_json],
            "touched": [list(row) for row in self.touched],
            "before": [
                json.loads(row) if row is not None else None for row in self.before_json
            ],
            "display_titles": list(self.display_titles),
            "result_ids": list(self.result_ids),
            "requires_owner": self.requires_owner,
            "safety_policy_digest": self.safety_policy_digest,
            "expires_at": self.expires_at,
        }


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TaintedText(StrictModel):
    value: str
    source: Literal["things_cloud"] = "things_cloud"
    trust: Literal["untrusted"] = "untrusted"


Weekday = Literal[
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


class RepeatOn(StrictModel):
    """One semantic selected date in a regular repeat pattern."""

    month: int | None = Field(default=None, ge=1, le=12)
    day: int | None = None
    weekday: Weekday | None = None
    ordinal: int | None = None

    @model_validator(mode="after")
    def coherent_selector(self) -> Self:
        if (self.day is None) == (self.weekday is None):
            raise ValueError("on needs exactly one day or weekday")
        if self.day is not None and self.day not in {-1, *range(1, 32)}:
            raise ValueError("day needs 1 through 31, or -1 for the last day")
        if self.weekday is None and self.ordinal is not None:
            raise ValueError("ordinal needs a weekday")
        if self.weekday is not None and self.ordinal not in {None, -1, 1, 2, 3, 4, 5}:
            raise ValueError("ordinal needs 1 through 5, or -1 for the last weekday")
        return self


class RepeatCreate(StrictModel):
    """Complete semantic repeat rule for a newly captured Task or Project."""

    unit: Literal["day", "week", "month", "year"]
    mode: Literal["fixed", "after_completion"] = "fixed"
    interval: int = Field(default=1, ge=1, le=366)
    weekdays: list[Weekday] = Field(default_factory=list, max_length=7)
    on: list[RepeatOn] = Field(default_factory=list, max_length=64)
    until: str | None = None
    paused: bool = False

    @field_validator("weekdays")
    @classmethod
    def unique_weekdays(cls, value: list[Weekday]) -> list[Weekday]:
        if len(value) != len(set(value)):
            raise ValueError("weekdays cannot contain duplicates")
        return value

    @model_validator(mode="after")
    def valid_pattern(self) -> Self:
        if "on" in self.model_fields_set and not self.on:
            raise ValueError("on needs at least one selected date")
        if self.weekdays and self.on:
            raise ValueError("use either weekdays or on")
        if self.weekdays and self.unit != "week":
            raise ValueError("weekdays need a weekly repeat rule")
        if self.weekdays and self.mode != "fixed":
            raise ValueError("weekdays need fixed repeat mode")
        _validate_repeat_on(self.unit, self.mode, self.on)
        if self.until is not None:
            _valid_date(self.until)
        if self.mode == "after_completion" and self.until is not None:
            raise ValueError("after-completion repeats do not use an end date")
        return self


class RepeatEdit(StrictModel):
    """Start or edit repetition, create a copy, or stop a repeat series."""

    mode: Literal["fixed", "after_completion"] | None = None
    unit: Literal["day", "week", "month", "year"] | None = None
    interval: int | None = Field(default=None, ge=1, le=366)
    weekdays: list[Weekday] | None = Field(default=None, max_length=7)
    on: list[RepeatOn] | None = Field(default=None, max_length=64)
    until: str | None = None
    remove: Literal[True] | None = None
    create_next: Literal[True] | None = None
    paused: bool | None = None

    @model_validator(mode="after")
    def valid_edit(self) -> Self:
        null_fields = {
            field
            for field in self.model_fields_set - {"until"}
            if getattr(self, field) is None
        }
        if null_fields:
            raise ValueError("repeat fields other than until cannot be null")
        if self.create_next:
            if self.model_fields_set != {"create_next"}:
                raise ValueError("repeat create next cannot combine with other fields")
            return self
        if self.remove:
            if self.model_fields_set != {"remove"}:
                raise ValueError("repeat removal cannot combine with rule fields")
            return self
        if not self.model_fields_set:
            raise ValueError("repeat needs a mode, unit, interval, or remove")
        if self.weekdays is not None and len(self.weekdays) != len(set(self.weekdays)):
            raise ValueError("weekdays cannot contain duplicates")
        if self.unit is not None and self.unit != "week" and self.weekdays:
            raise ValueError("weekdays need a weekly repeat rule")
        if self.mode == "after_completion" and self.weekdays:
            raise ValueError("weekdays need fixed repeat mode")
        if self.weekdays is not None and self.on is not None:
            raise ValueError("use either weekdays or on")
        if self.on is not None:
            if not self.on:
                raise ValueError("on needs at least one selected date")
            if self.unit is not None:
                _validate_repeat_on(self.unit, self.mode or "fixed", self.on)
        if self.until is not None:
            _valid_date(self.until)
        if self.mode == "after_completion" and self.until is not None:
            raise ValueError("after-completion repeats do not use an end date")
        return self


class PublicRecurrence(StrictModel):
    """Semantic recurrence fact projected from an existing ItemFact."""

    kind: Literal[
        "none", "fixed_instance", "after_completion_instance", "template", "unknown"
    ]
    engine: Literal["rt1", "rt2"] = "rt1"
    template_id: str | None = Field(default=None, pattern=ITEM_ID, max_length=512)
    mode: Literal["fixed", "after_completion"] | None = None
    unit: Literal["day", "week", "month", "year"] | None = None
    interval: int | None = Field(default=None, ge=1, le=366)
    weekdays: list[Weekday] = Field(default_factory=list, max_length=7)
    linked_item_ids: list[str] = Field(default_factory=list, max_length=40)
    paused: bool | None = None
    created_through: str | None = None
    generated_count: int | None = Field(default=None, ge=0)
    completed_on: str | None = None
    next_on: str | None = None
    on: list[RepeatOn] = Field(default_factory=list, max_length=64)
    until: str | None = None
    start_early_days: int | None = Field(default=None, ge=0, le=366)
    reminder_time: str | None = None
    adds_deadline: bool = False

    @field_validator("weekdays")
    @classmethod
    def unique_weekdays(cls, value: list[Weekday]) -> list[Weekday]:
        if len(value) != len(set(value)):
            raise ValueError("weekdays cannot contain duplicates")
        return value

    @field_validator("linked_item_ids")
    @classmethod
    def valid_linked_items(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(
            re.fullmatch(ITEM_ID, item) is None for item in value
        ):
            raise ValueError("linked_item_ids need unique exact item IDs")
        return value


RecurrenceFact = PublicRecurrence


class PublicChecklistRow(StrictModel):
    id: str
    title: TaintedText
    status: Literal["open", "completed", "canceled"]


class PublicItem(StrictModel):
    id: str
    kind: Literal["task", "project", "area", "heading"]
    title: TaintedText
    status: Literal["open", "completed", "canceled"]
    notes: TaintedText | None = None
    into_id: str | None = None
    start: str | None = None
    deadline: str | None = None
    tags: list[TaintedText] = Field(default_factory=list, max_length=80)
    checklist: list[PublicChecklistRow] = Field(default_factory=list, max_length=100)
    direct_tag_ids: list[str] = Field(default_factory=list, max_length=40)
    inherited_tag_ids: list[str] = Field(default_factory=list, max_length=40)
    truncated_fields: list[TruncatedField] = Field(default_factory=list, max_length=4)
    recurrence: PublicRecurrence | None = None


class PublicTag(StrictModel):
    id: str
    title: TaintedText


class PublicIssue(StrictModel):
    path: str = Field(max_length=512)
    code: str = Field(max_length=120)
    hint: str = Field(max_length=1000)
    item_index: int | None = None
    item_id: str | None = Field(default=None, pattern=ITEM_ID, max_length=512)


class PublicEffect(StrictModel):
    kind: Literal["repeat_started"]
    explanation: str = Field(max_length=500)
    template_id: str | None = Field(default=None, pattern=ITEM_ID, max_length=512)
    instance_id: str | None = Field(default=None, pattern=ITEM_ID, max_length=512)


class PublicResult(StrictModel):
    state: Literal["ok", "pending", "applied", "unchanged", "not_applied", "partial", "partial_resolved", "stale", "declined", "rejected"]
    instruction: str
    code: Literal[
        "ok", "validation_error", "unknown_tool", "request_conflict",
        "write_fenced", "missing_target", "invalid_destination",
        "inactive_destination", "expanded_write_limit",
        "pending_unknown", "applied", "unchanged", "not_applied_precondition",
        "partial", "partial_resolved", "stale", "declined", "receipt_missing",
        "cursor_invalid", "read_unavailable", "internal_error",
    ]
    next_action: Literal[
        "none", "correct_request", "retry_same", "read_fresh", "read_receipt",
        "wait", "contact_operator", "continue_read",
    ]
    operation_id: str | None = None
    blocking_operation_ids: list[str] = Field(default_factory=list)
    items: list[PublicItem] = Field(default_factory=list)
    tags: list[PublicTag] = Field(default_factory=list)
    rows: list[dict[str, object]] = Field(default_factory=list)
    cursor: str | None = None
    receipt_hash: str | None = None
    missing_ids: list[str] = Field(default_factory=list)
    issues: list[PublicIssue] = Field(default_factory=list, max_length=20)
    effects: list[PublicEffect] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def coherent_outcome(self) -> Self:
        state_codes = {
            "ok": "ok", "pending": "pending_unknown",
            "applied": "applied", "unchanged": "unchanged",
            "not_applied": "not_applied_precondition", "partial": "partial",
            "partial_resolved": "partial_resolved", "stale": "stale", "declined": "declined",
        }
        if self.state in state_codes and self.code != state_codes[self.state]:
            raise ValueError("state and code disagree")
        state_actions = {
            "ok": "none",
            "pending": "retry_same",
            "applied": "read_receipt", "unchanged": "read_receipt",
            "not_applied": "read_receipt", "partial": "read_receipt",
            "partial_resolved": "none", "stale": "read_fresh", "declined": "none",
        }
        if self.state == "ok" and self.next_action == "continue_read":
            pass
        elif self.state in state_actions and self.next_action != state_actions[self.state]:
            raise ValueError("state and next_action disagree")
        if self.state == "rejected" and self.code == "ok":
            raise ValueError("rejected needs a rejection code")
        if self.state not in {"ok", "rejected"} and self.operation_id is None:
            raise ValueError("operation states require operation_id")
        if self.blocking_operation_ids and self.code != "write_fenced":
            raise ValueError("blocking IDs require write_fenced")
        if self.missing_ids and self.code != "missing_target":
            raise ValueError("missing IDs require missing_target")
        if self.next_action == "continue_read" and self.cursor is None:
            raise ValueError("continue_read needs a cursor")
        if self.rows and self.receipt_hash is None:
            raise ValueError("receipt rows require receipt_hash")
        return self


class ViewCall(StrictModel):
    view: Literal["today", "inbox", "week", "repeating", "logbook", "projects", "areas", "tags", "trash"] | None = None
    limit: int = Field(default=20, ge=1, le=40)
    cursor: str | None = None


class FindCall(StrictModel):
    text: str | None = Field(default=None, min_length=1, max_length=500)
    within: str | None = Field(
        default=None, pattern=r"^(project|area):[^\s:]+$", max_length=512
    )
    limit: int = Field(default=20, ge=1, le=40)
    cursor: str | None = None

    @model_validator(mode="after")
    def initial_or_continuation(self) -> Self:
        if self.cursor is not None and (self.text is not None or self.within is not None):
            raise ValueError("a find cursor already binds its search scope")
        if self.cursor is None and self.text is None and self.within is None:
            raise ValueError("find needs text, an exact Project or Area within, or a cursor")
        return self


class GetCall(StrictModel):
    ids: list[str] = Field(min_length=1, max_length=50)

    @field_validator("ids")
    @classmethod
    def exact_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("ids must be unique")
        if any(re.fullmatch(r"(?:task|project|area|heading):[^\s:]+", item) is None for item in value):
            raise ValueError("ids must be exact typed Things IDs")
        return value


class NestedTask(StrictModel):
    title: str = Field(min_length=1, max_length=1000)
    notes: str | None = Field(default=None, max_length=50_000)

    @field_validator("title")
    @classmethod
    def visible_title(cls, value: str) -> str:
        return _visible_title(value)


class _CaptureBase(StrictModel):
    title: str = Field(min_length=1, max_length=1000)
    notes: str | None = Field(default=None, max_length=50_000)
    start: str | None = None
    deadline: str | None = None

    @field_validator("title")
    @classmethod
    def visible_title(cls, value: str) -> str:
        return _visible_title(value)

    @field_validator("start")
    @classmethod
    def valid_start(cls, value: str | None) -> str | None:
        return _valid_start(value)

    @field_validator("deadline")
    @classmethod
    def valid_deadline(cls, value: str | None) -> str | None:
        return _valid_date(value)



class TaskCapture(_CaptureBase):
    kind: Literal["task"]
    into_id: str | None = Field(default=None, pattern=r"^(project|area):[^\s:]+$")
    repeat: RepeatCreate | None = Field(
        default=None,
        description="Optional complete semantic repeat rule for this Task.",
    )


class ProjectCapture(_CaptureBase):
    kind: Literal["project"]
    into_id: str | None = Field(default=None, pattern=r"^area:[^\s:]+$")
    tasks: list[NestedTask] = Field(default_factory=list, max_length=40)
    repeat: RepeatCreate | None = Field(
        default=None,
        description="Optional complete semantic repeat rule for this Project.",
    )


CaptureItem = Annotated[TaskCapture | ProjectCapture, Field(discriminator="kind")]


class CaptureCall(StrictModel):
    request_id: str = Field(pattern=REQUEST_ID)
    items: list[CaptureItem] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def bounded_expansion(self) -> Self:
        total = sum(
            (1 + len(item.tasks) if isinstance(item, ProjectCapture) else 1)
            * (2 if item.repeat is not None else 1)
            for item in self.items
        )
        if total > 120:
            raise ValueError("capture expands to at most 120 writes")
        return self


class CaptureDiscoveryItem(_CaptureBase):
    """Union-free discovery shape; runtime models enforce kind-specific fields."""

    kind: Literal["task", "project"] = Field(
        description="Tasks may use a Project or Area destination. Projects may use only an Area."
    )
    into_id: str | None = Field(
        default=None,
        pattern=r"^(project|area):[^\s:]+$",
        description="Optional exact destination. A Project destination is valid only for a Task.",
    )
    repeat: RepeatCreate | None = Field(
        default=None,
        description="Optional complete semantic repeat rule for a Task or Project.",
    )
    tasks: list[NestedTask] = Field(
        default_factory=list,
        max_length=40,
        description="Nested new Tasks. Valid only when kind is project.",
    )


class CaptureDiscoveryCall(StrictModel):
    request_id: str = Field(pattern=REQUEST_ID)
    items: list[CaptureDiscoveryItem] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def bounded_expansion(self) -> Self:
        total = sum(
            (1 + len(item.tasks) if item.kind == "project" else 1)
            * (2 if item.repeat is not None else 1)
            for item in self.items
        )
        if total > 120:
            raise ValueError("capture expands to at most 120 writes")
        return self


ExactTagId = Annotated[str, Field(pattern=r"^tag:[^\s:]+$", max_length=512)]
ExactChecklistId = Annotated[
    str, Field(pattern=r"^check:[^\s:]+$", max_length=512)
]


class TagDelta(StrictModel):
    add: list[ExactTagId] = Field(default_factory=list, max_length=40)
    remove: list[ExactTagId] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def exact_disjoint_delta(self) -> Self:
        for values in (self.add, self.remove):
            if len(values) != len(set(values)):
                raise ValueError("tag deltas need unique exact tag IDs")
        if set(self.add).intersection(self.remove):
            raise ValueError("a tag cannot be both added and removed")
        if not self.add and not self.remove:
            raise ValueError("tags needs an add or remove delta")
        return self


class ChecklistAddPatch(StrictModel):
    title: str = Field(min_length=1, max_length=1000)
    status: Literal["open", "completed", "canceled"] = "open"

    @field_validator("title")
    @classmethod
    def visible_title(cls, value: str) -> str:
        return _visible_title(value)


class ChecklistSetPatch(StrictModel):
    title: str | SkipJsonSchema[None] = Field(
        default=None, min_length=1, max_length=1000
    )
    status: (
        Literal["open", "completed", "canceled"] | SkipJsonSchema[None]
    ) = None

    @field_validator("title", "status", mode="before")
    @classmethod
    def no_implicit_clear(cls, value: object) -> object:
        if value is None:
            raise ValueError("checklist fields do not support null; omit unchanged fields")
        return value

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("checklist update set needs a field")
        return self


class ChecklistUpdatePatch(StrictModel):
    id: ExactChecklistId
    set: ChecklistSetPatch


class ChecklistPatch(StrictModel):
    add: list[ChecklistAddPatch] = Field(default_factory=list, max_length=100)
    update: list[ChecklistUpdatePatch] = Field(default_factory=list, max_length=100)
    remove: list[ExactChecklistId] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def exact_disjoint_patch(self) -> Self:
        updated = [row.id for row in self.update]
        if len(updated) != len(set(updated)):
            raise ValueError("each checklist row can be updated once")
        if len(self.remove) != len(set(self.remove)):
            raise ValueError("checklist remove needs unique exact check IDs")
        if set(updated).intersection(self.remove):
            raise ValueError("a checklist row cannot be updated and removed")
        if not self.add and not self.update and not self.remove:
            raise ValueError("checklist needs an add, update, or remove patch")
        if len(self.add) + len(self.update) + len(self.remove) > 100:
            raise ValueError("checklist patch expands to at most 100 rows")
        return self


class UpdateFields(StrictModel):
    title: str | SkipJsonSchema[None] = Field(default=None, min_length=1, max_length=1000)
    notes: str | SkipJsonSchema[None] = Field(default=None, max_length=50_000)
    start: str | None = Field(
        default=None,
        description="Today, tomorrow, evening, someday, anytime, an ISO date, or null.",
    )
    deadline: str | None = None
    remind_at: str | None = None
    into_id: str | SkipJsonSchema[None] = Field(
        default=None, pattern=r"^(project|area):[^\s:]+$", max_length=512
    )
    tags: TagDelta | SkipJsonSchema[None] = None
    checklist: ChecklistPatch | SkipJsonSchema[None] = None
    repeat: RepeatEdit | None = Field(
        default=None,
        description=(
            "Optional semantic repeat change, {create_next: true} for Create Next "
            "Copy, or {remove: true} to materialize the template as a fresh "
            "ordinary next-date item and delete the hidden template graph."
        ),
    )

    @field_validator("into_id", "tags", "checklist", mode="before")
    @classmethod
    def no_null_deep_field(cls, value: object) -> object:
        if value is None:
            raise ValueError("null is not supported; omit this field to preserve it")
        return value

    @field_validator("repeat", mode="before")
    @classmethod
    def no_null_repeat(cls, value: object) -> object:
        if value is None:
            raise ValueError("repeat null is not supported; use {remove: true}")
        return value

    @field_validator("title", "notes", mode="before")
    @classmethod
    def no_implicit_clear(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("null is not a supported clear operation")
        return value

    @field_validator("title")
    @classmethod
    def visible_title(cls, value: str) -> str:
        return _visible_title(value)

    @field_validator("start")
    @classmethod
    def valid_start(cls, value: str | None) -> str | None:
        return _valid_start(value)

    @field_validator("deadline")
    @classmethod
    def valid_deadline(cls, value: str | None) -> str | None:
        return _valid_date(value)

    @field_validator("remind_at")
    @classmethod
    def valid_reminder(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError as error:
                raise ValueError("remind_at needs an ISO 8601 date-time") from error
            if parsed.tzinfo is None:
                raise ValueError("remind_at needs an explicit time-zone offset")
        return value

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("set needs an explicit ordinary field")
        if (
            self.repeat is not None
            and self.repeat.remove
            and self.model_fields_set != {"repeat"}
        ):
            raise ValueError("repeat removal cannot combine with ordinary fields")
        return self


class UpdateItem(StrictModel):
    id: str = Field(pattern=r"^(task|project):[^\s:]+$", max_length=512)
    set: UpdateFields


class UpdateCall(StrictModel):
    request_id: str = Field(pattern=REQUEST_ID)
    items: list[UpdateItem] = Field(min_length=1, max_length=120)

    @field_validator("items")
    @classmethod
    def unique_targets(cls, value: list[UpdateItem]) -> list[UpdateItem]:
        ids = [item.id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("update targets must be unique")
        return value


class IdBatchCall(StrictModel):
    request_id: str = Field(pattern=REQUEST_ID)
    ids: list[str] = Field(min_length=1, max_length=120)

    @field_validator("ids")
    @classmethod
    def unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("ids must be unique")
        if any(re.fullmatch(r"(?:task|project):[^\s:]+", item) is None for item in value):
            raise ValueError("mutation ids must be exact Task or Project IDs")
        return value


class ReceiptCall(StrictModel):
    operation_id: str = Field(pattern=r"^op_[A-Za-z0-9_-]{8,120}$")
    limit: int = Field(default=40, ge=1, le=100)
    cursor: str | None = None


MODELS: dict[str, type[StrictModel]] = {
    "things_view": ViewCall,
    "things_find": FindCall,
    "things_get": GetCall,
    "things_capture": CaptureCall,
    "things_update": UpdateCall,
    "things_complete": IdBatchCall,
    "things_trash": IdBatchCall,
    "things_receipt": ReceiptCall,
}

DISCOVERY_MODELS: dict[str, type[StrictModel]] = {
    **MODELS,
    "things_capture": CaptureDiscoveryCall,
}

DESCRIPTIONS = {
    "things_view": "Read one current Things list.",
    "things_find": "Search by owner text, or read direct membership within one exact Project or Area. Continue pages with only the returned cursor.",
    "things_get": "Read one to fifty exact item IDs, including exact checklist and direct/inherited tag IDs.",
    "things_capture": "Create an atomic batch of Tasks or Projects with optional nested Project Tasks and a semantic repeat rule for Tasks or Projects.",
    "things_update": "Atomically set named fields, move Tasks or Projects in place, patch direct tags and exact checklist rows, or change a semantic repeat rule. Supports Anytime, Create Next Copy, and Stop.",
    "things_complete": "Complete one atomic batch of exact items.",
    "things_trash": "Move one atomic batch to recoverable Trash.",
    "things_receipt": "Read immutable content-minimized receipt rows.",
}


def _validate_repeat_on(
    unit: Literal["day", "week", "month", "year"],
    mode: Literal["fixed", "after_completion"],
    values: list[RepeatOn],
) -> None:
    if values and mode != "fixed":
        raise ValueError("selected dates need fixed repeat mode")
    if values and unit == "day":
        raise ValueError("daily repeats do not use selected dates")
    for value in values:
        if unit == "week" and (
            value.weekday is None
            or value.month is not None
            or value.ordinal is not None
        ):
            raise ValueError("weekly selected dates need plain weekdays")
        if unit == "month" and value.month is not None:
            raise ValueError("monthly selected dates do not name a month")
        if unit == "month" and value.weekday is not None and value.ordinal is None:
            raise ValueError("monthly weekdays need an ordinal")
        if unit == "year" and value.month is None:
            raise ValueError("yearly selected dates need a month")
        if unit == "year" and value.weekday is not None and value.ordinal is None:
            raise ValueError("yearly weekdays need an ordinal")


def _valid_start(value: str | None) -> str | None:
    if value is None or value in {"today", "tomorrow", "evening", "someday", "anytime"}:
        return value
    return _valid_date(value)


def _visible_title(value: str) -> str:
    if not value.strip():
        raise ValueError("title cannot be blank")
    return value


def _valid_date(value: str | None) -> str | None:
    if value is not None:
        try:
            date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("value needs an ISO 8601 date") from error
    return value


def flat_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Inline Pydantic definitions for MCP clients with shallow schema support."""

    schema = model.model_json_schema()
    definitions = cast(dict[str, object], schema.pop("$defs", {}))

    def inline(value: object) -> object:
        if isinstance(value, list):
            return [inline(item) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            target = definitions[reference.removeprefix("#/$defs/")]
            return inline(target)
        mapped = {
            key: inline(item)
            for key, item in value.items()
            if key != "discriminator"
        }
        variants = mapped.get("anyOf")
        if isinstance(variants, list) and len(variants) == 2 and {str(item) for item in variants if isinstance(item, dict) and item.get("type") == "null"}:
            non_null = next(item for item in variants if isinstance(item, dict) and item.get("type") != "null")
            if isinstance(non_null, dict) and isinstance(non_null.get("type"), str):
                mapped.pop("anyOf")
                mapped.update(non_null)
                mapped["type"] = [non_null["type"], "null"]
        return mapped

    return cast(dict[str, Any], inline(schema))


@dataclass(frozen=True, slots=True)
class _WithinPage:
    within: str
    revision: str
    remaining_ids: tuple[str, ...]


class ThingsV2:
    def __init__(self, workspace: Any) -> None:
        self.workspace = workspace
        self._cursor_routes: dict[str, str] = {}
        self._within_pages: dict[str, _WithinPage] = {}
        self._last_prune_date: date | None = None

    def dispatch(self, name: str, arguments: dict[str, Any]) -> PublicResult:
        current = self.workspace._clock()
        if self._last_prune_date != current.date():
            self.workspace._journal.prune_v2(
                now=current.isoformat(), retention_days=7
            )
            self._last_prune_date = current.date()
        call = MODELS[name].model_validate(arguments)
        if isinstance(call, ViewCall):
            return self._view(call)
        if isinstance(call, FindCall):
            return self._find(call)
        if isinstance(call, GetCall):
            return self._get(call.ids)
        if isinstance(call, ReceiptCall):
            operation = self.workspace._journal.get_v2_operation(call.operation_id)
            if operation is None or not same_account_id(
                operation.account_id, self.workspace._account_id
            ):
                return PublicResult(state="rejected", code="receipt_missing", next_action="correct_request", instruction="That receipt does not exist for this account.")
            try:
                page = self.workspace._journal.v2_receipt_page(self.workspace._account_id, call.operation_id, limit=call.limit, cursor=call.cursor)
            except (KeyError, ValueError):
                return PublicResult(state="rejected", code="cursor_invalid", next_action="correct_request", instruction="That receipt cursor is invalid or no longer retained.", operation_id=call.operation_id)
            return PublicResult(state=cast(Any, operation.state), code=cast(Any, _result_code(operation.state)), next_action=cast(Any, _result_next_action(operation.state)), instruction="Immutable receipt rows.", operation_id=call.operation_id, rows=page.rows, cursor=page.cursor, receipt_hash=page.receipt_hash)
        payload = call.model_dump(mode="json", exclude_unset=True)
        request_id = cast(str, payload.pop("request_id"))
        result = self.workspace.execute_v2(OperationDraft.build(name, request_id, payload))
        return self._mutation(result)

    def _view(self, call: ViewCall) -> PublicResult:
        if call.cursor is not None:
            route = self._cursor_routes.get(call.cursor)
            if route is None or not route.startswith("view:"):
                return self._invalid_read_cursor()
            public_view = route.removeprefix("view:")
            if call.view is not None and call.view != public_view:
                return self._invalid_read_cursor()
            result = self.workspace.read(ReadCall(limit=call.limit, cursor=call.cursor))
        else:
            public_view = call.view or "today"
            result = (
                self.workspace.read_v2_registry(
                    kind="project" if public_view == "projects" else "area",
                    limit=call.limit,
                )
                if public_view in {"projects", "areas"}
                else self.workspace.read(
                    ReadCall(view=cast(Any, public_view), limit=call.limit)
                )
            )
        if public_view == "tags":
            if result.status != "ok":
                return self._read_failure(result)
            cursor = self._remember_cursor(result.cursor, f"view:{public_view}")
            return PublicResult(
                state="ok",
                code="ok",
                next_action="continue_read" if cursor is not None else "none",
                instruction=(
                    "Current Things tags; more results remain. Continue with only this cursor."
                    if cursor is not None
                    else "Current Things tags."
                ),
                tags=[PublicTag(id=tag.id, title=TaintedText(value=tag.title)) for tag in result.tags],
                cursor=cursor,
            )
        return self._project_read(result, route=f"view:{public_view}")

    def _find(self, call: FindCall) -> PublicResult:
        if call.cursor is not None:
            if self._cursor_routes.get(call.cursor) != "find":
                return self._invalid_read_cursor()
            if call.cursor in self._within_pages:
                return self._continue_within_page(call.cursor, call.limit)
            result = self.workspace.read(ReadCall(cursor=call.cursor, limit=call.limit))
        else:
            if call.text is None:
                assert call.within is not None
                membership = self._within_membership(call.within)
                if isinstance(membership, PublicResult):
                    return membership
                records, revision = membership
                page, rest = records[: call.limit], records[call.limit :]
                cursor = self._store_within_page(
                    call.within, revision, [item.id for item in rest]
                )
                return self._within_result(page, cursor)
            else:
                result = self.workspace.read(
                    ReadCall(find=call.text, within=call.within, limit=call.limit)
                )
        return self._project_read(result, route="find")

    def _within_membership(
        self, within: str
    ) -> tuple[list[Any], str] | PublicResult:
        failed = self.workspace._refresh(force=True)
        if failed is not None:
            return PublicResult(
                state="rejected",
                code="read_unavailable",
                next_action="retry_same",
                instruction="Things Cloud is unavailable; retry this read.",
            )
        container = self.workspace._exact_item(within)
        expected_kind = "project" if within.startswith("project:") else "area"
        if (
            container is None
            or container.kind != expected_kind
            or container.status != "open"
            or container.trashed
        ):
            return PublicResult(
                state="rejected",
                code="missing_target",
                next_action="correct_request",
                instruction="That exact active container was not found.",
                missing_ids=[within],
            )
        records = sorted(
            [
                item
                for item in self.workspace._library.records.values()
                if item.is_open()
                and (
                    item.parent_uuid == container.uuid
                    if container.kind == "project"
                    else item.area_uuid == container.uuid
                    and item.parent_uuid is None
                )
            ],
            key=lambda item: (item.sort_index, item.uuid),
        )
        return records, self.workspace._scope_revision([container, *records])

    def _continue_within_page(self, cursor: str, limit: int) -> PublicResult:
        stored = self._within_pages[cursor]
        membership = self._within_membership(stored.within)
        if isinstance(membership, PublicResult):
            if membership.code == "read_unavailable":
                return membership
            self._within_pages.pop(cursor, None)
            self._cursor_routes.pop(cursor, None)
            return self._invalid_read_cursor()
        records, revision = membership
        if revision != stored.revision:
            return self._invalid_read_cursor()
        by_id = {item.id: item for item in records}
        if any(item_id not in by_id for item_id in stored.remaining_ids):
            return self._invalid_read_cursor()
        remaining = [by_id[item_id] for item_id in stored.remaining_ids]
        page, rest = remaining[:limit], remaining[limit:]
        next_cursor = self._store_within_page(
            stored.within, revision, [item.id for item in rest]
        )
        return self._within_result(page, next_cursor)

    def _store_within_page(
        self, within: str, revision: str, remaining_ids: list[str]
    ) -> str | None:
        if not remaining_ids:
            return None
        page = _WithinPage(
            within=within,
            revision=revision,
            remaining_ids=tuple(remaining_ids),
        )
        existing = next(
            (
                cursor
                for cursor, stored in self._within_pages.items()
                if stored == page
            ),
            None,
        )
        if existing is not None:
            return existing
        cursor = "cur_" + token_urlsafe(18)
        self._within_pages[cursor] = page
        self._cursor_routes[cursor] = "find"
        while len(self._within_pages) > 256:
            expired = next(iter(self._within_pages))
            del self._within_pages[expired]
            self._cursor_routes.pop(expired, None)
        return cursor

    def _within_result(
        self, records: list[Any], cursor: str | None
    ) -> PublicResult:
        return PublicResult(
            state="ok",
            code="ok",
            next_action="continue_read" if cursor else "none",
            instruction=(
                "Current Things facts; more results remain. Continue with only this cursor."
                if cursor
                else "Current Things facts."
            ),
            items=[
                self._item(
                    self.workspace._fact(
                        item, full=False, include_revision=False
                    )
                )
                for item in records
            ],
            cursor=cursor,
        )

    def _get(self, ids: list[str]) -> PublicResult:
        items: list[Any] = []
        for offset in range(0, len(ids), 10):
            result = self.workspace.read(
                ReadCall(
                    ids=ids[offset : offset + 10],
                    fields=["notes", "checklist", "tags", "recurrence"],
                )
            )
            if result.status == "unavailable":
                return PublicResult(
                    state="rejected", code="read_unavailable", next_action="retry_same",
                    instruction="Things Cloud is unavailable; no IDs were classified as missing.",
                )
            items.extend(result.items)
        found = {item.id for item in items}
        missing = [item_id for item_id in ids if item_id not in found]
        return PublicResult(
            state="rejected" if missing else "ok",
            code="missing_target" if missing else "ok",
            next_action="correct_request" if missing else "none",
            instruction="Some exact IDs were not found." if missing else "Current exact items.",
            items=[self._item(item) for item in items],
            missing_ids=missing,
        )

    def _mutation(self, result: dict[str, object]) -> PublicResult:
        item_ids = cast(list[str], result.pop("item_ids", []))
        fresh_items = result.pop("_fresh_items", False) is True
        items = [
            self._item(
                self.workspace._fact(
                    item,
                    full=True,
                    include_revision=False,
                    detail=("notes", "checklist", "tags", "recurrence"),
                )
            )
            for item_id in item_ids
            if (item := self.workspace._exact_item(item_id)) is not None
        ] if fresh_items else self._get(item_ids).items if item_ids else []
        effects: list[PublicEffect] = []
        issues = [
            PublicIssue.model_validate(issue)
            for issue in cast(list[object], result.get("issues", []))
        ]
        if result.get("state") == "rejected" and not issues:
            issues = [PublicIssue(
                path=cast(str, result.get("issue_path", "items")),
                code=cast(str, result.get("code", "validation_error")),
                hint=cast(str, result.get("instruction", "Correct the request.")),
                item_id=cast(str | None, result.get("issue_item_id")),
            )]
        operation_id = cast(str | None, result.get("operation_id"))
        if operation_id is not None and result.get("state") in {"applied", "unchanged"}:
            operation = self.workspace._journal.get_v2_operation(operation_id)
            if operation is not None:
                writes = cast(list[dict[str, object]], operation.manifest.get("writes", []))
                templates = [row for row in writes if row.get("action") == "create" and row.get("recurrence_rule")]
                for template in templates:
                    template_uuid = template.get("uuid")
                    linked = next(
                        (
                            row for row in writes
                            if row.get("action") in {"create", "repeat_link"}
                            and isinstance(row.get("recurrence_links"), list)
                            and template_uuid in cast(list[object], row["recurrence_links"])
                        ),
                        None,
                    )
                    effects.append(PublicEffect(
                        kind="repeat_started",
                        explanation="Starting this repeat creates one hidden template and one visible current instance.",
                        template_id=(f"{template.get('kind', 'task')}:{template_uuid}" if isinstance(template_uuid, str) else None),
                        instance_id=(f"{linked.get('kind', 'task')}:{linked['uuid']}" if linked is not None and isinstance(linked.get("uuid"), str) else None),
                    ))
        return PublicResult(
            state=cast(Any, result["state"]),
            code=cast(Any, result.get("code", _result_code(cast(str, result["state"])))),
            next_action=cast(Any, result.get("next_action", _result_next_action(cast(str, result["state"])))),
            instruction=cast(str, result["instruction"]),
            operation_id=operation_id,
            blocking_operation_ids=cast(list[str], result.get("blocking_operation_ids", [])),
            items=items,
            effects=effects,
            issues=issues,
        )

    def _project_read(
        self,
        result: Any,
        *,
        items: list[Any] | None = None,
        route: str | None = None,
    ) -> PublicResult:
        ok = result.status == "ok"
        if not ok:
            return self._read_failure(result)
        cursor = self._remember_cursor(result.cursor, route)
        return PublicResult(
            state="ok",
            code="ok",
            next_action="continue_read" if cursor is not None else "none",
            instruction=(
                "Current Things facts; more results remain. Continue with only this cursor."
                if cursor is not None
                else "Current Things facts."
            ),
            items=[self._item(item) for item in (result.items if items is None else items)],
            cursor=cursor,
        )

    @staticmethod
    def _read_failure(result: Any) -> PublicResult:
        if result.status == "stale":
            return PublicResult(
                state="rejected",
                code="cursor_invalid",
                next_action="correct_request",
                instruction="That cursor is invalid or stale. Start the read again.",
            )
        if result.status == "unavailable":
            return PublicResult(
                state="rejected",
                code="read_unavailable",
                next_action="retry_same",
                instruction="Things Cloud is unavailable; retry this read.",
            )
        return PublicResult(
            state="rejected",
            code="validation_error",
            next_action="correct_request",
            instruction="That read request is not valid for the current Things state.",
        )

    def _remember_cursor(self, cursor: str | None, route: str | None) -> str | None:
        if cursor is None or route is None:
            return cursor
        self._cursor_routes[cursor] = route
        while len(self._cursor_routes) > 256:
            del self._cursor_routes[next(iter(self._cursor_routes))]
        return cursor

    @staticmethod
    def _invalid_read_cursor() -> PublicResult:
        return PublicResult(
            state="rejected",
            code="cursor_invalid",
            next_action="correct_request",
            instruction="That read cursor is invalid or belongs to another tool.",
        )

    def _item(self, item: Any) -> PublicItem:
        record = self.workspace._library.records.get(item.id.partition(":")[2])
        start = item.start
        if (
            start is None
            and record is not None
            and record.kind in {"task", "project"}
            and not record.heading
            and not record.inbox
        ):
            start = "anytime"
        return PublicItem(
            id=item.id,
            kind=item.kind,
            title=TaintedText(value=item.title),
            status=item.status,
            notes=TaintedText(value=item.notes_markdown)
            if item.notes_markdown
            else None,
            into_id=item.into_id,
            start=start,
            deadline=item.deadline,
            tags=[
                TaintedText(value=tag.title)
                for tag in [*item.direct_tags, *item.inherited_tags]
            ],
            checklist=[
                PublicChecklistRow(
                    id=row.id,
                    title=TaintedText(value=row.title),
                    status=row.status,
                )
                for row in item.checklist
            ],
            direct_tag_ids=list(item.direct_tag_ids),
            inherited_tag_ids=list(item.inherited_tag_ids),
            truncated_fields=list(item.truncated_fields),
            recurrence=(
                PublicRecurrence.model_validate(item.recurrence.model_dump())
                if item.recurrence is not None
                else None
            ),
        )


def _result_code(state: str) -> str:
    return {
        "pending": "pending_unknown",
        "applied": "applied",
        "unchanged": "unchanged",
        "not_applied": "not_applied_precondition",
        "partial": "partial",
        "partial_resolved": "partial_resolved",
        "stale": "stale",
        "declined": "declined",
    }.get(state, "validation_error" if state == "rejected" else "ok")


def _result_next_action(state: str) -> str:
    if state == "stale":
        return "read_fresh"
    if state == "pending":
        return "retry_same"
    if state in {"applied", "unchanged", "not_applied", "partial"}:
        return "read_receipt"
    return "correct_request" if state == "rejected" else "none"
