from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOGFOOD = ROOT / "docs" / "dogfood.md"


def test_dogfood_register_covers_each_owner_workflow() -> None:
    text = DOGFOOD.read_text()

    expected = (
        "Source-heavy Project capture",
        "Full reorganization",
        "Weekly review",
        "Routine capture and refusal gate",
        "Named home and tag capture",
        "Ordinary Project form",
        "Inbox processing",
        "Daily focus",
        "Exact change and scheduling",
        "Recurrence lifecycle",
        "Tags and Waiting",
        "Project organization and merge",
        "Trash, restore, permanent delete, and rich-note replacement",
        "Focused Area redesign",
        "New-system setup",
        "Install, update, rollback, and recovery",
    )

    for workflow in expected:
        assert workflow in text


def test_dogfood_register_keeps_human_and_automated_proof_separate() -> None:
    text = DOGFOOD.read_text()

    assert "Automated tests and isolated model replays do not count" in text
    assert "Weekly review — old contract completed" in text
    assert "This is the next workflow" in text
    assert "Repeat required" in text
