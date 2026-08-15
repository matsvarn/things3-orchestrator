"""Lossless recurrence state and safe semantic changes."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Literal, TypeAlias

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)
RepeatUnit = Literal["day", "week", "month", "year"]

_UNITS: dict[int, RepeatUnit] = {
    4: "year",
    8: "day",
    16: "month",
    256: "week",
}


@dataclass(frozen=True)
class RecurrenceState:
    """One coherent, lossless recurrence value for a Things record."""

    role: Literal["none", "template", "instance"] = "none"
    repeat_type: Literal["none", "fixed", "after_completion", "unknown"] = "none"
    template_uuid: str | None = None
    rule: dict[str, JsonValue] | None = None
    links: tuple[str, ...] = ()

    @property
    def unit(self) -> RepeatUnit | None:
        return repeat_unit(self.rule)

    @property
    def interval(self) -> int | None:
        return repeat_interval(self.rule)

    def change_interval(self, interval: int, *, kind: str) -> RecurrenceState:
        self.validate_interval_template(kind=kind)
        return replace(self, rule=change_interval(self.rule, interval))

    def validate_interval_template(self, *, kind: str) -> None:
        if (
            kind != "task"
            or self.role != "template"
            or self.repeat_type not in {"fixed", "after_completion"}
            or self.rule is None
            or self.links
        ):
            raise ValueError("Repeat changes need an exact repeating Task template")


def repeat_unit(rule: dict[str, JsonValue] | None) -> RepeatUnit | None:
    """Return the public unit name without normalizing an unknown value."""
    if rule is None:
        return None
    value = rule.get("fu")
    return _UNITS.get(value) if isinstance(value, int) else None


def repeat_interval(rule: dict[str, JsonValue] | None) -> int | None:
    """Return a valid public interval."""
    if rule is None:
        return None
    value = rule.get("fa")
    return value if isinstance(value, int) and value > 0 else None


def change_interval(
    rule: dict[str, JsonValue] | None,
    interval: int,
) -> dict[str, JsonValue]:
    """Change only the interval and preserve the complete opaque rule."""
    if rule is None:
        raise ValueError("Only a repeating template can change its repeat interval")
    current = rule.get("fa")
    if not isinstance(current, int) or current < 1:
        raise ValueError("This repeat rule has an unsupported interval")
    changed = deepcopy(rule)
    changed["fa"] = interval
    return changed
