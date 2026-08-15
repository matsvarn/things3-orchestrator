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
    assert "generated copies unchanged" in lower
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


def test_skill_does_not_claim_unavailable_write_forms() -> None:
    lower = _skill_text().lower()

    unavailable_claims = (
        r"change\w*\s+(?:a\s+)?repeat rule",
    )
    assert not any(re.search(pattern, lower) for pattern in unavailable_claims)


def test_main_and_reference_files_stay_lean() -> None:
    assert len(SKILL.read_text(encoding="utf-8").splitlines()) < 70
    for reference in REFERENCES.glob("*.md"):
        assert len(reference.read_text(encoding="utf-8").splitlines()) < 40
