"""Run destructive, disposable proof cases against one Things Cloud account.

This script creates records with a unique ``__TO_PROBE__`` prefix. It removes
only those exact UUIDs. Run it only when you own the configured account.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from datetime import datetime, timezone
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
            child_tag = own("Tag4")
            tag_parent_title = f"{prefix} tag parent"
            tag_child_title = f"{prefix} tag child"
            created_tags = module.commit(
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-tag-create-{parent_tag}",
                        "ensure_tags": [
                            {"key": "$parent", "title": tag_parent_title},
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
            actual_child_tag = library.tag_uuid(tag_child_title)
            if actual_parent_tag is None or actual_child_tag is None:
                raise RuntimeError("live tag create did not return exact tags")
            owned.pop(parent_tag)
            owned.pop(child_tag)
            parent_tag, child_tag = actual_parent_tag, actual_child_tag
            owned[parent_tag] = "Tag4"
            owned[child_tag] = "Tag4"
            _proof(
                library.tag_parents.get(child_tag) == [parent_tag],
                "tag.create_hierarchy",
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
                                "parent_id": None,
                            }
                        ],
                    }
                ),
            )
            _proof(
                library.tags.get(child_tag) == renamed_tag_title
                and library.tag_parents.get(child_tag) == [],
                "tag.rename_reparent",
                results,
            )
            for tag_uuid in (child_tag, parent_tag):
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
            owned.pop(child_tag)
            owned.pop(parent_tag)

            # Headings and a non-empty Project lifecycle.
            project = own("Task6")
            heading_a = own("Task6")
            heading_b = own("Task6")
            project_task = own("Task6")
            checklist = own("ChecklistItem3")
            library.apply(
                [
                    Write(action="create", uuid=project, kind="project", title=f"{prefix} project"),
                    Write(
                        action="create_heading",
                        uuid=heading_a,
                        title=f"{prefix} heading A",
                        into_uuid=project,
                        into_kind="project",
                        sort_index=0,
                    ),
                    Write(
                        action="create_heading",
                        uuid=heading_b,
                        title=f"{prefix} heading B",
                        into_uuid=project,
                        into_kind="project",
                        sort_index=1024,
                    ),
                    Write(
                        action="create",
                        uuid=project_task,
                        title=f"{prefix} project task",
                        into_uuid=project,
                        into_kind="project",
                        heading_uuid=heading_a,
                    ),
                    Write(
                        action="checklist",
                        uuid=checklist,
                        title=f"{prefix} project checklist",
                        checklist_parent_uuid=project_task,
                        checklist_status="open",
                    ),
                ]
            )
            library.apply(
                [
                    Write(action="update", uuid=heading_b, sort_index=0),
                    Write(action="update", uuid=heading_a, sort_index=1024),
                ]
            )
            _proof(
                library.records[heading_b].sort_index < library.records[heading_a].sort_index,
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
                                "if_revision": _revision(module, f"heading:{heading_a}"),
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
            _approved_commit(
                module,
                CommitCall.model_validate(
                    {
                        "intent_id": f"probe-heading-delete-{heading_a}",
                        "change": [
                            {
                                "id": f"heading:{heading_a}",
                                "if_revision": _revision(module, f"heading:{heading_a}"),
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
            task = own("Task6")
            library.apply([Write(action="create", uuid=task, title=f"{prefix} lifecycle")])
            library.apply([Write(action="trash", uuid=task)])
            library.apply([Write(action="restore", uuid=task)])
            _proof(not library.records[task].trashed, "task.restore", results)
            library.apply([Write(action="trash", uuid=task)])
            library.apply([Write(action="permanent_delete", uuid=task)])
            _proof(task not in library.records, "task.purge", results)
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
                    lambda: rich in library.records
                    and library.records[rich].notes_format == "rich",
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
                if item.title == recurring_title
                and item.recurrence.role == "template"
            )
            instance_record = next(
                item
                for item in library.records.values()
                if item.title == recurring_title
                and item.recurrence.role == "instance"
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
            # Delete only UUIDs created by this run. This path is best-effort.
            try:
                library.refresh(force=True)
                for item in library.records.values():
                    if item.title.startswith(prefix):
                        owned.setdefault(item.uuid, item.entity or "Task6")
            except Exception:
                pass
            if owned:
                try:
                    client.commit(
                        [Envelope(uuid, 2, kind, {}) for uuid, kind in owned.items()]
                    )
                except Exception:
                    pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply-live-probes", action="store_true")
    args = parser.parse_args()
    if not args.apply_live_probes:
        parser.error("live Cloud writes need --apply-live-probes")
    print(json.dumps(run(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
