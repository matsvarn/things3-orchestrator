from __future__ import annotations

from things_orchestrator.interface import (
    ItemFact,
    PlanFact,
    Result,
    ReviewSection,
)
from things_orchestrator.owner_text import OWNER_TEXT_LIMIT, owner_text


def test_weekly_card_uses_section_counts_and_hides_plan_ids() -> None:
    text = owner_text(
        Result(
            next="done",
            status="ok",
            instruction=(
                "This review contains the exceptions and choices for Get Clear, "
                "Get Current, and Get Creative. Ask for uncaptured work."
            ),
            items=[
                ItemFact(id="task:one", kind="task", title="File invoice", status="open"),
                ItemFact(id="task:two", kind="task", title="Call bank", status="open"),
            ],
            sections=[
                ReviewSection(
                    key="get_clear",
                    title="Get Clear",
                    signals=["Inbox: 4.", "Ask for work that is not yet in Things."],
                ),
                ReviewSection(
                    key="get_current",
                    title="Get Current",
                    signals=[
                        "Stale starts: 2; overdue deadlines: 1; Waiting: 3.",
                    ],
                ),
                ReviewSection(
                    key="plan_week",
                    title="Plan the week, if requested",
                    signals=[
                        "Things load by day: 2026-08-22: 3; 2026-08-23: 1.",
                    ],
                ),
            ],
        )
    )
    assert text.startswith("**Weekly review.** 2 exception(s) need a decision.")
    assert "**Inbox:** 4" in text
    assert "**Stale starts:** 2" in text
    assert "**Overdue:** 1" in text
    assert "**Waiting:** 3" in text
    assert "**Load:** 22:3 23:1" in text
    assert "If Inbox is still above zero" in text
    assert "Ask about any line." in text
    assert len(text.splitlines()) <= 5
    assert "Get Clear" not in text
    assert "plan_" not in text


def test_approval_card_never_emits_plan_id() -> None:
    text = owner_text(
        Result(
            next="approve",
            status="needs_approval",
            instruction=(
                "Show one exact before-and-after manifest. "
                "Call things_approve only after a clear yes."
            ),
            plan=PlanFact(
                id="plan_abcdefghijkl",
                expires_at="2026-08-23T12:00:00+00:00",
                summary=[
                    'Move Task "Old draft" to recoverable Trash.',
                    "plan_abcdefghijkl must stay private.",
                ],
            ),
        )
    )
    assert text.startswith("**Needs confirmation.**")
    assert "Old draft" in text
    assert "plan_" not in text
    assert "If a line is wrong" in text
    assert "Ask about any line." in text
    assert len(text.splitlines()) <= 5


def test_needs_input_keeps_instruction() -> None:
    text = owner_text(
        Result(
            next="ask",
            status="needs_input",
            instruction="Two items match. Which invoice?",
        )
    )
    assert text == "Two items match. Which invoice?"


def test_rejected_keeps_instruction_for_repair() -> None:
    text = owner_text(
        Result(
            next="ask",
            status="rejected",
            instruction=(
                "Invalid tool request. start accepts today, evening, tomorrow, "
                "someday, an ISO date, or null to clear scheduling."
            ),
        )
    )
    assert "today, evening, tomorrow" in text
    assert text.startswith("Invalid tool request.")


def test_today_card_lists_titles_by_bucket() -> None:
    text = owner_text(
        Result(
            next="done",
            status="ok",
            instruction="Each into_id is the Area or Project home.",
            items=[
                ItemFact(
                    id="task:a",
                    kind="task",
                    title="Pay rent",
                    status="open",
                    signals=["overdue"],
                ),
                ItemFact(
                    id="task:b",
                    kind="task",
                    title="Water plants",
                    status="open",
                    signals=["evening"],
                ),
            ],
            sections=[
                ReviewSection(key="overdue", title="Overdue"),
                ReviewSection(key="evening", title="Evening"),
            ],
        )
    )
    assert text.startswith("**Today.** 2 item(s).")
    assert "**Overdue:** Pay rent" in text
    assert "**Evening:** Water plants" in text
    assert "Ask about any line." in text
    assert len(text.splitlines()) <= 5


def test_owner_text_caps_length() -> None:
    text = owner_text(
        Result(
            next="ask",
            status="rejected",
            instruction="x" * 1000,
        )
    )
    assert len(text) <= OWNER_TEXT_LIMIT
