from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOGFOOD = ROOT / "docs" / "dogfood.md"
WEEKLY_REVIEW_PROMPT = ROOT / "tests" / "fixtures" / "weekly_review_owner_prompt.txt"


def test_dogfood_register_queues_only_currently_supported_workflows() -> None:
    text = DOGFOOD.read_text()

    supported = (
        "First correct read",
        "Useful Inbox capture and refusal gate",
        "Named home and tag capture",
        "Ordinary Project capture",
        "Inbox processing",
        "Daily focus",
        "Exact changes and scheduling",
        "Recurrence lifecycle",
        "Tags, checklist, and Waiting",
        "Recoverable Trash",
        "Install, update, rollback, and recovery",
    )
    deferred = (
        "native heading deletion or Project merge",
        "restore from Trash or permanent deletion",
        "arbitrary rich-note replacement",
        "tag or other registry mutation",
        "focused Area redesign or other advanced scope editing",
        "empty-account or full-system setup",
    )

    for workflow in supported:
        assert f"**{workflow}. Queued for v0.10.1.**" in text
    for workflow in deferred:
        assert workflow in text


def test_dogfood_register_preserves_historical_runs() -> None:
    text = DOGFOOD.read_text()

    historical = (
        "Source-heavy Project capture",
        "Full reorganization",
        "Weekly review",
    )

    for workflow in historical:
        assert workflow in text


def test_dogfood_register_keeps_human_and_automated_proof_separate() -> None:
    text = DOGFOOD.read_text()

    assert "Automated tests and isolated model replays do not count" in text
    assert "Weekly review: round 1 complete" in text
    assert "primary event is one correct current read" in text
    assert "receipt and Cloud" in text
    assert "whether the Things skill was installed" in text
    assert "Repeat required" in text


def test_dogfood_records_bounded_routines_owner_acceptance() -> None:
    text = " ".join(DOGFOOD.read_text().split())

    for evidence in (
        "private VPS",
        "history phase reached `live`",
        "fresh untagged task",
        "exact `AI` tag directly",
        "one event",
        "fetched the selected task through MCP",
        "updated that task's notes",
        "zero candidates",
        "zero pending events",
        "one delivered event",
        "zero dead letters",
        "topology only",
        "does not include the exact deployed commit SHA",
        "Grok client version",
        "installed skill state",
        "owner intervention details",
    ):
        assert evidence in text
    assert "record proves owner" not in text


def test_next_dogfood_prompt_stays_natural() -> None:
    prompt = WEEKLY_REVIEW_PROMPT.read_text().strip()

    assert "weekly review" in prompt.casefold()
    assert "things_read" not in prompt
    assert "view=" not in prompt
    assert "approval" not in prompt.casefold()
