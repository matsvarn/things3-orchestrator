"""Lossless recurrence state and safe semantic changes."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timezone
from typing import Literal, TypeAlias

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)
RepeatUnit = Literal["day", "week", "month", "year"]
RepeatMode = Literal["fixed", "after_completion"]
RepeatType = Literal["none", "fixed", "after_completion", "unknown"]

_UNITS: dict[int, RepeatUnit] = {
    4: "year",
    8: "day",
    16: "month",
    256: "week",
}
_UNIT_CODES: dict[RepeatUnit, int] = {unit: code for code, unit in _UNITS.items()}
_MODE_CODES: dict[RepeatMode, int] = {"fixed": 0, "after_completion": 1}
_MAX_INTERVAL = 366
_NEVER = 64_092_211_200


@dataclass(frozen=True)
class RecurrenceState:
    """One coherent, lossless recurrence value for a Things record."""

    role: Literal["none", "template", "instance"] = "none"
    repeat_type: RepeatType = "none"
    template_uuid: str | None = None
    rule: dict[str, JsonValue] | None = None
    links: tuple[str, ...] = ()

    @property
    def unit(self) -> RepeatUnit | None:
        return repeat_unit(self.rule)

    @property
    def interval(self) -> int | None:
        return repeat_interval(self.rule)

    @property
    def weekday_codes(self) -> tuple[int, ...]:
        """Return semantic weekly selectors when every stored offset is known."""
        if self.unit != "week" or self.rule is None:
            return ()
        offsets = self.rule.get("of")
        if not isinstance(offsets, list):
            return ()
        codes: list[int] = []
        for offset in offsets:
            if not isinstance(offset, dict):
                return ()
            code = offset.get("wd")
            if not isinstance(code, int) or isinstance(code, bool) or not 0 <= code <= 6:
                return ()
            codes.append(code)
        return tuple(codes)

    def change_interval(self, interval: int, *, kind: str) -> RecurrenceState:
        return self.transition(kind=kind, interval=interval)

    def transition(
        self,
        *,
        kind: str,
        mode: RepeatMode | None = None,
        unit: RepeatUnit | None = None,
        interval: int | None = None,
        weekday_codes: list[int] | None = None,
        remove: bool = False,
    ) -> RecurrenceState:
        """Apply one atomic semantic change to an existing repeat template.

        The implementation changes only the known ``tp``, ``fu``, and ``fa``
        fields. All other Cloud fields remain opaque and are copied exactly.
        ``remove`` is exclusive because clearing a rule is a lifecycle change,
        not a rule edit.
        """
        self.validate_interval_template(kind=kind)
        if remove and any(
            value is not None for value in (mode, unit, interval, weekday_codes)
        ):
            raise ValueError("Removing a repeat rule cannot combine with rule changes")
        if (
            not remove
            and mode is None
            and unit is None
            and interval is None
            and weekday_codes is None
        ):
            raise ValueError("A repeat transition needs a rule change or remove")
        if interval is not None and (
            isinstance(interval, bool)
            or not isinstance(interval, int)
            or not 1 <= interval <= _MAX_INTERVAL
        ):
            raise ValueError(f"Repeat interval must be between 1 and {_MAX_INTERVAL}")
        if mode is not None and mode not in _MODE_CODES:
            raise ValueError("Repeat mode must be fixed or after_completion")
        if unit is not None and unit not in _UNIT_CODES:
            raise ValueError("Repeat unit must be day, week, month, or year")

        rule = self.rule
        assert rule is not None
        if self.repeat_type not in _MODE_CODES:
            raise ValueError("This repeat rule has an unsupported mode")
        if repeat_unit(rule) is None:
            raise ValueError("This repeat rule has an unsupported unit")
        if repeat_interval(rule) is None:
            raise ValueError("This repeat rule has an unsupported interval")

        if remove:
            return RecurrenceState()

        changed = deepcopy(rule)
        if mode is not None:
            changed["tp"] = _MODE_CODES[mode]
        if unit is not None:
            changed["fu"] = _UNIT_CODES[unit]
        if interval is not None:
            changed["fa"] = interval
        if unit is not None or weekday_codes is not None:
            target_unit = unit or repeat_unit(rule)
            assert target_unit is not None
            changed["of"] = _offsets_for_unit(
                rule,
                target_unit,
                weekday_codes=weekday_codes,
            )
            if isinstance(rule.get("sr"), (int, float)):
                changed["ts"] = -7 if target_unit == "year" else 0
        next_mode: RepeatMode = mode or (
            "fixed" if self.repeat_type == "fixed" else "after_completion"
        )
        return replace(
            self,
            repeat_type=next_mode,
            rule=changed,
        )

    def validate_interval_template(self, *, kind: str) -> None:
        if (
            kind != "task"
            or self.role != "template"
            or self.repeat_type not in {"fixed", "after_completion"}
            or self.rule is None
            or self.links
        ):
            raise ValueError("Repeat changes need an exact repeating Task template")

    def fold_rule(self, value: object) -> RecurrenceState:
        if isinstance(value, dict):
            rule = deepcopy(value)
            code = rule.get("tp", 0)
            repeat_type: RepeatType = (
                "fixed"
                if code == 0
                else "after_completion" if code == 1 else "unknown"
            )
            return replace(
                self,
                role="template",
                repeat_type=repeat_type,
                template_uuid=None,
                rule=rule,
            )
        if self.role == "template":
            return RecurrenceState(links=self.links)
        return replace(self, rule=None)

    def fold_links(self, value: object) -> RecurrenceState:
        links = (
            (str(value),)
            if isinstance(value, str)
            else tuple(str(link) for link in value)
            if isinstance(value, list)
            else ()
        )
        if links:
            return replace(
                self,
                role="instance",
                template_uuid=links[0],
                rule=None,
                links=links,
            )
        if self.role == "instance":
            return RecurrenceState()
        return replace(self, template_uuid=None, links=())

    def resolve_instance_type(self, repeat_type: RepeatType | None) -> RecurrenceState:
        if self.role != "instance":
            return self
        resolved: RepeatType = (
            repeat_type
            if repeat_type in {"fixed", "after_completion"}
            else "unknown"
        )
        return replace(self, repeat_type=resolved)


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
    return (
        value
        if isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= _MAX_INTERVAL
        else None
    )


def change_interval(
    rule: dict[str, JsonValue] | None,
    interval: int,
) -> dict[str, JsonValue]:
    """Change only the interval and preserve the complete opaque rule."""
    if rule is None:
        raise ValueError("Only a repeating template can change its repeat interval")
    if repeat_interval(rule) is None:
        raise ValueError("This repeat rule has an unsupported interval")
    if (
        isinstance(interval, bool)
        or not isinstance(interval, int)
        or not 1 <= interval <= _MAX_INTERVAL
    ):
        raise ValueError(f"Repeat interval must be between 1 and {_MAX_INTERVAL}")
    changed = deepcopy(rule)
    changed["fa"] = interval
    return changed


def new_rule(
    *,
    mode: RepeatMode,
    unit: RepeatUnit,
    interval: int,
    anchor: date,
    weekday_codes: list[int] | None = None,
) -> dict[str, JsonValue]:
    """Build the complete observed Cloud rule for a new repeat template."""
    if not 1 <= interval <= _MAX_INTERVAL:
        raise ValueError(f"Repeat interval must be between 1 and {_MAX_INTERVAL}")
    codes = list(weekday_codes or [])
    if codes and unit != "week":
        raise ValueError("Weekday selectors need a weekly repeat rule")
    if any(code < 0 or code > 6 for code in codes) or len(codes) != len(set(codes)):
        raise ValueError("Weekday selectors must be unique values from 0 through 6")
    if unit == "week":
        offsets: list[JsonValue] = [
            {"wd": code}
            for code in (codes or [(anchor.weekday() + 1) % 7])
        ]
    elif unit == "month":
        offsets = [{"dy": anchor.day - 1}]
    elif unit == "year":
        offsets = [{"dy": anchor.day - 1, "mo": anchor.month - 1}]
    else:
        offsets = []
    stamp = int(datetime.combine(anchor, time.min, tzinfo=timezone.utc).timestamp())
    return {
        "tp": _MODE_CODES[mode],
        "fu": _UNIT_CODES[unit],
        "fa": interval,
        "of": offsets,
        "sr": stamp,
        "ia": stamp,
        "ed": _NEVER,
        "rc": 0,
        "ts": -7 if unit == "year" else 0,
        "rrv": 4,
    }


def _offsets_for_unit(
    rule: dict[str, JsonValue],
    unit: RepeatUnit,
    *,
    weekday_codes: list[int] | None,
) -> list[JsonValue]:
    raw = rule.get("sr")
    if not isinstance(raw, (int, float)):
        if weekday_codes is not None:
            raise ValueError("This repeat rule has an unsupported start anchor")
        current = rule.get("of")
        return deepcopy(current) if isinstance(current, list) else []
    anchor = datetime.fromtimestamp(raw, timezone.utc).date()
    if unit == "week":
        codes = (
            weekday_codes
            if weekday_codes is not None
            else [(anchor.weekday() + 1) % 7]
        )
        if any(code < 0 or code > 6 for code in codes) or len(codes) != len(set(codes)):
            raise ValueError("Weekday selectors must be unique values from 0 through 6")
        return [{"wd": code} for code in codes]
    if weekday_codes:
        raise ValueError("Weekday selectors need a weekly repeat rule")
    if unit == "month":
        return [{"dy": anchor.day - 1}]
    if unit == "year":
        return [{"dy": anchor.day - 1, "mo": anchor.month - 1}]
    return []
