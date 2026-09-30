"""Native-state conflict detection for one Things record."""

from __future__ import annotations

from datetime import time

from .library import MemoryLibrary, Record, template_uuid_of


def remind_is_valid(value: str) -> bool:
    """Return whether a stored reminder is a real clock time."""

    try:
        hour_text, minute_text = value.split(":", 1)
        time(int(hour_text), int(minute_text))
    except (TypeError, ValueError):
        return False
    return True


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
    inherited_project_area = (
        item.kind == "task"
        and parent is not None
        and parent.kind == "project"
        and parent.area_uuid == item.area_uuid
    )
    if item.parent_uuid and item.area_uuid and not inherited_project_area:
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
        if heading is None or not heading.heading or (
            heading.trashed and not item.trashed
        ):
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
        template = library.records.get(template_uuid_of(item) or "")
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
