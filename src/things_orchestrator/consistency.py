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
    "project_with_project_parent": "move the Project to an Area or to root Anytime",
    "area_with_area_home": "clear the nested Area home",
    "area_with_project_parent": "clear the Area parent",
}
_REPAIR_KIND = {
    "inbox_with_project": "repeat_placement",
    "inbox_with_area": "repeat_placement",
    "both_project_and_area": "owner_choice",
    "inbox_with_schedule": "clear_inbox_or_schedule",
    "someday_with_start": "clear_someday_or_start",
    "someday_with_evening": "clear_someday_or_evening",
    "reminder_without_schedule": "clear_reminder",
    "malformed_reminder": "clear_reminder",
    "orphaned_heading": "clear_heading",
    "heading_wrong_project": "clear_heading",
    "heading_without_project": "clear_heading",
    "heading_entity_without_project": "rehome_heading",
    "missing_parent": "rehome_item",
    "trashed_parent": "restore_or_move_child",
    "parent_not_project": "rehome_item",
    "missing_area": "rehome_or_clear_area",
    "trashed_area": "restore_or_move_child",
    "area_not_area": "rehome_or_clear_area",
    "missing_repeat_template": "inspect_recurrence",
    "malformed_repeat": "inspect_recurrence",
    "dangling_tag_parent": "clear_or_repair_tag_parent",
    "tag_parent_self_reference": "clear_tag_parent",
    "tag_parent_cycle": "clear_tag_parent",
    "project_with_project_parent": "rehome_project",
    "area_with_area_home": "clear_area_home",
    "area_with_project_parent": "clear_area_parent",
}


@dataclass(frozen=True)
class Conflict:
    """One record whose stored state cannot be a valid native Things item."""

    item_id: str
    signals: tuple[str, ...]
    repair: str | None = None
    repair_kind: str | None = None


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
    if item.kind == "project" and parent is not None and parent.kind == "project":
        signals.append("project_with_project_parent")
    if item.kind == "area" and parent is not None and parent.kind == "project":
        signals.append("area_with_project_parent")
    if item.kind == "area" and area is not None and area.kind == "area":
        signals.append("area_with_area_home")
    if item.parent_uuid and parent is None:
        signals.append("missing_parent")
    elif (
        parent is not None
        and parent.kind != "project"
        and item.kind != "area"
    ):
        signals.append("parent_not_project")
    elif parent is not None and parent.trashed and not item.trashed:
        signals.append("trashed_parent")
    if item.area_uuid and area is None:
        signals.append("missing_area")
    elif (
        area is not None
        and area.kind != "area"
        and item.kind != "area"
    ):
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
    kinds = [_REPAIR_KIND[name] for name in signals if name in _REPAIR_KIND]
    return Conflict(
        item_id=item_id,
        signals=tuple(signals),
        repair="; ".join(dict.fromkeys(hints)) or None,
        repair_kind=next(iter(dict.fromkeys(kinds)), None),
    )


def _tag_conflicts(library: MemoryLibrary) -> list[Conflict]:
    cyclic = _cyclic_tags(library.tag_parents)
    found: list[Conflict] = []
    for uuid, parents in sorted(library.tag_parents.items()):
        if uuid not in library.tags:
            continue
        signals: list[str] = []
        if uuid in parents:
            signals.append("tag_parent_self_reference")
        if any(parent not in library.tags for parent in parents):
            signals.append("dangling_tag_parent")
        if uuid in cyclic:
            signals.append("tag_parent_cycle")
        if signals:
            found.append(_conflict(f"tag:{uuid}", signals))
    return found


def _cyclic_tags(parents: dict[str, list[str]]) -> set[str]:
    """Return every tag that participates in a parent cycle."""

    nodes = set(parents)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    cyclic: set[str] = set()
    clock = 0

    def strongconnect(node: str) -> None:
        nonlocal clock
        index[node] = low[node] = clock
        clock += 1
        stack.append(node)
        on_stack.add(node)
        for parent in parents.get(node, []):
            if parent not in parents:
                continue
            if parent not in index:
                strongconnect(parent)
                low[node] = min(low[node], low[parent])
            elif parent in on_stack:
                low[node] = min(low[node], index[parent])
        if low[node] != index[node]:
            return
        component: list[str] = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == node:
                break
        if len(component) > 1 or node in parents.get(node, []):
            cyclic.update(component)

    for node in sorted(nodes):
        if node not in index:
            strongconnect(node)
    return cyclic
