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
    "area_invalid_parent": "clear the invalid Area parent",
    "area_missing_parent": "clear the invalid Area parent",
    "project_missing_parent": "move the Project to an Area or to root Anytime",
    "project_invalid_parent": "move the Project to an Area or to root Anytime",
    "missing_area": "place the item in an active Area or clear the Area home",
    "trashed_area": "restore the Area or move the child",
    "area_not_area": "clear the Area home or choose an Area",
    "area_invalid_home": "clear the invalid Area home",
    "area_missing_home": "clear the invalid Area home",
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
    "area_invalid_parent": "clear_area_parent",
    "area_missing_parent": "clear_area_parent",
    "project_missing_parent": "rehome_project",
    "project_invalid_parent": "rehome_project",
    "missing_area": "rehome_or_clear_area",
    "trashed_area": "restore_or_move_child",
    "area_not_area": "rehome_or_clear_area",
    "area_invalid_home": "clear_area_home",
    "area_missing_home": "clear_area_home",
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
    repairs: tuple[tuple[str, str], ...] = ()


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
    if item.parent_uuid and (
        parent_signal := _parent_conflict(item, parent)
    ):
        signals.append(parent_signal)
    if item.area_uuid and (home_signal := _area_home_conflict(item, area)):
        signals.append(home_signal)
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


def _parent_conflict(item: Record, parent: Record | None) -> str | None:
    if item.kind == "area":
        if parent is None:
            return "area_missing_parent"
        if parent.kind == "project":
            return "area_with_project_parent"
        return "area_invalid_parent"
    if item.kind == "project":
        if parent is None:
            return "project_missing_parent"
        if parent.kind == "project":
            return "project_with_project_parent"
        return "project_invalid_parent"
    if parent is None:
        return "missing_parent"
    if parent.kind != "project":
        return "parent_not_project"
    if parent.trashed and not item.trashed:
        return "trashed_parent"
    return None


def _area_home_conflict(item: Record, area: Record | None) -> str | None:
    if item.kind == "area":
        if area is None:
            return "area_missing_home"
        if area.kind == "area":
            return "area_with_area_home"
        return "area_invalid_home"
    if area is None:
        return "missing_area"
    if area.kind != "area":
        return "area_not_area"
    if area.trashed and not item.trashed:
        return "trashed_area"
    return None


def _conflict(item_id: str, signals: list[str]) -> Conflict:
    hints = [_REPAIR[name] for name in signals if name in _REPAIR]
    repairs = tuple(
        (name, _REPAIR_KIND[name]) for name in signals if name in _REPAIR_KIND
    )
    return Conflict(
        item_id=item_id,
        signals=tuple(signals),
        repair=_legacy_repair(hints),
        repair_kind=repairs[0][1] if len(repairs) == 1 else None,
        repairs=repairs,
    )


def _legacy_repair(hints: list[str]) -> str | None:
    """Keep singular prose only when it fits; repairs[] is the complete answer."""

    text = "; ".join(dict.fromkeys(hints))
    if not text or len(text) > 400:
        return None
    return text


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
    reverse: dict[str, list[str]] = {node: [] for node in nodes}
    for node, links in parents.items():
        for parent in links:
            if parent in reverse:
                reverse[parent].append(node)

    def postorder(graph: dict[str, list[str]]) -> list[str]:
        seen: set[str] = set()
        order: list[str] = []
        for start in sorted(nodes):
            if start in seen:
                continue
            stack: list[tuple[str, bool]] = [(start, False)]
            while stack:
                node, finished = stack.pop()
                if finished:
                    order.append(node)
                    continue
                if node in seen:
                    continue
                seen.add(node)
                stack.append((node, True))
                for nxt in reversed(graph.get(node, [])):
                    if nxt not in seen and nxt in nodes:
                        stack.append((nxt, False))
        return order

    cyclic: set[str] = set()
    seen: set[str] = set()
    for start in reversed(postorder(parents)):
        if start in seen:
            continue
        component: list[str] = []
        stack = [start]
        seen.add(start)
        while stack:
            node = stack.pop()
            component.append(node)
            for nxt in reverse.get(node, []):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        if len(component) > 1 or start in parents.get(start, []):
            cyclic.update(component)
    return cyclic
