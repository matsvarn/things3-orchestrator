from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOGFOOD = ROOT / "docs" / "dogfood.md"
WEEKLY_REVIEW_PROMPT = ROOT / "tests" / "fixtures" / "weekly_review_owner_prompt.txt"


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
    assert "Weekly review — Round 1 complete" in text
    assert "next first-round workflow is Routine capture" in text
    assert "Repeat required" in text


def test_next_dogfood_prompt_stays_natural() -> None:
    prompt = WEEKLY_REVIEW_PROMPT.read_text().strip()

    assert "weekly review" in prompt.casefold()
    assert "things_read" not in prompt
    assert "view=" not in prompt
    assert "approval" not in prompt.casefold()
