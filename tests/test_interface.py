from __future__ import annotations

import pytest
from pydantic import ValidationError

from things_orchestrator.interface import (
    BULK_ID_LIMIT,
    ReadCall,
    RecurrenceFact,
    RepeatOn,
    Result,
    dump_result,
)
from things_orchestrator.v2 import PublicItem, TaintedText


def test_empty_read_means_today() -> None:
    assert ReadCall.model_validate({}).view is None
    assert ReadCall.model_validate({"view": "logbook"}).view == "logbook"
    assert ReadCall.model_validate({"id": "area:home"}).id == "area:home"


@pytest.mark.parametrize(
    "payload",
    [
        {"view": "today", "find": "tax"},
        {"view": "project"},
        {"view": "area"},
        {"view": "audit"},
        {"view": "system"},
        {"view": "inbox", "within": "area:home"},
        {"find": "tax", "within": "task:one"},
        {"ids": ["task:one"], "view": "today"},
        {"ids": []},
        {"fields": ["notes"]},
        {"signals_any": ["someday"]},
        {"purpose": "change", "id": "task:one"},
        {"purpose": "organize", "find": "Launch"},
        {"purpose": "recurrence", "id": "task:repeat"},
        {"view": "weekly_review"},
        {"view": "diagnostics"},
        {"include": [{"id": "task:anchor"}]},
        {"view": "logbook", "from": "2026-08-01"},
        {"view": "logbook", "from": "2026-08-01", "to": "2026-08-15"},
        {"find": "tax", "unknown": True},
        {"limit": "20"},
    ],
)
def test_read_rejects_ambiguous_or_invalid_input(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ReadCall.model_validate(payload)


def test_read_keeps_live_selector_rules() -> None:
    for payload in (
        {"within": "trash"},
        {"view": "today", "within": "trash"},
        {"cursor": "cursor_12345678", "id": "task:one"},
        {"cursor": "cursor_12345678", "ids": ["task:one"]},
    ):
        with pytest.raises(ValidationError):
            ReadCall.model_validate(payload)

    trash = ReadCall.model_validate({"find": "Later", "within": "trash"})
    assert trash.within == "trash"


def test_read_limit_and_bulk_ids_stay_bounded() -> None:
    assert ReadCall(limit=40).limit == 40
    with pytest.raises(ValidationError):
        ReadCall(limit=41)
    ids = [f"task:{index:02d}" for index in range(BULK_ID_LIMIT)]
    assert ReadCall(ids=ids).ids == ids
    with pytest.raises(ValidationError):
        ReadCall(ids=[*ids, "task:overflow"])

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
        "instruction": "Current facts.",
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
            "tags": [{"id": "tag:focus", "title": "Focus"}],
        }
    )
    assert result.items[0].revision == "r_17"
    assert result.items[0].checklist[0].status == "completed"
    assert result.tags[0].id == "tag:focus"


def test_dump_result_keeps_truncated_true() -> None:
    result = Result(
        next="read",
        status="ok",
        instruction="Continue the cursor.",
        truncated=True,
    )
    payload = dump_result(result)
    assert payload["truncated"] is True


def test_public_item_recurrence_is_the_internal_fact() -> None:
    fact = RecurrenceFact(
        kind="template",
        mode="fixed",
        unit="week",
        interval=1,
        weekdays=["monday"],
    )
    item = PublicItem(
        id="task:repeat",
        kind="task",
        title=TaintedText(value="Plan week"),
        status="open",
        recurrence=fact,
    )

    assert item.recurrence is fact
    assert RecurrenceFact.model_json_schema()["title"] == "PublicRecurrence"


def test_recurrence_fact_keeps_weekday_and_selector_rules() -> None:
    with pytest.raises(ValidationError, match="weekdays cannot contain duplicates"):
        RecurrenceFact(kind="template", weekdays=["monday", "monday"])
    with pytest.raises(ValidationError, match="linked_item_ids"):
        RecurrenceFact(kind="template", linked_item_ids=["task:one", "task:one"])
    with pytest.raises(ValidationError, match="linked_item_ids"):
        RecurrenceFact(kind="template", linked_item_ids=["not-an-id"])
    with pytest.raises(ValidationError, match="exactly one day or weekday"):
        RecurrenceFact(kind="template", on=[RepeatOn()])
    with pytest.raises(ValidationError, match="ordinal needs a weekday"):
        RepeatOn(day=1, ordinal=1)
