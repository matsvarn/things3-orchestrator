"""Account-wide Things consistency checks behind one diagnose() call."""

from __future__ import annotations

from dataclasses import dataclass

from .library import MemoryLibrary, Record


@dataclass(frozen=True)
class Conflict:
    """One record whose stored state cannot be a valid native Things item."""

    item_id: str
    signals: tuple[str, ...]


def diagnose(library: MemoryLibrary) -> list[Conflict]:
    """Return every record with a native-state contradiction, once each."""

    found: list[Conflict] = []
    for item in sorted(library.records.values(), key=lambda row: row.id):
        signals = item_conflicts(item, library)
        if signals:
            found.append(Conflict(item_id=item.id, signals=tuple(signals)))
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
    if item.heading_uuid:
        if heading is None or not heading.heading:
            signals.append("orphaned_heading")
        elif item.parent_uuid and heading.parent_uuid != item.parent_uuid:
            signals.append("heading_wrong_project")
    if item.parent_uuid and parent is None:
        signals.append("missing_parent")
    elif parent is not None and parent.trashed and not item.trashed:
        signals.append("trashed_parent")
    if item.area_uuid and area is None:
        signals.append("missing_area")
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


def _tag_conflicts(library: MemoryLibrary) -> list[Conflict]:
    found: list[Conflict] = []
    for uuid, parents in sorted(library.tag_parents.items()):
        if uuid not in library.tags:
            continue
        if any(parent not in library.tags for parent in parents):
            found.append(
                Conflict(item_id=f"tag:{uuid}", signals=("dangling_tag_parent",))
            )
    return found
