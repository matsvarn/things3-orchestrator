from __future__ import annotations

import pytest
from pydantic import ValidationError

from things_orchestrator.interface import (
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
        {"purpose": "change", "id": "task:one"},
        {"purpose": "organize", "find": "Launch"},
        {"purpose": "recurrence", "id": "task:repeat"},
        {"view": "weekly_review"},
        {"view": "diagnostics"},
        {"include": [{"id": "task:anchor"}]},
        {"view": "logbook", "from": "2026-08-01"},
        {"view": "logbook", "from": "2026-08-15", "to": "2026-08-01"},
        {"find": "tax", "unknown": True},
        {"limit": "20"},
    ],
)
def test_read_rejects_ambiguous_or_invalid_input(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ReadCall.model_validate(payload)


def test_logbook_range_compares_mixed_iso_calendar_and_week_dates() -> None:
    call = ReadCall.model_validate(
        {"view": "logbook", "from": "2026-W02-1", "to": "2026-01-15"}
    )
    assert call.from_date == "2026-W02-1"
    assert call.to_date == "2026-01-15"
    with pytest.raises(ValidationError, match="from must not be after to"):
        ReadCall.model_validate(
            {"view": "logbook", "from": "2026-01-15", "to": "2026-W02-1"}
        )


def test_read_keeps_live_selector_rules() -> None:
    assert ReadCall.model_validate({"view": "system"}).view == "system"
    assert ReadCall.model_validate(
        {"view": "audit", "signals_any": ["someday", "waiting"]}
    ).signals_any == ["someday", "waiting"]
    for signal in ("", "x" * 81):
        with pytest.raises(ValidationError):
            ReadCall.model_validate({"view": "audit", "signals_any": [signal]})
    with pytest.raises(ValueError, match="signals_any needs view audit"):
        ReadCall.model_validate({"view": "today", "signals_any": ["someday"]})
    for payload in (
        {"within": "trash"},
        {"view": "today", "within": "trash"},
        {"cursor": "cursor_12345678", "id": "task:one"},
    ):
        with pytest.raises(ValidationError):
            ReadCall.model_validate(payload)

    trash = ReadCall.model_validate({"find": "Later", "within": "trash"})
    assert trash.within == "trash"


def test_read_limit_stays_bounded() -> None:
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


def test_dump_result_keeps_truncated_true() -> None:
    result = Result(
        next="read",
        status="ok",
        instruction="Continue the cursor.",
        truncated=True,
    )
    payload = dump_result(result)
    assert payload["truncated"] is True
