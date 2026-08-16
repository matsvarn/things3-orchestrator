"""Compile contextual model drafts into the exact legacy commit interface."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .context import ContextRef, ReadContext, UnknownReference
from .interface import CommitCall, OrganizeDraft
from .library import MemoryLibrary, Record, parse_id

_SHORT_REF = re.compile(r"^[a-z][a-z0-9]{0,11}$")


class ContextualCompileError(ValueError):
    """A contextual request cannot become one safe legacy commit."""


class ContextualInputError(ContextualCompileError):
    """The request contains redundant identity that disagrees with its ref."""


@dataclass(frozen=True)
class _Bound:
    context: ContextRef
    record: Record


@dataclass(slots=True)
class _CompileState:
    """Mutable compilation state shared by one contextual commit."""

    creates: list[dict[str, Any]]
    create_by_key: dict[str, dict[str, Any]]
    changes: dict[str, dict[str, Any]]
    change_order: list[str]
    claimed_local_tasks: set[str]
    claimed_exact_tasks: set[str]

    @classmethod
    def from_call(cls, call: CommitCall) -> _CompileState:
        creates = [
            entry.model_dump(mode="python", exclude_unset=True) for entry in call.create
        ]
        return cls(
            creates=creates,
            create_by_key={
                payload["key"]: payload
                for payload in creates
                if isinstance(payload.get("key"), str)
            },
            changes={},
            change_order=[],
            claimed_local_tasks=set(),
            claimed_exact_tasks=set(),
        )


def _kind_phrase(kinds: set[str], *, article: bool = True) -> str:
    """Render stable user-facing kind names for contextual input errors."""
    labels = {
        "area": "Area",
        "heading": "heading",
        "project": "Project",
        "task": "Task",
    }
    names = [labels.get(kind, kind) for kind in sorted(kinds)]
    if not names:
        return "no destination"
    if len(names) == 1:
        prefix = ""
        if article:
            prefix = "an " if names[0][0].lower() in "aeiou" else "a "
        return f"{prefix}{names[0]}"
    return " or ".join(names)


class _Index:
    def __init__(self, context: ReadContext, library: MemoryLibrary) -> None:
        self.context = context
        self._library = library
        self._by_ref: dict[str, _Bound] = {}
        self._by_id: dict[str, _Bound] = {}
        for entry in context.refs:
            record = _exact_record(library, entry.exact_id)
            if record is None:
                raise ContextualCompileError(
                    f"context item is no longer available: {entry.ref}"
                )
            bound = _Bound(context=entry, record=record)
            self._by_ref[entry.ref] = bound
            self._by_id[entry.exact_id] = bound

    def ref(self, value: str) -> _Bound:
        try:
            return self._by_ref[value]
        except KeyError as error:
            raise UnknownReference(f"unknown context reference: {value}") from error

    def exact(self, value: str) -> _Bound:
        try:
            return self._by_id[value]
        except KeyError as error:
            raise ContextualCompileError(
                f"complete context is missing exact item: {value}"
            ) from error

    def project(self, value: str) -> _Bound:
        bound = self.ref(value)
        if bound.record.public_kind != "project":
            raise ContextualCompileError("project_ref must identify a Project")
        return bound

    def area(self, value: str) -> _Bound:
        bound = self.ref(value)
        if bound.record.public_kind != "area":
            raise ContextualInputError("into ref must identify an Area")
        return bound

    def members(self, project: _Bound) -> tuple[_Bound, ...]:
        return tuple(
            self.exact(member.id) for member in self._library.project(project.record.id)
        )

    def heading(self, value: str, project: _Bound) -> _Bound:
        bound = self.ref(value)
        if not bound.record.heading:
            raise ContextualCompileError("heading_ref must identify a heading")
        if bound.record.parent_uuid != project.record.uuid or bound.record.trashed:
            raise ContextualCompileError(
                "heading_ref must belong to the organized Project"
            )
        return bound

    def task(self, value: str, project: _Bound) -> _Bound:
        bound = self.ref(value)
        record = bound.record
        if record.heading or record.kind != "task":
            raise ContextualCompileError("task_refs must identify Tasks")
        if (
            record.parent_uuid != project.record.uuid
            or record.trashed
            or record.status != "open"
            or record.recurrence.role == "template"
        ):
            raise ContextualCompileError(
                "task_ref must be active work in the organized Project"
            )
        return bound


class ContextualCommitCompiler:
    """Deep compiler from short contextual intent to exact commit facts."""

    def compile(
        self,
        call: CommitCall,
        context: ReadContext,
        library: MemoryLibrary,
    ) -> CommitCall:
        if call.context_id != context.id:
            raise ContextualCompileError("commit context_id does not match the context")
        index = _Index(context, library)
        state = _CompileState.from_call(call)

        for change in call.change:
            payload = change.model_dump(mode="python", exclude_unset=True)
            if change.ref is not None:
                bound = index.ref(change.ref)
                if change.id is not None and change.id != bound.context.exact_id:
                    raise ContextualInputError(
                        "The context ref is authoritative. Remove id when using ref."
                    )
                if (
                    change.if_revision is not None
                    and change.if_revision != bound.context.revision
                ):
                    raise ContextualInputError(
                        "The context ref is authoritative. Remove if_revision when using ref."
                    )
                payload.pop("ref", None)
                payload["id"] = bound.context.exact_id
                payload["if_revision"] = bound.context.revision
            else:
                # An exact change is still contextual when context_id is present.
                # Resolve it through the context so a model cannot use the
                # contextual seam to write an unrelated exact item. Keep this
                # form tolerant: callers that already have an exact id and
                # revision need not spend another turn converting to a ref.
                assert isinstance(change.id, str)
                assert isinstance(change.if_revision, str)
                bound = index.exact(change.id)
                if change.if_revision != bound.context.revision:
                    raise ContextualInputError(
                        "The exact item revision does not match this context. "
                        "Read the item again."
                    )
                payload["id"] = bound.context.exact_id
                payload["if_revision"] = bound.context.revision
            self._compile_change_relationships(
                payload,
                source=bound,
                index=index,
                state=state,
            )
            assert isinstance(payload.get("id"), str)
            assert isinstance(payload.get("if_revision"), str)
            _merge_change(state.changes, state.change_order, payload)

        for draft in call.organize:
            self._compile_draft(
                draft,
                context=context,
                index=index,
                state=state,
            )

        # A contextual commit is allowed to contain relationship destinations,
        # but each existing destination must be part of the read evidence.  Do
        # this after organize has merged local create ownership, so a conflict
        # between an input destination and an organize destination is reported
        # as such instead of being mistaken for a missing context item.
        for payload in state.creates:
            self._compile_create_relationships(payload, index=index, state=state)

        try:
            ordered_creates = _ordered_creates(state.creates)
            return CommitCall.model_validate(
                {
                    "intent_id": call.intent_id,
                    "scope_revision": call.scope_revision,
                    "tags_revision": call.tags_revision,
                    "ensure_tags": [
                        entry.model_dump(mode="python", exclude_unset=True)
                        for entry in call.ensure_tags
                    ],
                    "change_tags": [
                        entry.model_dump(mode="python", exclude_unset=True)
                        for entry in call.change_tags
                    ],
                    "create": ordered_creates,
                    "change": [state.changes[item_id] for item_id in state.change_order],
                }
            )
        except ValidationError as error:
            raise ContextualCompileError(
                "the contextual plan cannot use the legacy commit interface"
            ) from error

    def _compile_create_relationships(
        self,
        payload: dict[str, Any],
        *,
        index: _Index,
        state: _CompileState,
    ) -> None:
        """Bind every existing create destination to this read context."""
        kind = payload.get("kind", "task")
        if not isinstance(kind, str):
            raise ContextualCompileError("a create kind must be a string")

        if "into" in payload:
            allowed = {"project"} if kind == "heading" else (
                {"area"} if kind == "project" else {"area", "project"}
            )
            payload["into"] = self._relationship(
                payload["into"],
                label="create.into",
                allowed=allowed,
                index=index,
                state=state,
                safe={"inbox", "anytime"},
            )
        if "after" in payload:
            after_kind = "heading" if kind == "heading" else kind
            payload["after"] = self._relationship(
                payload["after"],
                label="create.after",
                allowed={after_kind},
                index=index,
                state=state,
            )
        if "today_after" in payload:
            payload["today_after"] = self._relationship(
                payload["today_after"],
                label="create.today_after",
                allowed={"task", "project", "area"},
                index=index,
                state=state,
            )
        if "heading_id" in payload:
            payload["heading_id"] = self._relationship(
                payload["heading_id"],
                label="create.heading_id",
                allowed={"heading"},
                index=index,
                state=state,
            )

    def _compile_change_relationships(
        self,
        payload: dict[str, Any],
        *,
        source: _Bound,
        index: _Index,
        state: _CompileState,
    ) -> None:
        """Bind existing change destinations and check their required kinds."""
        source_kind = source.record.public_kind
        if "into" in payload:
            if source_kind == "heading":
                allowed = {"project"}
            elif source_kind == "project":
                allowed = {"area"}
            elif source_kind == "task":
                allowed = {"area", "project"}
            else:
                allowed = set()
            payload["into"] = self._relationship(
                payload["into"],
                label="change.into",
                allowed=allowed,
                index=index,
                state=state,
                safe={"inbox", "anytime"},
            )
        if "after" in payload:
            after_kind = "heading" if source_kind == "heading" else source_kind
            payload["after"] = self._relationship(
                payload["after"],
                label="change.after",
                allowed={after_kind},
                index=index,
                state=state,
            )
        if "today_after" in payload:
            payload["today_after"] = self._relationship(
                payload["today_after"],
                label="change.today_after",
                allowed={"task", "project", "area"},
                index=index,
                state=state,
            )
        if "heading_id" in payload:
            payload["heading_id"] = self._relationship(
                payload["heading_id"],
                label="change.heading_id",
                allowed={"heading"},
                index=index,
                state=state,
            )
        if "move_contents_to" in payload:
            payload["move_contents_to"] = self._relationship(
                payload["move_contents_to"],
                label="change.move_contents_to",
                allowed={"area"},
                index=index,
                state=state,
            )

    @staticmethod
    def _relationship(
        value: Any,
        *,
        label: str,
        allowed: set[str],
        index: _Index,
        state: _CompileState,
        safe: set[str] | None = None,
    ) -> Any:
        """Resolve one relationship through context or an in-request create."""
        if value is None:
            return None
        if not isinstance(value, str):
            raise ContextualCompileError(f"{label} must be a reference")
        if safe is not None and value in safe:
            return value
        if value.startswith("$"):
            created = state.create_by_key.get(value)
            if created is None:
                raise ContextualCompileError(
                    f"{label} local reference is not a created item: {value}"
                )
            created_kind = created.get("kind", "task")
            if created_kind not in allowed:
                raise ContextualInputError(
                    f"{label} must identify {_kind_phrase(allowed)}; got "
                    f"{_kind_phrase({str(created_kind)}, article=False)}"
                )
            return value
        bound = index.ref(value) if _SHORT_REF.fullmatch(value) else index.exact(value)
        if bound.record.public_kind not in allowed:
            expected = _kind_phrase(allowed)
            raise ContextualInputError(
                f"{label} must identify {expected}; got "
                f"{_kind_phrase({bound.record.public_kind}, article=False)}"
            )
        # Always emit the exact ID stored in the context.  This also makes an
        # exact ID and its short ref equivalent without trusting model input.
        return bound.context.exact_id

    def _compile_draft(
        self,
        draft: OrganizeDraft,
        *,
        context: ReadContext,
        index: _Index,
        state: _CompileState,
    ) -> None:
        if context.selector.purpose != "organize":
            raise ContextualCompileError("organize drafts need an organize read")
        project = index.project(draft.project_ref)
        project_id = project.record.id
        if not context.is_complete(project_id):
            raise ContextualCompileError(
                f"organize needs complete Project scope: {project_id}"
            )
        if context.selector.view == "project" and context.selector.within != project_id:
            raise ContextualCompileError(
                "organize Project differs from the read context scope"
            )
        index.members(project)

        heading_tokens: list[str] = []
        task_layout: list[tuple[str, str | None]] = []
        kept_heading_ids: set[str] = set()

        for section in draft.sections:
            heading_token: str | None = None
            if section.heading_ref is not None:
                heading = index.heading(section.heading_ref, project)
                heading_token = heading.record.id
                heading_tokens.append(heading_token)
                kept_heading_ids.add(heading_token)
                if section.heading_title is not None:
                    _merge_generated_change(
                        state.changes,
                        state.change_order,
                        heading,
                        {"title": section.heading_title},
                    )
            elif section.heading_key is not None:
                heading_token = section.heading_key
                heading_tokens.append(heading_token)
                heading_payload: dict[str, Any] = {
                    "key": section.heading_key,
                    "kind": "heading",
                    "title": section.heading_title,
                    "into": project_id,
                }
                state.creates.append(heading_payload)
                state.create_by_key[section.heading_key] = heading_payload

            for task_ref in section.task_refs:
                task_layout.append((task_ref, heading_token))

        previous_heading: str | None = None
        for token in heading_tokens:
            if token.startswith("$"):
                state.create_by_key[token]["after"] = previous_heading
                previous_heading = token
                continue
            heading = index.exact(token)
            _merge_generated_change(
                state.changes,
                state.change_order,
                heading,
                {"after": previous_heading},
            )
            previous_heading = token

        previous_task: str | None = None
        for task_ref, heading_token in task_layout:
            if task_ref.startswith("$"):
                if task_ref in state.claimed_local_tasks:
                    raise ContextualCompileError(
                        f"local Task appears in more than one draft: {task_ref}"
                    )
                state.claimed_local_tasks.add(task_ref)
                task_payload = state.create_by_key.get(task_ref)
                if task_payload is None or task_payload.get("kind", "task") != "task":
                    raise ContextualCompileError(
                        f"organize local ref must identify a created Task: {task_ref}"
                    )
                _set_create(task_payload, "into", project_id)
                _set_create(task_payload, "heading_id", heading_token)
                _set_create(task_payload, "after", previous_task)
            else:
                task = index.task(task_ref, project)
                if task.record.id in state.claimed_exact_tasks:
                    raise ContextualCompileError(
                        f"Task appears in more than one draft: {task.record.id}"
                    )
                state.claimed_exact_tasks.add(task.record.id)
                _merge_generated_change(
                    state.changes,
                    state.change_order,
                    task,
                    {"heading_id": heading_token, "after": previous_task},
                )
                task_ref = task.record.id
            previous_task = task_ref

        for deleted_ref in draft.delete_headings:
            deleted = index.heading(deleted_ref, project)
            if deleted.record.id in kept_heading_ids:
                raise ContextualCompileError("a heading cannot be kept and deleted")
            _merge_generated_change(
                state.changes,
                state.change_order,
                deleted,
                {"lifecycle": "delete_permanently"},
            )


def _exact_record(library: MemoryLibrary, exact_id: str) -> Record | None:
    kind, uuid = parse_id(exact_id)
    if kind is None:
        return None
    record = library.records.get(uuid)
    if record is None or record.id != exact_id:
        return None
    return record


def _merge_generated_change(
    changes: dict[str, dict[str, Any]],
    order: list[str],
    bound: _Bound,
    fields: Mapping[str, Any],
) -> None:
    _merge_change(
        changes,
        order,
        {
            "id": bound.context.exact_id,
            "if_revision": bound.context.revision,
            **fields,
        },
    )


def _merge_change(
    changes: dict[str, dict[str, Any]],
    order: list[str],
    payload: Mapping[str, Any],
) -> None:
    item_id = payload.get("id")
    revision = payload.get("if_revision")
    if not isinstance(item_id, str) or not isinstance(revision, str):
        raise ContextualCompileError("an exact change needs an ID and revision")
    current = changes.get(item_id)
    if current is None:
        changes[item_id] = dict(payload)
        order.append(item_id)
        return
    if current["if_revision"] != revision:
        raise ContextualCompileError(f"conflicting revisions for {item_id}")
    for field, value in payload.items():
        if field in {"id", "if_revision"}:
            continue
        if field in current and current[field] != value:
            raise ContextualCompileError(
                f"conflicting contextual values for {item_id}.{field}"
            )
        current[field] = value


def _set_create(payload: dict[str, Any], field: str, value: Any) -> None:
    if field in payload and payload[field] != value:
        key = payload.get("key", "created item")
        raise ContextualCompileError(f"conflicting organize value for {key}.{field}")
    payload[field] = value


def _ordered_creates(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Place local dependencies first while keeping unrelated input stable."""
    key_to_index = {
        value: index
        for index, payload in enumerate(payloads)
        if isinstance((value := payload.get("key")), str)
    }
    dependencies: list[set[int]] = []
    for payload in payloads:
        required: set[int] = set()
        for field in ("into", "after", "today_after", "heading_id"):
            value = payload.get(field)
            if isinstance(value, str) and value.startswith("$"):
                dependency = key_to_index.get(value)
                if dependency is None:
                    raise ContextualCompileError(
                        f"unknown local create dependency: {value}"
                    )
                required.add(dependency)
        dependencies.append(required)

    remaining = set(range(len(payloads)))
    emitted: set[int] = set()
    output: list[dict[str, Any]] = []
    while remaining:
        available = sorted(
            index for index in remaining if dependencies[index] <= emitted
        )
        if not available:
            raise ContextualCompileError("local create references contain a cycle")
        index = available[0]
        output.append(payloads[index])
        remaining.remove(index)
        emitted.add(index)
    return output
