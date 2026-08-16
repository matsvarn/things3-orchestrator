from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
SKILL = ROOT / "plugin/skills/things-orchestrator/SKILL.md"
REFERENCES = SKILL.parent / "references"


def _agent_manifest(skill: Path) -> dict[str, object]:
    payload = yaml.safe_load(
        (skill.parent / "agents/openai.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(payload, dict)
    return payload


def _frontmatter(skill: Path) -> dict[str, str]:
    source = skill.read_text(encoding="utf-8")
    payload = yaml.safe_load(source.split("---", 2)[1])
    assert isinstance(payload, dict)
    return payload


def _skill_text() -> str:
    files = [SKILL, *sorted(REFERENCES.glob("*.md"))]
    return "\n".join(path.read_text(encoding="utf-8") for path in files)


def test_skill_is_model_invoked_with_matching_ui_metadata() -> None:
    main_meta = _frontmatter(SKILL)
    main = _agent_manifest(SKILL)

    assert set(main_meta) == {"name", "description"}
    assert main_meta["name"] == "things-orchestrator"
    assert "capture" in main_meta["description"]
    assert "review" in main_meta["description"]

    assert main["policy"] == {"allow_implicit_invocation": True}
    assert main["dependencies"] == {
        "tools": [
            {
                "type": "mcp",
                "value": "things",
                "description": "Things Orchestrator",
            }
        ]
    }

    interface = main["interface"]
    assert isinstance(interface, dict)
    assert 25 <= len(interface["short_description"]) <= 64
    assert f"${main_meta['name']}" in interface["default_prompt"]

    skill_dirs = {
        path.name
        for path in (ROOT / "plugin/skills").iterdir()
        if path.is_dir()
    }
    assert skill_dirs == {"things-orchestrator"}


def test_main_skill_selects_the_smallest_high_quality_things_form() -> None:
    source = SKILL.read_text(encoding="utf-8")
    lower = source.lower()

    assert "smallest useful form" in lower
    for native_form in (
        "task",
        "things checklist",
        "project",
        "markdown notes",
        "area",
        "start date",
        "deadline",
        "reminder",
        "tag",
        "someday",
    ):
        assert native_form in lower

    assert re.search(r"next\s+(?:one to three|1[^\w]3)\s+useful actions", lower)
    assert "visible action" in lower
    assert "finish criteria" in lower

    planning_stop = lower.split("stop planning", 1)[1].split("##", 1)[0]
    assert "start" in planning_stop
    assert "context" in planning_stop
    assert "finished" in planning_stop


def test_main_skill_preserves_owner_control_and_natural_language() -> None:
    source = SKILL.read_text(encoding="utf-8")
    lower = source.lower()

    assert "owner's words" in lower
    assert "natural things terms" in lower
    assert "preserve the owner's dates and importance" in lower
    assert "do not infer urgency" in lower
    assert "time-specific start cue" in lower
    assert "repeating template" in lower
    assert "generated copy for current work" in lower
    assert "stopping repetition" in lower
    assert "ask one short question" in lower
    assert "preserve anything the owner did not ask to change" in lower
    assert "ask one short question in the owner's words" in lower
    assert "keep the plan id" in lower
    assert "private" in lower

    internal_jargon = {
        "commitment",
        "outcome",
        "evidence label",
        "blueprint",
        "invariant",
        "domain outcome",
        "target registry",
    }
    assert not any(term in _skill_text().lower() for term in internal_jargon)


def test_skill_leaves_request_mechanics_to_the_mcp_interface() -> None:
    combined = _skill_text()
    tool_names = re.findall(r"things_[a-z_]+", combined)

    assert set(tool_names) == {"things_read", "things_commit", "things_approve"}
    assert len(tool_names) == 3
    assert "follow each returned" in combined.lower()

    mechanics = {
        "intent_id",
        "if_revision",
        "scope_revision",
        "plan_id",
        "retry_same",
        "move_contents_to",
        "remove_if_empty",
        "cursor",
    }
    assert not any(term in combined for term in mechanics)
    assert '{"' not in combined


def test_skill_discloses_only_the_three_distinct_judgment_branches() -> None:
    source = SKILL.read_text(encoding="utf-8")
    links = set(re.findall(r"\(references/([^)]+)\)", source))

    assert links == {"research.md", "reconcile.md", "task-system.md"}
    assert {path.name for path in REFERENCES.iterdir()} == links

    clarify = (REFERENCES / "task-system.md").read_text(encoding="utf-8").lower()
    review = (REFERENCES / "reconcile.md").read_text(encoding="utf-8").lower()
    research = (REFERENCES / "research.md").read_text(encoding="utf-8").lower()

    for concept in ("broad task", "vague project", "waiting", "someday", "area"):
        assert concept in clarify
    assert "one concise question" in clarify

    for concept in ("inbox", "next action", "duplicates", "waiting", "someday", "area"):
        assert concept in review
    assert "every reviewed item" in review
    assert "preserve all other work" in review

    for concept in ("direct", "sources", "uncertainty", "owner", "markdown notes", "tasks"):
        assert concept in research
    assert "every accepted things change" in research


def test_skill_teaches_the_proven_write_forms() -> None:
    lower = _skill_text().lower()

    for capability in (
        "rename or reorder a heading",
        "repeating template for future copies",
        "generated copy for the current cycle",
        "complete repeat rule",
        "batch both changes",
        "repair tag names and parent relationships",
        "restore accidental cleanup",
        "permanently delete",
        "rich notes",
        "lifecycle=trash",
        "ordinary task or project",
        "every active visible direct child",
    ):
        assert capability in lower


def test_skill_teaches_safe_delete_and_merge_forms() -> None:
    lower = _skill_text().lower()

    assert "lifecycle=trash` only for an ordinary task or project delete" in lower
    assert "set the source project to `lifecycle=trash` only" in lower
    assert "every active visible direct child" in lower
    assert "if completed, trashed, template, or hidden children exist" in lower
    assert "do not use atomic merge" in lower
    assert "choose separate safe cleanup" in lower
    assert "every permanent task or project deletion target must already be in trash" in lower
    assert "including tasks and empty projects" in lower
    assert "for a non-empty project, read it completely" in lower
    assert "lifecycle=delete_permanently` with `delete_contents=true`" in lower
    assert "then approve the plan" in lower
    assert "organize.delete_headings" in lower
    assert "change_tags.delete_permanently" in lower


def test_skill_teaches_the_contextual_short_path() -> None:
    lower = _skill_text().lower()

    for instruction in (
        "purpose=change",
        "purpose=organize",
        "context refs",
        "do not copy revisions",
        "editable draft",
        "ordered sections",
        "unlisted=keep",
        "batch related normal changes",
        "structured recovery",
        "current copy and template",
    ):
        assert instruction in lower

    assert "read once" in lower
    assert "one commit" in lower
    assert "rebuild once" in lower
    assert "response is lost" in lower
    assert "pending or unknown" in lower
    assert "no new facts" in lower
    assert "stale or expired" in lower


def test_skill_teaches_weak_model_selector_and_dependency_rules() -> None:
    lower = _skill_text().lower()

    for instruction in (
        "select one view",
        "only a project view uses `within`",
        "never combine a view with id or find",
        "search named existing items and edit them",
        "create only when asked to add",
        "search first",
        "purpose=recurrence",
        "purpose=change",
        "define local refs before use",
        "parent tags before children",
        "start=evening",
        "delete_headings",
        "never `lifecycle`",
    ):
        assert instruction in lower


def test_main_and_reference_files_stay_lean() -> None:
    assert len(SKILL.read_text(encoding="utf-8").splitlines()) < 70
    for reference in REFERENCES.glob("*.md"):
        assert len(reference.read_text(encoding="utf-8").splitlines()) < 40
