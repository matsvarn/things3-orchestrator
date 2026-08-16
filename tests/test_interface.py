from __future__ import annotations

import json

import pytest
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
    CommitCall,
    ReadCall,
    Result,
)


def test_empty_read_means_today_and_aliases_are_wire_names() -> None:
    assert ReadCall.model_validate({}).view is None
    call = ReadCall.model_validate(
        {"view": "logbook", "from": "2026-08-01", "to": "2026-08-15", "limit": 40}
    )
    assert call.from_date == "2026-08-01"
    assert call.model_dump(by_alias=True)["from"] == "2026-08-01"


@pytest.mark.parametrize(
    "payload",
    [
        {"view": "today", "find": "tax"},
        {"view": "project"},
        {"view": "inbox", "within": "area:home"},
        {"find": "tax", "within": "task:one"},
        {"view": "project", "within": "task:one"},
        {"view": "logbook", "from": "2026-08-01"},
        {"view": "logbook", "from": "2026-08-15", "to": "2026-08-01"},
        {"find": "tax", "unknown": True},
        {"limit": "20"},
    ],
)
def test_read_rejects_ambiguous_or_invalid_input(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ReadCall.model_validate(payload)


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


@pytest.mark.parametrize(
    "create, message",
    [
        (
            [
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
            "earlier heading create entry",
        ),
        (
            [
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
            "earlier heading create entry",
        ),
    ],
    ids=["forward-heading", "non-heading"],
)
def test_task_create_rejects_invalid_local_heading_references(
    create: list[dict[str, object]], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        CommitCall.model_validate(
            {"intent_id": "heading-local-invalid-001", "create": create}
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


def test_commit_rejects_forward_local_order_anchors() -> None:
    with pytest.raises(ValidationError, match="earlier create entries"):
        CommitCall.model_validate(
            {
                "intent_id": "forward-order-001",
                "create": [
                    {"title": "First", "after": "$later"},
                    {"key": "$later", "title": "Later"},
                ],
            }
        )


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
    assert "tags" in COMMIT_OUT["properties"]
    assert "tags" in APPROVE_OUT["properties"]


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
    assert discovery_chars < 13_000
    wire_schemas = (READ_IN, COMMIT_IN, APPROVE_IN, READ_OUT, COMMIT_OUT, APPROVE_OUT)
    wire_chars = sum(
        len(json.dumps(schema, separators=(",", ":"))) for schema in wire_schemas
    )
    assert wire_chars < 13_100
    assert READ_DESC and COMMIT_DESC and APPROVE_DESC
    assert "natural confirmation" in COMMIT_DESC
    assert "private" in COMMIT_DESC
    assert "private" in APPROVE_DESC
