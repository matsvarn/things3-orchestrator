from __future__ import annotations

import pytest
from pydantic import ValidationError

from things_orchestrator.interface import (
    ContextFact,
    ReadCall,
    Result,
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
    assert (
        ReadCall.model_validate({"view": "weekly_review"}).view
        == "weekly_review"
    )
    assert ReadCall.model_validate(
        {"view": "weekly_review", "category": "someday"}
    ).category == "someday"
    with pytest.raises(ValueError, match="signals_any needs view audit"):
        ReadCall.model_validate(
            {"view": "weekly_review", "signals_any": ["someday", "waiting"]}
        )
    with pytest.raises(ValidationError):
        ReadCall.model_validate(
            {"view": "weekly_review", "category": "not_a_category"}
        )
    assert ReadCall.model_validate(
        {"view": "audit", "signals_any": ["someday", "waiting"]}
    ).signals_any == ["someday", "waiting"]
    for signal in ("", "x" * 81):
        with pytest.raises(ValidationError):
            ReadCall.model_validate({"view": "audit", "signals_any": [signal]})
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
