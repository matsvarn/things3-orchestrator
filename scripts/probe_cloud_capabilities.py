"""Run destructive, disposable proof cases against one Things Cloud account.

This script creates records with a unique ``__TO_PROBE__`` prefix. It removes
only those exact UUIDs. Run it only when you own the configured account.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path
from typing import Callable

from things_orchestrator.cloud import (
    CloudClient,
    CloudLibrary,
    Envelope,
    _create_payload,
    load_credentials,
)
from things_orchestrator.interface import ApproveCall, CommitCall, ReadCall, Result
from things_orchestrator.library import Write, new_uuid
from things_orchestrator.workspace import ThingsWorkspace

# Keep this mapping beside the disposable cases.  The proof document and its
# structural test use these exact keys; no live network call runs in CI.
PROBE_CAPABILITY_KEYS: dict[str, tuple[str, ...]] = {
    "Capture a Task": ("recurrence.create_template_and_instance",),
    "Capture a Project and Area": ("ax.project_move_to_area",),
    "Schedule start, deadline, reminder, and placement": (
        "recurrence.convert_existing_task",
    ),
    "Checklist add, change, remove, order, and preservation": (
        "recurrence.convert_existing_task",
        "project.restore_tree",
    ),
    "Task and Project Trash or restore": ("task.restore", "project.restore_tree"),
    "Task purge and descendant-first Project purge": (
        "task.purge",
        "project.purge_tree_descendants_first",
    ),
    "Task, Project, and heading placement": (
        "ax.project_move_to_area",
        "ax.organize_draft",
    ),
    "Area registry create and Project-to-Area placement": (
        "ax.project_move_to_area",
    ),
    "Context refs for exact change and placement": (
        "ax.context_change",
        "ax.project_move_to_area",
    ),
    "Editable Project organize drafts": ("ax.organize_draft",),
    "Atomic Project merge": ("ax.project_merge",),
    "Heading create, rename, assign, clear, and reorder": (
        "ax.organize_draft",
        "heading.reorder",
        "heading.rename",
        "heading.clear_assignment",
        "heading.delete_with_assignments",
    ),
    "Heading deletion with assignment cleanup": ("heading.delete_with_assignments",),
    "Tag create, assign, rename, reparent, and delete": (
        "tag.create_hierarchy",
        "tag.assign_task_readback",
        "tag.rename_reparent",
        "tag.delete",
    ),
    "Markdown write and explicit rich-note replacement": (
        "note.write_rich_structure",
        "note.replace_rich_with_markdown",
    ),
    "Repeat inspect, create, convert, edit, complete, and stop": (
        "recurrence.inspect_relationship",
        "recurrence.create_template_and_instance",
        "recurrence.convert_existing_task",
        "recurrence.change_full_rule",
        "recurrence.change_generated_copy",
        "recurrence.complete_current_copy",
        "recurrence.remove_keep_copy",
    ),
}


def _wait_for(
    library: CloudLibrary,
    predicate: Callable[[], bool],
    *,
    seconds: float = 20,
) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        library.refresh(force=True)
        if predicate():
            return True
        time.sleep(1)
    return False


def _proof(condition: bool, name: str, results: dict[str, bool]) -> None:
    results[name] = condition
    if not condition:
        raise RuntimeError(f"live proof failed: {name}")


def _task_create(uuid: str, title: str) -> Envelope:
    return Envelope(
        uuid=uuid,
        action=0,
        kind="Task6",
        payload=_create_payload(Write(action="create", uuid=uuid, title=title)),
    )


def _approved_commit(module: ThingsWorkspace, call: CommitCall) -> Result:
    prepared = module.commit(call)
    if prepared.plan is None or prepared.status != "needs_approval":
        raise RuntimeError(f"live proof did not produce an approval plan: {prepared}")
    applied = module.approve(ApproveCall(plan_id=prepared.plan.id))
    if applied.status not in {"applied", "unchanged"}:
        raise RuntimeError(f"live proof approval did not apply: {applied}")
    return applied


def _applied_commit(module: ThingsWorkspace, call: CommitCall) -> Result:
    """Apply one public commit, including approval when the plan requires it."""
    result = module.commit(call)
    if result.status == "needs_approval" and result.plan is not None:
        result = module.approve(ApproveCall(plan_id=result.plan.id))
    if result.status not in {"applied", "unchanged"}:
        raise RuntimeError(f"live proof commit did not apply: {result}")
    return result


def _revision(module: ThingsWorkspace, item_id: str) -> str:
    result = module.read(ReadCall(id=item_id, limit=40))
    if result.status != "ok" or len(result.items) != 1:
        raise RuntimeError(f"live proof could not read {item_id}: {result}")
    return result.items[0].revision


def _tags_revision(module: ThingsWorkspace) -> str:
    result = module.read(ReadCall(view="tags", limit=40))
    if result.status != "ok" or result.scope_revision is None:
        raise RuntimeError(f"live proof could not read tags: {result}")
    return result.scope_revision


def _record_depth(uuid: str, records: dict[str, object]) -> int:
    """Return a bounded parent depth for descendant-first cleanup."""
    depth = 0
    current = records.get(uuid)
    seen: set[str] = set()
    while current is not None and getattr(current, "parent_uuid", None):
        parent_uuid = str(getattr(current, "parent_uuid"))
        if parent_uuid in seen:
            break
        seen.add(parent_uuid)
        depth += 1
        current = records.get(parent_uuid)
        if depth >= 100:
            break
    return depth


def _tag_depth(uuid: str, parents: dict[str, list[str]]) -> int:
    depth = 0
    current = uuid
    seen: set[str] = set()
    while parents.get(current):
        parent = parents[current][0]
        if parent in seen:
            break
        seen.add(parent)
        depth += 1
        current = parent
        if depth >= 100:
            break
    return depth


def _cleanup_probe_records(
    client: CloudClient,
    library: CloudLibrary,
    owned: dict[str, str],
    prefix: str,
) -> None:
    """Delete only this run's UUIDs, deepest first, and prove cleanup."""
    errors: list[Exception] = []
    try:
        library.refresh(force=True)
    except Exception as error:
        errors.append(error)

    # A unique title prefix lets us recover records created by a timed-out
    # commit.  UUIDs remain the deletion authority for every known record.
    for uuid, item in library.records.items():
        if item.title.startswith(prefix):
            owned.setdefault(uuid, item.entity or "Task6")
    for uuid, title in library.tags.items():
        if title.startswith(prefix):
            owned.setdefault(uuid, "Tag4")
    for item in library.records.values():
        for row in item.checklists:
            if row.title.startswith(prefix):
                owned.setdefault(row.uuid, "ChecklistItem3")

    records = library.records
    checklists = sorted(
        {
            uuid: kind
            for uuid, kind in owned.items()
            if kind.startswith("ChecklistItem")
        }.items()
    )
    # Checklist rows are separate Cloud entities. Delete them before their
    # parent Tasks or Projects so no orphaned rows can survive a purge.
    if checklists:
        try:
            client.commit([Envelope(uuid, 2, kind, {}) for uuid, kind in checklists])
        except Exception as error:
            errors.append(error)

    record_entries = [
        (uuid, kind)
        for uuid, kind in owned.items()
        if not kind.startswith("ChecklistItem") and uuid not in library.tags
    ]
    record_entries.sort(
        key=lambda entry: (-_record_depth(entry[0], records), entry[0])
    )
    for depth in sorted(
        {_record_depth(uuid, records) for uuid, _kind in record_entries},
        reverse=True,
    ):
        batch = [
            Envelope(uuid, 2, kind, {})
            for uuid, kind in record_entries
            if _record_depth(uuid, records) == depth
        ]
        if not batch:
            continue
        try:
            client.commit(batch)
        except Exception as error:
            errors.append(error)

    tag_entries = [
        (uuid, kind) for uuid, kind in owned.items() if kind.startswith("Tag")
    ]
    for depth in sorted(
        {_tag_depth(uuid, library.tag_parents) for uuid, _kind in tag_entries},
        reverse=True,
    ):
        batch = [
            Envelope(uuid, 2, kind, {})
            for uuid, kind in tag_entries
            if _tag_depth(uuid, library.tag_parents) == depth
        ]
        if not batch:
            continue
        try:
            client.commit(batch)
        except Exception as error:
            errors.append(error)

    try:
        library.refresh(force=True)
    except Exception as error:
        errors.append(error)

    remaining_records = [
        item.uuid
        for item in library.records.values()
        if item.title.startswith(prefix) or item.uuid in owned
    ]
    remaining_tags = [
        uuid
        for uuid, title in library.tags.items()
        if title.startswith(prefix) or uuid in owned
    ]
    remaining_checklists = [
        row.uuid
        for item in library.records.values()
        for row in item.checklists
        if row.title.startswith(prefix) or row.uuid in owned
    ]
    if remaining_records or remaining_tags or remaining_checklists:
        errors.append(
            RuntimeError(
                "probe cleanup left exact UUIDs: "
                f"records={remaining_records}, tags={remaining_tags}, "
                f"checklists={remaining_checklists}"
            )
        )
    if errors:
        detail = "; ".join(str(error) for error in errors)
        raise RuntimeError(f"live probe cleanup failed: {detail}") from errors[0]


def run() -> dict[str, bool]:
    email, password, _token = load_credentials()
    client = CloudClient(email, password)
    results: dict[str, bool] = {}
    prefix = f"__TO_PROBE__{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    owned: dict[str, str] = {}

    def own(kind: str) -> str:
        uuid = new_uuid()
        owned[uuid] = kind
        return uuid

    with tempfile.TemporaryDirectory(prefix="things-proof-") as temp:
        library = CloudLibrary(client, cache=Path(temp) / "state.json")
        library.refresh(force=True)
        module = ThingsWorkspace(library)
        try:
            # Tags: hierarchy, rename, reparent, and deletion.
            parent_tag = own("Tag4")
            second_parent_tag = own("Tag4")
            child_tag = own("Tag4")
            tag_parent_title = f"{prefix} tag parent"
            tag_second_parent_title = f"{prefix} tag second parent"
            tag_child_title = f"{prefix} tag child"
            created_tags = module.commit(
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-tag-create-{parent_tag}",
                        "ensure_tags": [
                            {"key": "$parent", "title": tag_parent_title},
                            {"key": "$second", "title": tag_second_parent_title},
                            {
                                "key": "$child",
                                "title": tag_child_title,
                                "parent_id": "$parent",
                            },
                        ],
                    }
                )
            )
            if created_tags.status != "applied":
                raise RuntimeError(f"live tag create did not apply: {created_tags}")
            actual_parent_tag = library.tag_uuid(tag_parent_title)
            actual_second_parent_tag = library.tag_uuid(tag_second_parent_title)
            actual_child_tag = library.tag_uuid(tag_child_title)
            if (
                actual_parent_tag is None
                or actual_second_parent_tag is None
                or actual_child_tag is None
            ):
                raise RuntimeError("live tag create did not return exact tags")
            owned.pop(parent_tag)
            owned.pop(second_parent_tag)
            owned.pop(child_tag)
            parent_tag, second_parent_tag, child_tag = (
                actual_parent_tag,
                actual_second_parent_tag,
                actual_child_tag,
            )
            owned[parent_tag] = "Tag4"
            owned[second_parent_tag] = "Tag4"
            owned[child_tag] = "Tag4"
            _proof(
                library.tag_parents.get(child_tag) == [parent_tag],
                "tag.create_hierarchy",
                results,
            )
            tag_target = own("Task6")
            library.apply(
                [
                    Write(
                        action="create",
                        uuid=tag_target,
                        title=f"{prefix} tag assignment target",
                    )
                ]
            )
            _applied_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-tag-assign-{tag_target}",
                        "tags_revision": _tags_revision(module),
                        "change": [
                            {
                                "id": f"task:{tag_target}",
                                "if_revision": _revision(
                                    module, f"task:{tag_target}"
                                ),
                                "tags_add": [f"tag:{child_tag}"],
                            }
                        ],
                    }
                ),
            )

            def tag_assignment_is_visible() -> bool:
                library.refresh(force=True)
                target = library.records.get(tag_target)
                if target is None or child_tag not in target.tag_uuids:
                    return False
                read_back = module.read(ReadCall(id=f"task:{tag_target}"))
                return (
                    read_back.status == "ok"
                    and len(read_back.items) == 1
                    and f"tag:{child_tag}"
                    in {tag.id for tag in read_back.items[0].direct_tags}
                )

            _proof(
                _wait_for(library, tag_assignment_is_visible),
                "tag.assign_task_readback",
                results,
            )
            renamed_tag_title = f"{prefix} tag renamed"
            _approved_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-tag-change-{child_tag}",
                        "tags_revision": _tags_revision(module),
                        "change_tags": [
                            {
                                "id": f"tag:{child_tag}",
                                "title": renamed_tag_title,
                                "parent_id": f"tag:{second_parent_tag}",
                            }
                        ],
                    }
                ),
            )
            _proof(
                library.tags.get(child_tag) == renamed_tag_title
                and library.tag_parents.get(child_tag) == [second_parent_tag],
                "tag.rename_reparent",
                results,
            )
            for tag_uuid in (child_tag, second_parent_tag, parent_tag):
                _approved_commit(
                    module,
                    CommitCall.model_validate(
                        {
                            "intent_id": f"probe-tag-delete-{tag_uuid}",
                            "tags_revision": _tags_revision(module),
                            "change_tags": [
                                {
                                    "id": f"tag:{tag_uuid}",
                                    "delete_permanently": True,
                                }
                            ],
                        }
                    ),
                )
            _proof(
                child_tag not in library.tags and parent_tag not in library.tags,
                "tag.delete",
                results,
            )
            library.apply([Write(action="permanent_delete", uuid=tag_target)])
            _proof(tag_target not in library.records, "tag.assignment_target_cleanup", results)
            owned.pop(child_tag)
            owned.pop(second_parent_tag)
            owned.pop(parent_tag)
            owned.pop(tag_target)

            # Public contextual path: short refs and one desired Project layout.
            ax_project = own("Task6")
            ax_heading = own("Task6")
            ax_changed = own("Task6")
            ax_assigned = own("Task6")
            ax_unlisted = own("Task6")
            ax_changed_title = f"{prefix} context changed"
            ax_unlisted_title = f"{prefix} unlisted"
            library.apply(
                [
                    Write(
                        action="create",
                        uuid=ax_project,
                        kind="project",
                        title=f"{prefix} AX project",
                    ),
                    Write(
                        action="create_heading",
                        uuid=ax_heading,
                        title=f"{prefix} AX existing heading",
                        into_uuid=ax_project,
                        into_kind="project",
                        sort_index=0,
                    ),
                    Write(
                        action="create",
                        uuid=ax_changed,
                        title=f"{prefix} context original",
                        notes="Keep context metadata",
                        into_uuid=ax_project,
                        into_kind="project",
                        heading_uuid=ax_heading,
                        sort_index=0,
                    ),
                    Write(
                        action="create",
                        uuid=ax_assigned,
                        title=f"{prefix} assign to new heading",
                        into_uuid=ax_project,
                        into_kind="project",
                        sort_index=1024,
                    ),
                    Write(
                        action="create",
                        uuid=ax_unlisted,
                        title=ax_unlisted_title,
                        notes="This work is intentionally unlisted.",
                        into_uuid=ax_project,
                        into_kind="project",
                        sort_index=2048,
                    ),
                ]
            )

            change_read = module.read(
                ReadCall(purpose="change", id=f"task:{ax_changed}", limit=20)
            )
            if change_read.status != "ok" or change_read.context is None:
                raise RuntimeError(f"live contextual change read failed: {change_read}")
            changed_ref = next(
                item.ref
                for item in change_read.items
                if item.id == f"task:{ax_changed}"
            )
            if changed_ref is None:
                raise RuntimeError("live contextual change read omitted its short ref")
            _applied_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-ax-change-{ax_changed}",
                        "context_id": change_read.context.id,
                        "change": [{"ref": changed_ref, "title": ax_changed_title}],
                    }
                ),
            )
            _proof(
                _wait_for(
                    library,
                    lambda: (
                        ax_changed in library.records
                        and library.records[ax_changed].title == ax_changed_title
                    ),
                ),
                "ax.context_change",
                results,
            )

            organize_read = module.read(
                ReadCall(
                    purpose="organize",
                    view="project",
                    within=f"project:{ax_project}",
                    limit=20,
                )
            )
            if (
                organize_read.status != "ok"
                or organize_read.context is None
                or not organize_read.context.complete
            ):
                raise RuntimeError(
                    f"live contextual organize read failed: {organize_read}"
                )
            refs = {item.id: item.ref for item in organize_read.items}
            required_ids = {
                f"project:{ax_project}",
                f"heading:{ax_heading}",
                f"task:{ax_changed}",
                f"task:{ax_assigned}",
            }
            if any(refs.get(item_id) is None for item_id in required_ids):
                raise RuntimeError(
                    "live contextual organize read omitted required refs"
                )
            unlisted_before = library.records[ax_unlisted]
            unlisted_state = (
                unlisted_before.title,
                unlisted_before.notes,
                unlisted_before.heading_uuid,
                unlisted_before.sort_index,
                unlisted_before.status,
            )
            new_heading_title = f"{prefix} AX new heading"
            _applied_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-ax-organize-{ax_project}",
                        "context_id": organize_read.context.id,
                        "organize": [
                            {
                                "project_ref": refs[f"project:{ax_project}"],
                                "sections": [
                                    {
                                        "heading_key": "$new",
                                        "heading_title": new_heading_title,
                                        "task_refs": [refs[f"task:{ax_assigned}"]],
                                    },
                                    {
                                        "heading_ref": refs[f"heading:{ax_heading}"],
                                        "task_refs": [refs[f"task:{ax_changed}"]],
                                    },
                                ],
                                "unlisted": "keep",
                            }
                        ],
                    }
                ),
            )

            def organized_layout_is_visible() -> bool:
                candidates = [
                    item
                    for item in library.records.values()
                    if item.title == new_heading_title and item.heading
                ]
                if len(candidates) != 1:
                    return False
                candidate = candidates[0]
                current_heading = library.records.get(ax_heading)
                current_assigned = library.records.get(ax_assigned)
                current_changed = library.records.get(ax_changed)
                current_unlisted = library.records.get(ax_unlisted)
                if any(
                    item is None
                    for item in (
                        current_heading,
                        current_assigned,
                        current_changed,
                        current_unlisted,
                    )
                ):
                    return False
                assert current_heading is not None
                assert current_assigned is not None
                assert current_changed is not None
                assert current_unlisted is not None
                return (
                    candidate.parent_uuid == ax_project
                    and candidate.sort_index < current_heading.sort_index
                    and current_assigned.heading_uuid == candidate.uuid
                    and current_changed.heading_uuid == ax_heading
                    and (
                        current_unlisted.title,
                        current_unlisted.notes,
                        current_unlisted.heading_uuid,
                        current_unlisted.sort_index,
                        current_unlisted.status,
                    )
                    == unlisted_state
                )

            _proof(
                _wait_for(library, organized_layout_is_visible),
                "ax.organize_draft",
                results,
            )
            new_heading = next(
                item
                for item in library.records.values()
                if item.title == new_heading_title and item.heading
            )
            owned[new_heading.uuid] = new_heading.entity or "Task6"
            library.apply(
                [
                    Write(action="permanent_delete", uuid=ax_changed),
                    Write(action="permanent_delete", uuid=ax_assigned),
                    Write(action="permanent_delete", uuid=ax_unlisted),
                    Write(action="permanent_delete", uuid=new_heading.uuid),
                    Write(action="permanent_delete", uuid=ax_heading),
                    Write(action="permanent_delete", uuid=ax_project),
                ]
            )
            for uuid in (
                ax_changed,
                ax_assigned,
                ax_unlisted,
                new_heading.uuid,
                ax_heading,
                ax_project,
            ):
                owned.pop(uuid)

            # Public contextual path: merge one Project in one approved batch.
            # The organize read carries source members, destination Project
            # refs, and the destination Area anchor in one bounded context.
            merge_area = own("Area3")
            merge_source = own("Task6")
            merge_destination = own("Task6")
            merge_heading = own("Task6")
            merge_task = own("Task6")
            library.apply(
                [
                    Write(
                        action="create",
                        uuid=merge_area,
                        kind="area",
                        title=f"{prefix} merge area",
                    ),
                    Write(
                        action="create",
                        uuid=merge_source,
                        kind="project",
                        title=f"{prefix} merge source",
                    ),
                    Write(
                        action="create",
                        uuid=merge_destination,
                        kind="project",
                        title=f"{prefix} merge destination",
                        into_uuid=merge_area,
                        into_kind="area",
                    ),
                    Write(
                        action="create_heading",
                        uuid=merge_heading,
                        title=f"{prefix} merge heading",
                        into_uuid=merge_source,
                        into_kind="project",
                    ),
                    Write(
                        action="create",
                        uuid=merge_task,
                        title=f"{prefix} merge task",
                        into_uuid=merge_source,
                        into_kind="project",
                        heading_uuid=merge_heading,
                    ),
                ]
            )
            merge_read = module.read(
                ReadCall(purpose="organize", id=f"project:{merge_source}")
            )
            if merge_read.status != "ok" or merge_read.context is None:
                raise RuntimeError(f"live Project merge read failed: {merge_read}")
            merge_refs = {item.id: item.ref for item in merge_read.items}
            required_merge_ids = {
                f"project:{merge_source}",
                f"project:{merge_destination}",
                f"area:{merge_area}",
                f"heading:{merge_heading}",
                f"task:{merge_task}",
            }
            if any(merge_refs.get(item_id) is None for item_id in required_merge_ids):
                raise RuntimeError("live Project merge read omitted required refs")
            _approved_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-ax-project-merge-{merge_source}",
                        "context_id": merge_read.context.id,
                        "change": [
                            {
                                "ref": merge_refs[f"heading:{merge_heading}"],
                                "into": merge_refs[f"project:{merge_destination}"],
                            },
                            {
                                "ref": merge_refs[f"task:{merge_task}"],
                                "into": merge_refs[f"project:{merge_destination}"],
                            },
                            {
                                "ref": merge_refs[f"project:{merge_source}"],
                                "lifecycle": "trash",
                            },
                        ],
                    }
                ),
            )

            def merged_tree_is_visible() -> bool:
                library.refresh(force=True)
                source = library.records.get(merge_source)
                heading = library.records.get(merge_heading)
                task = library.records.get(merge_task)
                return (
                    source is not None
                    and source.trashed
                    and heading is not None
                    and heading.parent_uuid == merge_destination
                    and task is not None
                    and task.parent_uuid == merge_destination
                    and task.heading_uuid == merge_heading
                )

            _proof(
                _wait_for(library, merged_tree_is_visible),
                "ax.project_merge",
                results,
            )
            merge_read_back = module.read(ReadCall(id=f"task:{merge_task}"))
            if merge_read_back.status != "ok" or not merge_read_back.items:
                raise RuntimeError("live Project merge read-back failed")
            _proof(
                merge_read_back.items[0].into_id == f"project:{merge_destination}",
                "ax.project_merge_readback",
                results,
            )
            library.apply(
                [
                    Write(action="permanent_delete", uuid=merge_task),
                    Write(action="permanent_delete", uuid=merge_heading),
                    Write(action="permanent_delete", uuid=merge_source),
                    Write(action="permanent_delete", uuid=merge_destination),
                    Write(action="permanent_delete", uuid=merge_area),
                ]
            )
            for uuid in (
                merge_task,
                merge_heading,
                merge_source,
                merge_destination,
                merge_area,
            ):
                owned.pop(uuid)

            # Public contextual path: move a Project to an Area with short refs.
            # Create both Areas and the Project through the same public commit
            # path.  This also proves that the destination Area is available in
            # the Project change context, without copying a revision.
            areas_before = module.read(ReadCall(view="system", limit=40))
            if areas_before.status != "ok" or areas_before.scope_revision is None:
                raise RuntimeError(
                    f"live Area registry read failed: {areas_before}"
                )
            _applied_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-ax-project-area-{new_uuid()}",
                        "scope_revision": areas_before.scope_revision,
                        "create": [
                            {
                                "key": "$source",
                                "kind": "area",
                                "title": f"{prefix} source area",
                            },
                            {
                                "key": "$destination",
                                "kind": "area",
                                "title": f"{prefix} destination area",
                            },
                            {
                                "key": "$project",
                                "kind": "project",
                                "title": f"{prefix} movable project",
                                "into": "$source",
                            },
                        ],
                    }
                ),
            )

            def created_project_area_layout_is_visible() -> bool:
                return any(
                    item.kind == "area"
                    and item.title == f"{prefix} source area"
                    for item in library.records.values()
                ) and any(
                    item.kind == "area"
                    and item.title == f"{prefix} destination area"
                    for item in library.records.values()
                ) and any(
                    item.kind == "project"
                    and item.title == f"{prefix} movable project"
                    for item in library.records.values()
                )

            if not _wait_for(library, created_project_area_layout_is_visible):
                raise RuntimeError("live Project to Area setup did not become visible")
            source_area = next(
                item
                for item in library.records.values()
                if item.kind == "area" and item.title == f"{prefix} source area"
            )
            destination_area = next(
                item
                for item in library.records.values()
                if item.kind == "area" and item.title == f"{prefix} destination area"
            )
            movable_project = next(
                item
                for item in library.records.values()
                if item.kind == "project" and item.title == f"{prefix} movable project"
            )
            for item in (source_area, destination_area, movable_project):
                owned[item.uuid] = item.entity or (
                    "Area3" if item.kind == "area" else "Task6"
                )

            project_change = module.read(
                ReadCall(purpose="change", id=f"project:{movable_project.uuid}")
            )
            if project_change.status != "ok" or project_change.context is None:
                raise RuntimeError(
                    f"live Project Area context read failed: {project_change}"
                )
            context_refs = {item.id: item.ref for item in project_change.items}
            project_ref = context_refs.get(f"project:{movable_project.uuid}")
            destination_ref = context_refs.get(f"area:{destination_area.uuid}")
            if project_ref is None or destination_ref is None:
                raise RuntimeError(
                    "live Project Area context omitted Project or destination ref"
                )
            _applied_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-ax-project-move-{movable_project.uuid}",
                        "context_id": project_change.context.id,
                        "change": [
                            {"ref": project_ref, "into": destination_ref},
                        ],
                    }
                ),
            )

            def project_move_is_visible() -> bool:
                moved = library.records.get(movable_project.uuid)
                if moved is None or moved.area_uuid != destination_area.uuid:
                    return False
                read_back = module.read(
                    ReadCall(id=f"project:{movable_project.uuid}")
                )
                return (
                    read_back.status == "ok"
                    and len(read_back.items) == 1
                    and read_back.items[0].into_id == f"area:{destination_area.uuid}"
                )

            _proof(
                _wait_for(library, project_move_is_visible),
                "ax.project_move_to_area",
                results,
            )
            # Headings and a non-empty Project lifecycle.
            project = own("Task6")
            heading_a = own("Task6")
            heading_b = own("Task6")
            project_task = own("Task6")
            checklist = own("ChecklistItem3")
            _applied_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-heading-fixture-{project}",
                        "create": [
                            {
                                "key": "$project",
                                "kind": "project",
                                "title": f"{prefix} project",
                            },
                            {
                                "key": "$heading_a",
                                "kind": "heading",
                                "title": f"{prefix} heading A",
                                "into": "$project",
                            },
                            {
                                "key": "$heading_b",
                                "kind": "heading",
                                "title": f"{prefix} heading B",
                                "into": "$project",
                            },
                            {
                                "key": "$project_task",
                                "kind": "task",
                                "title": f"{prefix} project task",
                                "into": "$project",
                                "heading_id": "$heading_a",
                                "checklist": [f"{prefix} project checklist"],
                            },
                        ],
                    }
                ),
            )
            if not _wait_for(
                library,
                lambda: all(
                    any(item.title == title for item in library.records.values())
                    for title in (
                        f"{prefix} project",
                        f"{prefix} heading A",
                        f"{prefix} heading B",
                        f"{prefix} project task",
                    )
                ),
            ):
                raise RuntimeError("public heading fixture did not become visible")

            def created_uuid(title: str) -> str:
                matches = [
                    item.uuid
                    for item in library.records.values()
                    if item.title == title
                ]
                if len(matches) != 1:
                    raise RuntimeError(f"public fixture title was not unique: {title}")
                return matches[0]

            fixture_ids = {
                "project": created_uuid(f"{prefix} project"),
                "heading_a": created_uuid(f"{prefix} heading A"),
                "heading_b": created_uuid(f"{prefix} heading B"),
                "project_task": created_uuid(f"{prefix} project task"),
            }
            for placeholder in (project, heading_a, heading_b, project_task):
                owned.pop(placeholder)
            project = fixture_ids["project"]
            heading_a = fixture_ids["heading_a"]
            heading_b = fixture_ids["heading_b"]
            project_task = fixture_ids["project_task"]
            owned.update(
                {
                    project: "Task6",
                    heading_a: "Task6",
                    heading_b: "Task6",
                    project_task: "Task6",
                }
            )
            task_fixture_read = module.read(
                ReadCall(id=f"task:{project_task}", limit=40)
            )
            if (
                task_fixture_read.status != "ok"
                or len(task_fixture_read.items) != 1
                or len(task_fixture_read.items[0].checklist) != 1
            ):
                raise RuntimeError("public heading fixture lost its checklist")
            owned.pop(checklist)
            checklist = task_fixture_read.items[0].checklist[0].id.removeprefix(
                "check:"
            )
            owned[checklist] = "ChecklistItem3"

            _applied_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-heading-reorder-{heading_b}",
                        "change": [
                            {
                                "id": f"heading:{heading_b}",
                                "if_revision": _revision(
                                    module, f"heading:{heading_b}"
                                ),
                                "after": None,
                            }
                        ],
                    }
                ),
            )

            def heading_reorder_is_visible() -> bool:
                moved_read = module.read(ReadCall(id=f"heading:{heading_b}"))
                first_read = module.read(ReadCall(id=f"heading:{heading_a}"))
                return (
                    moved_read.status == "ok"
                    and first_read.status == "ok"
                    and len(moved_read.items) == 1
                    and len(first_read.items) == 1
                    and moved_read.items[0].order < first_read.items[0].order
                )

            _proof(
                _wait_for(library, heading_reorder_is_visible),
                "heading.reorder",
                results,
            )
            _approved_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-trash-{project}",
                        "change": [
                            {
                                "id": f"project:{project}",
                                "if_revision": _revision(module, f"project:{project}"),
                                "lifecycle": "trash",
                            }
                        ],
                    }
                ),
            )
            _proof(library.records[project].trashed, "project.trash", results)
            _approved_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-restore-{project}",
                        "change": [
                            {
                                "id": f"project:{project}",
                                "if_revision": _revision(module, f"project:{project}"),
                                "lifecycle": "restore",
                            }
                        ],
                    }
                ),
            )
            _proof(
                not library.records[project].trashed
                and not library.records[project_task].trashed
                and library.records[project_task].parent_uuid == project
                and any(
                    row.uuid == checklist
                    for row in library.records[project_task].checklists
                ),
                "project.restore_tree",
                results,
            )
            renamed_heading = module.commit(
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-heading-rename-{heading_a}",
                        "change": [
                            {
                                "id": f"heading:{heading_a}",
                                "if_revision": _revision(
                                    module, f"heading:{heading_a}"
                                ),
                                "title": f"{prefix} heading renamed",
                            }
                        ],
                    }
                )
            )
            _proof(
                renamed_heading.status == "applied"
                and library.records[heading_a].title.endswith("heading renamed"),
                "heading.rename",
                results,
            )
            # Exercise explicit assignment and clearing through the public
            # change path.  The exact read-back proves that Cloud removed the
            # heading assignment, not only that the request was accepted.
            _applied_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-heading-assign-{project_task}",
                        "change": [
                            {
                                "id": f"task:{project_task}",
                                "if_revision": _revision(
                                    module, f"task:{project_task}"
                                ),
                                "heading_id": f"heading:{heading_b}",
                            }
                        ],
                    }
                ),
            )

            def heading_clear_is_visible() -> bool:
                read_back = module.read(ReadCall(id=f"task:{project_task}"))
                return (
                    read_back.status == "ok"
                    and len(read_back.items) == 1
                    and read_back.items[0].heading_id is None
                )

            _applied_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-heading-clear-{project_task}",
                        "change": [
                            {
                                "id": f"task:{project_task}",
                                "if_revision": _revision(
                                    module, f"task:{project_task}"
                                ),
                                "heading_id": None,
                            }
                        ],
                    }
                ),
            )
            _proof(
                _wait_for(library, heading_clear_is_visible),
                "heading.clear_assignment",
                results,
            )
            # Keep the later heading-deletion proof meaningful: it still
            # deletes a heading with an assigned disposable Task.
            _applied_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-heading-reassign-{project_task}",
                        "change": [
                            {
                                "id": f"task:{project_task}",
                                "if_revision": _revision(
                                    module, f"task:{project_task}"
                                ),
                                "heading_id": f"heading:{heading_a}",
                            }
                        ],
                    }
                ),
            )
            _approved_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-heading-delete-{heading_a}",
                        "change": [
                            {
                                "id": f"heading:{heading_a}",
                                "if_revision": _revision(
                                    module, f"heading:{heading_a}"
                                ),
                                "lifecycle": "delete_permanently",
                            }
                        ],
                    }
                ),
            )
            _proof(
                heading_a not in library.records
                and library.records[project_task].heading_uuid is None,
                "heading.delete_with_assignments",
                results,
            )
            owned.pop(heading_a)
            _approved_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-retrash-{project}",
                        "change": [
                            {
                                "id": f"project:{project}",
                                "if_revision": _revision(module, f"project:{project}"),
                                "lifecycle": "trash",
                            }
                        ],
                    }
                ),
            )
            purged = _approved_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-purge-{project}",
                        "change": [
                            {
                                "id": f"project:{project}",
                                "if_revision": _revision(module, f"project:{project}"),
                                "lifecycle": "delete_permanently",
                                "delete_contents": True,
                            }
                        ],
                    }
                ),
            )
            _proof(
                all(
                    uuid not in library.records
                    for uuid in (project_task, heading_b, project)
                )
                and purged.status in {"applied", "unchanged"},
                "project.purge_tree_descendants_first",
                results,
            )
            _proof(
                purged.status in {"applied", "unchanged"},
                "commit.forced_read_back",
                results,
            )
            for uuid in (checklist, project_task, heading_b, project):
                owned.pop(uuid)

            # Standalone Task lifecycle.
            task_placeholder = own("Task6")
            _applied_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-task-lifecycle-create-{task_placeholder}",
                        "create": [{"title": f"{prefix} lifecycle"}],
                    }
                ),
            )
            if not _wait_for(
                library,
                lambda: any(
                    item.title == f"{prefix} lifecycle"
                    for item in library.records.values()
                ),
            ):
                raise RuntimeError("public Task lifecycle fixture did not appear")
            task_matches = [
                item.uuid
                for item in library.records.values()
                if item.title == f"{prefix} lifecycle"
            ]
            if len(task_matches) != 1:
                raise RuntimeError("public Task lifecycle fixture was not unique")
            task = task_matches[0]
            owned.pop(task_placeholder)
            owned[task] = "Task6"
            _approved_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-task-trash-{task}",
                        "change": [
                            {
                                "id": f"task:{task}",
                                "if_revision": _revision(module, f"task:{task}"),
                                "lifecycle": "trash",
                            }
                        ],
                    }
                ),
            )
            _approved_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-task-restore-{task}",
                        "change": [
                            {
                                "id": f"task:{task}",
                                "if_revision": _revision(module, f"task:{task}"),
                                "lifecycle": "restore",
                            }
                        ],
                    }
                ),
            )

            def task_restore_is_visible() -> bool:
                read_back = module.read(ReadCall(id=f"task:{task}"))
                return (
                    read_back.status == "ok"
                    and len(read_back.items) == 1
                    and read_back.items[0].status == "open"
                )

            _proof(_wait_for(library, task_restore_is_visible), "task.restore", results)
            _approved_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-task-retrash-{task}",
                        "change": [
                            {
                                "id": f"task:{task}",
                                "if_revision": _revision(module, f"task:{task}"),
                                "lifecycle": "trash",
                            }
                        ],
                    }
                ),
            )
            _approved_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-task-purge-{task}",
                        "change": [
                            {
                                "id": f"task:{task}",
                                "if_revision": _revision(module, f"task:{task}"),
                                "lifecycle": "delete_permanently",
                            }
                        ],
                    }
                ),
            )

            def task_purge_is_visible() -> bool:
                read_back = module.read(ReadCall(id=f"task:{task}"))
                return read_back.status == "needs_input" and not read_back.items

            _proof(_wait_for(library, task_purge_is_visible), "task.purge", results)
            owned.pop(task)

            # Rich structured note acceptance and explicit Markdown replacement.
            rich = own("Task6")
            rich_payload = _create_payload(
                Write(action="create", uuid=rich, title=f"{prefix} rich note")
            )
            rich_payload["nt"] = {
                "_t": "tx",
                "t": 2,
                "ps": [{"r": "Structured probe", "rs": []}],
            }
            client.commit([Envelope(rich, 0, "Task6", rich_payload)])
            _proof(
                _wait_for(
                    library,
                    lambda: (
                        rich in library.records
                        and library.records[rich].notes_format == "rich"
                    ),
                ),
                "note.write_rich_structure",
                results,
            )
            _approved_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-rich-replace-{rich}",
                        "change": [
                            {
                                "id": f"task:{rich}",
                                "if_revision": _revision(module, f"task:{rich}"),
                                "notes_markdown": "Markdown replacement",
                                "replace_rich_note": True,
                            }
                        ],
                    }
                ),
            )
            _proof(
                library.records[rich].notes == "Markdown replacement"
                and library.records[rich].notes_format == "markdown",
                "note.replace_rich_with_markdown",
                results,
            )
            library.apply([Write(action="permanent_delete", uuid=rich)])
            owned.pop(rich)

            # Recurrence: convert an existing Task without replacing its identity.
            existing_repeat = own("Task6")
            existing_repeat_check = own("ChecklistItem3")
            existing_repeat_remove = own("ChecklistItem3")
            existing_repeat_project = own("Task6")
            existing_repeat_heading = own("Task6")
            existing_repeat_list_anchor = own("Task6")
            existing_repeat_today_anchor = own("Task6")
            existing_repeat_title = f"{prefix} existing recurring"
            local_now = datetime.now().astimezone()
            repeat_today = local_now.date()
            repeat_deadline = repeat_today + timedelta(days=3)
            repeat_reminder = datetime.combine(
                repeat_today, dt_time(9, 30), tzinfo=local_now.tzinfo
            )
            library.apply(
                [
                    Write(
                        action="create",
                        uuid=existing_repeat_project,
                        kind="project",
                        title=f"{prefix} repeat destination",
                        anytime=True,
                    ),
                    Write(
                        action="create_heading",
                        uuid=existing_repeat_heading,
                        title=f"{prefix} repeat heading",
                        into_uuid=existing_repeat_project,
                        into_kind="project",
                        anytime=True,
                    ),
                    Write(
                        action="create",
                        uuid=existing_repeat_list_anchor,
                        title=f"{prefix} repeat list anchor",
                        into_uuid=existing_repeat_project,
                        into_kind="project",
                        anytime=True,
                        sort_index=0,
                    ),
                    Write(
                        action="create",
                        uuid=existing_repeat_today_anchor,
                        title=f"{prefix} repeat today anchor",
                        start=repeat_today,
                        today_index=0,
                        owner_today=repeat_today,
                    ),
                    Write(
                        action="create",
                        uuid=existing_repeat,
                        title=existing_repeat_title,
                        notes="Preserve this note",
                    ),
                    Write(
                        action="checklist",
                        uuid=existing_repeat_check,
                        title="Preserve this checklist",
                        checklist_parent_uuid=existing_repeat,
                        checklist_status="open",
                    ),
                    Write(
                        action="checklist",
                        uuid=existing_repeat_remove,
                        title="Remove this checklist",
                        checklist_parent_uuid=existing_repeat,
                        checklist_status="done",
                        checklist_index=1024,
                    ),
                ]
            )
            converted_existing = _approved_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-repeat-convert-{existing_repeat}",
                        "change": [
                            {
                                "id": f"task:{existing_repeat}",
                                "if_revision": _revision(
                                    module, f"task:{existing_repeat}"
                                ),
                                "repeat": {
                                    "unit": "week",
                                    "interval": 2,
                                    "weekdays": ["monday", "friday"],
                                },
                                "into": f"project:{existing_repeat_project}",
                                "heading_id": f"heading:{existing_repeat_heading}",
                                "start": "today",
                                "deadline": repeat_deadline.isoformat(),
                                "remind_at": repeat_reminder.isoformat(),
                                "after": f"task:{existing_repeat_list_anchor}",
                                "today_after": f"task:{existing_repeat_today_anchor}",
                                "checklist_add": [
                                    {"key": "$new_step", "title": "Added step"}
                                ],
                                "checklist_change": [
                                    {
                                        "id": f"check:{existing_repeat_check}",
                                        "title": "Preserved and completed",
                                        "status": "completed",
                                    }
                                ],
                                "checklist_remove": [f"check:{existing_repeat_remove}"],
                                "checklist_order": [
                                    "$new_step",
                                    f"check:{existing_repeat_check}",
                                ],
                            }
                        ],
                    }
                ),
            )
            existing_template_record = next(
                item
                for item in library.records.values()
                if item.title == existing_repeat_title
                and item.recurrence.role == "template"
            )
            existing_template = existing_template_record.uuid
            owned[existing_template] = existing_template_record.entity or "Task6"
            owned.pop(existing_repeat_remove)
            current_rows = library.records[existing_repeat].checklists
            template_rows = existing_template_record.checklists
            for row in [*current_rows, *template_rows]:
                owned[row.uuid] = "ChecklistItem3"
            _proof(
                library.records[existing_repeat].recurrence.template_uuid
                == existing_template
                and {item.id for item in converted_existing.items}
                == {
                    f"task:{existing_repeat}",
                    f"task:{existing_template}",
                }
                and library.records[existing_repeat].notes == "Preserve this note"
                and existing_template_record.notes == "Preserve this note"
                and all(
                    record.parent_uuid == existing_repeat_project
                    and record.heading_uuid == existing_repeat_heading
                    and record.start == repeat_today
                    and record.deadline == repeat_deadline
                    and record.remind == "09:30"
                    and record.sort_index
                    > library.records[existing_repeat_list_anchor].sort_index
                    and record.today_index
                    > library.records[existing_repeat_today_anchor].today_index
                    for record in (
                        library.records[existing_repeat],
                        existing_template_record,
                    )
                )
                and [row.title for row in current_rows]
                == ["Added step", "Preserved and completed"]
                and [row.status for row in current_rows] == ["open", "done"]
                and [row.title for row in template_rows]
                == ["Added step", "Preserved and completed"]
                and [row.status for row in template_rows] == ["open", "open"],
                "recurrence.convert_existing_task",
                results,
            )
            _approved_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-repeat-metadata-{existing_repeat}",
                        "change": [
                            {
                                "id": f"task:{existing_template}",
                                "if_revision": _revision(
                                    module, f"task:{existing_template}"
                                ),
                                "title": f"{prefix} future recurring",
                                "notes_markdown": "Future cycles",
                            },
                            {
                                "id": f"task:{existing_repeat}",
                                "if_revision": _revision(
                                    module, f"task:{existing_repeat}"
                                ),
                                "title": f"{prefix} current recurring",
                            },
                        ],
                    }
                ),
            )
            _proof(
                library.records[existing_template].title.endswith("future recurring")
                and library.records[existing_template].notes == "Future cycles"
                and library.records[existing_repeat].title.endswith(
                    "current recurring"
                ),
                "recurrence.change_template_and_current_metadata",
                results,
            )
            _approved_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-repeat-convert-stop-{existing_template}",
                        "change": [
                            {
                                "id": f"task:{existing_template}",
                                "if_revision": _revision(
                                    module, f"task:{existing_template}"
                                ),
                                "repeat": {"remove": True},
                            }
                        ],
                    }
                ),
            )
            for row in template_rows:
                owned.pop(row.uuid)
            owned.pop(existing_template)
            current_rows = library.records[existing_repeat].checklists
            library.apply(
                [
                    *[
                        Write(
                            action="checklist",
                            uuid=row.uuid,
                            checklist_parent_uuid=existing_repeat,
                            checklist_remove=True,
                        )
                        for row in current_rows
                    ],
                    Write(action="permanent_delete", uuid=existing_repeat),
                    Write(action="permanent_delete", uuid=existing_repeat_list_anchor),
                    Write(action="permanent_delete", uuid=existing_repeat_today_anchor),
                    Write(action="permanent_delete", uuid=existing_repeat_heading),
                    Write(action="permanent_delete", uuid=existing_repeat_project),
                ]
            )
            owned.pop(existing_repeat)
            owned.pop(existing_repeat_list_anchor)
            owned.pop(existing_repeat_today_anchor)
            owned.pop(existing_repeat_heading)
            owned.pop(existing_repeat_project)
            for row in current_rows:
                owned.pop(row.uuid)

            # Recurrence: create a template and current generated copy atomically.
            recurring_title = f"{prefix} recurring"
            _approved_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-repeat-create-{new_uuid()}",
                        "create": [
                            {
                                "title": recurring_title,
                                "repeat": {"unit": "day", "interval": 1},
                            }
                        ],
                    }
                ),
            )
            template_record = next(
                item
                for item in library.records.values()
                if item.title == recurring_title and item.recurrence.role == "template"
            )
            instance_record = next(
                item
                for item in library.records.values()
                if item.title == recurring_title and item.recurrence.role == "instance"
            )
            template = template_record.uuid
            instance = instance_record.uuid
            owned[template] = template_record.entity or "Task6"
            owned[instance] = instance_record.entity or "Task6"
            _proof(
                library.records[instance].recurrence.template_uuid == template
                and library.records[template].recurrence.role == "template",
                "recurrence.create_template_and_instance",
                results,
            )
            template_inspection = module.read(
                ReadCall(purpose="recurrence", id=f"task:{template}")
            )
            instance_inspection = module.read(
                ReadCall(purpose="recurrence", id=f"task:{instance}")
            )
            _proof(
                template_inspection.status == "ok"
                and instance_inspection.status == "ok"
                and "recurrence_relationship_verified"
                in template_inspection.signals
                and "recurrence_relationship_verified"
                in instance_inspection.signals
                and len(template_inspection.items) == 1
                and len(instance_inspection.items) == 1
                and template_inspection.items[0].recurrence is not None
                and instance_inspection.items[0].recurrence is not None
                and f"task:{instance}"
                in template_inspection.items[0].recurrence.linked_item_ids
                and instance_inspection.items[0].recurrence.template_id
                == f"task:{template}",
                "recurrence.inspect_relationship",
                results,
            )
            _approved_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-repeat-mode-{template}",
                        "change": [
                            {
                                "id": f"task:{template}",
                                "if_revision": _revision(module, f"task:{template}"),
                                "repeat": {"mode": "after_completion"},
                            }
                        ],
                    }
                ),
            )
            _approved_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-repeat-rule-{template}",
                        "change": [
                            {
                                "id": f"task:{template}",
                                "if_revision": _revision(module, f"task:{template}"),
                                "repeat": {
                                    "mode": "fixed",
                                    "unit": "week",
                                    "interval": 2,
                                    "weekdays": ["monday", "thursday"],
                                },
                            }
                        ],
                    }
                ),
            )
            changed_rule = library.records[template].recurrence.rule
            _proof(
                changed_rule is not None
                and changed_rule.get("tp") == 0
                and changed_rule.get("fu") == 256
                and changed_rule.get("fa") == 2
                and changed_rule.get("of") == [{"wd": 1}, {"wd": 4}],
                "recurrence.change_full_rule",
                results,
            )
            edited = module.commit(
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-repeat-copy-edit-{instance}",
                        "change": [
                            {
                                "id": f"task:{instance}",
                                "if_revision": _revision(module, f"task:{instance}"),
                                "title": f"{prefix} generated edited",
                            }
                        ],
                    }
                )
            )
            _proof(
                edited.status == "applied"
                and library.records[instance].title.endswith("generated edited"),
                "recurrence.change_generated_copy",
                results,
            )
            completed = module.commit(
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-repeat-copy-complete-{instance}",
                        "change": [
                            {
                                "id": f"task:{instance}",
                                "if_revision": _revision(module, f"task:{instance}"),
                                "status": "completed",
                            }
                        ],
                    }
                )
            )
            _proof(
                completed.status == "applied"
                and library.records[instance].status == "done",
                "recurrence.complete_current_copy",
                results,
            )
            _approved_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-repeat-stop-{template}",
                        "change": [
                            {
                                "id": f"task:{template}",
                                "if_revision": _revision(module, f"task:{template}"),
                                "repeat": {"remove": True},
                            }
                        ],
                    }
                ),
            )
            _proof(
                template not in library.records
                and library.records[instance].recurrence.role == "none",
                "recurrence.remove_keep_copy",
                results,
            )
            owned.pop(template)
            for candidate in list(library.records.values()):
                if candidate.title.startswith(prefix) and "recurr" in candidate.title:
                    owned[candidate.uuid] = candidate.entity or "Task6"
                    library.apply(
                        [Write(action="permanent_delete", uuid=candidate.uuid)]
                    )
                    owned.pop(candidate.uuid)

            return results
        finally:
            _cleanup_probe_records(client, library, owned, prefix)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply-live-probes", action="store_true")
    args = parser.parse_args()
    if not args.apply_live_probes:
        parser.error("live Cloud writes need --apply-live-probes")
    print(json.dumps(run(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
