from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from things_orchestrator.context import (
    CompletenessFact,
    ContextRef,
    MemoryContextStore,
    ReadContext,
    ReadSelector,
    UnknownReference,
)
from things_orchestrator.contextual import (
    ContextualCommitCompiler,
    ContextualCompileError,
)
from things_orchestrator.interface import CommitCall, ReadCall
from things_orchestrator.library import MemoryLibrary, Record
from things_orchestrator.workspace import ThingsWorkspace

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def project_library() -> MemoryLibrary:
    return MemoryLibrary(
        [
            Record(uuid="p", kind="project", title="Launch"),
            Record(
                uuid="h1",
                kind="task",
                title="Plan",
                parent_uuid="p",
                heading=True,
                sort_index=0,
            ),
            Record(
                uuid="h2",
                kind="task",
                title="Deliver",
                parent_uuid="p",
                heading=True,
                sort_index=1024,
            ),
            Record(
                uuid="t1",
                kind="task",
                title="Draft",
                parent_uuid="p",
                heading_uuid="h1",
                sort_index=0,
            ),
            Record(
                uuid="t2",
                kind="task",
                title="Ship",
                parent_uuid="p",
                heading_uuid="h2",
                sort_index=1024,
            ),
            Record(
                uuid="t3",
                kind="task",
                title="Review",
                parent_uuid="p",
                heading_uuid="h2",
                sort_index=2048,
            ),
            Record(
                uuid="untouched",
                kind="task",
                title="Keep relative",
                parent_uuid="p",
                sort_index=3072,
            ),
        ]
    )


def organize_context(
    library: MemoryLibrary,
    *,
    complete: bool = True,
    include: set[str] | None = None,
) -> ReadContext:
    names = {
        "project:p": "p1",
        "heading:h1": "h1",
        "heading:h2": "h2",
        "task:t1": "t1",
        "task:t2": "t2",
        "task:t3": "t3",
        "task:untouched": "u1",
        "project:foreign": "pf",
        "heading:foreign": "hf",
        "task:foreign": "tf",
    }
    exact_ids = include or {record.id for record in library.project("project:p")}
    refs = tuple(
        ContextRef(
            ref=names[exact_id],
            exact_id=exact_id,
            revision=f"revision-{exact_id}",
        )
        for exact_id in sorted(exact_ids)
    )
    return ReadContext(
        id="ctx_12345678",
        account_binding="sha256:account",
        selector=ReadSelector(
            purpose="organize",
            view="project",
            within="project:p",
            limit=200,
        ),
        refs=refs,
        completeness=(
            CompletenessFact(
                scope="project:p",
                seen=len(refs),
                total=len(refs) if complete else len(refs) + 1,
                next_cursor=None if complete else "more",
                complete=complete,
            ),
        ),
        expires_at=NOW,
    )


def compile_call(
    payload: dict[str, object],
    *,
    library: MemoryLibrary | None = None,
    context: ReadContext | None = None,
) -> CommitCall:
    current = library or project_library()
    evidence = context or organize_context(current)
    return ContextualCommitCompiler().compile(
        CommitCall.model_validate(payload), evidence, current
    )


def changes_by_id(call: CommitCall) -> dict[str, object]:
    return {change.id: change for change in call.change if change.id is not None}


def test_contextual_change_resolves_exact_id_and_revision() -> None:
    library = MemoryLibrary([Record(uuid="one", kind="task", title="Old")])
    context = ReadContext(
        id="ctx_12345678",
        account_binding="sha256:account",
        selector=ReadSelector(purpose="change", item_id="task:one"),
        refs=(ContextRef("t1", "task:one", "revision-one"),),
        completeness=(),
        expires_at=NOW,
    )

    compiled = compile_call(
        {
            "intent_id": "context-change-001",
            "context_id": context.id,
            "change": [{"ref": "t1", "title": "New"}],
        },
        library=library,
        context=context,
    )

    assert compiled.context_id is None
    assert compiled.organize == []
    assert compiled.change[0].id == "task:one"
    assert compiled.change[0].if_revision == "revision-one"
    assert compiled.change[0].ref is None
    assert compiled.change[0].title == "New"


def test_contextual_change_accepts_an_exact_item_bound_to_the_context() -> None:
    library = MemoryLibrary([Record(uuid="one", kind="task", title="Old")])
    context = ReadContext(
        id="ctx_12345678",
        account_binding="sha256:account",
        selector=ReadSelector(purpose="change", item_id="task:one"),
        refs=(ContextRef("t1", "task:one", "revision-one"),),
        completeness=(),
        expires_at=NOW,
    )

    compiled = compile_call(
        {
            "intent_id": "context-exact-001",
            "context_id": context.id,
            "change": [
                {
                    "id": "task:one",
                    "if_revision": "revision-one",
                    "title": "New",
                }
            ],
        },
        library=library,
        context=context,
    )

    assert compiled.change[0].id == "task:one"
    assert compiled.change[0].if_revision == "revision-one"
    assert compiled.change[0].title == "New"


def test_contextual_change_rejects_an_exact_item_outside_the_context() -> None:
    library = MemoryLibrary(
        [
            Record(uuid="one", kind="task", title="A"),
            Record(uuid="two", kind="task", title="B"),
        ]
    )
    context = ReadContext(
        id="ctx_12345678",
        account_binding="sha256:account",
        selector=ReadSelector(purpose="change", item_id="task:one"),
        refs=(ContextRef("t1", "task:one", "revision-one"),),
        completeness=(),
        expires_at=NOW,
    )

    with pytest.raises(ContextualCompileError, match="missing exact item"):
        compile_call(
            {
                "intent_id": "context-outside-001",
                "context_id": context.id,
                "change": [
                    {
                        "id": "task:two",
                        "if_revision": "revision-two",
                        "title": "Must not apply",
                    }
                ],
            },
            library=library,
            context=context,
        )


def test_contextual_area_destination_rejects_a_task_ref() -> None:
    library = MemoryLibrary(
        [
            Record(uuid="project", kind="project", title="Website"),
            Record(uuid="task", kind="task", title="Draft"),
            Record(uuid="area", kind="area", title="Business"),
        ]
    )
    context = ReadContext(
        id="ctx_12345678",
        account_binding="sha256:account",
        selector=ReadSelector(purpose="change", item_id="project:project"),
        refs=(
            ContextRef("p1", "project:project", "revision-project"),
            ContextRef("t1", "task:task", "revision-task"),
            ContextRef("a1", "area:area", "revision-area"),
        ),
        completeness=(),
        expires_at=NOW,
    )

    with pytest.raises(ContextualCompileError, match="must identify an Area"):
        compile_call(
            {
                "intent_id": "context-task-area-001",
                "context_id": context.id,
                "change": [{"ref": "p1", "into": "t1"}],
            },
            library=library,
            context=context,
        )


def test_contextual_compiler_resolves_short_relationship_refs_end_to_end() -> None:
    library = MemoryLibrary(
        [
            Record(uuid="project", kind="project", title="Launch"),
            Record(uuid="area", kind="area", title="Work"),
            Record(uuid="other-area", kind="area", title="Home"),
            Record(
                uuid="heading",
                kind="task",
                title="Next",
                parent_uuid="project",
                heading=True,
            ),
            Record(
                uuid="task",
                kind="task",
                title="Draft",
                parent_uuid="project",
            ),
            Record(
                uuid="anchor",
                kind="task",
                title="Anchor",
                parent_uuid="project",
            ),
        ]
    )
    context = ReadContext(
        id="ctx_12345678",
        account_binding="sha256:account",
        selector=ReadSelector(purpose="change", item_id="task:task"),
        refs=tuple(
            ContextRef(ref, exact_id, f"revision-{uuid}")
            for ref, exact_id, uuid in (
                ("p1", "project:project", "project"),
                ("a1", "area:area", "area"),
                ("a2", "area:other-area", "other-area"),
                ("h1", "heading:heading", "heading"),
                ("t1", "task:task", "task"),
                ("t2", "task:anchor", "anchor"),
            )
        ),
        completeness=(),
        expires_at=NOW,
    )

    compiled = compile_call(
        {
            "intent_id": "context-relationships-001",
            "context_id": context.id,
            "scope_revision": "scope-1",
            "create": [
                {
                    "kind": "heading",
                    "title": "Later",
                    "into": "p1",
                    "after": "h1",
                },
                {
                    "title": "Follow up",
                    "into": "p1",
                    "after": "t1",
                    "today_after": "t2",
                    "heading_id": "h1",
                },
            ],
            "change": [
                {
                    "ref": "t1",
                    "into": "p1",
                    "after": "t2",
                    "today_after": "t2",
                    "heading_id": "h1",
                },
                {"ref": "a1", "move_contents_to": "a2"},
            ],
        },
        library=library,
        context=context,
    )

    heading, task = compiled.create
    assert heading.into == "project:project"
    assert heading.after == "heading:heading"
    assert task.into == "project:project"
    assert task.after == "task:task"
    assert task.today_after == "task:anchor"
    assert task.heading_id == "heading:heading"
    changed = changes_by_id(compiled)
    assert changed["task:task"].into == "project:project"  # type: ignore[attr-defined]
    assert changed["task:task"].after == "task:anchor"  # type: ignore[attr-defined]
    assert changed["task:task"].today_after == "task:anchor"  # type: ignore[attr-defined]
    assert changed["task:task"].heading_id == "heading:heading"  # type: ignore[attr-defined]
    assert changed["area:area"].move_contents_to == "area:other-area"  # type: ignore[attr-defined]


def test_organize_compiles_existing_headings_assignments_and_stable_order() -> None:
    compiled = compile_call(
        {
            "intent_id": "organize-existing-001",
            "context_id": "ctx_12345678",
            "organize": [
                {
                    "project_ref": "p1",
                    "sections": [
                        {
                            "heading_ref": "h2",
                            "heading_title": "Delivery",
                            "task_refs": ["t2"],
                        },
                        {"heading_ref": "h1", "task_refs": ["t1"]},
                        {"task_refs": ["t3"]},
                    ],
                }
            ],
        }
    )
    changes = changes_by_id(compiled)

    assert compiled.context_id is None
    assert compiled.organize == []
    assert changes["heading:h2"].title == "Delivery"  # type: ignore[attr-defined]
    assert changes["heading:h2"].after is None  # type: ignore[attr-defined]
    assert changes["heading:h1"].after == "heading:h2"  # type: ignore[attr-defined]
    assert changes["task:t2"].heading_id == "heading:h2"  # type: ignore[attr-defined]
    assert changes["task:t2"].after is None  # type: ignore[attr-defined]
    assert changes["task:t1"].heading_id == "heading:h1"  # type: ignore[attr-defined]
    assert changes["task:t1"].after == "task:t2"  # type: ignore[attr-defined]
    assert changes["task:t3"].heading_id is None  # type: ignore[attr-defined]
    assert changes["task:t3"].after == "task:t1"  # type: ignore[attr-defined]
    assert "task:untouched" not in changes


def test_organize_merges_context_change_with_generated_task_change() -> None:
    compiled = compile_call(
        {
            "intent_id": "organize-merge-001",
            "context_id": "ctx_12345678",
            "change": [{"ref": "t1", "title": "Draft final brief"}],
            "organize": [
                {
                    "project_ref": "p1",
                    "sections": [{"heading_ref": "h2", "task_refs": ["t1"]}],
                }
            ],
        }
    )
    task_changes = [change for change in compiled.change if change.id == "task:t1"]

    assert len(task_changes) == 1
    assert task_changes[0].title == "Draft final brief"
    assert task_changes[0].heading_id == "heading:h2"
    assert task_changes[0].after is None


def test_new_heading_and_local_task_compile_with_heading_first() -> None:
    compiled = compile_call(
        {
            "intent_id": "organize-local-001",
            "context_id": "ctx_12345678",
            "create": [{"key": "$newtask", "title": "Write launch note"}],
            "organize": [
                {
                    "project_ref": "p1",
                    "sections": [
                        {"heading_ref": "h1", "task_refs": ["t1"]},
                        {
                            "heading_key": "$later",
                            "heading_title": "Later",
                            "task_refs": ["$newtask"],
                        },
                    ],
                }
            ],
        }
    )

    assert [entry.key for entry in compiled.create] == ["$later", "$newtask"]
    heading, task = compiled.create
    assert heading.kind == "heading"
    assert heading.into == "project:p"
    assert task.kind == "task"
    assert task.into == "project:p"
    assert task.heading_id == "$later"
    assert task.after == "task:t1"


def test_delete_heading_compiles_to_exact_permanent_delete() -> None:
    compiled = compile_call(
        {
            "intent_id": "organize-delete-001",
            "context_id": "ctx_12345678",
            "organize": [
                {
                    "project_ref": "p1",
                    "sections": [{"heading_ref": "h1", "task_refs": ["t1"]}],
                    "delete_headings": ["h2"],
                }
            ],
        }
    )
    deleted = changes_by_id(compiled)["heading:h2"]

    assert deleted.lifecycle == "delete_permanently"  # type: ignore[attr-defined]
    assert deleted.if_revision == "revision-heading:h2"  # type: ignore[attr-defined]


def test_existing_task_can_use_new_local_heading_in_same_commit() -> None:
    compiled = compile_call(
        {
            "intent_id": "organize-local-heading-001",
            "context_id": "ctx_12345678",
            "organize": [
                {
                    "project_ref": "p1",
                    "sections": [
                        {
                            "heading_key": "$later",
                            "heading_title": "Later",
                            "task_refs": ["t1"],
                        }
                    ],
                }
            ],
        }
    )

    assert len(compiled.create) == 1
    assert compiled.create[0].key == "$later"
    assert compiled.create[0].kind == "heading"
    task = changes_by_id(compiled)["task:t1"]
    assert task.heading_id == "$later"  # type: ignore[attr-defined]
    assert task.after is None  # type: ignore[attr-defined]


def test_new_heading_can_precede_existing_heading_through_workspace() -> None:
    compiled = compile_call(
        {
            "intent_id": "organize-heading-order-compile-001",
            "context_id": "ctx_12345678",
            "organize": [
                {
                    "project_ref": "p1",
                    "sections": [
                        {"heading_key": "$new", "heading_title": "New"},
                        {"heading_ref": "h1", "task_refs": ["t1"]},
                    ],
                }
            ],
        }
    )
    assert compiled.create[0].after is None
    assert changes_by_id(compiled)["heading:h1"].after == "$new"  # type: ignore[attr-defined]

    library = project_library()
    workspace = ThingsWorkspace(
        library,
        clock=lambda: NOW,
        context_store=MemoryContextStore(
            clock=lambda: NOW, token_factory=lambda: "ctx_12345678"
        ),
    )
    read = workspace.read(
        ReadCall(purpose="organize", view="project", within="project:p", limit=20)
    )
    assert read.context is not None
    refs = {item.id: item.ref for item in read.items}

    result = workspace.commit(
        CommitCall.model_validate(
            {
                "intent_id": "organize-heading-order-001",
                "context_id": read.context.id,
                "organize": [
                    {
                        "project_ref": refs["project:p"],
                        "sections": [
                            {"heading_key": "$new", "heading_title": "New"},
                            {
                                "heading_ref": refs["heading:h1"],
                                "task_refs": [refs["task:t1"]],
                            },
                        ],
                    }
                ],
            }
        )
    )

    assert result.status == "applied"
    headings = sorted(
        (record for record in library.records.values() if record.heading),
        key=lambda record: (record.sort_index, record.uuid),
    )
    assert [heading.title for heading in headings] == ["New", "Plan", "Deliver"]


def test_organize_requires_matching_complete_project_scope() -> None:
    library = project_library()
    incomplete = organize_context(library, complete=False)
    call = {
        "intent_id": "organize-incomplete-001",
        "context_id": incomplete.id,
        "organize": [
            {
                "project_ref": "p1",
                "sections": [{"heading_ref": "h1", "task_refs": ["t1"]}],
            }
        ],
    }

    with pytest.raises(ContextualCompileError, match="complete Project scope"):
        compile_call(call, library=library, context=incomplete)
    with pytest.raises(ContextualCompileError, match="does not match"):
        compile_call(
            {**call, "context_id": "ctx_87654321"}, library=library, context=incomplete
        )


def test_complete_context_must_bind_every_visible_project_member() -> None:
    library = project_library()
    include = {record.id for record in library.project("project:p")}
    include.remove("task:untouched")
    context = organize_context(library, include=include)

    with pytest.raises(ContextualCompileError, match="missing exact item"):
        compile_call(
            {
                "intent_id": "organize-missing-member-001",
                "context_id": context.id,
                "organize": [
                    {
                        "project_ref": "p1",
                        "sections": [{"heading_ref": "h1", "task_refs": ["t1"]}],
                    }
                ],
            },
            library=library,
            context=context,
        )


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ({"heading_ref": "t1", "task_refs": ["t2"]}, "identify a heading"),
        ({"heading_ref": "h1", "task_refs": ["h2"]}, "identify Tasks"),
        ({"heading_ref": "h1", "task_refs": ["tf"]}, "organized Project"),
    ],
)
def test_organize_rejects_wrong_kinds_and_foreign_members(
    section: dict[str, object], message: str
) -> None:
    library = project_library()
    library.records["foreign"] = Record(
        uuid="foreign", kind="task", title="Foreign", parent_uuid="other"
    )
    include = {record.id for record in library.project("project:p")} | {"task:foreign"}
    context = organize_context(library, include=include)

    with pytest.raises(ContextualCompileError, match=message):
        compile_call(
            {
                "intent_id": "organize-invalid-member-001",
                "context_id": context.id,
                "organize": [{"project_ref": "p1", "sections": [section]}],
            },
            library=library,
            context=context,
        )


def test_public_contract_rejects_non_task_local_organize_ref() -> None:
    with pytest.raises(ValidationError, match="must identify Tasks"):
        compile_call(
            {
                "intent_id": "organize-wrong-local-001",
                "context_id": "ctx_12345678",
                "create": [{"key": "$area", "kind": "area", "title": "Area"}],
                "scope_revision": "scope-1",
                "organize": [
                    {
                        "project_ref": "p1",
                        "sections": [{"task_refs": ["$area"]}],
                    }
                ],
            }
        )


def test_unknown_context_ref_and_conflicting_create_values_fail() -> None:
    with pytest.raises(UnknownReference):
        compile_call(
            {
                "intent_id": "organize-unknown-ref-001",
                "context_id": "ctx_12345678",
                "change": [{"ref": "bad", "title": "No"}],
            }
        )
    with pytest.raises(ContextualCompileError, match="conflicting organize value"):
        compile_call(
            {
                "intent_id": "organize-conflict-001",
                "context_id": "ctx_12345678",
                "create": [
                    {"key": "$newtask", "title": "New", "into": "project:other"}
                ],
                "organize": [
                    {
                        "project_ref": "p1",
                        "sections": [{"task_refs": ["$newtask"]}],
                    }
                ],
            }
        )


def test_compilation_is_deterministic() -> None:
    payload = {
        "intent_id": "organize-deterministic-001",
        "context_id": "ctx_12345678",
        "organize": [
            {
                "project_ref": "p1",
                "sections": [
                    {"heading_ref": "h2", "task_refs": ["t2"]},
                    {"heading_ref": "h1", "task_refs": ["t1", "t3"]},
                ],
            }
        ],
    }

    first = compile_call(payload).model_dump(mode="json", exclude_unset=True)
    second = compile_call(payload).model_dump(mode="json", exclude_unset=True)

    assert first == second
