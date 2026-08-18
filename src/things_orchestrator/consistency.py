"""Account-wide Things consistency checks behind one diagnose() call."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from .library import MemoryLibrary, Record

_REPAIR = {
    "inbox_with_project": "repeat the current Project placement",
    "inbox_with_area": "repeat the current Area placement",
    "both_project_and_area": "ask the owner whether the Project or Area is home",
    "inbox_with_schedule": "clear Inbox or clear the schedule",
    "someday_with_start": "clear Someday or clear the start date",
    "someday_with_evening": "clear Someday or clear Evening",
    "reminder_without_schedule": "clear the reminder or set a start date",
    "malformed_reminder": "clear remind_at or set a valid clock time",
    "orphaned_heading": "clear heading_id",
    "heading_wrong_project": "clear heading_id or move the heading",
    "heading_without_project": "clear heading_id or place the Task in a Project",
    "heading_entity_without_project": "place the heading in a Project",
    "missing_parent": "place the item in an active Project",
    "trashed_parent": "restore the parent or move the child",
    "parent_not_project": "place the item under a Project",
    "missing_area": "place the item in an active Area or clear the Area home",
    "trashed_area": "restore the Area or move the child",
    "area_not_area": "clear the Area home or choose an Area",
    "missing_repeat_template": "inspect recurrence before changing it",
    "malformed_repeat": "inspect recurrence before changing it",
    "dangling_tag_parent": "clear or repair the tag parent",
    "tag_parent_self_reference": "clear the tag parent",
    "tag_parent_cycle": "clear the cyclic tag parent",
}


@dataclass(frozen=True)
class Conflict:
    """One record whose stored state cannot be a valid native Things item."""

    item_id: str
    signals: tuple[str, ...]
    repair: str | None = None


def remind_is_valid(value: str) -> bool:
    """Return whether a stored reminder is a real clock time."""

    try:
        hour_text, minute_text = value.split(":", 1)
        time(int(hour_text), int(minute_text))
    except (TypeError, ValueError):
        return False
    return True


def diagnose(library: MemoryLibrary) -> list[Conflict]:
    """Return every record with a native-state contradiction, once each."""

    found: list[Conflict] = []
    for item in sorted(library.records.values(), key=lambda row: row.id):
        signals = item_conflicts(item, library)
        if signals:
            found.append(_conflict(item.id, signals))
    found.extend(_tag_conflicts(library))
    return found


def item_conflicts(item: Record, library: MemoryLibrary) -> list[str]:
    """Return the native-state contradictions on one record."""

    signals: list[str] = []
    parent = library.records.get(item.parent_uuid or "")
    area = library.records.get(item.area_uuid or "")
    heading = library.records.get(item.heading_uuid or "")

    if item.inbox and item.parent_uuid:
        signals.append("inbox_with_project")
    if item.inbox and item.area_uuid:
        signals.append("inbox_with_area")
    if item.parent_uuid and item.area_uuid:
        signals.append("both_project_and_area")
    if item.inbox and (item.someday or item.tonight or item.start is not None):
        signals.append("inbox_with_schedule")
    if item.someday and item.start is not None:
        signals.append("someday_with_start")
    if item.someday and item.tonight:
        signals.append("someday_with_evening")
    if item.remind is not None and item.start is None and not item.tonight:
        signals.append("reminder_without_schedule")
    if item.remind is not None and not remind_is_valid(item.remind):
        signals.append("malformed_reminder")
    if item.heading and not item.parent_uuid:
        signals.append("heading_entity_without_project")
    if item.heading_uuid and not item.parent_uuid:
        signals.append("heading_without_project")
    if item.heading_uuid:
        if heading is None or not heading.heading:
            signals.append("orphaned_heading")
        elif item.parent_uuid and heading.parent_uuid != item.parent_uuid:
            signals.append("heading_wrong_project")
    if item.parent_uuid and parent is None:
        signals.append("missing_parent")
    elif parent is not None and parent.kind != "project":
        signals.append("parent_not_project")
    elif parent is not None and parent.trashed and not item.trashed:
        signals.append("trashed_parent")
    if item.area_uuid and area is None:
        signals.append("missing_area")
    elif area is not None and area.kind != "area":
        signals.append("area_not_area")
    elif area is not None and area.trashed and not item.trashed:
        signals.append("trashed_area")
    if item.recurrence.role == "instance":
        template = library.records.get(item.recurrence.template_uuid or "")
        if (
            template is None
            or template.recurrence.role != "template"
            or template.recurrence.rule is None
        ):
            signals.append("missing_repeat_template")
    if item.recurrence.role == "template" and item.recurrence.rule is None:
        signals.append("malformed_repeat")
    if item.recurrence.role == "instance" and item.recurrence.repeat_type == "unknown":
        signals.append("malformed_repeat")
    return signals


def _conflict(item_id: str, signals: list[str]) -> Conflict:
    hints = [_REPAIR[name] for name in signals if name in _REPAIR]
    return Conflict(
        item_id=item_id,
        signals=tuple(signals),
        repair="; ".join(dict.fromkeys(hints)) or None,
    )


def _tag_conflicts(library: MemoryLibrary) -> list[Conflict]:
    found: list[Conflict] = []
    for uuid, parents in sorted(library.tag_parents.items()):
        if uuid not in library.tags:
            continue
        signals: list[str] = []
        if uuid in parents:
            signals.append("tag_parent_self_reference")
        if any(parent not in library.tags for parent in parents):
            signals.append("dangling_tag_parent")
        if _tag_cycle(uuid, library.tag_parents):
            signals.append("tag_parent_cycle")
        if signals:
            found.append(_conflict(f"tag:{uuid}", signals))
    return found


def _tag_cycle(start: str, parents: dict[str, list[str]]) -> bool:
    seen: set[str] = set()
    stack = list(parents.get(start, []))
    while stack:
        node = stack.pop()
        if node == start:
            return True
        if node in seen or node not in parents:
            continue
        seen.add(node)
        stack.extend(parents.get(node, []))
    return False
