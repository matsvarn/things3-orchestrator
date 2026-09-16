from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from things_orchestrator.journal import (
    IntentRecord,
    SQLiteJournal,
    _json,
    journal_path,
)


def test_sqlite_journal_survives_reopen_and_uses_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "state" / "things-orchestrator" / "journal.sqlite3"
    leftover = IntentRecord(
        intent_id="turn-20260815-renew-passport",
        fingerprint="sha256:request",
        state="pending",
        plan={"summary": "Merge Personal into Life", "writes": [{"id": "task-1"}]},
        plan_id="plan_pS7ExactIdentity",
        expires_at="2026-08-15T16:30:00+02:00",
    )
    SQLiteJournal(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """INSERT INTO intents (
                intent_id, fingerprint, state, plan_json,
                plan_id, expires_at, result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                leftover.intent_id,
                leftover.fingerprint,
                leftover.state,
                _json(leftover.plan),
                leftover.plan_id,
                leftover.expires_at,
                None,
            ),
        )

    assert SQLiteJournal(path).get(leftover.intent_id) == leftover
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600


def test_journal_path_is_private_to_one_normalized_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    alice = journal_path("Alice@Example.com")

    assert alice == journal_path(" alice@example.COM ")
    assert alice != journal_path("bob@example.com")
    assert "alice" not in alice.name.casefold()
