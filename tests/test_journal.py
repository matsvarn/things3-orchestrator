from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from things_orchestrator.journal import (
    IntentRecord,
    IntentState,
    Journal,
    MemoryJournal,
    SQLiteJournal,
    journal_path,
)


def _record(state: IntentState = "needs_approval") -> IntentRecord:
    return IntentRecord(
        intent_id="turn-20260815-renew-passport",
        fingerprint="sha256:request",
        state=state,
        plan={"summary": "Merge Personal into Life", "writes": [{"id": "task-1"}]},
        plan_id="plan_pS7ExactIdentity",
        expires_at="2026-08-15T16:30:00+02:00",
    )


@pytest.mark.parametrize("journal_kind", ["memory", "sqlite"])
def test_journal_persists_and_finds_intent_and_plan(
    journal_kind: str, tmp_path: Path
) -> None:
    journal: Journal = (
        MemoryJournal()
        if journal_kind == "memory"
        else SQLiteJournal(tmp_path / "private" / "journal.sqlite3")
    )
    expected = _record()

    journal.save(expected)

    assert journal.get(expected.intent_id) == expected
    assert journal.get_by_plan_id(expected.plan_id or "") == expected
    assert journal.get("missing") is None
    assert journal.get_by_plan_id("missing") is None


@pytest.mark.parametrize("journal_kind", ["memory", "sqlite"])
def test_save_replaces_one_intent_atomically(journal_kind: str, tmp_path: Path) -> None:
    journal: Journal = (
        MemoryJournal()
        if journal_kind == "memory"
        else SQLiteJournal(tmp_path / "private" / "journal.sqlite3")
    )
    original = _record()
    settled = IntentRecord(
        intent_id=original.intent_id,
        fingerprint=original.fingerprint,
        state="applied",
        plan=original.plan,
        result={"outcome": "applied", "receipt": "r_123"},
    )

    journal.save(original)
    journal.save(settled)

    assert journal.get(original.intent_id) == settled
    assert journal.get_by_plan_id(original.plan_id or "") is None


def test_sqlite_journal_survives_reopen_and_uses_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "state" / "things-orchestrator" / "journal.sqlite3"
    SQLiteJournal(path).save(_record())

    assert SQLiteJournal(path).get(_record().intent_id) == _record()
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("journal_kind", ["memory", "sqlite"])
def test_plan_id_cannot_point_to_two_intents(journal_kind: str, tmp_path: Path) -> None:
    journal: Journal = (
        MemoryJournal()
        if journal_kind == "memory"
        else SQLiteJournal(tmp_path / "journal.sqlite3")
    )
    journal.save(_record())
    duplicate = IntentRecord(
        intent_id="turn-20260815-another-intent",
        fingerprint="sha256:another-request",
        state="needs_approval",
        plan_id=_record().plan_id,
    )

    with pytest.raises(ValueError, match="plan ID already exists"):
        journal.save(duplicate)

    assert journal.get(duplicate.intent_id) is None


@pytest.mark.parametrize("journal_kind", ["memory", "sqlite"])
def test_reserve_and_transition_have_first_writer_semantics(
    journal_kind: str, tmp_path: Path
) -> None:
    if journal_kind == "memory":
        first: Journal = MemoryJournal()
        second = first
    else:
        path = tmp_path / "journal.sqlite3"
        first = SQLiteJournal(path)
        second = SQLiteJournal(path)
    prepared = replace(_record(), state="prepared", plan_id=None, expires_at=None)
    competing = replace(prepared, fingerprint="sha256:different")

    assert first.reserve(prepared) == prepared
    assert second.reserve(competing) == prepared
    pending = replace(prepared, state="pending")
    assert second.transition(pending, expected="prepared") is True
    assert first.transition(replace(prepared, state="stale"), expected="prepared") is False
    assert (
        first.transition(
            replace(pending, fingerprint="sha256:different", state="applied"),
            expected="pending",
        )
        is False
    )
    assert first.get(prepared.intent_id) == pending


def test_journal_path_is_private_to_one_normalized_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    alice = journal_path("Alice@Example.com")

    assert alice == journal_path(" alice@example.COM ")
    assert alice != journal_path("bob@example.com")
    assert "alice" not in alice.name.casefold()
