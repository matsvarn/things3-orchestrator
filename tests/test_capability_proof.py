from pathlib import Path

import pytest

from scripts.probe_cloud_capabilities import V2_CAPABILITY_KEYS, _unique_ids, bare_uuid

ROOT = Path(__file__).parents[1]


def test_native_parity_compares_the_uuid_independent_of_public_kind() -> None:
    assert bare_uuid("task:same") == bare_uuid("project:same") == "same"


def test_native_parity_rejects_duplicate_rows() -> None:
    with pytest.raises(RuntimeError, match="duplicate public Today IDs"):
        _unique_ids(["same", "same"], source="public", view="Today")


def test_capability_proof_names_the_v2_safety_gate() -> None:
    document = " ".join(
        (ROOT / "docs/capability-proof.md").read_text().casefold().split()
    )
    for key in V2_CAPABILITY_KEYS:
        assert key.casefold() in document
    assert "did not make live things cloud calls" in document
    assert "advanced project scopes" in document


def test_routines_owner_record_does_not_overstate_live_evidence() -> None:
    document = " ".join(
        (ROOT / "docs/capability-proof.md").read_text().casefold().split()
    )

    assert "reports one private vps result" in document
    assert "exact deployed commit sha" in document
    assert "grok client version" in document
    assert "installed skill state" in document
    assert "owner intervention details" in document
    assert "proves one private vps" not in document


def test_live_probe_is_read_only_v2_and_has_no_legacy_approval_path() -> None:
    source = (ROOT / "scripts/probe_cloud_capabilities.py").read_text()
    assert "--read-only-live-probe" in source
    assert "--native-parity" in source
    assert 'dispatch("things_view"' in source
    assert '"exact_id_match"' in source
    for forbidden in (
        "CommitCall",
        "ApproveCall",
        "things_commit",
        "things_approve",
        "permanent_delete",
        "library.apply",
    ):
        assert forbidden not in source


def test_public_contract_defers_advanced_mutation_surfaces() -> None:
    skill = (ROOT / "plugin/skills/things-orchestrator/SKILL.md").read_text().lower()
    owner = (ROOT / "docs/owner.md").read_text().lower()
    for text in (skill, owner):
        assert "advanced" in text
        assert "recurrence" in text
        assert "permanent" in text
