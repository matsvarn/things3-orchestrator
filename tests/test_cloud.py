from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from things_orchestrator.cloud import (
    _CACHE_VERSION,
    CloudClient,
    CloudError,
    CloudLibrary,
    Envelope,
    HistoryPage,
    fold_events,
)
from things_orchestrator.interface import ApproveCall, CommitCall, ReadCall
from things_orchestrator.journal import MemoryJournal
from things_orchestrator.library import (
    MAX_RECURRENCE_INSTANCE_COUNT,
    ChecklistLine,
    MemoryLibrary,
    Record,
    Write,
    day_ts,
    from_ts,
    new_uuid,
)
from things_orchestrator.owner_authority import (
    enroll_owner_factor,
    verified_authorization,
)
from things_orchestrator.recurrence import RecurrenceState
from things_orchestrator.v2 import ThingsV2
from things_orchestrator.workspace import ThingsWorkspace

_MALFORMED_NATIVE_NUMBERS: tuple[object, ...] = (
    "invalid",
    True,
    float("inf"),
    float("-inf"),
    float("nan"),
    10**100,
    -(10**100),
)


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs POSIX timezone control")
def test_wire_calendar_dates_do_not_depend_on_host_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = os.environ.get("TZ")
    target = date(2026, 8, 17)
    expected = int(datetime(2026, 8, 17, tzinfo=timezone.utc).timestamp())
    try:
        monkeypatch.setenv("TZ", "Pacific/Kiritimati")
        time.tzset()
        assert day_ts(target) == expected
        assert day_ts(target + timedelta(days=1)) - day_ts(target) == 86_400

        monkeypatch.setenv("TZ", "America/Los_Angeles")
        time.tzset()
        assert from_ts(expected) == target
    finally:
        if previous is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", previous)
        time.tzset()


def test_commit_does_not_skip_unfetched_head() -> None:
    client = CloudClient("a@b.c", "pw")
    client.history_id = "h"
    client.server_index = 4
    client.loaded_index = 4
    client._request = lambda *args, **kwargs: {"server-head-index": 5}  # noqa: SLF001
    client.commit([Envelope("x", 0, "Task6", {"tt": "Hi"})])
    assert client.server_index == 5
    assert client.loaded_index == 4


def test_fold_keeps_headings_and_drops_recurring_templates() -> None:
    library = MemoryLibrary()
    fold_events(
        [
            {
                "uuid": "head",
                "e": "Task6",
                "t": 0,
                "p": {"tt": "Next", "tp": 2, "ss": 0, "st": 1, "tr": False},
            },
            {
                "uuid": "child",
                "e": "Task6",
                "t": 0,
                "p": {
                    "tt": "Call bank",
                    "tp": 0,
                    "ss": 0,
                    "st": 1,
                    "tr": False,
                    "agr": ["head"],
                    "pr": ["proj"],
                },
            },
            {
                "uuid": "repeat",
                "e": "Task6",
                "t": 0,
                "p": {
                    "tt": "Weekly",
                    "tp": 0,
                    "ss": 0,
                    "st": 1,
                    "tr": False,
                    "rr": {"tp": 0},
                },
            },
        ],
        library=library,
    )
    assert library.records["head"].heading is True
    assert library.records["head"].is_open() is False
    assert library.records["child"].heading_uuid == "head"
    assert library.records["repeat"].recurrence.role == "template"
    assert library.records["repeat"].is_open() is False
    fold_events(
        [{"uuid": "repeat", "e": "Task6", "t": 1, "p": {"tt": "Weekly still"}}],
        library=library,
    )
    assert library.records["repeat"].recurrence.role == "template"
    assert library.records["repeat"].is_open() is False


@pytest.mark.parametrize(
    "field", ["sp", "sr", "dd", "ato", "icsd", "icc", "acrd", "tir"]
)
@pytest.mark.parametrize(
    "value", _MALFORMED_NATIVE_NUMBERS
)
def test_malformed_native_numeric_fields_fold_as_missing(
    field: str, value: object
) -> None:
    library = MemoryLibrary()

    fold_events(
        [
            {
                "uuid": "template",
                "e": "Task7",
                "t": 0,
                "p": {
                    "tt": "Routine",
                    "tp": 0,
                    "rr": {"tp": 0, "fu": 256, "fa": 1, "of": []},
                    field: value,
                },
            }
        ],
        library=library,
    )

    template = library.records["template"]
    assert template.completed_at is None
    assert template.start is None
    assert template.deadline is None
    assert template.remind is None
    assert template.recurrence_created_through is None
    assert template.recurrence_instance_count == 0
    assert template.recurrence_instance_count_known is False
    assert template.recurrence_completed_on is None
    assert template.recurrence_next_on is None


@pytest.mark.parametrize("value", _MALFORMED_NATIVE_NUMBERS)
def test_malformed_native_generated_count_preserves_the_last_valid_count(
    value: object,
) -> None:
    library = MemoryLibrary(
        [
            Record(
                uuid="template",
                kind="task",
                title="Routine",
                recurrence=RecurrenceState(
                    role="template",
                    repeat_type="fixed",
                    rule={"tp": 0, "fu": 256, "fa": 1, "of": []},
                ),
                recurrence_instance_count=3,
            )
        ]
    )

    fold_events(
        [
            {
                "uuid": "template",
                "e": "Task7",
                "t": 1,
                "p": {"icc": value},
            }
        ],
        library=library,
    )

    assert library.records["template"].recurrence_instance_count == 3
    assert library.records["template"].recurrence_instance_count_known is True


@pytest.mark.parametrize("paused", ["false", 0, 1, None, [], {}])
def test_malformed_native_paused_state_is_unavailable_and_not_writable(
    paused: object,
) -> None:
    library = MemoryLibrary()
    fold_events(
        [
            {
                "uuid": "template",
                "e": "Task7",
                "t": 0,
                "p": {
                    "tt": "Routine",
                    "tp": 0,
                    "rr": {"tp": 0, "fu": 256, "fa": 1, "of": []},
                    "icp": paused,
                },
            }
        ],
        library=library,
    )
    template = library.records["template"]
    assert template.recurrence.paused is False
    assert template.recurrence_paused_known is False

    journal = MemoryJournal()
    request_id = "0198f0ee-98d4-7bd5-91ba-8e76019b2992"
    interface = ThingsV2(
        ThingsWorkspace(
            library,
            journal=journal,
            account_id="owner@example.com",
        )
    )
    recurrence = interface.dispatch(
        "things_get", {"ids": ["task:template"]}
    ).items[0].recurrence
    assert recurrence is not None
    assert recurrence.paused is None

    result = interface.dispatch(
        "things_update",
        {
            "request_id": request_id,
            "items": [
                {"id": "task:template", "set": {"repeat": {"paused": True}}}
            ],
        },
    )
    assert result.state == "rejected"
    assert result.code == "validation_error"
    assert result.next_action == "read_fresh"
    assert journal.get_v2_request("owner@example.com", "2", request_id) is None


def test_malformed_sparse_native_paused_state_preserves_last_valid_state() -> None:
    library = MemoryLibrary()
    fold_events(
        [
            {
                "uuid": "template",
                "e": "Task7",
                "t": 0,
                "p": {
                    "tt": "Routine",
                    "tp": 0,
                    "rr": {"tp": 0, "fu": 256, "fa": 1, "of": []},
                    "icp": True,
                },
            },
            {
                "uuid": "template",
                "e": "Task7",
                "t": 1,
                "p": {"icp": "false"},
            },
        ],
        library=library,
    )

    assert library.records["template"].recurrence.paused is True
    assert library.records["template"].recurrence_paused_known is True


@pytest.mark.parametrize(
    "count", ["invalid", MAX_RECURRENCE_INSTANCE_COUNT], ids=["unknown", "exhausted"]
)
def test_create_next_rejects_unavailable_or_exhausted_native_counts(
    count: object,
) -> None:
    library = MemoryLibrary()
    fold_events(
        [
            {
                "uuid": "template",
                "e": "Task7",
                "t": 0,
                "p": {
                    "tt": "Routine",
                    "tp": 0,
                    "rr": {"tp": 0, "fu": 256, "fa": 1, "of": []},
                    "tir": day_ts(date(2026, 9, 6)),
                    "icc": count,
                },
            },
            {
                "uuid": "current",
                "e": "Task7",
                "t": 0,
                "p": {
                    "tt": "Routine",
                    "tp": 0,
                    "sr": day_ts(date(2026, 8, 30)),
                    "rt": ["template"],
                    "lt": True,
                },
            },
        ],
        library=library,
    )
    journal = MemoryJournal()
    interface = ThingsV2(
        ThingsWorkspace(
            library,
            journal=journal,
            clock=lambda: datetime(2026, 8, 30, 12, tzinfo=timezone.utc),
            account_id="owner@example.com",
        )
    )
    request_id = "0198f0ee-98d4-7bd5-91ba-8e76019b2990"

    recurrence = interface.dispatch(
        "things_get", {"ids": ["task:current"]}
    ).items[0].recurrence
    assert recurrence is not None
    assert recurrence.generated_count == (
        MAX_RECURRENCE_INSTANCE_COUNT
        if count == MAX_RECURRENCE_INSTANCE_COUNT
        else None
    )

    result = interface.dispatch(
        "things_update",
        {
            "request_id": request_id,
            "items": [
                {
                    "id": "task:current",
                    "set": {"repeat": {"create_next": True}},
                }
            ],
        },
    )

    assert result.state == "rejected"
    assert result.code == "validation_error"
    assert result.next_action == "read_fresh"
    assert set(library.records) == {"template", "current"}
    assert journal.get_v2_request("owner@example.com", "2", request_id) is None


def test_fold_accepts_task7_and_preserves_its_entity_for_updates(
    tmp_path: Path,
) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    fold_events(
        [
            {
                "uuid": "new-task",
                "e": "Task7",
                "t": 0,
                "p": {
                    "tt": "New task",
                    "tp": 0,
                    "ss": 0,
                    "st": 0,
                    "tr": False,
                },
            }
        ],
        library=library,
    )

    item = library.records["new-task"]
    assert item.inbox is True
    assert item.entity == "Task7"

    library.apply([Write(action="update", uuid=item.uuid, title="Renamed")])

    assert client.committed[0].kind == "Task7"


def test_task_creates_and_legacy_mutations_emit_task7(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records["legacy"] = Record(
        uuid="legacy", kind="task", title="Legacy", entity="Task6"
    )

    created = library._envelope(
        Write(action="create", uuid="new", kind="task", title="New")
    )
    heading = library._envelope(
        Write(
            action="create_heading",
            uuid="heading",
            kind="task",
            title="Heading",
            heading=True,
        )
    )
    updated = library._envelope(
        Write(action="update", uuid="legacy", kind="task", title="Updated")
    )
    completed = library._envelope(
        Write(action="complete", uuid="legacy", kind="task")
    )
    trashed = library._envelope(
        Write(action="trash", uuid="legacy", kind="task")
    )

    assert {created.kind, heading.kind, updated.kind, completed.kind, trashed.kind} == {
        "Task7"
    }


@pytest.mark.parametrize("entity", ["Task8", "Area4", "Tag5", "ChecklistItem4"])
def test_fold_rejects_unknown_versioned_entities(entity: str) -> None:
    library = MemoryLibrary()
    with pytest.raises(CloudError, match="unsupported Things Cloud entity"):
        fold_events(
            [
                {
                    "uuid": "known",
                    "e": "Task7",
                    "t": 0,
                    "p": {"tt": "Known", "tp": 0, "ss": 0, "st": 0},
                },
                {"uuid": "future", "e": entity, "t": 0, "p": {"tt": "Future"}},
            ],
            library=library,
        )
    assert library.records == {}


def test_fold_preserves_opaque_repeat_rules_and_partial_updates() -> None:
    rule = {
        "tp": 1,
        "fu": 256,
        "fa": 2,
        "of": [{"wd": 3, "future_wire_key": {"nested": True}}],
        "sr": 1_786_838_400,
        "ia": 1_786_924_800,
        "ed": 64_092_211_200,
        "rc": 0,
        "ts": 7,
        "rrv": 99,
        "future_rule_key": ["preserve", 4],
    }
    library = MemoryLibrary()
    fold_events(
        [
            {
                "uuid": "template",
                "e": "Task6",
                "t": 0,
                "p": {"tt": "Routine", "tp": 0, "rr": rule, "rt": []},
            },
            {
                "uuid": "instance",
                "e": "Task6",
                "t": 0,
                "p": {"tt": "Routine copy", "tp": 0, "rt": ["template"]},
            },
        ],
        library=library,
    )

    assert library.records["template"].recurrence.rule == rule
    assert library.records["template"].recurrence.repeat_type == "after_completion"
    assert library.records["instance"].recurrence.links == ("template",)

    # A sparse event must not erase fields that were not present in the patch.
    fold_events(
        [{"uuid": "template", "e": "Task6", "t": 1, "p": {"tt": "Renamed"}}],
        library=library,
    )
    assert library.records["template"].recurrence.rule == rule

    fold_events(
        [{"uuid": "template", "e": "Task6", "t": 1, "p": {"rr": None}}],
        library=library,
    )
    assert library.records["template"].recurrence.rule is None
    assert library.records["template"].recurrence.role == "none"


def test_fold_and_cache_preserve_opaque_task7_repeater_payload(tmp_path: Path) -> None:
    repeater = {
        "v": 1,
        "t": 0,
        "pfu": 1,
        "pfa": 2,
        "po": [{"wd": 1}, {"wd": 4}],
        "future": {"preserve": True},
    }
    library = MemoryLibrary()

    fold_events(
        [
            {
                "uuid": "rt2",
                "e": "Task7",
                "t": 0,
                "p": {"tt": "Future repeater", "tp": 0, "rp": repeater},
            }
        ],
        library=library,
    )

    assert library.records["rt2"].repeater == repeater
    assert library.records["rt2"].repeater is not repeater
    assert library.records["rt2"].recurrence_instance_count_known is False
    assert library.records["rt2"].recurrence_paused_known is False

    cache = tmp_path / "state.json"
    cloud = CloudLibrary(_CaptureClient(), cache=cache)  # type: ignore[arg-type]
    cloud.records = library.records
    cloud.client.history_id = "history"
    cloud._save_cache()

    restored = CloudLibrary(_CaptureClient(), cache=cache)  # type: ignore[arg-type]
    assert restored._restore_cache("history") is True
    assert restored.records["rt2"].repeater == repeater
    assert restored.records["rt2"].recurrence_instance_count_known is False
    assert restored.records["rt2"].recurrence_paused_known is False


def test_task7_repeat_pause_round_trips_as_template_bookkeeping(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records["template"] = Record(
        uuid="template",
        kind="task",
        title="Routine",
        recurrence=RecurrenceState(
            role="template",
            repeat_type="fixed",
            rule={"tp": 0, "fu": 256, "fa": 1, "of": [{"wd": 1}]},
        ),
        entity="Task7",
    )

    library.apply(
        [
            Write(
                action="repeat",
                uuid="template",
                recurrence_paused=True,
            )
        ]
    )

    assert set(client.committed[0].payload) == {"md", "icp"}
    assert client.committed[0].payload["icp"] is True
    assert library.records["template"].recurrence.paused is True


def test_task7_sparse_rule_clear_keeps_the_record(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records["template"] = Record(
        uuid="template",
        kind="task",
        title="Routine",
        recurrence=RecurrenceState(
            role="template",
            repeat_type="fixed",
            rule={"tp": 0, "fu": 256, "fa": 1, "of": [{"wd": 1}]},
        ),
        entity="Task7",
    )

    library.apply(
        [
            Write(
                action="repeat",
                uuid="template",
                clear_recurrence_rule=True,
            )
        ]
    )

    assert client.committed[0].action == 1
    assert client.committed[0].payload["rr"] is None
    assert "template" in library.records
    assert library.records["template"].recurrence == RecurrenceState()


def test_fold_sparse_placement_clears_incompatible_home() -> None:
    library = MemoryLibrary()
    fold_events(
        [
            {
                "uuid": "task",
                "e": "Task6",
                "t": 0,
                "p": {"tt": "Task", "tp": 0, "ar": ["area"], "st": 1},
            }
        ],
        library=library,
    )

    # Things sends only the new project relation when a task moves between homes.
    fold_events(
        [{"uuid": "task", "e": "Task6", "t": 1, "p": {"pr": ["project"]}}],
        library=library,
    )
    task = library.records["task"]
    assert task.parent_uuid == "project"
    assert task.area_uuid is None
    assert task.inbox is False

    # The reverse move is also sparse and must clear the old project relation.
    fold_events(
        [{"uuid": "task", "e": "Task6", "t": 1, "p": {"ar": ["area"]}}],
        library=library,
    )
    assert task.parent_uuid is None
    assert task.area_uuid == "area"
    assert task.inbox is False

    # Native Things lists st=0 in Inbox even when a project or area relation exists.
    fold_events(
        [{"uuid": "task", "e": "Task6", "t": 1, "p": {"st": 0}}],
        library=library,
    )
    assert task.inbox is True


def test_fold_preserves_a_generated_occurrence_date_after_rescheduling() -> None:
    original = date(2026, 9, 6)
    rescheduled = date(2026, 9, 20)
    library = MemoryLibrary()

    fold_events(
        [
            {
                "uuid": "generated",
                "e": "Task7",
                "t": 0,
                "p": {
                    "tt": "Generated",
                    "tp": 0,
                    "sr": day_ts(original),
                    "rt": ["template"],
                    "lt": True,
                },
            },
            {
                "uuid": "generated",
                "e": "Task7",
                "t": 1,
                "p": {"sr": day_ts(rescheduled)},
            },
        ],
        library=library,
    )

    generated = library.records["generated"]
    assert generated.start == rescheduled
    assert generated.recurrence_generated_on == original


def test_fold_tag4_deletion_removes_direct_and_parent_references() -> None:
    library = MemoryLibrary(
        [
            Record(uuid="task", kind="task", title="Tagged", tag_uuids=["tag"]),
            Record(
                uuid="area", kind="area", title="Tagged area", tag_uuids=["tag", "keep"]
            ),
        ]
    )
    library.tags.update({"tag": "Removed", "keep": "Keep"})
    library.tag_parents.update({"tag": ["root"], "keep": ["tag", "root"]})

    fold_events(
        [{"uuid": "tag", "e": "Tag4", "t": 2, "p": {}}],
        library=library,
    )

    assert "tag" not in library.tags
    assert "tag" not in library.tag_parents
    assert library.records["task"].tag_uuids == []
    assert library.records["area"].tag_uuids == ["keep"]
    assert library.tag_parents["keep"] == ["root"]


def test_repeat_interval_preserves_opaque_rule_and_emits_sparse_patch(
    tmp_path: Path,
) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    rule = {"tp": 0, "fu": 16, "fa": 1, "of": [{"future": "value"}], "rrv": 42}
    library.records["template"] = Record(
        uuid="template",
        kind="task",
        title="Monthly",
        entity="Task6",
        recurrence=RecurrenceState(role="template", repeat_type="fixed", rule=rule),
    )

    changed = {**rule, "fa": 3}
    library.apply([Write(action="repeat", uuid="template", recurrence_rule=changed)])

    assert len(client.committed) == 1
    envelope = client.committed[0]
    assert envelope.action == 1
    assert envelope.kind == "Task7"
    assert envelope.payload["rr"] == changed
    assert set(envelope.payload) == {"rr", "md"}
    assert library.records["template"].recurrence.rule == changed


@pytest.mark.parametrize(
    "record",
    [
        Record(uuid="normal", kind="task", title="Normal"),
        Record(
            uuid="instance",
            kind="task",
            title="Generated",
            recurrence=RecurrenceState(
                role="instance",
                repeat_type="after_completion",
                template_uuid="template",
                links=("template",),
            ),
        ),
        Record(
            uuid="unknown",
            kind="task",
            title="Unknown template",
            recurrence=RecurrenceState(
                role="template",
                repeat_type="unknown",
                rule={"tp": 99, "fu": 256, "fa": 1},
            ),
        ),
        Record(
            uuid="inconsistent",
            kind="task",
            title="Missing rule",
            recurrence=RecurrenceState(role="template", repeat_type="fixed"),
        ),
    ],
    ids=["normal", "instance", "unknown-template", "template-without-rule"],
)
def test_memory_repeat_rejects_non_template_or_inconsistent_records(
    record: Record,
) -> None:
    library = MemoryLibrary([record])

    with pytest.raises(ValueError, match="exact repeating Task or Project template"):
        library.apply(
            [
                Write(
                    action="repeat",
                    uuid=record.uuid,
                    recurrence_rule={"tp": 0, "fu": 256, "fa": 2},
                )
            ]
        )


@pytest.mark.parametrize(
    "record",
    [
        Record(uuid="normal", kind="task", title="Normal"),
        Record(
            uuid="instance",
            kind="task",
            title="Generated",
            recurrence=RecurrenceState(
                role="instance",
                repeat_type="after_completion",
                template_uuid="template",
                links=("template",),
            ),
        ),
        Record(
            uuid="unknown",
            kind="task",
            title="Unknown template",
            recurrence=RecurrenceState(
                role="template",
                repeat_type="unknown",
                rule={"tp": 99, "fu": 256, "fa": 1},
            ),
        ),
        Record(
            uuid="inconsistent",
            kind="task",
            title="Missing rule",
            recurrence=RecurrenceState(role="template", repeat_type="fixed"),
        ),
    ],
    ids=["normal", "instance", "unknown-template", "template-without-rule"],
)
def test_cloud_repeat_rejects_non_template_or_inconsistent_records(
    tmp_path: Path, record: Record
) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records[record.uuid] = record

    with pytest.raises(CloudError, match="exact repeating Task or Project template"):
        library.apply(
            [
                Write(
                    action="repeat",
                    uuid=record.uuid,
                    recurrence_rule={"tp": 0, "fu": 256, "fa": 2},
                )
            ]
        )
    assert client.committed == []


def test_fold_appends_checklist_lines() -> None:
    library = MemoryLibrary([Record(uuid="task", kind="task", title="Pack")])
    fold_events(
        [
            {
                "uuid": "box",
                "e": "ChecklistItem3",
                "t": 0,
                "p": {"tt": "passport", "ss": 0, "ts": ["task"]},
            }
        ],
        library=library,
    )
    assert library.records["task"].checklists[0].title == "passport"
    assert library.records["task"].checklists[0].done is False
    fold_events(
        [{"uuid": "box", "e": "ChecklistItem3", "t": 1, "p": {"ss": 3}}],
        library=library,
    )
    assert library.records["task"].checklists[0].title == "passport"
    assert library.records["task"].checklists[0].done is True


def test_checklist_event_before_parent_in_the_same_page() -> None:
    library = MemoryLibrary()
    fold_events(
        [
            {
                "uuid": "box",
                "e": "ChecklistItem3",
                "t": 0,
                "p": {"tt": "passport", "ss": 0, "ts": ["task"]},
            },
            {
                "uuid": "task",
                "e": "Task6",
                "t": 0,
                "p": {"tt": "Pack", "tp": 0, "ss": 0, "st": 0, "tr": False},
            },
        ],
        library=library,
    )
    assert library.records["task"].checklists[0].title == "passport"


def test_checklist_reparent_removes_the_old_parent_copy() -> None:
    library = MemoryLibrary(
        [
            Record(
                uuid="first",
                kind="task",
                title="First",
                checklists=[ChecklistLine("row", "Passport")],
            ),
            Record(uuid="second", kind="task", title="Second"),
        ]
    )

    fold_events(
        [
            {
                "uuid": "row",
                "e": "ChecklistItem3",
                "t": 1,
                "p": {"ts": ["second"], "ix": 20},
            }
        ],
        library=library,
    )

    assert library.records["first"].checklists == []
    assert library.records["second"].checklists == [
        ChecklistLine("row", "Passport", sort_index=20)
    ]


def test_snapshot_resumes_from_loaded_index(tmp_path: Path) -> None:
    cache = tmp_path / "state.json"

    class FakeClient:
        def __init__(self) -> None:
            self.email = "a@b.c"
            self.history_id = ""
            self.server_index = 0
            self.loaded_index = 0
            self.pages = 0

        def verify(self) -> str:
            self.history_id = "hist"
            return self.history_id

        def items(self, start_index: int) -> HistoryPage:
            self.pages += 1
            if start_index == 0:
                return HistoryPage(
                    events=[
                        {
                            "uuid": "t1",
                            "e": "Task6",
                            "t": 0,
                            "p": {"tt": "Old", "tp": 0, "ss": 0, "st": 0, "tr": False},
                        }
                    ],
                    current=2,
                    groups=1,
                    end_size=1,
                    latest_size=1,
                )
            return HistoryPage(
                events=[], current=2, groups=0, end_size=1, latest_size=1
            )

    first = CloudLibrary(FakeClient(), cache=cache)  # type: ignore[arg-type]
    first.refresh()
    assert "t1" in first.records
    first_pages = first.client.pages  # type: ignore[attr-defined]

    second = CloudLibrary(FakeClient(), cache=cache)  # type: ignore[arg-type]
    second.refresh()
    assert "t1" in second.records
    assert second.client.loaded_index >= 1
    assert second.client.pages == 1  # type: ignore[attr-defined]
    assert first_pages >= 1


def test_snapshot_round_trip_keeps_repeat_rule_and_links(tmp_path: Path) -> None:
    cache = tmp_path / "state.json"
    rule = {"tp": 0, "fu": 256, "fa": 1, "of": [{"wd": 0}], "rrv": 42}
    generated_on = date(2026, 9, 6)

    class FakeClient:
        def __init__(self) -> None:
            self.email = "a@b.c"
            self.history_id = ""
            self.server_index = 0
            self.loaded_index = 0
            self.pages = 0

        def verify(self) -> str:
            self.history_id = "hist"
            return self.history_id

        def items(self, start_index: int) -> HistoryPage:
            self.pages += 1
            if start_index == 0:
                return HistoryPage(
                    events=[
                        {
                            "uuid": "template",
                            "e": "Task6",
                            "t": 0,
                            "p": {"tt": "Weekly", "tp": 0, "rr": rule, "rt": []},
                        },
                        {
                            "uuid": "instance",
                            "e": "Task6",
                            "t": 0,
                            "p": {
                                "tt": "Weekly copy",
                                "tp": 0,
                                "sr": day_ts(generated_on),
                                "rt": ["template"],
                                "lt": True,
                            },
                        },
                    ],
                    current=2,
                    groups=1,
                    end_size=1,
                    latest_size=1,
                )
            return HistoryPage(
                events=[], current=2, groups=0, end_size=1, latest_size=1
            )

    first = CloudLibrary(FakeClient(), cache=cache)  # type: ignore[arg-type]
    first.refresh()
    second = CloudLibrary(FakeClient(), cache=cache)  # type: ignore[arg-type]
    second.refresh()

    assert second.records["template"].recurrence.rule == rule
    assert second.records["instance"].recurrence.links == ("template",)
    assert second.records["instance"].recurrence.template_uuid == "template"
    assert second.records["instance"].recurrence_generated_on == generated_on
    assert second.client.pages == 1  # type: ignore[attr-defined]


def test_malformed_cache_is_discarded_before_fresh_replay(tmp_path: Path) -> None:
    cache = tmp_path / "state.json"
    cache.write_text(
        '{"version":2,"history_id":"hist","loaded_index":9,'
        '"server_index":9,"records":[{"title":"missing uuid"}],'
        '"tags":{},"tag_parents":{}}'
    )

    class FakeClient:
        def __init__(self) -> None:
            self.history_id = ""
            self.server_index = 0
            self.loaded_index = 0
            self.starts: list[int] = []

        def verify(self) -> str:
            self.history_id = "hist"
            return self.history_id

        def items(self, start_index: int) -> HistoryPage:
            self.starts.append(start_index)
            if start_index == 0:
                return HistoryPage(
                    events=[
                        {
                            "uuid": "fresh",
                            "e": "Task6",
                            "t": 0,
                            "p": {
                                "tt": "Fresh",
                                "tp": 0,
                                "ss": 0,
                                "st": 0,
                                "tr": False,
                            },
                        }
                    ],
                    current=1,
                    groups=1,
                    end_size=1,
                    latest_size=1,
                )
            return HistoryPage(
                events=[], current=1, groups=0, end_size=1, latest_size=1
            )

    client = FakeClient()
    library = CloudLibrary(client, cache=cache)  # type: ignore[arg-type]

    library.refresh()

    assert client.starts == [0]
    assert list(library.records) == ["fresh"]


def test_previous_cache_version_replays_task7_from_zero(tmp_path: Path) -> None:
    cache = tmp_path / "state.json"
    cache.write_text(
        json.dumps(
            {
                "version": _CACHE_VERSION - 1,
                "history_id": "hist",
                "loaded_index": 9,
                "server_index": 9,
                "records": [],
                "tags": {},
                "tag_parents": {},
            }
        )
    )

    class FakeClient:
        def __init__(self) -> None:
            self.history_id = ""
            self.server_index = 0
            self.loaded_index = 0
            self.starts: list[int] = []

        def verify(self) -> str:
            self.history_id = "hist"
            return self.history_id

        def items(self, start_index: int) -> HistoryPage:
            self.starts.append(start_index)
            if start_index == 0:
                return HistoryPage(
                    events=[
                        {
                            "uuid": "task7",
                            "e": "Task7",
                            "t": 0,
                            "p": {
                                "tt": "Recovered",
                                "tp": 0,
                                "ss": 0,
                                "st": 0,
                                "tr": False,
                            },
                        }
                    ],
                    current=1,
                    groups=1,
                    end_size=1,
                    latest_size=1,
                )
            return HistoryPage(
                events=[], current=1, groups=0, end_size=1, latest_size=1
            )

    client = FakeClient()
    library = CloudLibrary(client, cache=cache)  # type: ignore[arg-type]

    library.refresh()

    assert client.starts == [0]
    assert list(library.records) == ["task7"]


@pytest.mark.parametrize(
    "bad_field",
    [
        ("recurrence_rule", ["not", "an", "object"]),
        ("recurrence_links", "template-id"),
        ("recurrence_instance_count", -1),
        ("recurrence_instance_count", False),
        ("recurrence_instance_count", 1.5),
        ("recurrence_instance_count", MAX_RECURRENCE_INSTANCE_COUNT + 1),
        ("recurrence_instance_count_known", "false"),
        ("recurrence_paused", "false"),
        ("recurrence_paused", 0),
        ("recurrence_paused", 1),
        ("recurrence_paused", None),
        ("recurrence_paused", []),
        ("recurrence_paused", {}),
        ("recurrence_paused_known", "false"),
    ],
)
def test_malformed_cached_recurrence_is_discarded_before_replay(
    tmp_path: Path, bad_field: tuple[str, object]
) -> None:
    cache = tmp_path / "state.json"
    record = {
        "uuid": "cached",
        "kind": "task",
        "title": "Bad cache",
        "recurrence_rule": {"tp": 0, "fu": 256, "fa": 1},
        "recurrence_links": [],
        "recurrence_instance_count": 0,
        "recurrence_instance_count_known": False,
        "recurrence_paused": False,
        "recurrence_paused_known": False,
    }
    record[bad_field[0]] = bad_field[1]
    cache.write_text(
        json.dumps(
            {
                "version": _CACHE_VERSION,
                "history_id": "hist",
                "loaded_index": 9,
                "server_index": 9,
                "records": [record],
                "tags": {},
                "tag_parents": {},
            }
        )
    )

    class FakeClient:
        def __init__(self) -> None:
            self.history_id = ""
            self.server_index = 0
            self.loaded_index = 0
            self.starts: list[int] = []

        def verify(self) -> str:
            self.history_id = "hist"
            return self.history_id

        def items(self, start_index: int) -> HistoryPage:
            self.starts.append(start_index)
            if start_index == 0:
                return HistoryPage(
                    events=[
                        {
                            "uuid": "fresh",
                            "e": "Task6",
                            "t": 0,
                            "p": {
                                "tt": "Fresh",
                                "tp": 0,
                                "ss": 0,
                                "st": 0,
                                "tr": False,
                            },
                        }
                    ],
                    current=1,
                    groups=1,
                    end_size=1,
                    latest_size=1,
                )
            return HistoryPage(
                events=[], current=1, groups=0, end_size=1, latest_size=1
            )

    client = FakeClient()
    library = CloudLibrary(client, cache=cache)  # type: ignore[arg-type]
    library.refresh()

    assert client.starts == [0]
    assert list(library.records) == ["fresh"]

    journal = MemoryJournal()
    request_id = "0198f0ee-98d4-7bd5-91ba-8e76019b2991"
    result = ThingsV2(
        ThingsWorkspace(
            library,
            journal=journal,
            account_id="owner@example.com",
        )
    ).dispatch(
        "things_update",
        {
            "request_id": request_id,
            "items": [
                {"id": "task:fresh", "set": {"repeat": {"create_next": True}}}
            ],
        },
    )
    assert result.state == "rejected"
    assert journal.get_v2_request("owner@example.com", "2", request_id) is None


def test_create_ix_follows_siblings() -> None:
    library = MemoryLibrary(
        [Record(uuid="a", kind="task", title="First", inbox=True, sort_index=0)]
    )
    index = library.next_index(Write(action="create", uuid="b", title="Second"))
    assert index == 1024


def test_new_area_ix_follows_areas_not_inbox() -> None:
    library = MemoryLibrary(
        [
            Record(uuid="work", kind="area", title="Work", sort_index=0),
            Record(
                uuid="note", kind="task", title="Inbox note", inbox=True, sort_index=50
            ),
        ]
    )
    index = library.next_index(
        Write(action="create", uuid="home", kind="area", title="Home")
    )
    assert index == 1024


def test_new_ids_are_compact_base58() -> None:
    uuid = new_uuid()
    assert 15 <= len(uuid) <= 22
    assert all(char not in uuid for char in "0OIl")


def test_memory_apply_rolls_back_a_failed_batch() -> None:
    library = MemoryLibrary()

    with pytest.raises(ValueError, match="needs a task parent"):
        library.apply(
            [
                Write(action="create", uuid="created", title="Should roll back"),
                Write(
                    action="checklist",
                    uuid="row",
                    title="Invalid",
                    checklist_parent_uuid="missing",
                ),
            ]
        )

    assert library.records == {}


class _CaptureClient:
    def __init__(self) -> None:
        self.history_id = "h"
        self.server_index = 1
        self.loaded_index = 1
        self.committed: list[Envelope] = []
        self.pending: list[dict[str, object]] = []

    def verify(self) -> str:
        return self.history_id

    def items(self, start_index: int) -> HistoryPage:
        if self.pending:
            events = self.pending
            self.pending = []
            self.server_index += 1
            return HistoryPage(
                events=events,
                current=self.server_index,
                groups=1,
                end_size=self.server_index,
                latest_size=self.server_index,
            )
        return HistoryPage(
            events=[],
            current=self.server_index,
            groups=0,
            end_size=self.server_index,
            latest_size=self.server_index,
        )

    def commit(self, envelopes: list[Envelope]) -> None:
        self.committed = envelopes
        self.pending = [
            {"uuid": item.uuid, "e": item.kind, "t": item.action, "p": item.payload}
            for item in envelopes
        ]


def test_cloud_serializes_all_date_only_fields_as_utc_days(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    start = date(2026, 8, 17)
    deadline = date(2026, 8, 24)

    library.apply(
        [
            Write(
                action="create",
                uuid="scheduled",
                title="Scheduled",
                start=start,
                deadline=deadline,
                remind="10:00",
                owner_today=date(2026, 8, 15),
            )
        ]
    )

    payload = client.committed[0].payload
    start_stamp = int(datetime(2026, 8, 17, tzinfo=timezone.utc).timestamp())
    deadline_stamp = int(datetime(2026, 8, 24, tzinfo=timezone.utc).timestamp())
    assert payload["sr"] == start_stamp
    assert payload["tir"] == start_stamp
    assert payload["rmd"] == start_stamp
    assert payload["dd"] == deadline_stamp
    assert payload["ato"] == 36_000


def test_today_clears_evening_and_inbox_clears_start(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records["abc"] = Record(
        uuid="abc",
        kind="task",
        title="Call",
        start=date(2026, 8, 13),
        tonight=True,
        entity="Task6",
    )
    library.apply(
        [Write(action="update", uuid="abc", kind="task", start=date(2026, 8, 13))]
    )
    payload = client.committed[0].payload
    assert payload["sb"] == 0
    library.apply([Write(action="update", uuid="abc", kind="task", inbox=True)])
    inbox = client.committed[0].payload
    assert inbox["st"] == 0
    assert inbox["sr"] is None
    assert inbox["tir"] is None
    assert inbox["agr"] == []


def test_scheduled_creates_keep_their_project_or_area(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records.update(
        {
            "project": Record(
                uuid="project", kind="project", title="Launch", entity="Task6"
            ),
            "area": Record(uuid="area", kind="area", title="Work", entity="Area3"),
        }
    )
    today = date(2026, 8, 15)
    future = date(2026, 8, 20)

    library.apply(
        [
            Write(
                action="create",
                uuid="later",
                title="Later",
                into_uuid="project",
                into_kind="project",
                someday=True,
                owner_today=today,
            ),
            Write(
                action="create",
                uuid="scheduled",
                title="Scheduled",
                into_uuid="area",
                into_kind="area",
                start=future,
                owner_today=today,
            ),
        ]
    )

    envelopes = {item.uuid: item.payload for item in client.committed}
    assert envelopes["later"]["pr"] == ["project"]
    assert envelopes["later"]["st"] == 2
    assert envelopes["scheduled"]["ar"] == ["area"]
    assert envelopes["scheduled"]["st"] == 2
    assert library.records["later"].parent_uuid == "project"
    assert library.records["later"].someday is True
    assert library.records["scheduled"].area_uuid == "area"
    assert library.records["scheduled"].start == future


def test_combined_schedule_and_move_preserves_both_effects(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records.update(
        {
            "area": Record(uuid="area", kind="area", title="Work", entity="Area3"),
            "later": Record(uuid="later", kind="task", title="Later", entity="Task6"),
            "scheduled": Record(
                uuid="scheduled", kind="task", title="Scheduled", entity="Task6"
            ),
        }
    )
    today = date(2026, 8, 15)
    future = date(2026, 8, 20)
    writes = [
        Write(
            action="update",
            uuid="later",
            into_uuid="area",
            into_kind="area",
            someday=True,
            owner_today=today,
        ),
        Write(
            action="update",
            uuid="scheduled",
            into_uuid="area",
            into_kind="area",
            start=future,
            owner_today=today,
        ),
    ]

    library.apply(writes)

    envelopes = {item.uuid: item.payload for item in client.committed}
    assert envelopes["later"]["ar"] == ["area"]
    assert envelopes["later"]["st"] == 2
    assert envelopes["scheduled"]["ar"] == ["area"]
    assert envelopes["scheduled"]["st"] == 2
    assert library.records["later"].area_uuid == "area"
    assert library.records["later"].someday is True
    assert library.records["scheduled"].area_uuid == "area"
    assert library.records["scheduled"].start == future

    memory = MemoryLibrary(
        [
            Record(uuid="area", kind="area", title="Work"),
            Record(uuid="later", kind="task", title="Later"),
            Record(uuid="scheduled", kind="task", title="Scheduled"),
        ]
    )
    memory.apply(writes)
    assert memory.records["later"].area_uuid == "area"
    assert memory.records["later"].someday is True
    assert memory.records["scheduled"].area_uuid == "area"
    assert memory.records["scheduled"].start == future


def test_move_without_schedule_change_keeps_existing_schedule(tmp_path: Path) -> None:
    future = date(2026, 8, 20)
    records = [
        Record(uuid="area", kind="area", title="Work", entity="Area3"),
        Record(uuid="later", kind="task", title="Later", someday=True, entity="Task6"),
        Record(
            uuid="scheduled",
            kind="task",
            title="Scheduled",
            start=future,
            entity="Task6",
        ),
    ]
    writes = [
        Write(action="update", uuid="later", into_uuid="area", into_kind="area"),
        Write(action="update", uuid="scheduled", into_uuid="area", into_kind="area"),
    ]
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records.update({item.uuid: item for item in records})

    library.apply(writes)

    envelopes = {item.uuid: item.payload for item in client.committed}
    assert "st" not in envelopes["later"]
    assert "st" not in envelopes["scheduled"]
    assert library.records["later"].someday is True
    assert library.records["scheduled"].start == future

    memory = MemoryLibrary(
        [
            Record(uuid="area", kind="area", title="Work"),
            Record(uuid="later", kind="task", title="Later", someday=True),
            Record(uuid="scheduled", kind="task", title="Scheduled", start=future),
        ]
    )
    memory.apply(writes)
    assert memory.records["later"].someday is True
    assert memory.records["scheduled"].start == future


def test_inbox_move_to_project_or_area_clears_inbox_list_state(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records.update(
        {
            "project": Record(
                uuid="project", kind="project", title="Launch", entity="Task6"
            ),
            "area": Record(uuid="area", kind="area", title="Work", entity="Area3"),
            "inbox-project": Record(
                uuid="inbox-project",
                kind="task",
                title="File this",
                inbox=True,
                entity="Task6",
            ),
            "inbox-area": Record(
                uuid="inbox-area",
                kind="task",
                title="Park this",
                inbox=True,
                entity="Task6",
            ),
        }
    )

    library.apply(
        [
            Write(
                action="update",
                uuid="inbox-project",
                into_uuid="project",
                into_kind="project",
            ),
            Write(
                action="move",
                uuid="inbox-area",
                into_uuid="area",
                into_kind="area",
            ),
        ]
    )

    envelopes = {item.uuid: item.payload for item in client.committed}
    assert envelopes["inbox-project"]["pr"] == ["project"]
    assert envelopes["inbox-project"]["st"] == 1
    assert envelopes["inbox-area"]["ar"] == ["area"]
    assert envelopes["inbox-area"]["st"] == 1
    assert library.records["inbox-project"].inbox is False
    assert library.records["inbox-area"].inbox is False
    assert library.inbox() == []


def test_inbox_project_move_repairs_masked_inbox_list_state(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records.update(
        {
            "project": Record(
                uuid="project", kind="project", title="Launch", entity="Task6"
            ),
            "stuck": Record(
                uuid="stuck",
                kind="task",
                title="Still in Inbox",
                parent_uuid="project",
                inbox=False,
                entity="Task6",
            ),
        }
    )

    library.apply(
        [
            Write(
                action="update",
                uuid="stuck",
                into_uuid="project",
                into_kind="project",
            )
        ]
    )

    payload = client.committed[0].payload
    assert payload["pr"] == ["project"]
    assert payload["st"] == 1


def test_inbox_project_move_readback_fails_if_cloud_keeps_inbox_state(
    tmp_path: Path,
) -> None:
    class StripListStateClient(_CaptureClient):
        def commit(self, envelopes: list[Envelope]) -> None:
            self.committed = envelopes
            self.pending = [
                {
                    "uuid": item.uuid,
                    "e": item.kind,
                    "t": item.action,
                    "p": {
                        key: value for key, value in item.payload.items() if key != "st"
                    },
                }
                for item in envelopes
            ]

    client = StripListStateClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records.update(
        {
            "project": Record(
                uuid="project", kind="project", title="Launch", entity="Task6"
            ),
            "inbox": Record(
                uuid="inbox",
                kind="task",
                title="File this",
                inbox=True,
                entity="Task6",
            ),
        }
    )

    with pytest.raises(CloudError, match="read-back did not match"):
        library.apply(
            [
                Write(
                    action="update",
                    uuid="inbox",
                    into_uuid="project",
                    into_kind="project",
                )
            ]
        )


def test_clear_start_emits_anytime_state_and_keeps_project(
    tmp_path: Path,
) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records.update(
        {
            "project": Record(
                uuid="project", kind="project", title="Launch", entity="Task6"
            ),
            "later": Record(
                uuid="later",
                kind="task",
                title="Later",
                parent_uuid="project",
                someday=True,
                entity="Task6",
            ),
        }
    )
    write = Write(action="update", uuid="later", clear_start=True)

    assert library.matches([write]) is False
    library.apply([write])

    envelope = client.committed[0].payload
    assert envelope["st"] == 1
    assert envelope["sr"] is None
    assert envelope["rmd"] is None
    assert envelope["sb"] == 0
    assert "pr" not in envelope
    assert library.records["later"].someday is False
    assert library.records["later"].parent_uuid == "project"

    memory = MemoryLibrary(
        [
            Record(uuid="project", kind="project", title="Launch"),
            Record(
                uuid="later",
                kind="task",
                title="Later",
                parent_uuid="project",
                someday=True,
            ),
        ]
    )
    assert memory.matches([write]) is False
    memory.apply([write])
    assert memory.records["later"].someday is False
    assert memory.records["later"].parent_uuid == "project"
    public = ThingsWorkspace(memory).read(ReadCall(id="task:later")).items[0]
    assert public.start is None
    assert "someday" not in public.signals


def test_batch_creates_get_distinct_ix(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records["a"] = Record(
        uuid="a", kind="task", title="First", inbox=True, sort_index=0, entity="Task6"
    )
    library.apply(
        [
            Write(action="create", uuid="b", title="Second"),
            Write(action="create", uuid="c", title="Third"),
        ]
    )
    indexes = [item.payload["ix"] for item in client.committed]
    assert indexes == [1024, 2048]


def test_create_after_legacy_negative_siblings_starts_at_a_positive_ix(
    tmp_path: Path,
) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records["legacy"] = Record(
        uuid="legacy",
        kind="task",
        title="Legacy",
        inbox=True,
        sort_index=-2048,
        entity="Task6",
    )

    library.apply([Write(action="create", uuid="new", title="New")])

    assert client.committed[0].payload["ix"] == 1024


def test_create_without_an_explicit_ix_matches_after_apply(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    write = Write(action="create", uuid="new", title="New")

    library.apply([write])

    assert library.records["new"].sort_index == 1024
    assert library.matches([write]) is True


def test_create_without_an_explicit_ix_does_not_match_a_nonpositive_row(
    tmp_path: Path,
) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records["new"] = Record(
        uuid="new",
        kind="task",
        title="New",
        inbox=True,
        sort_index=0,
        entity="Task6",
    )

    assert library.matches([Write(action="create", uuid="new", title="New")]) is False


def test_task6_envelopes_never_use_a_non_positive_ix(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.apply(
        [
            Write(
                action="create",
                uuid="project",
                kind="project",
                title="Project",
                sort_index=0,
            ),
            Write(
                action="create",
                uuid="task",
                kind="task",
                title="Task",
                sort_index=-1024,
            ),
        ]
    )

    indexes = {item.uuid: item.payload["ix"] for item in client.committed}
    assert indexes == {"project": 1, "task": 1}


def test_area_rename_keeps_stored_entity(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records["work"] = Record(
        uuid="work", kind="area", title="Work", entity="Area2"
    )
    library.apply(
        [Write(action="rename_area", uuid="work", kind="area", title="Office")]
    )
    assert client.committed[0].kind == "Area2"


def test_timeout_scans_later_pages_instead_of_reposting() -> None:
    from things_orchestrator.cloud import CloudError

    client = CloudClient("a@b.c", "pw")
    client.history_id = "h"
    client.server_index = 2
    client.loaded_index = 1
    posts = 0

    def request(
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        body: bytes | None = None,
        retry: bool = True,
    ):
        nonlocal posts
        if method == "POST":
            posts += 1
            if posts == 1:
                raise CloudError("Things Cloud timed out")
            return {"server-head-index": 3}
        start = int((query or {}).get("start-index") or 0)
        if start == 2:
            return {
                "items": [{"x": {"t": 0, "e": "Task6", "p": {"tt": "Hi"}}}],
                "current-item-index": 3,
                "end-total-content-size": 2,
                "latest-total-content-size": 2,
            }
        return {
            "items": [{"x": {"t": 0, "e": "Task6", "p": {"tt": "Old"}}}],
            "current-item-index": 2,
            "end-total-content-size": 1,
            "latest-total-content-size": 2,
        }

    client._request = request  # noqa: SLF001
    client.commit([Envelope("x", 0, "Task6", {"tt": "Hi"})])
    assert posts == 1


def test_timeout_requires_expected_null_keys_to_be_present() -> None:
    client = CloudClient("a@b.c", "pw")
    client.history_id = "h"
    client.server_index = 2
    posts = 0

    def request(
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        body: bytes | None = None,
        retry: bool = True,
    ):
        nonlocal posts
        if method == "POST":
            posts += 1
            raise CloudError("Things Cloud timed out")
        return {
            "items": [{"x": {"t": 0, "e": "Task6", "p": {"tt": "Hi"}}}],
            "current-item-index": 3,
            "end-total-content-size": 1,
            "latest-total-content-size": 1,
        }

    client._request = request  # noqa: SLF001
    with pytest.raises(CloudError, match="outcome is unknown"):
        client.commit([Envelope("x", 0, "Task6", {"tt": "Hi", "sp": None})])
    assert posts == 1


def test_timeout_reconciliation_error_remains_outcome_unknown() -> None:
    client = CloudClient("a@b.c", "pw")
    client.history_id = "h"
    posts = 0

    def request(
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        body: bytes | None = None,
        retry: bool = True,
    ):
        nonlocal posts
        if method == "POST":
            posts += 1
            raise CloudError("Things Cloud timed out")
        raise CloudError("Things Cloud is unreachable")

    client._request = request  # noqa: SLF001
    with pytest.raises(CloudError, match="outcome is unknown"):
        client.commit([Envelope("x", 0, "Task6", {"tt": "Hi"})])
    assert posts == 1


def test_post_commit_pull_failure_is_an_unknown_outcome(tmp_path: Path) -> None:
    class ReadbackFailureClient:
        def __init__(self) -> None:
            self.history_id = "h"
            self.server_index = 0
            self.loaded_index = 0
            self.pulls = 0
            self.posts = 0

        def verify(self) -> str:
            return self.history_id

        def items(self, _start_index: int) -> HistoryPage:
            self.pulls += 1
            if self.pulls == 1:
                return HistoryPage(
                    events=[], current=0, groups=0, end_size=0, latest_size=0
                )
            raise CloudError("Things Cloud is unreachable")

        def commit(self, _envelopes: list[Envelope]) -> None:
            self.posts += 1

    client = ReadbackFailureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]

    with pytest.raises(CloudError, match="outcome is unknown"):
        library.apply([Write(action="create", uuid="task", title="Call")])

    assert client.posts == 1


def test_timeout_partial_overlap_is_unknown_and_does_not_repost() -> None:
    from things_orchestrator.cloud import CloudError

    client = CloudClient("a@b.c", "pw")
    client.history_id = "h"
    client.server_index = 2
    client.loaded_index = 1
    posts = 0

    def request(
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        body: bytes | None = None,
        retry: bool = True,
    ):
        nonlocal posts
        if method == "POST":
            posts += 1
            if posts == 1:
                raise CloudError("Things Cloud timed out")
            return {"server-head-index": 4}
        return {
            "items": [{"x": {"t": 0, "e": "Task6", "p": {"tt": "Hi"}}}],
            "current-item-index": 3,
            "end-total-content-size": 2,
            "latest-total-content-size": 2,
        }

    client._request = request  # noqa: SLF001
    with pytest.raises(CloudError, match="outcome is unknown"):
        client.commit(
            [
                Envelope("x", 0, "Task6", {"tt": "Hi"}),
                Envelope("y", 0, "Task6", {"tt": "Bye"}),
            ]
        )
    assert posts == 1


def test_timeout_ignores_older_event_for_the_same_uuid() -> None:
    from things_orchestrator.cloud import CloudError

    client = CloudClient("a@b.c", "pw")
    client.history_id = "h"
    client.server_index = 2
    client.loaded_index = 1
    posts = 0

    def request(
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        body: bytes | None = None,
        retry: bool = True,
    ):
        nonlocal posts
        if method == "POST":
            posts += 1
            if posts == 1:
                raise CloudError("Things Cloud timed out")
            return {"server-head-index": 4}
        start = int((query or {}).get("start-index") or 0)
        if start < 2:
            return {
                "items": [{"x": {"t": 0, "e": "Task6", "p": {"tt": "Hi"}}}],
                "current-item-index": 2,
                "end-total-content-size": 1,
                "latest-total-content-size": 2,
            }
        return {
            "items": [],
            "current-item-index": 2,
            "end-total-content-size": 2,
            "latest-total-content-size": 2,
        }

    client._request = request  # noqa: SLF001
    with pytest.raises(CloudError, match="outcome is unknown"):
        client.commit([Envelope("x", 0, "Task6", {"tt": "Hi"})])
    assert posts == 1


def test_timeout_never_reposts_when_first_pull_has_no_proof() -> None:
    from things_orchestrator.cloud import CloudError

    client = CloudClient("a@b.c", "pw")
    client.history_id = "h"
    client.server_index = 2
    client.loaded_index = 1
    posts = 0
    gets = 0

    def request(
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        body: bytes | None = None,
        retry: bool = True,
    ):
        nonlocal posts, gets
        if method == "POST":
            posts += 1
            raise CloudError("Things Cloud timed out")
        gets += 1
        if gets == 1:
            return {
                "items": [],
                "current-item-index": 2,
                "end-total-content-size": 1,
                "latest-total-content-size": 1,
            }
        return {
            "items": [{"x": {"t": 0, "e": "Task6", "p": {"tt": "Hi"}}}],
            "current-item-index": 3,
            "end-total-content-size": 2,
            "latest-total-content-size": 2,
        }

    client._request = request  # noqa: SLF001
    with pytest.raises(CloudError, match="outcome is unknown"):
        client.commit([Envelope("x", 0, "Task6", {"tt": "Hi"})])
    assert posts == 1
    assert gets == 1


def test_empty_history_page_does_not_rewind_head(tmp_path: Path) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.history_id = "h"
            self.server_index = 9
            self.loaded_index = 9

        def verify(self) -> str:
            return self.history_id

        def items(self, start_index: int) -> HistoryPage:
            return HistoryPage(
                events=[], current=2, groups=0, end_size=1, latest_size=1
            )

    library = CloudLibrary(FakeClient(), cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records["t"] = Record(uuid="t", kind="task", title="Call")
    library.refresh()
    assert library.client.server_index == 9


def test_stale_history_key_re_verifies() -> None:
    from things_orchestrator.cloud import CloudError

    client = CloudClient("a@b.c", "pw")
    client.history_id = "old"
    client.loaded_index = 4
    paths: list[str] = []

    def request(
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        body: bytes | None = None,
        retry: bool = True,
    ):
        paths.append(path)
        if "account" in path:
            return {"history-key": "new"}
        if "/history/old/" in path:
            raise CloudError("Things Cloud HTTP 404")
        return {
            "items": [],
            "current-item-index": 0,
            "end-total-content-size": 0,
            "latest-total-content-size": 0,
        }

    client._request = request  # noqa: SLF001
    client.items(4)
    assert client.history_id == "new"
    assert client.loaded_index == 0
    assert any("/history/new/" in path for path in paths)


def test_history_404_retries_the_requested_index_when_key_is_unchanged() -> None:
    from things_orchestrator.cloud import CloudError

    client = CloudClient("a@b.c", "pw")
    client.history_id = "h"
    client.loaded_index = 1
    starts: list[int] = []

    def request(
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        body: bytes | None = None,
        retry: bool = True,
    ):
        if "account" in path:
            return {"history-key": "h"}
        start = int((query or {}).get("start-index") or 0)
        starts.append(start)
        if start == 4:
            raise CloudError("Things Cloud HTTP 404")
        return {
            "items": [],
            "current-item-index": 1,
            "end-total-content-size": 1,
            "latest-total-content-size": 1,
        }

    client._request = request  # noqa: SLF001
    try:
        client.items(4)
    except CloudError as error:
        assert "404" in str(error)
    else:
        raise AssertionError("expected CloudError")
    assert starts == [4, 4]


def test_invalid_json_is_cloud_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from things_orchestrator.cloud import CloudError

    class _Resp:
        def read(self) -> bytes:
            return b"not-json"

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

    client = CloudClient("a@b.c", "pw")
    monkeypatch.setattr(
        "things_orchestrator.cloud.urlopen", lambda *args, **kwargs: _Resp()
    )
    try:
        client._request("GET", "/version/1/account/a")  # noqa: SLF001
    except CloudError as error:
        assert "unreadable" in str(error)
    else:
        raise AssertionError("expected CloudError")


def test_apply_pulls_after_commit(tmp_path: Path) -> None:
    import time

    client = _CaptureClient()
    gets = 0
    original = client.items

    def items(start_index: int) -> HistoryPage:
        nonlocal gets
        gets += 1
        return original(start_index)

    client.items = items  # type: ignore[method-assign]
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records["abc"] = Record(
        uuid="abc", kind="task", title="Call", entity="Task6"
    )
    library._synced_at = time.monotonic()  # noqa: SLF001
    library.apply([Write(action="update", uuid="abc", kind="task", title="Call bank")])
    assert gets >= 1
    assert library.records["abc"].title == "Call bank"


def test_empty_incremental_does_not_rewrite_cache(tmp_path: Path) -> None:
    cache = tmp_path / "state.json"

    class FakeClient:
        def __init__(self) -> None:
            self.email = "a@b.c"
            self.history_id = "hist"
            self.server_index = 2
            self.loaded_index = 1

        def verify(self) -> str:
            return self.history_id

        def items(self, start_index: int) -> HistoryPage:
            return HistoryPage(
                events=[], current=2, groups=0, end_size=1, latest_size=1
            )

    library = CloudLibrary(FakeClient(), cache=cache)  # type: ignore[arg-type]
    library.records["t1"] = Record(uuid="t1", kind="task", title="Old")
    library._save_cache()  # noqa: SLF001
    saves = 0
    original = library._save_cache

    def save() -> None:
        nonlocal saves
        saves += 1
        original()

    library._save_cache = save  # type: ignore[method-assign]
    library.refresh()
    assert saves == 0
    assert cache.stat().st_mode & 0o777 == 0o600


def test_rich_notes_join_paragraphs() -> None:
    from things_orchestrator.cloud import _note_text

    text = _note_text({"t": 2, "ps": [{"r": "one"}, {"r": "two"}]})
    assert text == "one\ntwo"


def test_fold_create_replaces_task_fields() -> None:
    library = MemoryLibrary()
    fold_events(
        [
            {
                "uuid": "t1",
                "e": "Task6",
                "t": 0,
                "p": {"tt": "Old", "tp": 0, "ss": 0, "st": 0, "tr": False, "tg": ["w"]},
            }
        ],
        library=library,
    )
    assert library.records["t1"].tag_uuids == ["w"]
    fold_events(
        [
            {
                "uuid": "t1",
                "e": "Task6",
                "t": 0,
                "p": {"tt": "New", "tp": 0, "ss": 0, "st": 0, "tr": False},
            }
        ],
        library=library,
    )
    assert library.records["t1"].title == "New"
    assert library.records["t1"].tag_uuids == []


def test_apply_stops_after_conflict_and_requires_fresh_facts(tmp_path: Path) -> None:
    from things_orchestrator.cloud import CloudError

    class ConflictClient:
        def __init__(self) -> None:
            self.history_id = "h"
            self.server_index = 1
            self.loaded_index = 1
            self.posts = 0
            self.pending: list[dict[str, object]] = []

        def verify(self) -> str:
            return self.history_id

        def items(self, start_index: int) -> HistoryPage:
            if self.pending:
                events = self.pending
                self.pending = []
                return HistoryPage(
                    events=events, current=2, groups=1, end_size=2, latest_size=2
                )
            return HistoryPage(
                events=[], current=1, groups=0, end_size=1, latest_size=1
            )

        def commit(self, envelopes: list[Envelope]) -> None:
            self.posts += 1
            if self.posts == 1:
                raise CloudError("Things Cloud HTTP 409")
            self.pending = [
                {"uuid": item.uuid, "e": item.kind, "t": item.action, "p": item.payload}
                for item in envelopes
            ]

    client = ConflictClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records["abc"] = Record(
        uuid="abc", kind="task", title="Call", entity="Task6"
    )
    with pytest.raises(CloudError, match="read fresh facts"):
        library.apply(
            [Write(action="update", uuid="abc", kind="task", title="Call bank")]
        )
    assert client.posts == 1
    assert library.records["abc"].title == "Call"


def test_commit_retries_404_after_verify() -> None:
    from things_orchestrator.cloud import CloudError

    client = CloudClient("a@b.c", "pw")
    client.history_id = "h"
    client.server_index = 2
    posts = 0

    def request(
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        body: bytes | None = None,
        retry: bool = True,
    ):
        nonlocal posts
        if "account" in path:
            return {"history-key": "h"}
        if method == "POST":
            posts += 1
            if posts == 1:
                raise CloudError("Things Cloud HTTP 404")
            return {"server-head-index": 3}
        raise AssertionError("unexpected GET")

    client._request = request  # noqa: SLF001
    client.commit([Envelope("x", 0, "Task6", {"tt": "Hi"})])
    assert posts == 2
    assert client.server_index == 3


def test_commit_404_does_not_replay_after_history_key_change() -> None:
    from things_orchestrator.cloud import CloudError

    client = CloudClient("a@b.c", "pw")
    client.history_id = "old"
    posts = 0

    def request(
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        body: bytes | None = None,
        retry: bool = True,
    ):
        nonlocal posts
        if "account" in path:
            return {"history-key": "new"}
        if method == "POST":
            posts += 1
            raise CloudError("Things Cloud HTTP 404")
        raise AssertionError("unexpected GET")

    client._request = request  # noqa: SLF001
    with pytest.raises(CloudError, match="HTTP 404"):
        client.commit([Envelope("x", 0, "Task6", {"tt": "Hi"})])
    assert posts == 1
    assert client.history_id == "new"


def test_empty_library_still_debounces(tmp_path: Path) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.email = "a@b.c"
            self.history_id = "hist"
            self.server_index = 0
            self.loaded_index = 0
            self.pages = 0

        def verify(self) -> str:
            return self.history_id

        def items(self, start_index: int) -> HistoryPage:
            self.pages += 1
            return HistoryPage(
                events=[], current=0, groups=0, end_size=0, latest_size=0
            )

    library = CloudLibrary(FakeClient(), cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.refresh()
    library.refresh()
    assert library.client.pages == 1  # type: ignore[attr-defined]
    library.refresh(force=True)
    assert library.client.pages == 2  # type: ignore[attr-defined]


def test_new_waiting_task_is_one_complete_create(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]

    result = library.apply(
        [
            Write(action="ensure_tag", uuid="waiting", title="Waiting"),
            Write(
                action="create",
                uuid="task",
                title="Wait for refund",
                notes="## Next\nEmail support",
            ),
            Write(action="tags", uuid="task", tag_uuids=["waiting"]),
        ]
    )

    assert [item.uuid for item in client.committed] == ["waiting", "task"]
    task = client.committed[1]
    assert task.action == 0
    assert task.payload["tt"] == "Wait for refund"
    assert task.payload["tg"] == ["waiting"]
    assert task.payload["nt"]["v"] == "## Next\nEmail support"
    assert result.verified == ["Wait for refund"]
    assert result.read_back_verified is True
    assert library.records["task"].tag_uuids == ["waiting"]


def test_existing_update_and_waiting_tag_share_one_envelope(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records["task"] = Record(
        uuid="task", kind="task", title="Refund", entity="Task6"
    )

    library.apply(
        [
            Write(action="update", uuid="task", title="Wait for refund"),
            Write(action="tags", uuid="task", tag_uuids=["waiting"]),
        ]
    )

    assert len(client.committed) == 1
    assert client.committed[0].payload["tt"] == "Wait for refund"
    assert client.committed[0].payload["tg"] == ["waiting"]


def test_cloud_replaces_a_deleted_canonical_waiting_tag(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    task = Record(
        uuid="refund",
        kind="task",
        title="Refund",
        tag_uuids=["waiting"],
        entity="Task6",
    )
    library.records[task.uuid] = task
    library.tags["waiting"] = "Waiting"
    module = ThingsWorkspace(library, journal=MemoryJournal())
    current = module.read(ReadCall(ids=[task.id])).items[0]
    tags_revision = module.read(ReadCall(view="tags")).scope_revision

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "cloud-replace-waiting-001",
                "tags_revision": tags_revision,
                "change_tags": [
                    {"id": "tag:waiting", "delete_permanently": True}
                ],
                "change": [
                    {
                        "id": task.id,
                        "if_revision": current.revision,
                        "waiting": True,
                    }
                ],
            }
        )
    )
    assert prepared.status == "needs_approval"
    assert prepared.plan is not None

    settled = module.approve(ApproveCall(plan_id=prepared.plan.id))

    assert settled.status == "applied"
    replacement_uuid = next(iter(library.tags))
    assert replacement_uuid != "waiting"
    assert library.tags[replacement_uuid] == "Waiting"
    assert task.tag_uuids == [replacement_uuid]
    replacement = next(
        envelope
        for envelope in client.committed
        if envelope.kind == "Tag4" and envelope.action == 0
    )
    assert replacement.uuid == replacement_uuid


def test_trash_is_a_recoverable_task_patch(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records["task"] = Record(
        uuid="task", kind="task", title="Remove me", entity="Task6"
    )

    library.apply([Write(action="trash", uuid="task")])

    assert client.committed[0].action == 1
    assert client.committed[0].kind == "Task7"
    assert client.committed[0].payload["tr"] is True
    assert library.records["task"].trashed is True


def test_repeat_template_and_generated_copy_create_in_one_cloud_commit(
    tmp_path: Path,
) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    rule = {
        "tp": 0,
        "fu": 256,
        "fa": 2,
        "of": [{"wd": 0}, {"wd": 4}],
        "sr": 1_775_232_000,
        "ia": 1_775_232_000,
        "ed": 64_092_211_200,
        "rc": 0,
        "ts": 0,
        "rrv": 4,
    }

    library.apply(
        [
            Write(
                action="create",
                uuid="template-new",
                title="Routine",
                recurrence_rule=rule,
                recurrence_created_through=date(2026, 4, 3),
            ),
            Write(
                action="create",
                uuid="instance-new",
                title="Routine",
                recurrence_links=["template-new"],
            ),
        ]
    )

    assert len(client.committed) == 2
    template = next(item for item in client.committed if item.uuid == "template-new")
    instance = next(item for item in client.committed if item.uuid == "instance-new")
    assert template.payload["rr"] == rule
    assert template.payload["rt"] == []
    assert template.payload["icsd"] == day_ts(date(2026, 4, 3))
    assert template.payload["md"] is None
    assert instance.payload["rr"] is None
    assert instance.payload["rt"] == ["template-new"]
    assert instance.payload["lt"] is False
    assert library.records["template-new"].recurrence.role == "template"
    assert library.records["instance-new"].recurrence.template_uuid == "template-new"


def test_repeat_link_can_be_cleared_before_template_delete(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records.update(
        {
            "template": Record(
                uuid="template",
                kind="task",
                title="Routine",
                entity="Task6",
                recurrence=RecurrenceState(
                    role="template",
                    repeat_type="fixed",
                    rule={"tp": 0, "fu": 8, "fa": 1},
                ),
            ),
            "instance": Record(
                uuid="instance",
                kind="task",
                title="Routine",
                entity="Task6",
                recurrence=RecurrenceState(
                    role="instance",
                    repeat_type="fixed",
                    template_uuid="template",
                    links=("template",),
                ),
            ),
        }
    )

    library.apply(
        [
            Write(action="repeat_link", uuid="instance", recurrence_links=[]),
            Write(action="permanent_delete", uuid="template"),
        ]
    )

    assert {item.uuid for item in client.committed} == {"instance", "template"}
    link = next(item for item in client.committed if item.uuid == "instance")
    assert link.payload["rt"] == []
    assert library.records["instance"].recurrence.role == "none"
    assert "template" not in library.records


def test_project_stop_emits_native_ordinary_graph_and_template_deletes(
    tmp_path: Path,
) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records.update(
        {
            "template-project": Record(
                uuid="template-project",
                kind="project",
                title="Release train",
                entity="Task7",
                recurrence=RecurrenceState(
                    role="template",
                    repeat_type="fixed",
                    rule={"tp": 0, "fu": 256, "fa": 1},
                ),
            ),
            "template-heading": Record(
                uuid="template-heading",
                kind="task",
                title="Ship",
                entity="Task7",
                heading=True,
                parent_uuid="template-project",
            ),
            "template-task": Record(
                uuid="template-task",
                kind="task",
                title="Deploy",
                status="done",
                entity="Task7",
                heading_uuid="template-heading",
                checklists=[
                    ChecklistLine(
                        uuid="template-check", title="Verify", status="done"
                    )
                ],
            ),
            "current-project": Record(
                uuid="current-project",
                kind="project",
                title="Release train",
                entity="Task7",
                recurrence=RecurrenceState(
                    role="instance",
                    repeat_type="fixed",
                    template_uuid="template-project",
                    links=("template-project",),
                ),
            ),
        }
    )
    library.records["template-project"].recurrence_next_on = date(2026, 9, 6)
    factor = tmp_path / "owner-factor.json"
    enroll_owner_factor("correct horse battery staple", path=factor)
    journal = MemoryJournal(
        owner_public_key=factor.with_name("owner-public-key.ed25519").read_bytes()
    )
    workspace = ThingsWorkspace(
        library,
        journal=journal,
        clock=lambda: datetime(2026, 8, 30, 12, tzinfo=timezone.utc),
        account_id="owner@example.com",
    )
    staged = ThingsV2(workspace).dispatch(
        "things_update",
        {
            "request_id": "0198f0ee-98d4-7bd5-91ba-8e76019b2901",
            "items": [
                {
                    "id": "project:current-project",
                    "set": {"repeat": {"remove": True}},
                }
            ],
        },
    )
    operation = journal.get_v2_operation(staged.operation_id or "")
    assert staged.state == "awaiting_owner" and operation is not None
    authorization = verified_authorization(
        operation,
        action="approve",
        passphrase="correct horse battery staple",
        path=factor,
    )
    assert authorization is not None

    result = workspace.host_approve_v2(operation.operation_id, authorization)

    assert result["state"] == "applied"

    by_uuid = {row.uuid: row for row in client.committed}
    assert by_uuid["current-project"].payload["rt"] == []
    assert set(by_uuid["current-project"].payload) == {"rt", "md"}
    root = next(
        row
        for row in client.committed
        if row.action == 0 and row.payload.get("tp") == 1
    )
    assert root.action == 0 and root.kind == "Task7"
    assert root.payload["tp"] == 1
    assert root.payload["st"] == 2
    assert root.payload["sr"] == root.payload["tir"] == day_ts(date(2026, 9, 6))
    assert root.payload["rr"] is None
    assert root.payload["rt"] == []
    assert root.payload["rp"] is None
    assert root.payload["lt"] is True
    heading = next(
        row
        for row in client.committed
        if row.action == 0 and row.payload.get("tp") == 2
    )
    assert heading.payload["tp"] == 2
    assert heading.payload["pr"] == [root.uuid]
    assert heading.payload["lt"] is True
    task = next(
        row
        for row in client.committed
        if row.action == 0
        and row.payload.get("tp") == 0
        and row.payload.get("tt") == "Deploy"
    )
    assert task.payload["pr"] == []
    assert task.payload["agr"] == [heading.uuid]
    assert task.payload["lt"] is True
    assert task.payload["ss"] == 0
    checklist = next(
        row
        for row in client.committed
        if row.kind == "ChecklistItem3" and row.action == 0
    )
    assert checklist.kind == "ChecklistItem3"
    assert checklist.payload["ts"] == [task.uuid]
    assert checklist.payload["ss"] == 0
    checklist_delete = by_uuid["template-check"]
    assert checklist_delete.kind == "ChecklistItem3"
    assert checklist_delete.action == 2
    assert checklist_delete.payload == {}
    committed_order = [row.uuid for row in client.committed]
    assert committed_order.index("template-check") < committed_order.index(
        "template-task"
    )
    assert committed_order.index("template-task") < committed_order.index(
        "template-heading"
    )
    assert committed_order.index("template-heading") < committed_order.index(
        "template-project"
    )
    assert all(
        by_uuid[item_uuid].action == 2 and by_uuid[item_uuid].payload == {}
        for item_uuid in ("template-task", "template-heading", "template-project")
    )
    assert library.records["current-project"].recurrence == RecurrenceState()
    assert library.records[root.uuid].recurrence == RecurrenceState()
    assert library.records[root.uuid].leavable is True
    assert not {
        "template-task",
        "template-heading",
        "template-project",
    }.intersection(library.records)


def test_project_create_next_emits_native_count_and_leavable_copy(
    tmp_path: Path,
) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records["template-project"] = Record(
        uuid="template-project",
        kind="project",
        title="Release train",
        recurrence=RecurrenceState(
            role="template",
            repeat_type="after_completion",
            rule={"tp": 1, "fu": 256, "fa": 1, "of": []},
        ),
        recurrence_instance_count=1,
    )

    library.apply(
        [
            Write(
                action="create",
                uuid="next-project",
                kind="project",
                title="Release train",
                start=date(2026, 8, 30),
                recurrence_links=["template-project"],
                leavable=True,
            ),
            Write(
                action="create_heading",
                uuid="next-heading",
                kind="task",
                title="Ship",
                into_uuid="next-project",
                into_kind="project",
                leavable=True,
            ),
            Write(
                action="create",
                uuid="next-task",
                kind="task",
                title="Deploy",
                heading_uuid="next-heading",
                leavable=True,
            ),
            Write(
                action="checklist",
                uuid="next-check",
                title="Verify",
                checklist_parent_uuid="next-task",
            ),
            Write(
                action="repeat_next",
                uuid="template-project",
                kind="project",
                recurrence_instance_count=2,
            ),
        ]
    )

    current = next(row for row in client.committed if row.uuid == "next-project")
    advance = next(
        row for row in client.committed if row.uuid == "template-project"
    )
    heading = next(row for row in client.committed if row.uuid == "next-heading")
    task = next(row for row in client.committed if row.uuid == "next-task")
    checklist = next(row for row in client.committed if row.uuid == "next-check")
    assert current.payload["tp"] == 1
    assert current.payload["rt"] == ["template-project"]
    assert current.payload["lt"] is True
    assert heading.payload["tp"] == 2
    assert heading.payload["pr"] == ["next-project"]
    assert task.payload["pr"] == []
    assert task.payload["agr"] == ["next-heading"]
    assert task.payload["lt"] is True
    assert checklist.payload["ts"] == ["next-task"]
    assert advance.payload == {"icc": 2}
    assert library.records["template-project"].recurrence_instance_count == 2
    assert library.records["next-project"].leavable is True


def test_cloud_repeat_next_matches_applied_post_state(tmp_path: Path) -> None:
    library = CloudLibrary(  # type: ignore[arg-type]
        _CaptureClient(), cache=tmp_path / "state.json"
    )
    library.records["template"] = Record(
        uuid="template",
        kind="task",
        title="Routine",
        recurrence=RecurrenceState(
            role="template",
            repeat_type="fixed",
            rule={"tp": 0, "fu": 256, "fa": 1, "of": []},
        ),
        recurrence_instance_count=2,
    )

    assert library.matches(
        [
            Write(
                action="repeat_next",
                uuid="template",
                recurrence_instance_count=2,
            )
        ]
    )


def test_v2_create_next_reconciles_after_reschedule_cache_restart_and_native_advance(
    tmp_path: Path,
) -> None:
    original = date(2026, 9, 6)
    advanced = date(2026, 9, 13)
    rescheduled = date(2026, 9, 20)
    rule = {
        "tp": 0,
        "fu": 256,
        "fa": 1,
        "of": [{"wd": 1}],
        "sr": day_ts(date(2026, 8, 30)),
    }

    class ReplayClient:
        def __init__(self, initial: list[dict[str, object]] | None = None) -> None:
            self.email = "owner@example.com"
            self.history_id = ""
            self.server_index = 0
            self.loaded_index = 0
            self.batches = [initial] if initial else []
            self.commits: list[list[Envelope]] = []

        def verify(self) -> str:
            self.history_id = "hist"
            return self.history_id

        def items(self, start_index: int) -> HistoryPage:
            if self.batches:
                events = self.batches.pop(0)
                self.server_index += 1
                return HistoryPage(
                    events=events,
                    current=self.server_index,
                    groups=1,
                    end_size=self.server_index,
                    latest_size=self.server_index,
                )
            return HistoryPage(
                events=[],
                current=self.server_index,
                groups=0,
                end_size=self.server_index,
                latest_size=self.server_index,
            )

        def commit(self, envelopes: list[Envelope]) -> None:
            self.commits.append(list(envelopes))
            self.batches.append(
                [
                    {
                        "uuid": envelope.uuid,
                        "e": envelope.kind,
                        "t": envelope.action,
                        "p": envelope.payload,
                    }
                    for envelope in envelopes
                ]
            )

    initial = [
        {
            "uuid": "template",
            "e": "Task7",
            "t": 0,
            "p": {
                "tt": "Weekly",
                "tp": 0,
                "rr": rule,
                "rt": [],
                "tir": day_ts(original),
                "icc": 1,
                "st": 2,
            },
        },
        {
            "uuid": "generated",
            "e": "Task7",
            "t": 0,
            "p": {
                "tt": "Weekly",
                "tp": 0,
                "sr": day_ts(original),
                "rt": ["template"],
                "lt": True,
                "st": 2,
            },
        },
        {
            "uuid": "generated",
            "e": "Task7",
            "t": 1,
            "p": {
                "sr": day_ts(rescheduled),
                "tir": day_ts(rescheduled),
            },
        },
    ]
    cache = tmp_path / "state.json"
    first = CloudLibrary(ReplayClient(initial), cache=cache)  # type: ignore[arg-type]
    first.refresh()

    client = ReplayClient()
    library = CloudLibrary(client, cache=cache)  # type: ignore[arg-type]
    library.refresh()

    generated = library.records["generated"]
    assert generated.start == rescheduled
    assert generated.recurrence_generated_on == original

    interface = ThingsV2(
        ThingsWorkspace(
            library,
            journal=MemoryJournal(),
            clock=lambda: datetime(2026, 8, 30, 12, tzinfo=timezone.utc),
            account_id="owner@example.com",
        )
    )
    stale = interface.dispatch(
        "things_update",
        {
            "request_id": "0198f0ee-98d4-7bd5-91ba-8e76019b2901",
            "items": [
                {
                    "id": generated.id,
                    "set": {"repeat": {"create_next": True}},
                }
            ],
        },
    )
    assert stale.state == "rejected"
    assert stale.code == "validation_error"
    assert stale.next_action == "read_fresh"
    assert client.commits == []

    client.batches.append(
        [
            {
                "uuid": "template",
                "e": "Task7",
                "t": 1,
                "p": {"tir": day_ts(advanced)},
            }
        ]
    )
    library.refresh(force=True)

    result = interface.dispatch(
        "things_update",
        {
            "request_id": "0198f0ee-98d4-7bd5-91ba-8e76019b2902",
            "items": [
                {
                    "id": generated.id,
                    "set": {"repeat": {"create_next": True}},
                }
            ],
        },
    )
    assert result.state == "applied"

    origins = sorted(
        (item.start, item.recurrence_generated_on)
        for item in library.recurrence_instances("template")
    )
    assert origins == [(advanced, advanced), (rescheduled, original)]
    assert library.records["template"].recurrence_instance_count == 2
    assert len(client.commits) == 1

    receipt = interface.dispatch(
        "things_receipt", {"operation_id": result.operation_id}
    )
    assert receipt.state == "applied"
    assert [row["result"] for row in receipt.rows] == ["applied", "applied"]


def test_existing_task_repeat_link_preserves_leavable_flag_and_reads_back(
    tmp_path: Path,
) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records.update(
        {
            "template": Record(
                uuid="template",
                kind="task",
                title="Routine",
                entity="Task6",
                recurrence=RecurrenceState(
                    role="template",
                    repeat_type="fixed",
                    rule={"tp": 0, "fu": 8, "fa": 1},
                ),
            ),
            "existing": Record(
                uuid="existing", kind="task", title="Routine", entity="Task6"
            ),
        }
    )

    library.apply(
        [
            Write(
                action="repeat_link",
                uuid="existing",
                recurrence_links=["template"],
            )
        ]
    )

    assert client.committed[0].payload["rt"] == ["template"]
    assert "lt" not in client.committed[0].payload
    assert library.records["existing"].recurrence.role == "instance"
    assert library.records["existing"].recurrence.template_uuid == "template"


def test_after_completion_progress_updates_template_dates(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records["template"] = Record(
        uuid="template",
        kind="task",
        title="Routine",
        entity="Task7",
        recurrence=RecurrenceState(
            role="template",
            repeat_type="after_completion",
            rule={"tp": 1, "fu": 256, "fa": 1},
        ),
    )

    library.apply(
        [
            Write(
                action="repeat_progress",
                uuid="template",
                recurrence_completed_on=date(2026, 8, 30),
                recurrence_next_on=date(2026, 9, 6),
            )
        ]
    )

    assert client.committed[0].payload["acrd"] == day_ts(date(2026, 8, 30))
    assert client.committed[0].payload["tir"] == day_ts(date(2026, 9, 6))
    assert library.records["template"].recurrence_completed_on == date(2026, 8, 30)
    assert library.records["template"].recurrence_next_on == date(2026, 9, 6)


@pytest.mark.parametrize(
    ("fields", "expected_inbox", "expected_someday"),
    [
        ({"into": "anytime"}, False, False),
        ({"into": "inbox"}, True, False),
        ({"start": "someday"}, False, True),
        ({"start": None}, False, False),
    ],
)
def test_cloud_repeat_conversion_reads_back_final_schedule_semantics(
    tmp_path: Path,
    fields: dict[str, object],
    expected_inbox: bool,
    expected_someday: bool,
) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    task = Record(
        uuid="cloud-scheduled-repeat",
        kind="task",
        title="Routine",
        start=date(2026, 8, 15),
        remind="09:30",
        entity="Task6",
    )
    library.records[task.uuid] = task
    module = ThingsWorkspace(
        library,
        journal=MemoryJournal(),
        clock=lambda: datetime(2026, 8, 15, 12, tzinfo=timezone.utc),
    )
    current = module.read(ReadCall(id=task.id)).items[0]
    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": f"cloud-repeat-schedule-{next(iter(fields))}-001",
                "change": [
                    {
                        "id": task.id,
                        "if_revision": current.revision,
                        "repeat": {"unit": "week"},
                        **fields,
                    }
                ],
            }
        )
    )

    assert prepared.status == "needs_approval"
    assert prepared.plan is not None
    applied = module.approve(ApproveCall(plan_id=prepared.plan.id))
    assert applied.status == "applied"
    template = next(
        item for item in library.records.values() if item.recurrence.role == "template"
    )
    assert task.start is None
    assert task.remind is None
    assert task.inbox is expected_inbox
    assert task.someday is expected_someday
    assert template.start is None
    assert template.remind is None
    assert template.inbox is False
    assert template.someday is True


@pytest.mark.parametrize("replacement", [False, True])
def test_cloud_repeat_conversion_projects_heading_and_chained_checklist_order(
    tmp_path: Path, replacement: bool
) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    old_project = Record(
        uuid="cloud-old-project", kind="project", title="Old", entity="Task6"
    )
    new_project = Record(
        uuid="cloud-new-project", kind="project", title="New", entity="Task6"
    )
    old_heading = Record(
        uuid="cloud-old-heading",
        kind="task",
        title="Old section",
        parent_uuid=old_project.uuid,
        heading=True,
        entity="Task6",
    )
    new_heading = Record(
        uuid="cloud-new-heading",
        kind="task",
        title="New section",
        parent_uuid=new_project.uuid,
        heading=True,
        entity="Task6",
    )
    task = Record(
        uuid="cloud-heading-repeat",
        kind="task",
        title="Routine",
        parent_uuid=old_project.uuid,
        heading_uuid=old_heading.uuid,
        entity="Task6",
        checklists=[
            ChecklistLine("cloud-after-a", "A", sort_index=0),
            ChecklistLine("cloud-after-b", "B", sort_index=1024),
            ChecklistLine("cloud-after-c", "C", sort_index=2048),
        ],
    )
    library.records.update(
        {
            item.uuid: item
            for item in [old_project, new_project, old_heading, new_heading, task]
        }
    )
    module = ThingsWorkspace(library, journal=MemoryJournal())
    current = module.read(ReadCall(id=task.id)).items[0]
    change: dict[str, object] = {
        "id": task.id,
        "if_revision": current.revision,
        "repeat": {"unit": "week"},
        "into": new_project.id,
        "checklist_change": [
            {"id": "check:cloud-after-b", "after": "check:cloud-after-c"},
            {"id": "check:cloud-after-a", "after": "check:cloud-after-b"},
        ],
    }
    if replacement:
        change["heading_id"] = new_heading.id
    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": f"cloud-repeat-heading-{replacement}-001",
                "change": [change],
            }
        )
    )

    assert prepared.status == "needs_approval"
    assert prepared.plan is not None
    assert module.approve(ApproveCall(plan_id=prepared.plan.id)).status == "applied"
    template = next(
        item for item in library.records.values() if item.recurrence.role == "template"
    )
    expected_heading = new_heading.uuid if replacement else None
    for record in (task, template):
        assert record.parent_uuid == new_project.uuid
        assert record.heading_uuid == expected_heading
        assert [row.title for row in record.checklists] == ["C", "B", "A"]


def test_memory_lifecycle_and_tag_admin_actions_are_reversible_until_delete() -> None:
    library = MemoryLibrary(
        [
            Record(uuid="heading", kind="task", title="Next", heading=True),
            Record(
                uuid="task",
                kind="task",
                title="Call",
                trashed=True,
                heading_uuid="heading",
                tag_uuids=["old"],
            ),
        ]
    )
    library.tags.update({"old": "Old", "parent": "Parent"})
    library.tag_parents["old"] = []

    library.apply(
        [
            Write(action="restore", uuid="task"),
            Write(action="rename_tag", uuid="old", title="New"),
            Write(action="reparent_tag", uuid="old", tag_parent_uuids=["parent"]),
        ]
    )

    assert library.records["task"].trashed is False
    assert library.tags["old"] == "New"
    assert library.tag_parents["old"] == ["parent"]

    library.apply([Write(action="permanent_delete", uuid="heading")])
    library.apply([Write(action="delete_tag", uuid="old")])
    assert "heading" not in library.records
    assert library.records["task"].heading_uuid is None
    assert library.records["task"].tag_uuids == []
    assert "old" not in library.tags


def test_cloud_lifecycle_and_tag_admin_actions_batch_and_read_back(
    tmp_path: Path,
) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records.update(
        {
            "task": Record(
                uuid="task",
                kind="task",
                title="Call",
                trashed=True,
                entity="Task6",
            ),
            "heading": Record(
                uuid="heading",
                kind="task",
                title="Next",
                heading=True,
                entity="Task6",
            ),
            "child": Record(
                uuid="child",
                kind="task",
                title="Ship",
                heading_uuid="heading",
                entity="Task6",
            ),
        }
    )
    library.tags.update({"old": "Old", "parent": "Parent"})
    library.tag_parents["old"] = []

    result = library.apply(
        [
            Write(action="restore", uuid="task"),
            Write(action="rename_tag", uuid="old", title="New"),
            Write(action="reparent_tag", uuid="old", tag_parent_uuids=["parent"]),
            Write(action="permanent_delete", uuid="heading", title="Next"),
        ]
    )

    assert len(client.committed) == 3
    restore = next(item for item in client.committed if item.uuid == "task")
    assert restore.action == 1
    assert restore.kind == "Task7"
    assert restore.payload["tr"] is False
    assert set(restore.payload) == {"tr", "md"}
    tag = next(item for item in client.committed if item.uuid == "old")
    assert tag.action == 1
    assert tag.kind == "Tag4"
    assert tag.payload["tt"] == "New"
    assert tag.payload["pn"] == ["parent"]
    assert set(tag.payload) == {"tt", "pn", "md"}
    heading = next(item for item in client.committed if item.uuid == "heading")
    assert heading.action == 2
    assert heading.kind == "Task7"
    assert heading.payload == {}
    assert result.verified == ["Call", "New", "Next"]
    assert library.records["task"].trashed is False
    assert library.tags["old"] == "New"
    assert library.tag_parents["old"] == ["parent"]
    assert "heading" not in library.records
    assert library.records["child"].heading_uuid is None

    library.apply([Write(action="delete_tag", uuid="old", title="New")])
    assert client.committed[0].action == 2
    assert client.committed[0].kind == "Tag4"
    assert client.committed[0].payload == {}
    assert "old" not in library.tags


def test_cloud_area_merge_keeps_tag_cleanup_in_the_task_envelope(
    tmp_path: Path,
) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    old = Record(uuid="old", kind="area", title="Old", entity="Area3")
    new = Record(uuid="new", kind="area", title="New", entity="Area3")
    task = Record(
        uuid="tagged-area-child",
        kind="task",
        title="Keep",
        area_uuid=old.uuid,
        tag_uuids=["focus"],
        entity="Task6",
    )
    library.records.update({item.uuid: item for item in [old, new, task]})
    library.tags["focus"] = "Focus"
    module = ThingsWorkspace(library, journal=MemoryJournal())
    system = module.read(ReadCall(view="system"))
    tags_revision = module.read(ReadCall(view="tags")).scope_revision
    old_fact = module.read(ReadCall(ids=[old.id])).items[0]

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "cloud-area-merge-delete-tag-001",
                "scope_revision": system.scope_revision,
                "tags_revision": tags_revision,
                "change_tags": [
                    {"id": "tag:focus", "delete_permanently": True}
                ],
                "change": [
                    {
                        "id": old.id,
                        "if_revision": old_fact.revision,
                        "move_contents_to": new.id,
                    }
                ],
            }
        )
    )
    assert prepared.status == "needs_approval"
    assert prepared.plan is not None

    settled = module.approve(ApproveCall(plan_id=prepared.plan.id))

    assert settled.status == "applied"
    task_envelope = next(item for item in client.committed if item.uuid == task.uuid)
    assert task_envelope.payload["ar"] == [new.uuid]
    assert task_envelope.payload["tg"] == []


def test_ensure_tag_preserves_parent_aliases_for_memory_and_cloud(
    tmp_path: Path,
) -> None:
    memory = MemoryLibrary()
    memory.tags["parent"] = "Parent"
    memory.apply(
        [
            Write(action="ensure_tag", uuid="$parent", title="Parent"),
            Write(
                action="ensure_tag",
                uuid="child",
                title="Child",
                tag_parent_uuids=["$parent"],
            ),
        ]
    )
    assert memory.tag_parents["child"] == ["parent"]

    client = _CaptureClient()
    cloud = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    cloud.tags["parent"] = "Parent"
    cloud.apply(
        [
            Write(action="ensure_tag", uuid="$parent", title="Parent"),
            Write(
                action="ensure_tag",
                uuid="child",
                title="Child",
                tag_parent_uuids=["$parent"],
            ),
        ]
    )
    child = next(item for item in client.committed if item.uuid == "child")
    assert child.kind == "Tag4"
    assert child.payload["pn"] == ["parent"]
    assert cloud.tag_parents["child"] == ["parent"]


def test_compact_project_headings_round_trip_through_cloud(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    module = ThingsWorkspace(library, journal=MemoryJournal())

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "cloud-compact-headings-001",
                "create": [
                    {
                        "kind": "project",
                        "title": "Ship release",
                        "tasks": [
                            {
                                "title": "Check notes",
                                "checklist": ["Read changes", "Check links"],
                                "heading_title": "Prepare",
                            },
                            {"title": "Build package", "heading_title": "Prepare"},
                            {"title": "Publish package", "heading_title": "Release"},
                        ],
                    }
                ],
            }
        )
    )

    assert result.status == "applied"
    envelopes = {item.payload["tt"]: item for item in client.committed}
    assert envelopes["Ship release"].payload["tp"] == 1
    assert envelopes["Prepare"].payload["tp"] == 2
    assert envelopes["Release"].payload["tp"] == 2
    prepare_uuid = envelopes["Prepare"].uuid
    release_uuid = envelopes["Release"].uuid
    assert envelopes["Check notes"].payload["agr"] == [prepare_uuid]
    assert envelopes["Build package"].payload["agr"] == [prepare_uuid]
    assert envelopes["Publish package"].payload["agr"] == [release_uuid]
    check_task_uuid = envelopes["Check notes"].uuid
    checklist_envelopes = [
        item
        for item in client.committed
        if item.kind == "ChecklistItem3" and item.payload["ts"] == [check_task_uuid]
    ]
    assert [item.payload["tt"] for item in checklist_envelopes] == [
        "Read changes",
        "Check links",
    ]
    assert [item.payload["ix"] for item in checklist_envelopes] == [0, 1024]
    assert [
        envelopes[title].payload["ix"]
        for title in ("Check notes", "Build package", "Publish package")
    ] == [1024, 2048, 3072]
    assert [
        envelopes[title].payload["ix"] for title in ("Prepare", "Release")
    ] == [1024, 2048]

    project = next(item for item in library.records.values() if item.kind == "project")
    visible = library.project(project.id)
    assert [item.title for item in visible] == [
        "Ship release",
        "Prepare",
        "Check notes",
        "Build package",
        "Release",
        "Publish package",
    ]
    check_notes = next(item for item in visible if item.title == "Check notes")
    assert [row.title for row in check_notes.checklists] == [
        "Read changes",
        "Check links",
    ]


def test_chained_area_reorder_round_trips_through_cloud(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    titles = ["Arbeit", "Studium", "Privat", "Finanzen", "Gesundheit", "Systeme"]
    records = [
        Record(
            uuid=title.lower(),
            kind="area",
            title=title,
            sort_index=index * 1024,
            entity="Area3",
        )
        for index, title in enumerate(titles, start=1)
    ]
    library.records.update({record.uuid: record for record in records})
    module = ThingsWorkspace(library, journal=MemoryJournal())
    current = {
        item.title: item
        for item in module.read(ReadCall(ids=[record.id for record in records])).items
    }
    scope_revision = module.read(ReadCall(view="system")).scope_revision

    prepared = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "cloud-area-chain-order-001",
                "scope_revision": scope_revision,
                "create": [
                    {
                        "kind": "area",
                        "key": "$products",
                        "title": "Products",
                        "after": current["Arbeit"].id,
                    }
                ],
                "change": [
                    {
                        "id": current["Arbeit"].id,
                        "if_revision": current["Arbeit"].revision,
                        "title": "Job",
                        "after": None,
                    },
                    {
                        "id": current["Systeme"].id,
                        "if_revision": current["Systeme"].revision,
                        "title": "Stack",
                        "after": "$products",
                    },
                    {
                        "id": current["Studium"].id,
                        "if_revision": current["Studium"].revision,
                        "title": "Study",
                        "after": current["Systeme"].id,
                    },
                    {
                        "id": current["Finanzen"].id,
                        "if_revision": current["Finanzen"].revision,
                        "title": "Money",
                        "after": current["Studium"].id,
                    },
                    {
                        "id": current["Gesundheit"].id,
                        "if_revision": current["Gesundheit"].revision,
                        "title": "Health",
                        "after": current["Finanzen"].id,
                    },
                    {
                        "id": current["Privat"].id,
                        "if_revision": current["Privat"].revision,
                        "title": "Private",
                        "after": current["Gesundheit"].id,
                    },
                ],
            }
        )
    )

    assert prepared.plan is not None
    settled = module.approve(ApproveCall(plan_id=prepared.plan.id))
    assert settled.status == "applied"
    assert [
        record.title
        for record in sorted(
            (record for record in library.records.values() if record.kind == "area"),
            key=lambda record: record.sort_index,
        )
    ] == ["Job", "Products", "Stack", "Study", "Money", "Health", "Private"]
    assert "Final Area order: Job, Products, Stack, Study, Money, Health, Private." in (
        settled.instruction
    )
    assert all(
        envelope.payload.get("ix", 1) > 0
        for envelope in client.committed
        if envelope.kind == "Area3"
    )


def test_source_document_finishes_round_trip_through_cloud(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    module = ThingsWorkspace(library, journal=MemoryJournal())
    titles = [
        "Choose evidence",
        "Extract candidate rules",
        "Compare the evidence",
        "Review the rules",
        "Draft the skill",
        "Test the skill",
        "Pin the tested skill",
    ]
    headings = ["Learn"] * 2 + ["Choose"] * 2 + ["Build"] + ["Use"] * 2

    result = module.commit(
        CommitCall.model_validate(
            {
                "intent_id": "cloud-source-document-001",
                "create": [
                    {
                        "kind": "project",
                        "document": "source",
                        "title": "Build one Agent Skill",
                        "outcome": "One reusable skill.",
                        "finished_when": ["The skill passes one real test."],
                        "keep_in_mind": ["Use observed evidence."],
                        "tasks": [
                            {
                                "title": title,
                                "finish": f"A visible result for {title.casefold()}.",
                                "heading_title": heading,
                            }
                            for title, heading in zip(titles, headings, strict=True)
                        ],
                    }
                ],
            }
        )
    )

    assert result.status == "applied"
    envelopes = {item.payload.get("tt"): item for item in client.committed}
    for title in titles:
        assert envelopes[title].payload["nt"]["v"].startswith(
            "## Done when\n\nA visible result"
        )
    tasks = [
        item
        for item in library.records.values()
        if item.kind == "task" and not item.heading
    ]
    assert len(tasks) == 7
    assert all(
        (task.notes or "").startswith("## Done when\n\nA visible result")
        for task in tasks
    )
    assert "all 7 Task notes passed read-back" in result.instruction


def test_heading_create_assignment_and_clear_round_trip(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records["project"] = Record(
        uuid="project", kind="project", title="Launch", entity="Task6"
    )

    library.apply(
        [
            Write(
                action="create_heading",
                uuid="heading",
                title="Next",
                into_uuid="project",
                into_kind="project",
                anytime=True,
                sort_index=0,
            )
        ]
    )

    assert client.committed[0].action == 0
    assert client.committed[0].payload["tp"] == 2
    assert client.committed[0].payload["pr"] == ["project"]
    assert library.records["heading"].heading is True

    library.records["task"] = Record(
        uuid="task",
        kind="task",
        title="Ship",
        parent_uuid="project",
        entity="Task6",
    )
    library.apply(
        [
            Write(
                action="update",
                uuid="task",
                into_uuid="project",
                into_kind="project",
                heading_uuid="heading",
            )
        ]
    )
    assert client.committed[0].payload["agr"] == ["heading"]
    assert library.records["task"].heading_uuid == "heading"

    library.apply(
        [
            Write(
                action="update",
                uuid="task",
                into_uuid="project",
                into_kind="project",
                clear_heading=True,
            )
        ]
    )
    assert client.committed[0].payload["agr"] == []
    assert library.records["task"].heading_uuid is None

    library.records["logbook"] = Record(
        uuid="logbook",
        kind="task",
        title="Done under heading",
        parent_uuid="project",
        heading_uuid="heading",
        status="done",
        entity="Task6",
    )
    library.apply(
        [Write(action="update", uuid="logbook", kind="task", clear_heading=True)]
    )
    assert client.committed[0].payload == {"agr": [], "md": client.committed[0].payload["md"]}
    assert "pr" not in client.committed[0].payload
    assert "st" not in client.committed[0].payload


def test_create_coalesces_update_move_tags_and_lifecycle(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records["area"] = Record(
        uuid="area", kind="area", title="Work", entity="Area3"
    )

    library.apply(
        [
            Write(action="create", uuid="task", title="Draft"),
            Write(action="update", uuid="task", title="Send draft", notes="Ready"),
            Write(action="move", uuid="task", into_uuid="area", into_kind="area"),
            Write(action="tags", uuid="task", tag_uuids=["important"]),
            Write(action="complete", uuid="task"),
        ]
    )

    assert len(client.committed) == 1
    envelope = client.committed[0]
    assert envelope.action == 0
    assert envelope.payload["tt"] == "Send draft"
    assert envelope.payload["nt"]["v"] == "Ready"
    assert envelope.payload["ar"] == ["area"]
    assert envelope.payload["st"] == 1
    assert envelope.payload["tg"] == ["important"]
    assert envelope.payload["ss"] == 3
    assert library.records["task"].status == "done"


def test_checklist_create_update_delete_round_trip(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records["task"] = Record(
        uuid="task", kind="task", title="Pack", entity="Task6"
    )

    library.apply(
        [
            Write(
                action="checklist",
                uuid="row",
                title="Passport",
                checklist_parent_uuid="task",
                checklist_status="open",
                checklist_index=20,
            )
        ]
    )
    assert client.committed[0].kind == "ChecklistItem3"
    assert client.committed[0].payload["ts"] == ["task"]
    assert library.records["task"].checklists == [
        ChecklistLine(uuid="row", title="Passport", status="open", sort_index=20)
    ]

    library.apply(
        [
            Write(
                action="checklist",
                uuid="row",
                title="Valid passport",
                checklist_status="dropped",
                checklist_index=5,
            )
        ]
    )
    line = library.records["task"].checklists[0]
    assert (line.uuid, line.title, line.status, line.sort_index) == (
        "row",
        "Valid passport",
        "dropped",
        5,
    )

    library.apply([Write(action="checklist", uuid="row", checklist_remove=True)])
    assert client.committed[0].action == 2
    assert library.records["task"].checklists == []


def test_fold_retains_cloud_quality_and_safety_facts() -> None:
    library = MemoryLibrary()
    fold_events(
        [
            {
                "uuid": "group",
                "e": "Tag4",
                "t": 0,
                "p": {"tt": "People", "pn": ["root"]},
            },
            {
                "uuid": "area",
                "e": "Area3",
                "t": 0,
                "p": {"tt": "Work", "tg": ["group"], "ix": 40},
            },
            {
                "uuid": "template",
                "e": "Task6",
                "t": 0,
                "p": {"tt": "Weekly", "tp": 0, "rr": {"tp": 1}, "rt": [], "st": 1},
            },
            {
                "uuid": "instance",
                "e": "Task6",
                "t": 0,
                "p": {
                    "tt": "Weekly instance",
                    "tp": 0,
                    "ss": 3,
                    "sp": 1_700_000_000,
                    "rt": ["template"],
                    "nt": {"t": 2, "ps": [{"r": "Rich"}]},
                    "st": 1,
                    "ix": 80,
                },
            },
        ],
        library=library,
    )

    assert library.records["area"].tag_uuids == ["group"]
    assert library.tag_parents["group"] == ["root"]
    instance = library.records["instance"]
    assert instance.completed_at is not None
    assert instance.notes_source == "structured"
    assert instance.notes_format == "rich"
    assert instance.recurrence.role == "instance"
    assert instance.recurrence.template_uuid == "template"
    assert instance.recurrence.repeat_type == "after_completion"
    assert instance.sort_index == 80


def test_projects_default_to_anytime_and_cannot_enter_inbox(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.apply(
        [Write(action="create", uuid="project", kind="project", title="Launch")]
    )
    assert client.committed[0].payload["st"] == 1
    assert library.records["project"].inbox is False

    with pytest.raises(CloudError, match="Projects cannot enter Inbox"):
        library.apply([Write(action="update", uuid="project", inbox=True)])


def test_heading_placement_keeps_exact_heading_identity(tmp_path: Path) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    library.records.update(
        {
            "project": Record(
                uuid="project", kind="project", title="Launch", entity="Task6"
            ),
            "heading": Record(
                uuid="heading",
                kind="task",
                title="Next",
                heading=True,
                parent_uuid="project",
                entity="Task6",
            ),
        }
    )

    library.apply(
        [
            Write(
                action="create",
                uuid="task",
                title="Write brief",
                into_uuid="project",
                into_kind="project",
                heading_uuid="heading",
            )
        ]
    )
    assert client.committed[0].payload["pr"] == ["project"]
    assert client.committed[0].payload["agr"] == ["heading"]
    assert library.records["task"].heading_uuid == "heading"


def test_project_merge_projects_heading_move_before_assigned_task_validation(
    tmp_path: Path,
) -> None:
    client = _CaptureClient()
    library = CloudLibrary(client, cache=tmp_path / "state.json")  # type: ignore[arg-type]
    source = Record(
        uuid="merge-source",
        kind="project",
        title="Source",
        entity="Task6",
    )
    destination = Record(
        uuid="merge-destination",
        kind="project",
        title="Destination",
        entity="Task6",
    )
    heading = Record(
        uuid="merge-heading",
        kind="task",
        title="Next",
        parent_uuid=source.uuid,
        heading=True,
        entity="Task6",
    )
    task = Record(
        uuid="merge-task",
        kind="task",
        title="Ship",
        parent_uuid=source.uuid,
        heading_uuid=heading.uuid,
        entity="Task6",
    )
    library.records.update(
        {item.uuid: item for item in [source, destination, heading, task]}
    )

    # Keep the Task first. The planner must still project the heading move.
    library.apply(
        [
            Write(
                action="update",
                uuid=task.uuid,
                kind="task",
                into_uuid=destination.uuid,
                into_kind="project",
                heading_uuid=heading.uuid,
            ),
            Write(
                action="update",
                uuid=heading.uuid,
                kind="task",
                into_uuid=destination.uuid,
                into_kind="project",
            ),
        ]
    )

    payloads = {envelope.uuid: envelope.payload for envelope in client.committed}
    assert payloads[task.uuid]["pr"] == [destination.uuid]
    assert payloads[task.uuid]["agr"] == [heading.uuid]
    assert payloads[heading.uuid]["pr"] == [destination.uuid]
    assert task.parent_uuid == destination.uuid
    assert task.heading_uuid == heading.uuid
    assert heading.parent_uuid == destination.uuid


def test_commit_rejects_duplicate_wire_ids() -> None:
    client = CloudClient("a@b.c", "pw")
    client.history_id = "h"
    with pytest.raises(CloudError, match="unique envelope UUIDs"):
        client.commit(
            [
                Envelope("same", 0, "Task6", {"tt": "One"}),
                Envelope("same", 1, "Task6", {"tt": "Two"}),
            ]
        )
