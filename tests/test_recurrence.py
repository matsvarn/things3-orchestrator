from __future__ import annotations

from datetime import datetime, timezone

import pytest

from things_orchestrator.recurrence import RecurrenceState, new_rule


def template(
    *,
    mode: str = "fixed",
    unit: int = 256,
    interval: int = 1,
    anchor: datetime | None = None,
) -> RecurrenceState:
    rule: dict[str, object] = {
        "tp": 0 if mode == "fixed" else 1,
        "fu": unit,
        "fa": interval,
        "of": [{"wd": 2, "future_key": {"keep": True}}],
        "future_rule_key": ["preserve", 4],
    }
    if anchor is not None:
        rule["sr"] = int(anchor.timestamp())
    return RecurrenceState(
        role="template",
        repeat_type=mode,  # type: ignore[arg-type]
        rule=rule,  # type: ignore[arg-type]
    )


def test_transition_changes_selected_fields_and_preserves_opaque_fields() -> None:
    original = template(mode="fixed", unit=256, interval=1)

    changed = original.transition(
        kind="task",
        mode="after_completion",
        unit="month",
        interval=3,
    )

    assert changed.repeat_type == "after_completion"
    assert changed.unit == "month"
    assert changed.interval == 3
    assert changed.rule == {
        "tp": 1,
        "fu": 16,
        "fa": 3,
        "of": [],
        "future_rule_key": ["preserve", 4],
    }
    assert original.rule is not None
    assert original.rule["tp"] == 0
    assert original.rule["fu"] == 256
    assert original.rule["fa"] == 1


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"mode": "after_completion"}, {"tp": 1}),
        ({"unit": "day"}, {"fu": 8}),
        ({"interval": 4}, {"fa": 4}),
    ],
)
def test_transition_accepts_one_semantic_change(
    kwargs: dict[str, object], expected: dict[str, int]
) -> None:
    changed = template().transition(kind="task", **kwargs)  # type: ignore[arg-type]

    assert changed.rule is not None
    for key, value in expected.items():
        assert changed.rule[key] == value


def test_transition_remove_clears_the_whole_recurrence_state() -> None:
    changed = template().transition(kind="task", remove=True)

    assert changed == RecurrenceState()


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"remove": True, "interval": 2},
        {"interval": 0},
        {"interval": 367},
        {"interval": True},
        {"mode": "unknown"},
        {"unit": "fortnight"},
    ],
)
def test_transition_rejects_unsafe_or_invalid_changes(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        template().transition(kind="task", **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "state",
    [
        RecurrenceState(),
        RecurrenceState(role="instance", repeat_type="fixed", links=("template",)),
        RecurrenceState(
            role="template",
            repeat_type="unknown",
            rule={"tp": 99, "fu": 256, "fa": 1},
        ),
        RecurrenceState(
            role="template",
            repeat_type="fixed",
            rule={"tp": 0, "fu": 999, "fa": 1},
        ),
        RecurrenceState(
            role="template",
            repeat_type="fixed",
            rule={"tp": 0, "fu": 256, "fa": 0},
        ),
    ],
    ids=["none", "instance", "unknown-mode", "unknown-unit", "invalid-interval"],
)
def test_transition_rejects_non_template_or_inconsistent_state(
    state: RecurrenceState,
) -> None:
    with pytest.raises(ValueError):
        state.transition(kind="task", interval=2)


def test_transition_rejects_non_task_templates() -> None:
    with pytest.raises(ValueError, match="Task template"):
        template().transition(kind="project", interval=2)


def test_change_interval_uses_the_atomic_transition() -> None:
    original = template()
    changed = original.change_interval(2, kind="task")

    assert changed.interval == 2
    assert changed.rule is not None
    assert changed.rule["future_rule_key"] == ["preserve", 4]


def test_after_completion_interval_change_preserves_opaque_offsets() -> None:
    original = template(mode="after_completion")

    changed = original.transition(kind="task", interval=2)

    assert changed.rule is not None
    assert changed.rule["of"] == [{"wd": 2, "future_key": {"keep": True}}]
    assert changed.rule["future_rule_key"] == ["preserve", 4]


def test_switch_to_after_completion_clears_fixed_offsets() -> None:
    changed = template().transition(kind="task", mode="after_completion")

    assert changed.rule is not None
    assert changed.rule["of"] == []


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("week", [{"wd": 4}]),
        ("month", [{"dy": 13}]),
        ("year", [{"dy": 13, "mo": 4}]),
    ],
)
def test_fixed_unit_change_rebuilds_offsets_from_anchor(
    unit: str, expected: list[dict[str, int]]
) -> None:
    original = template(
        unit=8,
        anchor=datetime(2026, 5, 14, tzinfo=timezone.utc),
    )

    changed = original.transition(kind="task", unit=unit)  # type: ignore[arg-type]

    assert changed.rule is not None
    assert changed.rule["of"] == expected
    assert changed.rule["future_rule_key"] == ["preserve", 4]


def test_switch_from_after_completion_to_fixed_rebuilds_current_unit() -> None:
    original = template(
        mode="after_completion",
        unit=16,
        anchor=datetime(2026, 5, 14, tzinfo=timezone.utc),
    )

    changed = original.transition(kind="task", mode="fixed")

    assert changed.rule is not None
    assert changed.rule["of"] == [{"dy": 13}]


def test_explicit_weekdays_do_not_need_an_anchor() -> None:
    original = template(mode="after_completion", unit=16)

    changed = original.transition(
        kind="task",
        mode="fixed",
        unit="week",
        weekday_codes=[1, 5],
    )

    assert changed.rule is not None
    assert changed.rule["of"] == [{"wd": 1}, {"wd": 5}]


@pytest.mark.parametrize("unit", ["week", "month", "year"])
def test_fixed_unit_change_rejects_missing_required_anchor(unit: str) -> None:
    with pytest.raises(ValueError, match="start anchor"):
        template(unit=8).transition(kind="task", unit=unit)  # type: ignore[arg-type]


def test_day_and_after_completion_changes_do_not_need_an_anchor() -> None:
    fixed_day = template(unit=16).transition(kind="task", unit="day")
    fixed_from_after_completion = template(mode="after_completion", unit=8).transition(
        kind="task", mode="fixed"
    )
    after_completion = template(unit=16).transition(
        kind="task", mode="after_completion", unit="year"
    )

    assert fixed_day.rule is not None
    assert fixed_day.rule["of"] == []
    assert fixed_from_after_completion.rule is not None
    assert fixed_from_after_completion.rule["of"] == []
    assert after_completion.rule is not None
    assert after_completion.rule["of"] == []


@pytest.mark.parametrize("weekday_codes", [[], [1, 1], [-1], [7], [True]])
def test_transition_rejects_invalid_weekday_selectors(
    weekday_codes: list[int],
) -> None:
    with pytest.raises(ValueError, match="Weekday selectors"):
        template().transition(kind="task", weekday_codes=weekday_codes)


def test_after_completion_rejects_weekday_selectors() -> None:
    with pytest.raises(ValueError, match="fixed weekly"):
        template().transition(
            kind="task",
            mode="after_completion",
            weekday_codes=[1],
        )


def test_new_after_completion_rule_has_no_fixed_offsets() -> None:
    rule = new_rule(
        mode="after_completion",
        unit="week",
        interval=2,
        anchor=datetime(2026, 8, 17, tzinfo=timezone.utc).date(),
    )

    assert rule["of"] == []
