from scripts.probe_cloud_capabilities import PROBE_CAPABILITY_KEYS


def test_capability_proof_uses_structural_capability_to_probe_mapping() -> None:
    from pathlib import Path

    document = (Path(__file__).parents[1] / "docs" / "capability-proof.md").read_text()
    rows = {
        line.split("|", 2)[1].strip(): line
        for line in document.splitlines()
        if line.startswith("|") and line.count("|") >= 7
    }
    assert set(PROBE_CAPABILITY_KEYS) <= set(rows)
    for capability, keys in PROBE_CAPABILITY_KEYS.items():
        row = rows[capability]
        assert all(f"`{key}`" in row for key in keys)


def test_capability_proof_names_all_live_contextual_keys() -> None:
    from pathlib import Path

    document = (Path(__file__).parents[1] / "docs" / "capability-proof.md").read_text()

    assert "`ax.context_change`" in document
    assert "`ax.project_move_to_area`" in document
    assert "`ax.organize_draft`" in document
    assert "`ax.project_merge`" in document
    assert "`ax.project_merge_readback`" in document
    assert "`heading.clear_assignment`" in document
    assert "Capture a Task" in document
    assert "Checklist add, change, remove, order, and preservation" in document
    assert "Area registry create and Project-to-Area placement" in document
    assert "Editable Project organize drafts" in document
    assert "Exercised" in document
    assert "Defined" in document


def test_public_contract_does_not_advertise_system_organization_drafts() -> None:
    from pathlib import Path

    root = Path(__file__).parents[1]
    interface = (root / "src" / "things_orchestrator" / "interface.py").read_text()
    context = (root / "src" / "things_orchestrator" / "context.py").read_text()
    skill = (root / "plugin" / "skills" / "things-orchestrator" / "SKILL.md").read_text()
    adr = (root / "docs" / "adr" / "0002-contextual-desired-state.md").read_text()

    for text in (interface, context, skill, adr):
        assert "system structure" not in text
        assert "system layout" not in text
        assert "system-wide organization draft" not in text
    assert "view=system is the Area and Project registry" in interface
    assert "purpose=organize" in skill
    assert "one complete Project layout" in adr


def test_live_proof_uses_public_paths_for_lifecycle_and_heading_reorder() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[1] / "scripts" / "probe_cloud_capabilities.py").read_text()
    heading_block = source.split("# Headings and a non-empty Project lifecycle.", 1)[1].split(
        "# Rich structured note acceptance", 1
    )[0]
    task_block = heading_block.split("# Standalone Task lifecycle.", 1)[1]
    tag_block = source.split("# Tags: hierarchy", 1)[1].split(
        "# Public contextual path", 1
    )[0]

    assert "library.apply(" not in heading_block
    assert "library.apply(" not in task_block
    assert "module.commit(" in heading_block
    assert "_approved_commit(" in heading_block
    assert "module.read(" in heading_block
    assert "_applied_commit(" in task_block
    assert "_approved_commit(" in task_block
    assert "module.read(" in task_block
    assert '"parent_id": f"tag:{second_parent_tag}"' in tag_block
    assert '"parent_id": None' not in tag_block


def test_owner_guide_is_how_to_talk() -> None:
    from pathlib import Path

    root = Path(__file__).parents[1]
    owner = (root / "docs" / "owner.md").read_text()
    skill = (
        root / "plugin" / "skills" / "things-orchestrator" / "SKILL.md"
    ).read_text()

    assert "purpose=recurrence" not in owner
    assert "purpose=change" not in owner
    assert "contextual refs" not in owner
    assert "`purpose=recurrence`" in skill
    assert "current copy and template" in skill.lower()


def test_full_reorganization_prompt_is_a_release_behavior_gate() -> None:
    from pathlib import Path

    root = Path(__file__).parents[1]
    prompt = (root / "tests" / "fixtures" / "full_reorg_owner_prompt.txt").read_text()
    proof = (root / "docs" / "capability-proof.md").read_text().lower()

    assert prompt.startswith("Help me fully reorganize my Things.")
    assert "full reorganization behavior gate" in proof
    assert "one complete audit" in proof
    assert "one exact before-and-after manifest" in proof
    assert "one commit and one approval" in proof
    assert "final area order" in proof
    assert "known incoherent project" in proof
