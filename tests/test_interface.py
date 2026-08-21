from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from things_orchestrator.interface import (
    APPROVE_DESC,
    APPROVE_IN,
    APPROVE_OUT,
    COMMIT_DESC,
    COMMIT_IN,
    COMMIT_OUT,
    READ_DESC,
    READ_IN,
    READ_OUT,
    RESULT_OUT,
    ApproveCall,
    ChangeEntry,
    ChangeTag,
    ChecklistAdd,
    ChecklistChange,
    ChecklistFact,
    CommitCall,
    ContextFact,
    CreateEntry,
    DiagnosticFact,
    DiagnosticRepair,
    EnsureTag,
    ItemFact,
    LayoutFact,
    LayoutSectionFact,
    OrganizeDraft,
    OrganizeSection,
    PlanFact,
    ProjectTask,
    ReadCall,
    ReadInclude,
    RecoveryFact,
    RecurrenceFact,
    RepeatCreate,
    RepeatEdit,
    Result,
    ReviewSection,
    SourceRef,
    TagFact,
    dump_result,
)


def test_empty_read_means_today_and_aliases_are_wire_names() -> None:
    assert ReadCall.model_validate({}).view is None
    call = ReadCall.model_validate(
        {"view": "logbook", "from": "2026-08-01", "to": "2026-08-15", "limit": 40}
    )
    assert call.from_date == "2026-08-01"
    assert call.model_dump(by_alias=True)["from"] == "2026-08-01"
    assert ReadCall.model_validate({"view": "logbook"}).to_date is None
    area = ReadCall.model_validate({"view": "area", "id": "area:home"})
    assert area.id == "area:home"
    assert ReadCall.model_validate({"id": "area:home"}).id == "area:home"


@pytest.mark.parametrize(
    "payload",
    [
        {"view": "today", "find": "tax"},
        {"view": "project"},
        {"view": "inbox", "within": "area:home"},
        {"find": "tax", "within": "task:one"},
        {"view": "project", "within": "task:one"},
        {"view": "project", "within": "area:home"},
        {"view": "area", "within": "project:one"},
        {"view": "audit", "id": "task:one"},
        {"ids": ["task:one"], "view": "today"},
        {"ids": []},
        {"fields": ["notes"]},
        {"ids": ["task:one"], "fields": ["notes", "notes"]},
        {"signals_any": ["someday"]},
        {
            "purpose": "change",
            "id": "task:one",
            "include": [{"id": "task:two"}, {"id": "task:two"}],
        },
        {"view": "logbook", "from": "2026-08-01"},
        {"view": "logbook", "from": "2026-08-15", "to": "2026-08-01"},
        {"find": "tax", "unknown": True},
        {"limit": "20"},
    ],
)
def test_read_rejects_ambiguous_or_invalid_input(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ReadCall.model_validate(payload)


def test_read_purpose_selects_task_oriented_context() -> None:
    assert ReadCall.model_validate({}).purpose == "review"
    assert (
        ReadCall.model_validate({"purpose": "change", "id": "task:one"}).purpose
        == "change"
    )
    assert (
        ReadCall.model_validate(
            {
                "purpose": "organize",
                "view": "project",
                "within": "project:one",
            }
        ).purpose
        == "organize"
    )
    assert ReadCall.model_validate({"view": "system"}).view == "system"
    assert ReadCall.model_validate(
        {"purpose": "organize", "find": "Launch"}
    ).find == "Launch"
    assert ReadCall.model_validate(
        {"purpose": "recurrence", "id": "task:repeat"}
    ).purpose == "recurrence"

    for payload in (
        {"purpose": "change"},
        {"purpose": "organize", "view": "today"},
        {"purpose": "organize", "view": "system"},
        {"purpose": "recurrence"},
        {"purpose": "recurrence", "id": "task:repeat", "view": "today"},
        {"purpose": "review", "cursor": "cursor_12345678"},
        {"within": "trash"},
        {"view": "today", "within": "trash"},
    ):
        with pytest.raises(ValidationError):
            ReadCall.model_validate(payload)

    trash = ReadCall.model_validate({"find": "Later", "within": "trash"})
    assert trash.within == "trash"


def test_change_include_is_compact_and_bounded() -> None:
    call = ReadCall.model_validate(
        {
            "purpose": "change",
            "id": "task:target",
            "include": [
                {"id": "task:anchor"},
                {"find": "Anchor", "within": "project:work"},
            ],
        }
    )
    assert call.include[0].id == "task:anchor"
    with pytest.raises(ValidationError, match="exactly one"):
        ReadCall.model_validate(
            {"purpose": "change", "id": "task:target", "include": [{}]}
        )
    review_include = ReadCall.model_validate(
        {"purpose": "review", "view": "inbox", "include": [{"id": "area:home"}]}
    )
    assert review_include.include[0].id == "area:home"
    with pytest.raises(ValidationError, match="only available"):
        ReadCall.model_validate(
            {"purpose": "recurrence", "id": "task:repeat", "include": [{"id": "task:anchor"}]}
        )
    assert (
        ReadCall.model_validate(
            {
                "purpose": "organize",
                "id": "project:source",
                "include": [{"id": "project:destination"}],
            }
        ).include[0].id
        == "project:destination"
    )
    with pytest.raises(ValidationError, match="cannot combine"):
        ReadCall.model_validate(
            {
                "purpose": "change",
                "id": "task:target",
                "cursor": "cursor_12345678",
                "include": [{"id": "task:anchor"}],
            }
        )


def test_manual_include_schema_matches_runtime_selector_rules() -> None:
    include_schema = READ_IN["properties"]["include"]["items"]
    assert include_schema["type"] == "object"
    assert include_schema["additionalProperties"] is False
    assert include_schema["minProperties"] == 1
    assert include_schema["not"] == {
        "required": ["id"],
        "minProperties": 2,
    }

    runtime = ReadInclude.model_json_schema()
    assert set(include_schema["properties"]) == set(runtime["properties"])
    for name in include_schema["properties"]:
        runtime_property = runtime["properties"][name]
        if "anyOf" in runtime_property:
            runtime_property = next(
                branch
                for branch in runtime_property["anyOf"]
                if branch.get("type") != "null"
            )
        for key in ("type", "pattern", "minLength", "maxLength"):
            if key == "maxLength":
                # The model enforces the bound. Omit repeated lengths from the
                # compact discovery schema.
                continue
            if key in runtime_property:
                assert include_schema["properties"][name][key] == runtime_property[key]

    validator = Draft202012Validator(include_schema)
    valid = (
        {"id": "task:anchor"},
        {"find": "Anchor"},
        {"find": "Anchor", "within": "project:work"},
    )
    invalid = (
        {},
        {"id": "task:anchor", "find": "Anchor"},
        {"id": "task:anchor", "within": "project:work"},
        {"find": ""},
        {"find": "Anchor", "unknown": True},
    )
    for payload in valid:
        validator.validate(payload)
        ReadInclude.model_validate(payload)
    for payload in invalid:
        assert not validator.is_valid(payload)
        with pytest.raises(ValidationError):
            ReadInclude.model_validate(payload)


def test_context_capacity_does_not_raise_normal_read_limit() -> None:
    assert ReadCall(limit=40).limit == 40
    with pytest.raises(ValidationError):
        ReadCall(limit=41)

    fact = {
        "id": "task:one",
        "revision": "r_1",
        "kind": "task",
        "title": "Task",
        "status": "open",
        "order": 1,
    }
    result = {
        "next": "done",
        "status": "ok",
        "instruction": "Complete context.",
        "items": [fact] * 120,
    }
    assert len(Result.model_validate(result).items) == 120
    with pytest.raises(ValidationError):
        Result.model_validate({**result, "items": [fact] * 121})

    assert RESULT_OUT["properties"]["items"]["maxItems"] == 120
    assert READ_OUT["properties"]["items"]["maxItems"] == 120


def test_commit_accepts_one_related_graph_with_local_keys() -> None:
    call = CommitCall.model_validate(
        {
            "intent_id": "turn-20260815-launch",
            "ensure_tags": [{"key": "$focus", "title": "Focus"}],
            "create": [
                {"key": "$launch", "kind": "project", "title": "Launch version 2"},
                {
                    "title": "Draft the brief",
                    "into": "$launch",
                    "tag_ids": ["$focus"],
                },
            ],
            "change": [
                {
                    "id": "task:exact",
                    "if_revision": "r_12",
                    "notes_markdown": "# Context\nOwner approved.",
                    "checklist_add": [{"key": "$proof", "title": "Add proof"}],
                    "checklist_order": ["$proof", "check:old"],
                    "tags_add": ["tag:waiting"],
                }
            ],
        }
    )
    assert call.create[1].into == "$launch"
    assert call.create[1].tag_ids == ["$focus"]
    assert call.change[0].notes_markdown.startswith("# Context")


def test_context_change_uses_short_ref_without_revision() -> None:
    call = CommitCall.model_validate(
        {
            "intent_id": "context-change-001",
            "context_id": "ctx_12345678",
            "change": [{"ref": "t1", "title": "Use the new title"}],
        }
    )

    assert call.change[0].ref == "t1"
    assert call.change[0].id is None
    assert call.change[0].if_revision is None

    for change in (
        {"ref": "t1", "title": "Missing context"},
        {"id": "task:one", "title": "Missing revision"},
        {"if_revision": "r_1", "title": "Missing id"},
    ):
        payload = {"intent_id": "context-invalid-001", "change": [change]}
        if "ref" in change and len(change) > 2:
            payload["context_id"] = "ctx_12345678"
        with pytest.raises(ValidationError):
            CommitCall.model_validate(payload)

    mixed = CommitCall.model_validate(
        {
            "intent_id": "context-mixed-001",
            "context_id": "ctx_12345678",
            "change": [
                {
                    "ref": "t1",
                    "id": "task:one",
                    "if_revision": "r_1",
                    "title": "Matching redundant identity",
                }
            ],
        }
    )
    assert mixed.change[0].ref == "t1"
    assert mixed.change[0].id == "task:one"

    revision_only = CommitCall.model_validate(
        {
            "intent_id": "context-revision-only-001",
            "context_id": "ctx_12345678",
            "change": [
                {"ref": "t1", "if_revision": "r_1", "title": "Ref wins"}
            ],
        }
    )
    assert revision_only.change[0].if_revision == "r_1"

    contextual_home = CommitCall.model_validate(
        {
            "intent_id": "context-area-home-001",
            "context_id": "ctx_12345678",
            "change": [{"ref": "p1", "into": "a1", "title": "Moved"}],
        }
    )
    assert contextual_home.change[0].into == "a1"

    with pytest.raises(ValidationError, match="short Area refs"):
        CommitCall.model_validate(
            {
                "intent_id": "legacy-short-home-001",
                "change": [
                    {
                        "id": "project:one",
                        "if_revision": "r_1",
                        "into": "a1",
                    }
                ],
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"create": [{"title": "Task", "into": "p1"}]},
        {"create": [{"title": "Task", "after": "t1"}]},
        {"create": [{"title": "Task", "today_after": "t1"}]},
        {"create": [{"kind": "heading", "title": "Section", "into": "p1"}]},
        {
            "change": [
                {"id": "task:one", "if_revision": "r_1", "after": "t1"}
            ]
        },
        {
            "change": [
                {"id": "task:one", "if_revision": "r_1", "today_after": "t1"}
            ]
        },
        {
            "change": [
                {
                    "id": "area:one",
                    "if_revision": "r_1",
                    "move_contents_to": "a1",
                }
            ]
        },
        {
            "change": [
                {"id": "task:one", "if_revision": "r_1", "heading_id": "h1"}
            ]
        },
    ],
    ids=[
        "create-into",
        "create-after",
        "create-today-after",
        "create-heading-into",
        "change-after",
        "change-today-after",
        "change-move-contents",
        "change-heading",
    ],
)
def test_legacy_commit_rejects_short_context_relationship_refs(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="short relationship refs"):
        CommitCall.model_validate(
            {"intent_id": "legacy-short-relationship-001", **payload}
        )


def test_organize_draft_supports_existing_new_and_unheaded_sections() -> None:
    call = CommitCall.model_validate(
        {
            "intent_id": "organize-project-001",
            "context_id": "ctx_12345678",
            "create": [{"key": "$newtask", "title": "Draft launch note"}],
            "organize": [
                {
                    "project_ref": "p1",
                    "sections": [
                        {"heading_ref": "h1", "task_refs": ["t1"]},
                        {
                            "heading_key": "$later",
                            "heading_title": "Later",
                            "task_refs": ["t2", "$newtask"],
                        },
                        {"task_refs": ["t3"]},
                        {"heading_key": "$empty", "heading_title": "Empty"},
                    ],
                    "delete_headings": ["h2"],
                }
            ],
        }
    )

    draft = call.organize[0]
    assert draft.unlisted == "keep"
    assert draft.sections[1].heading_title == "Later"
    assert draft.sections[2].heading_ref is None
    delete_only = CommitCall.model_validate(
        {
            "intent_id": "organize-delete-only-001",
            "context_id": "ctx_12345678",
            "organize": [
                {"project_ref": "p1", "delete_headings": ["h2"]},
            ],
        }
    )
    assert delete_only.organize[0].sections == []

    with pytest.raises(ValidationError, match="context refs need context_id"):
        CommitCall.model_validate(
            {
                "intent_id": "organize-no-context-001",
                "organize": [
                    {
                        "project_ref": "p1",
                        "sections": [{"task_refs": ["t1"]}],
                    }
                ],
            }
        )
    with pytest.raises(ValidationError, match="one organize section"):
        CommitCall.model_validate(
            {
                "intent_id": "organize-duplicate-001",
                "context_id": "ctx_12345678",
                "organize": [
                    {
                        "project_ref": "p1",
                        "sections": [
                            {"heading_ref": "h1", "task_refs": ["t1"]},
                            {"task_refs": ["t1"]},
                        ],
                    }
                ],
            }
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "intent_id": "duplicate-tag-key-001",
                "ensure_tags": [{"key": "$same", "title": "Focus"}],
                "create": [{"key": "$same", "title": "Draft"}],
            },
            "local keys must be unique",
        ),
        (
            {
                "intent_id": "duplicate-tag-title-001",
                "ensure_tags": [
                    {"key": "$first", "title": " Focus "},
                    {"key": "$second", "title": "focus"},
                ],
            },
            "ensure_tags titles must be unique",
        ),
        (
            {
                "intent_id": "unknown-tag-key-001",
                "create": [{"title": "Draft", "tag_ids": ["$missing"]}],
            },
            "local tag references need ensure_tags keys",
        ),
    ],
)
def test_commit_rejects_ambiguous_local_tags(
    payload: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        CommitCall.model_validate(payload)


def test_tag_removals_keep_exact_ids() -> None:
    with pytest.raises(ValidationError, match="tags_remove needs exact tag IDs"):
        CommitCall.model_validate(
            {
                "intent_id": "local-tag-remove-001",
                "ensure_tags": [{"key": "$focus", "title": "Focus"}],
                "change": [
                    {
                        "id": "task:exact",
                        "if_revision": "r_1",
                        "tags_remove": ["$focus"],
                    }
                ],
            }
        )


def test_commit_accepts_special_homes() -> None:
    call = CommitCall.model_validate(
        {
            "intent_id": "turn-20260815-homes",
            "create": [
                {"title": "Capture this", "into": "inbox"},
                {"kind": "project", "title": "Ship this", "into": "anytime"},
            ],
        }
    )
    assert [entry.into for entry in call.create] == ["inbox", "anytime"]


def test_trash_is_explicit_and_cannot_combine_with_other_changes() -> None:
    call = CommitCall.model_validate(
        {
            "intent_id": "trash-task-001",
            "change": [{"id": "task:one", "if_revision": "r_1", "trash": True}],
        }
    )
    assert call.change[0].trash is True
    assert COMMIT_IN["properties"]["change"]["items"]["properties"]["trash"] == {
        "const": True
    }

    with pytest.raises(ValidationError, match="Trash cannot combine"):
        CommitCall.model_validate(
            {
                "intent_id": "trash-task-mixed-001",
                "change": [
                    {
                        "id": "task:one",
                        "if_revision": "r_1",
                        "trash": True,
                        "title": "Changed",
                    }
                ],
            }
        )


def test_heading_create_rename_assignment_and_clear_are_explicit() -> None:
    call = CommitCall.model_validate(
        {
            "intent_id": "heading-operations-001",
            "create": [
                {"key": "$section", "kind": "heading", "title": "Next", "into": "project:p"}
            ],
            "change": [
                {"id": "heading:h", "if_revision": "r_h", "title": "Later"},
                {"id": "task:t", "if_revision": "r_t", "heading_id": None},
            ],
        }
    )

    assert call.create[0].kind == "heading"
    assert call.change[0].id == "heading:h"
    assert "heading_id" in call.change[1].model_fields_set

    with pytest.raises(ValidationError, match="accepts only"):
        CommitCall.model_validate(
            {
                "intent_id": "heading-invalid-001",
                "create": [
                    {
                        "kind": "heading",
                        "title": "Next",
                        "into": "project:p",
                        "notes_markdown": "No",
                    }
                ],
            }
        )


@pytest.mark.parametrize("into", ["inbox", "anytime", "area:home"])
def test_heading_change_into_rejects_non_project_homes(into: str) -> None:
    with pytest.raises(ValidationError, match="destination Project"):
        CommitCall.model_validate(
            {
                "intent_id": "heading-into-home-001",
                "change": [
                    {
                        "id": "heading:h",
                        "if_revision": "r_h",
                        "into": into,
                    }
                ],
            }
        )


def test_heading_change_into_remains_valid_for_an_atomic_merge_shape() -> None:
    call = CommitCall.model_validate(
        {
            "intent_id": "heading-merge-shape-001",
            "change": [
                {
                    "id": "heading:h",
                    "if_revision": "r_h",
                    "into": "project:dest",
                },
                {
                    "id": "project:source",
                    "if_revision": "r_s",
                    "lifecycle": "trash",
                },
            ],
        }
    )

    assert call.change[0].into == "project:dest"
    assert call.change[1].lifecycle == "trash"


def test_existing_task_can_use_a_heading_created_in_the_same_commit() -> None:
    call = CommitCall.model_validate(
        {
            "intent_id": "local-heading-change-001",
            "create": [
                {
                    "key": "$later",
                    "kind": "heading",
                    "title": "Later",
                    "into": "project:p",
                }
            ],
            "change": [
                {
                    "id": "task:t",
                    "if_revision": "r_t",
                    "heading_id": "$later",
                }
            ],
        }
    )

    assert call.change[0].heading_id == "$later"
    assert (
        COMMIT_IN["properties"]["change"]["items"]["properties"]["heading_id"][
            "pattern"
        ]
        == COMMIT_IN["properties"]["create"]["items"]["properties"]["heading_id"][
            "pattern"
        ]
    )

    with pytest.raises(ValidationError, match="created heading"):
        CommitCall.model_validate(
            {
                "intent_id": "local-heading-invalid-001",
                "create": [{"key": "$notheading", "title": "Task"}],
                "change": [
                    {
                        "id": "task:t",
                        "if_revision": "r_t",
                        "heading_id": "$notheading",
                    }
                ],
            }
        )


def test_task_create_accepts_a_heading_defined_later_in_the_array() -> None:
    call = CommitCall.model_validate(
        {
            "intent_id": "heading-local-forward-001",
            "create": [
                {
                    "key": "$task",
                    "kind": "task",
                    "title": "Ship",
                    "into": "project:p",
                    "heading_id": "$section",
                },
                {
                    "key": "$section",
                    "kind": "heading",
                    "title": "Next",
                    "into": "project:p",
                },
            ],
        }
    )

    assert [entry.key for entry in call.create] == ["$section", "$task"]
    assert call.create[1].heading_id == "$section"


def test_create_and_change_accept_into_title_without_into() -> None:
    created = CreateEntry.model_validate(
        {"title": "Buy milk", "into_title": "Kitchen"}
    )
    assert created.into is None
    assert created.into_title == "Kitchen"

    heading = CreateEntry.model_validate(
        {"kind": "heading", "title": "Later", "into_title": "Kitchen"}
    )
    assert heading.into_title == "Kitchen"

    changed = ChangeEntry.model_validate(
        {
            "id": "task:milk",
            "if_revision": "r_1",
            "into_title": " Kitchen ",
        }
    )
    assert changed.into_title == "Kitchen"

    both = CreateEntry.model_validate(
        {"title": "Buy milk", "into": "project:p", "into_title": "Kitchen"}
    )
    assert both.into == "project:p"
    assert both.into_title == "Kitchen"


def test_compact_project_tasks_are_project_only() -> None:
    project = CreateEntry.model_validate(
        {
            "kind": "project",
            "title": "Move house",
            "tasks": [{"title": "Call mover"}],
        }
    )
    assert [task.title for task in project.tasks] == ["Call mover"]

    staged = CreateEntry.model_validate(
        {
            "kind": "project",
            "title": "Move house",
            "tasks": [
                {
                    "title": "Call mover",
                    "checklist": ["Ask about Friday", "Confirm the quote"],
                    "heading_title": "Plan",
                },
                {"title": "Book van", "heading_title": "Plan"},
                {"title": "Move boxes", "heading_title": "Move"},
            ],
        }
    )
    assert [task.heading_title for task in staged.tasks] == ["Plan", "Plan", "Move"]
    assert staged.tasks[0].checklist == ["Ask about Friday", "Confirm the quote"]

    source = CreateEntry.model_validate(
        {
            "kind": "project",
            "document": "source",
            "title": "Build Mats Mode as a reusable Agent Skill",
            "note_style": "visual",
            "outcome": "One reusable skill based on how I work.",
            "finished_when": ["The skill passes one real task."],
            "keep_in_mind": ["Use actual chats, not installed inventory."],
            "tasks": [
                {
                    "title": "Choose representative chats",
                    "finish": "A dated source list for every active agent client.",
                    "start_here": ["The chats are on my MacBook."],
                    "approach": ["Keep each client separate."],
                    "sources": [
                        {
                            "label": "Optional extractor",
                            "location": "https://github.com/0xSero/ai-data-extraction",
                        }
                    ],
                    "heading_title": "Learn what works",
                }
            ],
        }
    )
    assert source.document == "source"
    assert source.note_style == "visual"
    assert source.outcome == "One reusable skill based on how I work."
    assert source.tasks[0].finish.startswith("A dated source list")
    assert source.tasks[0].sources[0].label == "Optional extractor"

    with pytest.raises(ValidationError, match="400 characters"):
        ProjectTask.model_validate({"title": "Draft", "finish": "x" * 401})

    with pytest.raises(ValidationError, match="cannot contain Markdown headings"):
        ProjectTask.model_validate(
            {"title": "Draft", "finish": "One draft.\n\n## Leave with\n\nAnother."}
        )

    with pytest.raises(ValidationError, match="1000 characters"):
        CreateEntry.model_validate(
            {
                "kind": "project",
                "title": "Move house",
                "tasks": [{"title": "Call mover", "checklist": ["x" * 1001]}],
            }
        )

    with pytest.raises(ValidationError, match="1000 characters"):
        CreateEntry.model_validate(
            {"title": "Call mover", "checklist": ["x" * 1001]}
        )

    with pytest.raises(ValidationError, match="headings on every Task or none"):
        CreateEntry.model_validate(
            {
                "kind": "project",
                "title": "Move house",
                "tasks": [
                    {"title": "Call mover", "heading_title": "Plan"},
                    {"title": "Book van"},
                ],
            }
        )

    with pytest.raises(ValidationError, match="contiguous"):
        CreateEntry.model_validate(
            {
                "kind": "project",
                "title": "Move house",
                "tasks": [
                    {"title": "Call mover", "heading_title": "Plan"},
                    {"title": "Move boxes", "heading_title": "Move"},
                    {"title": "Book van", "heading_title": "Plan"},
                ],
            }
        )

    with pytest.raises(ValidationError, match="stable spelling and case"):
        CreateEntry.model_validate(
            {
                "kind": "project",
                "title": "Move house",
                "tasks": [
                    {"title": "Call mover", "heading_title": "Plan"},
                    {"title": "Book van", "heading_title": "plan"},
                ],
            }
        )

    with pytest.raises(ValidationError, match="only a Project can have tasks"):
        CreateEntry.model_validate(
            {"title": "Move house", "tasks": [{"title": "Call mover"}]}
        )

    with pytest.raises(ValidationError, match="must be a Project"):
        CreateEntry.model_validate({"document": "source", "title": "Renew passport"})

    with pytest.raises(ValidationError, match="extra_forbidden"):
        CreateEntry.model_validate(
            {
                "kind": "project",
                "title": "Move house",
                "next_actions": [{"title": "Call mover"}],
            }
        )

    with pytest.raises(ValidationError, match="Project task titles must be unique"):
        CreateEntry.model_validate(
            {
                "kind": "project",
                "title": "Move house",
                "tasks": [{"title": "Call mover"}, {"title": "call mover"}],
            }
        )

    with pytest.raises(ValidationError, match="cannot mix compact tasks"):
        CommitCall.model_validate(
            {
                "intent_id": "mixed-project-tasks-001",
                "create": [
                    {
                        "key": "$move",
                        "kind": "project",
                        "title": "Move house",
                        "tasks": [{"title": "Call mover"}],
                    },
                    {
                        "title": "Book van",
                        "into": "$move",
                        "waiting": True,
                    },
                ],
            }
        )

    with pytest.raises(ValidationError, match="cannot mix compact tasks"):
        CommitCall.model_validate(
            {
                "intent_id": "mixed-moved-task-001",
                "create": [
                    {
                        "key": "$move",
                        "kind": "project",
                        "title": "Move house",
                        "tasks": [{"title": "Call mover"}],
                    }
                ],
                "change": [
                    {
                        "id": "task:van",
                        "if_revision": "r_1",
                        "into": "$move",
                    }
                ],
            }
        )

    with pytest.raises(ValidationError, match="new task titles must be unique"):
        CommitCall.model_validate(
            {
                "intent_id": "duplicate-batch-task-001",
                "create": [
                    {
                        "kind": "project",
                        "title": "Move A",
                        "tasks": [{"title": "Call mover"}],
                    },
                    {
                        "kind": "project",
                        "title": "Move B",
                        "tasks": [{"title": "call mover"}],
                    },
                ],
            }
        )


def test_semantic_project_fields_are_project_only_and_optional_for_ordinary_projects() -> None:
    ordinary = CreateEntry.model_validate(
        {
            "kind": "project",
            "title": "Replace the kitchen tap",
            "outcome": "A working kitchen tap with no leak.",
            "finished_when": ["The new tap runs without a leak."],
            "tasks": [
                {
                    "title": "Measure the existing tap fittings",
                    "finish": "The fitting sizes recorded in millimeters.",
                }
            ],
        }
    )
    assert ordinary.document is None
    assert ordinary.finished_when == ["The new tap runs without a leak."]

    for field, value in (
        ("note_style", "natural"),
        ("outcome", "A result."),
        ("finished_when", ["It is complete."]),
        ("keep_in_mind", ["Use the existing parts."]),
    ):
        with pytest.raises(ValidationError, match="only a Project"):
            CreateEntry.model_validate({"title": "Call the plumber", field: value})

    with pytest.raises(ValidationError, match="cannot mix notes_markdown"):
        CreateEntry.model_validate(
            {
                "kind": "project",
                "title": "Replace the kitchen tap",
                "outcome": "A working kitchen tap.",
                "notes_markdown": "Legacy support material.",
            }
        )
    with pytest.raises(ValidationError, match="cannot mix notes_markdown"):
        ProjectTask.model_validate(
            {
                "title": "Measure the fittings",
                "finish": "The fitting sizes recorded.",
                "notes_markdown": "Legacy support material.",
            }
        )


@pytest.mark.parametrize(
    "payload, message",
    [
        (
            {
                "kind": "project",
                "document": "source",
                "title": "Old source document",
                "notes_markdown": "## Result\n\nOne result.",
                "tasks": [{"title": "Draft", "finish": "One draft."}],
            },
            "cannot use notes_markdown",
        ),
        (
            {
                "kind": "project",
                "document": "source",
                "title": "Missing task notes",
                "outcome": "One result.",
                "finished_when": ["The result works."],
                "tasks": [{"title": "Draft"}],
            },
            "Task needs finish",
        ),
        (
            {
                "kind": "project",
                "document": "source",
                "title": "Raw task note",
                "outcome": "One result.",
                "finished_when": ["The result works."],
                "tasks": [
                    {
                        "title": "Draft",
                        "finish": "One draft.",
                        "notes_markdown": "Legacy note.",
                    }
                ],
            },
            "cannot mix notes_markdown",
        ),
    ],
)
def test_source_documents_reject_incomplete_or_legacy_shapes(
    payload: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        CreateEntry.model_validate(payload)


@pytest.mark.parametrize(
    "field, value",
    [
        ("outcome", "# Result"),
        ("outcome", "Result\nwith a second line"),
        ("finished_when", [" "]),
        ("finished_when", ["# Finished"]),
        ("keep_in_mind", ["x" * 301]),
    ],
)
def test_semantic_project_prose_is_compact_plain_text(
    field: str, value: object
) -> None:
    payload: dict[str, object] = {
        "kind": "project",
        "title": "Build the result",
        field: value,
    }
    with pytest.raises(ValidationError):
        CreateEntry.model_validate(payload)


@pytest.mark.parametrize(
    "value",
    [
        "**manual styling**",
        "[manual link](relative.md)",
        "- hidden list row",
        "obsidian:open?vault=Work",
        "javascript:alert(1)",
    ],
)
def test_semantic_prose_rejects_presentation_and_hidden_locations(
    value: str,
) -> None:
    with pytest.raises(ValidationError):
        ProjectTask.model_validate({"title": "Record evidence", "finish": value})


def test_semantic_lists_and_source_counts_are_bounded() -> None:
    with pytest.raises(ValidationError):
        CreateEntry.model_validate(
            {
                "kind": "project",
                "title": "Build the result",
                "finished_when": [f"Check {index}" for index in range(7)],
            }
        )
    with pytest.raises(ValidationError):
        ProjectTask.model_validate(
            {
                "title": "Collect evidence",
                "start_here": [f"Place {index}" for index in range(5)],
            }
        )
    with pytest.raises(ValidationError):
        ProjectTask.model_validate(
            {
                "title": "Collect evidence",
                "sources": [
                    {"label": f"Source {index}", "location": f"https://example.com/{index}"}
                    for index in range(13)
                ],
            }
        )


@pytest.mark.parametrize(
    "location",
    [
        "https://example.com/path",
        "http://localhost:8000/file",
        "file:///Users/mats/Documents/notes.md",
        "/Users/mats/Documents/notes.md",
        "~/Documents/notes.md",
        "things:///show?id=ABC-123",
        "x-devonthink-item://ABC-123",
    ],
)
def test_source_reference_accepts_safe_locations(location: str) -> None:
    source = SourceRef.model_validate({"label": "Reference", "location": location})
    assert source.location == location


@pytest.mark.parametrize(
    "location",
    [
        "relative/file.md",
        "https:///missing-host",
        "https://example.com:invalid/path",
        "https://user:secret@example.com/private",
        "file://remote.example.com/path",
        "file:relative/path",
        "things:///add?title=Mutation",
        "things:///show?id=",
        "things:///show?id=one&id=two",
        "javascript:alert(1)",
        "https://example.com/%0AInjected",
        "https://example.com/path\nInjected",
    ],
)
def test_source_reference_rejects_unsafe_locations(location: str) -> None:
    with pytest.raises(ValidationError):
        SourceRef.model_validate({"label": "Reference", "location": location})


@pytest.mark.parametrize("field", ["finish", "start_here", "approach"])
def test_semantic_task_prose_cannot_hide_a_source_location(field: str) -> None:
    value: object = ["Use https://example.com/evidence"]
    if field == "finish":
        value = "Use https://example.com/evidence"

    with pytest.raises(ValidationError, match="structured sources"):
        ProjectTask.model_validate({"title": "Record evidence", field: value})


def test_task_create_rejects_a_non_heading_local_heading_reference() -> None:
    with pytest.raises(ValidationError, match="heading create entry"):
        CommitCall.model_validate(
            {
                "intent_id": "heading-local-invalid-001",
                "create": [
                    {
                        "key": "$other",
                        "kind": "task",
                        "title": "Other",
                        "into": "project:p",
                    },
                    {
                        "kind": "task",
                        "title": "Ship",
                        "into": "project:p",
                        "heading_id": "$other",
                    },
                ],
            }
        )


def test_repeat_interval_change_is_explicit_and_isolated() -> None:
    call = CommitCall.model_validate(
        {
            "intent_id": "repeat-interval-001",
            "change": [
                {
                    "id": "task:template",
                    "if_revision": "r_template",
                    "repeat_interval": 3,
                }
            ],
        }
    )

    assert call.change[0].repeat_interval == 3

    for invalid in (
        {"repeat_interval": 0},
        {"repeat_interval": 367},
        {"repeat_interval": 2, "title": "Also rename"},
    ):
        with pytest.raises(ValidationError):
            CommitCall.model_validate(
                {
                    "intent_id": "repeat-interval-invalid-001",
                    "change": [
                        {
                            "id": "task:template",
                            "if_revision": "r_template",
                            **invalid,
                        }
                    ],
                }
            )


def test_commit_rejects_after_anchors_that_are_not_create_keys() -> None:
    with pytest.raises(ValidationError, match="create entry"):
        CommitCall.model_validate(
            {
                "intent_id": "after-tag-key-001",
                "ensure_tags": [{"key": "$tag", "title": "Waiting"}],
                "create": [{"title": "First", "after": "$tag"}],
            }
        )


def test_commit_orders_forward_local_after_anchors() -> None:
    call = CommitCall.model_validate(
        {
            "intent_id": "forward-order-001",
            "create": [
                {"title": "First", "after": "$later"},
                {"key": "$later", "title": "Later"},
            ],
        }
    )

    assert [entry.title for entry in call.create] == ["Later", "First"]
    assert call.create[1].after == "$later"


def test_commit_rejects_cross_item_checklist_keys() -> None:
    with pytest.raises(ValidationError, match="same item"):
        CommitCall.model_validate(
            {
                "intent_id": "cross-check-001",
                "change": [
                    {
                        "id": "task:one",
                        "if_revision": "r_1",
                        "checklist_add": [{"key": "$row", "title": "Row"}],
                    },
                    {
                        "id": "task:two",
                        "if_revision": "r_2",
                        "checklist_order": ["$row"],
                    },
                ],
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"intent_id": "short", "create": [{"title": "Do it"}]},
        {"intent_id": "turn-1234", "create": [{"title": "Do it", "extra": 1}]},
        {"intent_id": "turn-1234", "create": [{"title": "Do it", "into": "$missing"}]},
        {
            "intent_id": "turn-1234",
            "change": [{"id": "task:exact", "if_revision": "r_1"}],
        },
        {
            "intent_id": "turn-1234",
            "change": [
                {
                    "id": "task:exact",
                    "if_revision": "r_1",
                    "remind_at": "2026-08-17T09:00:00",
                }
            ],
        },
        {
            "intent_id": "turn-1234",
            "change": [
                {
                    "id": "task:exact",
                    "if_revision": "r_1",
                    "tags_add": ["tag:waiting"],
                    "tags_remove": ["tag:waiting"],
                }
            ],
        },
    ],
)
def test_commit_rejects_unsafe_or_incomplete_input(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CommitCall.model_validate(payload)


@pytest.mark.parametrize(
    "change",
    [
        {"title": None},
        {"status": None},
        {"waiting": None},
        {"tags_add": []},
        {"checklist_change": [{"id": "check:row", "status": None}]},
    ],
)
def test_commit_rejects_semantic_no_op_changes(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CommitCall.model_validate(
            {
                "intent_id": "no-op-change-001",
                "change": [
                    {"id": "task:exact", "if_revision": "r_1", **change}
                ],
            }
        )


def test_commit_rejects_conflicting_schedule_and_root_home() -> None:
    with pytest.raises(ValidationError, match="cannot combine with a schedule"):
        CommitCall.model_validate(
            {
                "intent_id": "root-schedule-001",
                "create": [
                    {"title": "Call", "into": "anytime", "start": "today"}
                ],
            }
        )


def test_commit_rejects_invalid_local_home_and_order_scope() -> None:
    with pytest.raises(ValidationError, match="local home"):
        CommitCall.model_validate(
            {
                "intent_id": "bad-local-home-001",
                "create": [
                    {"key": "$task", "title": "Task"},
                    {"title": "Child", "into": "$task"},
                ],
            }
        )
    with pytest.raises(ValidationError, match="same list"):
        CommitCall.model_validate(
            {
                "intent_id": "bad-local-order-001",
                "create": [
                    {"key": "$first", "title": "First", "into": "inbox"},
                    {"title": "Second", "into": "anytime", "after": "$first"},
                ],
            }
        )


def test_commit_requires_offset_aware_reminders() -> None:
    call = CommitCall.model_validate(
        {
            "intent_id": "turn-20260815-call",
            "create": [
                {"title": "Call Maya", "remind_at": "2026-08-17T09:00:00+02:00"}
            ],
        }
    )
    assert call.create[0].remind_at == "2026-08-17T09:00:00+02:00"


@pytest.mark.parametrize(
    "payload",
    [
        {
            "intent_id": "area-create-no-scope-001",
            "create": [{"kind": "area", "title": "Health"}],
        },
        {
            "intent_id": "area-rename-no-scope-001",
            "change": [
                {
                    "id": "area:health",
                    "if_revision": "r_1",
                    "title": "Wellbeing",
                }
            ],
        },
    ],
)
def test_area_changes_require_system_scope_revision(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="system scope_revision"):
        CommitCall.model_validate(payload)


def test_tag_changes_accept_tags_read_scope_revision() -> None:
    call = CommitCall.model_validate(
        {
            "intent_id": "rename-office-at-office-20260817",
            "scope_revision": "s_0af453b688a58b13dd349bc5",
            "change_tags": [
                {
                    "id": "tag:office",
                    "parent_id": "tag:contexts",
                    "title": "At office",
                }
            ],
        }
    )
    assert call.tags_revision == "s_0af453b688a58b13dd349bc5"


def test_tag_changes_without_any_revision_still_fail() -> None:
    with pytest.raises(ValidationError, match="tags_revision"):
        CommitCall.model_validate(
            {
                "intent_id": "rename-office-at-office-20260817",
                "change_tags": [{"id": "tag:office", "title": "At office"}],
            }
        )


def test_checklist_order_can_use_all_rows_from_paged_detail() -> None:
    order = [f"check:row-{index}" for index in range(101)]
    call = CommitCall.model_validate(
        {
            "intent_id": "large-checklist-001",
            "change": [
                {
                    "id": "task:large",
                    "if_revision": "r_1",
                    "checklist_order": order,
                }
            ],
        }
    )

    assert call.change[0].checklist_order == order


def test_approve_accepts_only_a_plan_id() -> None:
    assert ApproveCall.model_validate({"plan_id": "plan_12345678"}).plan_id
    with pytest.raises(ValidationError):
        ApproveCall.model_validate({"plan_id": "plan_12345678", "title": "different work"})


def test_result_keeps_exact_revisioned_facts_and_control() -> None:
    result = Result.model_validate(
        {
            "next": "done",
            "status": "ok",
            "instruction": "Use these current facts.",
            "items": [
                {
                    "id": "task:exact",
                    "revision": "r_17",
                    "kind": "task",
                    "title": "Call Maya",
                    "status": "open",
                    "order": 1024,
                    "notes_markdown": "# Context\nUse Signal.",
                    "checklist": [
                        {
                            "id": "check:exact",
                            "revision": "r_18",
                            "title": "Find number",
                            "status": "completed",
                            "order": 1024,
                        }
                    ],
                    "direct_tags": [{"id": "tag:waiting", "title": "Waiting"}],
                    "inherited_tags": [
                        {"id": "tag:work", "title": "Work", "from_id": "area:work"}
                    ],
                    "recurrence": {"kind": "none"},
                    "signals": ["waiting"],
                }
            ],
            "sections": [
                {
                    "key": "today",
                    "title": "Today",
                    "item_ids": ["task:exact"],
                }
            ],
            "tags": [{"id": "tag:focus", "title": "Focus"}],
        }
    )
    assert result.items[0].revision == "r_17"
    assert result.items[0].checklist[0].status == "completed"
    assert result.sections[0].item_ids == ["task:exact"]
    assert result.tags[0].id == "tag:focus"


def test_result_carries_compact_context_layout_and_recovery_facts() -> None:
    result = Result.model_validate(
        {
            "next": "read",
            "status": "stale",
            "instruction": "Read the Project again.",
            "items": [
                {
                    "ref": "t1",
                    "id": "task:one",
                    "revision": "r_1",
                    "kind": "task",
                    "title": "Draft",
                    "status": "open",
                    "order": 1,
                }
            ],
            "context": {
                "id": "ctx_12345678",
                "purpose": "organize",
                "expires_at": "2026-08-16T12:00:00+00:00",
                "complete": True,
            },
            "layouts": [
                {
                    "project_ref": "p1",
                    "sections": [{"heading_ref": "h1", "task_refs": ["t1"]}],
                    "complete": True,
                }
            ],
            "recovery": {
                "code": "context_conflict",
                "retry": "read",
                "read": {
                    "purpose": "organize",
                    "view": "project",
                    "within": "project:one",
                },
            },
        }
    )

    assert result.items[0].ref == "t1"
    assert result.context and result.context.complete
    assert result.layouts[0].sections[0].task_refs == ["t1"]
    assert result.recovery and result.recovery.read

    with pytest.raises(ValidationError, match="need context"):
        Result.model_validate(
            {
                "next": "done",
                "status": "ok",
                "instruction": "Current fact.",
                "items": [
                    {
                        "ref": "t1",
                        "id": "task:one",
                        "revision": "r_1",
                        "kind": "task",
                        "title": "Draft",
                        "status": "open",
                        "order": 1,
                    }
                ],
            }
        )


def test_tag_schema_matches_runtime_local_reference_rules() -> None:
    payload = {
        "intent_id": "tag-schema-parity-001",
        "ensure_tags": [{"key": "$focus", "title": "Focus"}],
        "create": [{"title": "Draft", "tag_ids": ["$focus"]}],
        "change": [
            {
                "id": "task:exact",
                "if_revision": "r_1",
                "tags_add": ["$focus"],
                "tags_remove": ["tag:old"],
            }
        ],
    }

    call = CommitCall.model_validate(payload)

    ensure_schema = COMMIT_IN["properties"]["ensure_tags"]["items"]
    create_tag_pattern = COMMIT_IN["properties"]["create"]["items"]["properties"][
        "tag_ids"
    ]["items"]["pattern"]
    change = COMMIT_IN["properties"]["change"]["items"]["properties"]
    assert ensure_schema["required"] == ["key", "title"]
    assert create_tag_pattern == change["tags_add"]["items"]["pattern"]
    assert change["tags_remove"]["items"]["pattern"].startswith("^tag:")
    assert call.ensure_tags[0].key == "$focus"
    assert "tags" in READ_OUT["properties"]
    assert "recovery" in READ_OUT["properties"]
    assert "tags" in COMMIT_OUT["properties"]
    assert "tags" in APPROVE_OUT["properties"]
    commit_item = COMMIT_OUT["properties"]["items"]["items"]
    assert "into_id" in commit_item["properties"]
    assert "start" in commit_item["properties"]
    assert "signals" in commit_item["properties"]


def test_advanced_mutations_stay_in_one_commit_shape() -> None:
    call = CommitCall.model_validate(
        {
            "intent_id": "advanced-mutation-shape-001",
            "tags_revision": "s_tags",
            "ensure_tags": [
                {"key": "$people", "title": "People"},
                {"key": "$alex", "title": "Alex", "parent_id": "$people"},
            ],
            "change_tags": [{"id": "tag:old", "title": "Archive"}],
            "change": [
                {
                    "id": "task:template",
                    "if_revision": "r_repeat",
                    "repeat": {
                        "mode": "after_completion",
                        "unit": "week",
                        "interval": 2,
                    },
                },
                {
                    "id": "heading:next",
                    "if_revision": "r_heading",
                    "after": None,
                },
                {
                    "id": "task:rich",
                    "if_revision": "r_rich",
                    "notes_markdown": "Replacement",
                    "replace_rich_note": True,
                },
            ],
        }
    )

    assert call.ensure_tags[1].parent_id == "$people"
    assert call.change_tags[0].title == "Archive"
    assert call.change[0].repeat is not None
    assert call.change[1].model_fields_set == {"id", "if_revision", "after"}
    change_schema = COMMIT_IN["properties"]["change"]["items"]["properties"]
    assert change_schema["lifecycle"]["enum"] == [
        "trash",
        "restore",
        "delete_permanently",
    ]


def test_irreversible_mutations_cannot_hide_other_edits() -> None:
    with pytest.raises(ValueError, match="lifecycle change"):
        CommitCall.model_validate(
            {
                "intent_id": "permanent-delete-mixed-001",
                "change": [
                    {
                        "id": "task:old",
                        "if_revision": "r_old",
                        "lifecycle": "delete_permanently",
                        "title": "Hidden edit",
                    }
                ],
            }
        )

    with pytest.raises(ValueError, match="tag deletion"):
        CommitCall.model_validate(
            {
                "intent_id": "tag-delete-mixed-001",
                "tags_revision": "s_tags",
                "change_tags": [
                    {
                        "id": "tag:old",
                        "title": "Hidden edit",
                        "delete_permanently": True,
                    }
                ],
            }
        )


def test_manual_schemas_are_flat_and_compact() -> None:
    schemas = (READ_IN, COMMIT_IN, APPROVE_IN, RESULT_OUT)
    for schema in schemas:
        text = str(schema)
        assert "oneOf" not in text
        assert "anyOf" not in text
        assert "$ref" not in text
        assert "$defs" not in text
        assert schema["additionalProperties"] is False

    assert COMMIT_IN["required"] == ["intent_id"]
    assert APPROVE_IN["required"] == ["plan_id"]
    assert RESULT_OUT["required"] == ["next", "status", "instruction"]
    assert RESULT_OUT["properties"]["sections"]["items"]["properties"][
        "item_ids"
    ]["items"]["pattern"]
    discovery_chars = sum(
        len(json.dumps(schema, separators=(",", ":"))) for schema in schemas
    )
    # Review completeness, DiagnosticFact, named homes, and compact Project
    # headings and native checklists. Keep the contract compact, but allow that
    # justified expansion.
    # Semantic Project and Task notes add explicit prose and source fields.
    assert discovery_chars < 20_000
    assert discovery_chars - 13_406 < 6_400
    wire_schemas = (READ_IN, COMMIT_IN, APPROVE_IN, READ_OUT, COMMIT_OUT, APPROVE_OUT)
    wire_chars = sum(
        len(json.dumps(schema, separators=(",", ":"))) for schema in wire_schemas
    )
    assert wire_chars < 23_000
    assert READ_DESC and COMMIT_DESC and APPROVE_DESC
    assert len(READ_DESC) < 700
    assert len(COMMIT_DESC) < 550
    assert len(APPROVE_DESC) < 220
    assert "natural confirmation" in COMMIT_DESC
    assert "private" in COMMIT_DESC
    assert "retry the same intent_id" in COMMIT_DESC
    assert "local neighborhood" in READ_DESC
    assert "include affected projects in one read" in READ_DESC.casefold()
    assert "limit=40" in READ_DESC
    assert "private" in APPROVE_DESC


def test_tool_descriptions_teach_low_turn_selector_and_dependency_order() -> None:
    read_lower = READ_DESC.lower()
    commit_lower = COMMIT_DESC.lower()

    for instruction in (
        "select exactly one view",
        "purpose=change is one item",
        "organize is the draft",
        "recurrence is one task",
        "local neighborhood",
        "include a destination",
        "within=trash searches trash",
        "writable neighborhood",
    ):
        assert instruction in read_lower
    for instruction in (
        "project tasks keep order",
        "accept checklists",
        "use heading_title on all tasks or none",
        "local create keys may appear in any order",
        "parent tags before children",
        "natural confirmation",
        "control fields private",
    ):
        assert instruction in commit_lower

    assert READ_IN["properties"]["view"]["enum"]
    assert READ_IN["properties"]["within"]["pattern"].startswith("^(trash|(project|area):")
    assert COMMIT_IN["properties"]["organize"]["items"]["properties"][
        "delete_headings"
    ]["items"]["pattern"].startswith("^[a-z]")
    assert COMMIT_IN["properties"]["create"]["items"]["properties"]["start"][
        "maxLength"
    ] >= len("evening")
    project_task = COMMIT_IN["properties"]["create"]["items"]["properties"][
        "tasks"
    ]["items"]
    assert "heading_title" in project_task["properties"]
    assert "checklist" in project_task["properties"]
    assert "finish" in project_task["properties"]
    assert project_task["properties"]["finish"]["maxLength"] == 400
    assert project_task["properties"]["start_here"]["maxItems"] == 4
    assert project_task["properties"]["approach"]["maxItems"] == 4
    assert project_task["properties"]["sources"]["maxItems"] == 12
    source_ref = project_task["properties"]["sources"]["items"]
    assert source_ref["required"] == ["label", "location"]
    assert source_ref["additionalProperties"] is False
    assert COMMIT_IN["properties"]["create"]["items"]["properties"]["document"] == {
        "const": "source"
    }
    project_properties = COMMIT_IN["properties"]["create"]["items"]["properties"]
    assert project_properties["note_style"]["enum"] == ["natural", "visual"]
    assert project_properties["outcome"]["maxLength"] == 400
    assert project_properties["finished_when"]["maxItems"] == 6
    assert project_properties["keep_in_mind"]["maxItems"] == 6
    assert "revise" in RESULT_OUT["properties"]["next"]["enum"]
    assert COMMIT_IN["properties"]["change"]["items"]["properties"]["start"][
        "maxLength"
    ] >= len("evening")
    start_pattern = COMMIT_IN["properties"]["change"]["items"]["properties"]["start"][
        "pattern"
    ]
    assert "today" in start_pattern
    assert "someday" in start_pattern
    assert READ_IN["properties"]["ids"]["maxItems"] == 10
    assert READ_IN["properties"]["ids"]["minItems"] == 1
    assert READ_IN["properties"]["include"]["maxItems"] == 40
    assert READ_IN["properties"]["include"]["uniqueItems"] is True
    assert "area" in READ_IN["properties"]["view"]["enum"]
    assert "audit" in READ_IN["properties"]["view"]["enum"]
    assert "diagnostics" in READ_IN["properties"]["view"]["enum"]
    read_item = READ_OUT["properties"]["items"]["items"]
    assert read_item["additionalProperties"] is True
    assert "truncated_fields" in read_item["properties"]
    assert "signals" in read_item["properties"]
    assert "direct_tag_ids" in read_item["properties"]
    assert READ_IN["properties"]["fields"]["items"]["enum"] == [
        "notes",
        "checklist",
        "tags",
        "recurrence",
    ]
    assert RESULT_OUT["properties"]["tags"]["maxItems"] == 400


def test_dump_result_keeps_complete_false() -> None:
    result = Result(
        next="read",
        status="ok",
        instruction="Continue the cursor.",
        context=ContextFact(
            id="ctx_abcdefgh",
            purpose="review",
            expires_at="2026-08-19T12:00:00+00:00",
            complete=False,
        ),
        truncated=True,
    )
    payload = dump_result(result)
    assert payload["context"]["complete"] is False
    assert payload["truncated"] is True


def test_manual_schema_contracts_match_the_runtime_models() -> None:
    pairs = (
        (ReadCall, READ_IN),
        (CommitCall, COMMIT_IN),
        (ApproveCall, APPROVE_IN),
        (CreateEntry, COMMIT_IN["properties"]["create"]["items"]),
        (
            ProjectTask,
            COMMIT_IN["properties"]["create"]["items"]["properties"][
                "tasks"
            ]["items"],
        ),
        (ChangeEntry, COMMIT_IN["properties"]["change"]["items"]),
        (EnsureTag, COMMIT_IN["properties"]["ensure_tags"]["items"]),
        (ChangeTag, COMMIT_IN["properties"]["change_tags"]["items"]),
        (OrganizeDraft, COMMIT_IN["properties"]["organize"]["items"]),
        (
            OrganizeSection,
            COMMIT_IN["properties"]["organize"]["items"]["properties"][
                "sections"
            ]["items"],
        ),
        (
            RepeatCreate,
            COMMIT_IN["properties"]["create"]["items"]["properties"]["repeat"],
        ),
        (
            RepeatEdit,
            COMMIT_IN["properties"]["change"]["items"]["properties"]["repeat"],
        ),
        (
            ChecklistAdd,
            COMMIT_IN["properties"]["change"]["items"]["properties"][
                "checklist_add"
            ]["items"],
        ),
        (
            ChecklistChange,
            COMMIT_IN["properties"]["change"]["items"]["properties"][
                "checklist_change"
            ]["items"],
        ),
        (Result, RESULT_OUT),
        (ItemFact, RESULT_OUT["properties"]["items"]["items"]),
        (ContextFact, RESULT_OUT["properties"]["context"]),
        (LayoutFact, RESULT_OUT["properties"]["layouts"]["items"]),
        (
            LayoutSectionFact,
            RESULT_OUT["properties"]["layouts"]["items"]["properties"][
                "sections"
            ]["items"],
        ),
        (RecoveryFact, RESULT_OUT["properties"]["recovery"]),
        (TagFact, RESULT_OUT["properties"]["tags"]["items"]),
        (
            ReviewSection,
            RESULT_OUT["properties"]["sections"]["items"],
        ),
        (
            DiagnosticFact,
            RESULT_OUT["properties"]["diagnostics"]["items"],
        ),
        (
            DiagnosticRepair,
            RESULT_OUT["properties"]["diagnostics"]["items"]["properties"][
                "repairs"
            ]["items"],
        ),
        (PlanFact, RESULT_OUT["properties"]["plan"]),
        (
            ChecklistFact,
            RESULT_OUT["properties"]["items"]["items"]["properties"][
                "checklist"
            ]["items"],
        ),
        (
            RecurrenceFact,
            RESULT_OUT["properties"]["items"]["items"]["properties"][
                "recurrence"
            ],
        ),
    )
    constraints = {
        "const",
        "default",
        "enum",
        "format",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "pattern",
    }

    for model, manual in pairs:
        runtime = model.model_json_schema(by_alias=True)
        assert set(manual.get("properties", {})) == set(runtime.get("properties", {}))
        assert set(manual.get("required", [])) == set(runtime.get("required", []))
        for name, runtime_property in runtime.get("properties", {}).items():
            while "$ref" in runtime_property:
                target = runtime
                for part in runtime_property["$ref"].removeprefix("#/").split("/"):
                    target = target[part]
                runtime_property = target
            if "anyOf" in runtime_property:
                non_null = [
                    branch
                    for branch in runtime_property["anyOf"]
                    if branch.get("type") != "null"
                ]
                if len(non_null) == 1:
                    runtime_property = non_null[0]
            manual_property = manual["properties"][name]
            for key in constraints & runtime_property.keys():
                if key == "maxLength" and model in {ReadInclude, ContextFact}:
                    # Keep repeated context metadata out of the compact wire
                    # schema. Pydantic still enforces these bounds at runtime.
                    continue
                if key == "default" and runtime_property[key] is None:
                    continue
                assert manual_property.get(key) == runtime_property[key], (
                    model.__name__,
                    name,
                    key,
                )
