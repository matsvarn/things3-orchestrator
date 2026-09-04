from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
SKILL = ROOT / "plugin/skills/things-orchestrator/SKILL.md"
REFERENCES = SKILL.parent / "references"


def _frontmatter() -> dict[str, object]:
    source = SKILL.read_text()
    return yaml.safe_load(source.split("---", 2)[1])


def _all_text() -> str:
    return "\n".join(
        [SKILL.read_text(), *[path.read_text() for path in sorted(REFERENCES.glob("*.md"))]]
    )


def test_skill_metadata_matches_bounded_v2_work() -> None:
    metadata = _frontmatter()
    assert set(metadata) == {"name", "description"}
    assert metadata["name"] == "things-orchestrator"
    description = str(metadata["description"]).lower()
    assert "capture" in description
    assert "review" in description
    assert "trash" in description


def test_skill_names_exactly_the_default_eight_tools() -> None:
    names = set(re.findall(r"things_[a-z_]+", _all_text()))
    assert names == {
        "things_view", "things_find", "things_get", "things_capture",
        "things_update", "things_complete", "things_trash", "things_receipt",
    }


def test_skill_discloses_judgment_and_routine_trust_references() -> None:
    links = set(re.findall(r"\(references/([^)]+)\)", SKILL.read_text()))
    assert links == {"research.md", "form.md", "review.md", "routines.md"}


def test_skill_requires_opaque_idempotency_and_exact_retry() -> None:
    lower = _all_text().lower()
    assert "uuid or ulid" in lower
    assert "exact same" in lower
    assert "fresh request id" in lower or "fresh uuid" in lower


def test_skill_keeps_private_transaction_language_out() -> None:
    lower = _all_text().lower()
    for forbidden in (
        "things_read", "things_commit", "things_approve", "context_id",
        "if_revision", "scope_revision", "require_approval", "local key",
    ):
        assert forbidden not in lower


def test_skill_bounds_update_and_capture() -> None:
    lower = _all_text().lower()
    for field in ("title", "notes", "start", "deadline", "remind_at"):
        assert field in lower
    assert "nested" in lower
    assert "new project" in lower
    assert "omitted fields" in lower


def test_skill_routes_bounded_mutations_without_an_owner_flow() -> None:
    lower = _all_text().lower()
    assert "applies directly" in lower
    assert "exact same request id and arguments" in lower
    assert "owner flow" not in lower


def test_skill_keeps_pending_fenced_and_treats_partial_as_terminal() -> None:
    lower = _all_text().lower()
    assert "stop all writes" in lower
    assert "never replay" in lower
    assert "partial` is terminal" in lower
    assert "fresh request id" in lower


def test_skill_treats_things_text_as_untrusted_data() -> None:
    lower = _all_text().lower()
    assert "untrusted" in lower
    for control in ("tool instruction", "state", "action", "identifier", "approval", "disposition", "recovery"):
        assert control in lower


def test_deferred_features_are_not_advertised_as_available() -> None:
    lower = _all_text().lower()
    assert "not available" in lower or "deferred" in lower
    for feature in ("permanent deletion", "advanced scopes", "mutation coaching"):
        assert feature in lower
