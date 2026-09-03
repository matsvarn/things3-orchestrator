from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import Event, Thread
from typing import Callable

import pytest

from things_orchestrator.cloud import CloudError
from things_orchestrator.journal import (
    IntentRecord,
    MemoryJournal,
    SQLiteJournal,
    V2ApplySession,
    V2Operation,
    _v2_sql_values,
    read_operation_state_counts,
    v2_manifest_hash,
    v2_manifest_is_valid,
)
from things_orchestrator.library import ApplyResult, MemoryLibrary, Record, Write
from things_orchestrator.owner_authority import (
    authorization_binding,
    enroll_owner_factor,
    host_escape,
    render_operation,
    verified_authorization,
    verify_owner_factor,
)
from things_orchestrator.v2 import SAFETY_POLICY_DIGEST, OperationDraft
from things_orchestrator.workspace import ThingsWorkspace


def _operation(
    operation_id: str,
    *,
    request_id: str,
    state: str = "pending",
) -> V2Operation:
    request_hash = "sha256:v1:" + sha256(request_id.encode()).hexdigest()
    expires_at = "2099-01-01T00:00:00+00:00" if state == "awaiting_owner" else None
    manifest = {
        "version": "v1",
        "account_id": "owner@example.com",
        "api_version": "2",
        "schema_version": "v2.0",
        "request_hash": request_hash,
        "tool": "things_capture",
        "preconditions": {},
        "writes": [
            {"action": "create", "uuid": "a", "kind": "task", "title": "A"}
        ],
        "touched": [["title"]],
        "before": [None],
        "display_titles": ["A"],
        "requires_owner": state == "awaiting_owner",
        "safety_policy_digest": "sha256:v1:policy",
        "expires_at": expires_at,
    }
    return V2Operation(
        account_id="owner@example.com",
        api_version="2",
        request_id=request_id,
        request_hash=request_hash,
        operation_id=operation_id,
        tool="things_capture",
        state=state,  # type: ignore[arg-type]
        manifest=manifest,
        manifest_hash=v2_manifest_hash(manifest),
        safety_policy_digest="sha256:v1:policy",
        expires_at=expires_at,
    )


def _with_manifest(operation: V2Operation, **changes: object) -> V2Operation:
    manifest = {**operation.manifest, **changes}
    if "tool" in changes:
        operation = replace(operation, tool=str(changes["tool"]))
    if "expires_at" in changes:
        operation = replace(operation, expires_at=str(changes["expires_at"]))
    manifest["tool"] = operation.tool
    manifest["account_id"] = operation.account_id
    manifest["api_version"] = operation.api_version
    manifest["request_hash"] = operation.request_hash
    manifest["safety_policy_digest"] = operation.safety_policy_digest
    manifest["expires_at"] = operation.expires_at
    return replace(
        operation,
        manifest=manifest,
        manifest_hash=v2_manifest_hash(manifest),
    )


def _inject_v2_operation(
    journal: MemoryJournal | SQLiteJournal, operation: V2Operation
) -> None:
    if isinstance(journal, MemoryJournal):
        journal._v2_operations[operation.operation_id] = operation  # noqa: SLF001
        journal._v2_times[operation.operation_id] = (  # noqa: SLF001
            "2020-01-01T00:00:00+00:00",
            None if operation.state in {"awaiting_owner", "pending"} else "2020-01-01T00:00:00+00:00",
        )
        return
    with sqlite3.connect(journal.path) as connection:
        connection.execute(
            "INSERT INTO owner_operations_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            _v2_sql_values(operation),
        )
        connection.execute(
            "INSERT INTO owner_operation_times_v2 VALUES (?,?,?)",
            (
                operation.operation_id,
                "2020-01-01T00:00:00+00:00",
                None if operation.state in {"awaiting_owner", "pending"} else "2020-01-01T00:00:00+00:00",
            ),
        )


def test_sqlite_creation_and_fence_claim_are_one_transaction(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    first = SQLiteJournal(path)
    second = SQLiteJournal(path)
    operation = _operation(
        "op_first",
        request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
    )
    assert first.create_v2(operation, claim_fence=True)[0] == "created"

    blocked = _operation(
        "op_second",
        request_id="0198f0ef-3923-79b6-96a8-2bf28eac0d67",
    )
    outcome, stored, blockers = second.create_v2(blocked, claim_fence=True)
    assert outcome == "blocked"
    assert stored is None
    assert blockers == ["op_first"]
    assert second.get_v2_request(blocked.account_id, blocked.api_version, blocked.request_id) is None


@pytest.mark.parametrize("journal_kind", ["memory", "sqlite"])
def test_case_only_relogin_preserves_v2_idempotency_and_pending_fence(
    journal_kind: str, tmp_path: Path
) -> None:
    journal = (
        MemoryJournal()
        if journal_kind == "memory"
        else SQLiteJournal(tmp_path / "journal.sqlite3")
    )
    original = replace(
        _operation(
            "op_original",
            request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
        ),
        account_id="Owner@Example.com",
    )
    original = _with_manifest(original)
    assert journal.create_v2(original, claim_fence=True)[0] == "created"

    retry = _operation(
        "op_duplicate",
        request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
    )
    outcome, stored, blockers = journal.create_v2(retry, claim_fence=True)

    assert outcome == "existing"
    assert stored == original
    assert stored.account_id == "Owner@Example.com"
    assert blockers == []
    assert journal.get_v2_request(
        "owner@example.com", original.api_version, original.request_id
    ) == original

    different = _operation(
        "op_blocked",
        request_id="0198f0ef-3923-79b6-96a8-2bf28eac0d67",
    )
    outcome, stored, blockers = journal.create_v2(different, claim_fence=True)

    assert outcome == "blocked"
    assert stored is None
    assert blockers == ["op_original"]


@pytest.mark.parametrize("journal_kind", ["memory", "sqlite"])
def test_duplicate_casefolded_requests_fail_closed_without_selecting_by_login(
    journal_kind: str, tmp_path: Path
) -> None:
    journal = (
        MemoryJournal()
        if journal_kind == "memory"
        else SQLiteJournal(tmp_path / "journal.sqlite3")
    )
    original = replace(
        _operation(
            "op_original",
            request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
        ),
        account_id="Owner@Example.com",
    )
    original = _with_manifest(original)
    assert journal.create_v2(original, claim_fence=True)[0] == "created"
    conflicting = replace(
        _operation(
            "op_conflicting",
            request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
        ),
        request_hash="sha256:conflicting-request",
    )
    conflicting = _with_manifest(conflicting)
    if isinstance(journal, MemoryJournal):
        journal._v2_operations[conflicting.operation_id] = conflicting
    else:
        with sqlite3.connect(journal.path) as connection:
            connection.execute(
                "INSERT INTO owner_operations_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                _v2_sql_values(conflicting),
            )

    for login in ("Owner@Example.com", "owner@example.com"):
        with pytest.raises(RuntimeError, match="ambiguous stored v2 request"):
            journal.get_v2_request(login, original.api_version, original.request_id)

    retry = _operation(
        "op_retry",
        request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
    )
    with pytest.raises(RuntimeError, match="ambiguous stored v2 request"):
        journal.create_v2(retry, claim_fence=True)


@pytest.mark.parametrize("journal_kind", ["memory", "sqlite"])
def test_operation_state_counts_are_aggregate_and_account_scoped(
    journal_kind: str, tmp_path: Path
) -> None:
    journal = (
        MemoryJournal()
        if journal_kind == "memory"
        else SQLiteJournal(tmp_path / "journal.sqlite3")
    )
    pending = _operation(
        "private-operation-id",
        request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
    )
    other_account = replace(
        _operation(
            "other-private-operation-id",
            request_id="0198f0ef-3923-79b6-96a8-2bf28eac0d67",
        ),
        account_id="other@example.com",
    )
    other_account = _with_manifest(other_account, account_id="other@example.com")
    journal.create_v2(pending, claim_fence=True)
    journal.create_v2(other_account, claim_fence=True)
    journal.save(
        IntentRecord(
            intent_id="private-legacy-id",
            fingerprint="private-fingerprint",
            state="stale",
        )
    )

    assert journal.operation_state_counts("owner@example.com") == (
        ("legacy.stale", 1),
        ("v2.pending", 1),
    )


def test_read_operation_state_counts_does_not_migrate_an_old_journal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE intents (intent_id TEXT PRIMARY KEY, state TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO intents VALUES ('private-id', 'pending')")
    before = path.read_bytes()

    assert read_operation_state_counts(path, "owner@example.com") == (
        ("legacy.pending", 1),
    )

    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert tables == {"intents"}


def test_sqlite_terminal_settlement_rolls_back_state_and_receipts_together(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    journal = SQLiteJournal(path)
    operation = _operation("op_atomic", request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735")
    assert journal.create_v2(operation, claim_fence=True)[0] == "created"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TRIGGER crash_receipt BEFORE INSERT ON owner_receipts_v2
               BEGIN SELECT RAISE(ABORT, 'injected crash'); END"""
        )
    rows = [{"sequence": 1, "action": "create", "target_id": "task:a", "desired": {}, "observed": {}, "result": "applied"}]
    try:
        journal.settle_v2("op_atomic", expected="pending", state="applied", response={"state": "applied"}, rows=rows)
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("receipt crash was not injected")
    stored = journal.get_v2_operation("op_atomic")
    assert stored is not None and stored.state == "pending"
    assert stored.response is None and stored.receipt_hash is None
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM owner_receipts_v2").fetchone() == (0,)


def test_sqlite_unchanged_settlement_rolls_back_state_with_receipts(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    journal = SQLiteJournal(path)
    operation = _operation(
        "op_unchanged", request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735"
    )
    assert journal.create_v2(operation, claim_fence=True)[0] == "created"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TRIGGER crash_unchanged_receipt BEFORE INSERT ON owner_receipts_v2
               BEGIN SELECT RAISE(ABORT, 'injected crash'); END"""
        )
    rows = [{"sequence": 1, "action": "update", "target_id": "task:a", "desired": {}, "observed": {}, "result": "unchanged"}]
    try:
        journal.settle_v2(
            operation.operation_id,
            expected="pending",
            state="unchanged",
            response={"state": "unchanged"},
            rows=rows,
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("receipt crash was not injected")
    stored = journal.get_v2_operation("op_unchanged")
    assert stored is not None and stored.state == "pending"
    assert stored.response is None and stored.receipt_hash is None


def test_unchanged_settlement_is_immediately_terminal_for_retention(tmp_path: Path) -> None:
    journal = SQLiteJournal(tmp_path / "journal.sqlite3")
    operation = _operation(
        "op_unchanged_retained",
        request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
    )
    rows = [{"sequence": 1, "action": "update", "target_id": "task:a", "desired": {}, "observed": {}, "result": "unchanged"}]
    assert journal.create_v2(operation, claim_fence=True)[0] == "created"
    assert journal.settle_v2(
        operation.operation_id,
        expected="pending",
        state="unchanged",
        response={"state": "unchanged"},
        rows=rows,
    )

    assert journal.prune_v2(now="2030-01-01T00:00:00+00:00") == 1
    tombstone = journal.get_v2_request(operation.account_id, operation.api_version, operation.request_id)
    assert tombstone is not None and tombstone.state == "unchanged"


def test_partial_settlement_is_terminal_receipted_and_nonblocking(tmp_path: Path) -> None:
    for journal in (
        MemoryJournal(),
        SQLiteJournal(tmp_path / "partial.sqlite3"),
    ):
        operation = _operation(
            f"op_partial_{type(journal).__name__}",
            request_id=(
                "0198f0ee-98d4-7bd5-91ba-8e76019b2735"
                if isinstance(journal, MemoryJournal)
                else "0198f0ef-3923-79b6-96a8-2bf28eac0d67"
            ),
        )
        rows = [{
            "sequence": 1,
            "action": "create",
            "target_id": "task:a",
            "desired": {"title": "A"},
            "observed": {"title": "Different"},
            "result": "not_applied",
        }]
        assert journal.create_v2(operation, claim_fence=True)[0] == "created"
        assert journal.settle_v2(
            operation.operation_id,
            expected="pending",
            state="partial",
            response={
                "state": "partial",
                "code": "partial",
                "next_action": "read_receipt",
                "instruction": "Use a fresh current-state correction; never replay.",
                "operation_id": operation.operation_id,
            },
            rows=rows,
        )
        assert journal.blocking_v2_operations(operation.account_id) == []
        receipt = journal.v2_receipt_page(
            operation.account_id, operation.operation_id, limit=10
        )
        assert receipt.rows == rows
        assert receipt.receipt_hash


@pytest.mark.parametrize("cutover", [False, True], ids=["prune", "cutover"])
def test_legacy_awaiting_owner_rows_retire_without_replay_for_both_journals(
    tmp_path: Path, cutover: bool
) -> None:
    for journal in (
        MemoryJournal(),
        SQLiteJournal(tmp_path / f"retire-{cutover}.sqlite3"),
    ):
        operation = _operation(
            f"op_retire_{type(journal).__name__}_{cutover}",
            request_id=(
                "0198f0ee-98d4-7bd5-91ba-8e76019b2735"
                if isinstance(journal, MemoryJournal)
                else "0198f0ef-3923-79b6-96a8-2bf28eac0d67"
            ),
            state="awaiting_owner",
        )
        assert journal.create_v2(operation, claim_fence=False)[0] == "created"
        if cutover:
            journal.cutover_v1()
            journal.cutover_v1()
        else:
            journal.prune_v2(now="2026-09-01T00:00:00+00:00")
            journal.prune_v2(now="2026-09-01T00:00:00+00:00")
        retired = journal.get_v2_operation(operation.operation_id)
        assert retired is not None and retired.state == "stale"
        assert retired.response is not None
        assert retired.response["next_action"] == "read_fresh"
        instruction = str(retired.response["instruction"])
        assert "without Cloud I/O" in instruction
        assert "Never replay" in instruction
        assert "fresh request" in instruction


def test_only_legal_v2_transitions_are_accepted() -> None:
    journal = MemoryJournal()
    operation = _operation(
        "op_pending",
        request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
    )
    journal.create_v2(operation, claim_fence=True)
    rows = [{"sequence": 1, "action": "create", "target_id": "task:a", "desired": {}, "observed": {}, "result": "applied"}]
    assert journal.settle_v2("op_pending", expected="pending", state="applied", response={"state": "applied"}, rows=rows)
    assert not journal.transition_v2("op_pending", expected="applied", state="pending")


def test_settlement_cannot_bypass_approval_or_fence(tmp_path: Path) -> None:
    for journal in (MemoryJournal(), SQLiteJournal(tmp_path / "journal.sqlite3")):
        operation = _operation("op_awaiting", request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735", state="awaiting_owner")
        journal.create_v2(operation, claim_fence=False)
        assert not journal.settle_v2(
            operation.operation_id,
            expected="awaiting_owner",
            state="pending",
            response={"state": "pending"},
            rows=[],
        )
        assert journal.get_v2_operation(operation.operation_id).state == "awaiting_owner"  # type: ignore[union-attr]


def test_journal_owns_v2_initial_and_pending_lifecycle(tmp_path: Path) -> None:
    for journal in (MemoryJournal(), SQLiteJournal(tmp_path / "journal.sqlite3")):
        invalid = replace(_operation("op_invalid", request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735"), state="applied")
        try:
            journal.create_v2(invalid, claim_fence=False)
        except ValueError:
            pass
        else:
            raise AssertionError("terminal initial state was accepted")
        unchanged = replace(invalid, state="unchanged")
        try:
            journal.create_v2(unchanged, claim_fence=False)
        except ValueError:
            pass
        else:
            raise AssertionError("unchanged without atomic receipts was accepted")
        pending = _operation("op_pending_owned", request_id="0198f0ef-3923-79b6-96a8-2bf28eac0d67")
        with pytest.raises(ValueError, match="cannot preseed receipt rows"):
            journal.create_v2(
                pending,
                claim_fence=True,
                receipt_rows=[{"sequence": 1, "result": "applied"}],
            )
        journal.create_v2(pending, claim_fence=True)
        with pytest.raises(ValueError, match="one receipt row per manifest write"):
            journal.settle_v2(
                pending.operation_id, expected="pending", state="applied",
                response={"state": "applied"}, rows=[],
            )
        with pytest.raises(ValueError, match="one receipt row per manifest write"):
            journal.settle_v2(
                pending.operation_id, expected="pending", state="applied",
                response={"state": "applied"},
                rows=[{"sequence": 1}, {"sequence": 2}],
            )
        assert not journal.transition_v2(pending.operation_id, expected="pending", state="applied")
        assert journal.get_v2_operation(pending.operation_id).state == "pending"  # type: ignore[union-attr]


def test_unchanged_settlement_requires_one_receipt_per_manifest_write(tmp_path: Path) -> None:
    for index, journal in enumerate((MemoryJournal(), SQLiteJournal(tmp_path / "journal.sqlite3"))):
        operation = _operation(
            f"op_unchanged_{index}",
            request_id=f"0198f0ee-98d4-7bd5-91ba-8e76019b273{index}",
        )
        assert journal.create_v2(operation, claim_fence=True)[0] == "created"
        for rows in ([], [{"sequence": 1}, {"sequence": 2}]):
            with pytest.raises(ValueError, match="one receipt row per manifest write"):
                journal.settle_v2(
                    operation.operation_id,
                    expected="pending",
                    state="unchanged",
                    response={"state": "unchanged"},
                    rows=rows,
                )
        stored = journal.get_v2_operation(operation.operation_id)
        assert stored is not None and stored.state == "pending"


def test_approval_transition_rejects_strings_and_accepts_verified_capability(tmp_path: Path) -> None:
    factor = tmp_path / "owner-factor.json"
    enroll_owner_factor("correct horse battery staple", path=factor)
    journal = MemoryJournal(owner_public_key=factor.with_name("owner-public-key.ed25519").read_bytes())
    operation = _operation(
        "op_approval",
        request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
        state="awaiting_owner",
    )
    journal.create_v2(operation, claim_fence=False)
    assert journal.authorize_v2("op_approval", "sha256:binding") == (False, [])
    assert journal.get_v2_operation("op_approval").state == "awaiting_owner"  # type: ignore[union-attr]
    authorization = verified_authorization(
        operation,
        action="approve",
        passphrase="correct horse battery staple",
        path=factor,
    )
    assert authorization is not None
    authorized, blockers = journal.authorize_v2("op_approval", authorization)
    assert authorized is True
    assert blockers == []
    stored = journal.get_v2_operation("op_approval")
    assert stored is not None
    assert stored.state == "pending"
    assert stored.authorization == authorization.record
    assert stored.authorization.startswith("ed25519:v1:")


def test_current_policy_partial_is_terminal_without_owner_resolution() -> None:
    journal = MemoryJournal()
    operation = _operation(
        "op_terminal_partial",
        request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
    )
    operation = replace(operation, safety_policy_digest=SAFETY_POLICY_DIGEST)
    operation = _with_manifest(operation)
    assert journal.create_v2(operation, claim_fence=True)[0] == "created"
    assert journal.settle_v2(
        operation.operation_id,
        expected="pending",
        state="partial",
        response={"state": "partial"},
        rows=[{"sequence": 1}],
    )

    workspace = ThingsWorkspace(
        MemoryLibrary([]),
        journal=journal,
        account_id="owner@example.com",
    )
    assert not workspace.host_resolve_partial_v2(
        operation.operation_id,
        "accepted_as_is",
        object(),
    )
    stored = journal.get_v2_operation(operation.operation_id)
    assert stored is not None and stored.state == "partial"


def test_authorization_rejects_wrong_key_and_altered_binding(tmp_path: Path) -> None:
    first = tmp_path / "first" / "owner-factor.json"
    second = tmp_path / "second" / "owner-factor.json"
    enroll_owner_factor("correct horse battery staple", path=first)
    enroll_owner_factor("another correct battery phrase", path=second)
    operation = _operation(
        "op_signed",
        request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
        state="awaiting_owner",
    )
    authorization = verified_authorization(
        operation,
        action="approve",
        passphrase="correct horse battery staple",
        path=first,
    )
    assert authorization is not None
    correct = MemoryJournal(
        owner_public_key=first.with_name("owner-public-key.ed25519").read_bytes()
    )
    wrong = MemoryJournal(
        owner_public_key=second.with_name("owner-public-key.ed25519").read_bytes()
    )
    assert correct.verify_v2_authorization(operation, "approve", authorization)
    assert wrong.verify_v2_authorization(operation, "approve", authorization) is None
    assert correct.verify_v2_authorization(operation, "decline", authorization) is None
    assert correct.verify_v2_authorization(
        replace(operation, manifest_hash="sha256:v1:changed"),
        "approve",
        authorization,
    ) is None
    assert correct.verify_v2_authorization(
        replace(operation, expires_at="2026-08-29T13:01:00+00:00"),
        "approve",
        authorization,
    ) is None
    assert correct.verify_v2_authorization(operation, "approve", object()) is None


def test_sqlite_approval_rejects_manifest_json_tampering(tmp_path: Path) -> None:
    factor = tmp_path / "owner-factor.json"
    enroll_owner_factor("correct horse battery staple", path=factor)
    journal_path = tmp_path / "journal.sqlite3"
    journal = SQLiteJournal(
        journal_path,
        owner_public_key=factor.with_name("owner-public-key.ed25519").read_bytes(),
    )
    operation = _with_manifest(
        _operation(
            "op_manifest_tamper",
            request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
            state="awaiting_owner",
        ),
        expires_at="2099-01-01T00:00:00+00:00",
        tool="things_trash",
        writes=[{"action": "trash", "uuid": "a", "kind": "task"}],
        before=[{"id": "task:a", "trashed": False}],
        touched=[["trashed"]],
        preconditions={},
        display_titles=["A"],
        requires_owner=True,
    )
    assert journal.create_v2(operation, claim_fence=False)[0] == "created"
    authorization = verified_authorization(
        operation,
        action="approve",
        passphrase="correct horse battery staple",
        path=factor,
    )
    assert authorization is not None
    tampered = {
        **operation.manifest,
        "writes": [
            {
                "action": "complete",
                "uuid": "a",
                "kind": "task",
                "status": "done",
            }
        ],
        "touched": [["status"]],
    }
    with sqlite3.connect(journal_path) as connection:
        connection.execute(
            "UPDATE owner_operations_v2 SET manifest_json=? WHERE operation_id=?",
            (
                json.dumps(tampered, separators=(",", ":"), sort_keys=True),
                operation.operation_id,
            ),
        )
    library = MemoryLibrary([Record(uuid="a", kind="task", title="A")])
    workspace = ThingsWorkspace(
        library,
        journal=journal,
        account_id=operation.account_id,
    )
    stored = journal.get_v2_operation(operation.operation_id)
    assert stored is not None
    assert not v2_manifest_is_valid(stored)
    with pytest.raises(ValueError, match="integrity"):
        render_operation(stored)
    assert journal.verify_v2_authorization(stored, "approve", authorization) is None
    direct_apply = workspace._apply_v2(stored)  # noqa: SLF001
    reconcile = workspace.host_reconcile_v2(operation.operation_id)

    result = workspace.host_approve_v2(operation.operation_id, authorization)

    assert direct_apply["state"] == "rejected"
    assert reconcile["state"] == "rejected"
    assert result["state"] == "rejected"
    assert library.records["a"].status == "open"


def test_sqlite_approval_rejects_api_version_downgrade_bypass(tmp_path: Path) -> None:
    factor = tmp_path / "owner-factor.json"
    enroll_owner_factor("correct horse battery staple", path=factor)
    journal_path = tmp_path / "journal.sqlite3"
    journal = SQLiteJournal(
        journal_path,
        owner_public_key=factor.with_name("owner-public-key.ed25519").read_bytes(),
    )
    operation = _with_manifest(
        _operation(
            "op_version_downgrade",
            request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
            state="awaiting_owner",
        ),
        tool="things_trash",
        writes=[{"action": "trash", "uuid": "a", "kind": "task"}],
        before=[{"id": "task:a", "trashed": False}],
        touched=[["trashed"]],
        preconditions={},
        display_titles=["A"],
        requires_owner=True,
    )
    assert journal.create_v2(operation, claim_fence=False)[0] == "created"
    authorization = verified_authorization(
        operation,
        action="approve",
        passphrase="correct horse battery staple",
        path=factor,
    )
    assert authorization is not None
    tampered = {
        **operation.manifest,
        "writes": [
            {
                "action": "complete",
                "uuid": "a",
                "kind": "task",
                "status": "done",
            }
        ],
        "touched": [["status"]],
    }
    with sqlite3.connect(journal_path) as connection:
        connection.execute(
            """UPDATE owner_operations_v2
               SET api_version='legacy-v1', manifest_json=?
               WHERE operation_id=?""",
            (
                json.dumps(tampered, separators=(",", ":"), sort_keys=True),
                operation.operation_id,
            ),
        )
    library = MemoryLibrary([Record(uuid="a", kind="task", title="A")])
    workspace = ThingsWorkspace(
        library,
        journal=journal,
        account_id=operation.account_id,
    )

    result = workspace.host_approve_v2(operation.operation_id, authorization)

    assert result["state"] == "rejected"
    assert library.records["a"].status == "open"


def test_workspace_direct_approval_cannot_substitute_an_arbitrary_string() -> None:
    journal = MemoryJournal()
    operation = _operation(
        "op_direct",
        request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
        state="awaiting_owner",
    )
    journal.create_v2(operation, claim_fence=False)
    assert not journal.transition_v2(
        operation.operation_id,
        expected="awaiting_owner",
        state="pending",
        authorization="sha256:forged",
    )
    assert not journal.transition_v2(
        operation.operation_id,
        expected="awaiting_owner",
        state="declined",
        authorization=object(),
    )
    workspace = ThingsWorkspace(
        MemoryLibrary(),
        journal=journal,
        account_id=operation.account_id,
    )

    result = workspace.host_approve_v2(operation.operation_id, "sha256:forged")

    assert result["state"] == "rejected"
    assert journal.get_v2_operation(operation.operation_id).state == "awaiting_owner"  # type: ignore[union-attr]


def test_owner_approval_rejects_ambiguous_canonical_request_before_cloud_io(
    tmp_path: Path,
) -> None:
    class CountingLibrary(MemoryLibrary):
        refreshes = 0
        apply_calls = 0

        def refresh(self, *, force: bool = False) -> None:
            self.refreshes += 1

        def apply(self, writes: list[Write]) -> ApplyResult:
            self.apply_calls += 1
            return super().apply(writes)

    factor = tmp_path / "owner-factor.json"
    enroll_owner_factor("correct horse battery staple", path=factor)
    journal = MemoryJournal(
        owner_public_key=factor.with_name("owner-public-key.ed25519").read_bytes()
    )
    original = replace(
        _operation(
            "op_original",
            request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
            state="awaiting_owner",
        ),
        account_id="Owner@Example.com",
    )
    original = _with_manifest(original)
    assert journal.create_v2(original, claim_fence=False)[0] == "created"
    conflicting = replace(
        _operation(
            "op_conflicting",
            request_id=original.request_id,
            state="awaiting_owner",
        ),
        request_hash="sha256:conflicting-request",
    )
    conflicting = _with_manifest(conflicting)
    journal._v2_operations[conflicting.operation_id] = conflicting  # noqa: SLF001
    authorization = verified_authorization(
        original,
        action="approve",
        passphrase="correct horse battery staple",
        path=factor,
    )
    assert authorization is not None
    library = CountingLibrary()
    workspace = ThingsWorkspace(
        library,
        journal=journal,
        account_id="Owner@Example.com",
    )

    result = workspace.host_approve_v2(original.operation_id, authorization)

    assert result == {
        "state": "rejected",
        "instruction": "Conflicting stored operations share this request_id.",
    }
    assert library.refreshes == 0
    assert library.apply_calls == 0
    stored = journal.get_v2_operation(original.operation_id)
    assert stored is not None and stored.state == "awaiting_owner"


@pytest.mark.parametrize("journal_kind", ["memory", "sqlite"])
def test_journal_authorization_rechecks_canonical_ambiguity_atomically(
    journal_kind: str, tmp_path: Path
) -> None:
    factor = tmp_path / "owner-factor.json"
    enroll_owner_factor("correct horse battery staple", path=factor)
    public_key = factor.with_name("owner-public-key.ed25519").read_bytes()
    journal = (
        MemoryJournal(owner_public_key=public_key)
        if journal_kind == "memory"
        else SQLiteJournal(tmp_path / "journal.sqlite3", owner_public_key=public_key)
    )
    original = replace(
        _operation(
            "op_original",
            request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
            state="awaiting_owner",
        ),
        account_id="Owner@Example.com",
    )
    original = _with_manifest(original)
    assert journal.create_v2(original, claim_fence=False)[0] == "created"
    conflicting = replace(
        _operation(
            "op_conflicting",
            request_id=original.request_id,
            state="awaiting_owner",
        ),
        request_hash="sha256:conflicting-request",
    )
    conflicting = _with_manifest(conflicting)
    if isinstance(journal, MemoryJournal):
        journal._v2_operations[conflicting.operation_id] = conflicting  # noqa: SLF001
    else:
        with sqlite3.connect(journal.path) as connection:
            connection.execute(
                "INSERT INTO owner_operations_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                _v2_sql_values(conflicting),
            )
    authorization = verified_authorization(
        original,
        action="approve",
        passphrase="correct horse battery staple",
        path=factor,
    )
    assert authorization is not None

    assert journal.authorize_v2(original.operation_id, authorization) == (False, [])
    stored = journal.get_v2_operation(original.operation_id)
    assert stored is not None and stored.state == "awaiting_owner"


@pytest.mark.parametrize("journal_kind", ["memory", "sqlite"])
@pytest.mark.parametrize("mutation", ["transition", "settle"])
def test_every_journal_mutation_rechecks_canonical_ambiguity(
    journal_kind: str, mutation: str, tmp_path: Path
) -> None:
    journal = (
        MemoryJournal()
        if journal_kind == "memory"
        else SQLiteJournal(tmp_path / f"{mutation}.sqlite3")
    )
    state = "awaiting_owner" if mutation == "transition" else "pending"
    original = _with_manifest(
        replace(
            _operation(
                "op_original",
                request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
                state=state,
            ),
            account_id="Owner@Example.com",
        )
    )
    assert journal.create_v2(
        original, claim_fence=state == "pending"
    )[0] == "created"
    conflicting = _with_manifest(
        replace(
            _operation(
                "op_conflicting",
                request_id=original.request_id,
                state=state,
            ),
            request_hash="sha256:conflicting-request",
        )
    )
    _inject_v2_operation(journal, conflicting)

    if mutation == "transition":
        changed = journal.transition_v2(
            original.operation_id,
            expected="awaiting_owner",
            state="stale",
            response={"state": "stale"},
        )
    else:
        changed = journal.settle_v2(
            original.operation_id,
            expected="pending",
            state="applied",
            response={"state": "applied"},
            rows=[{"sequence": 1, "result": "applied"}],
        )

    assert changed is False
    assert journal.get_v2_operation(original.operation_id) == original
    assert journal.get_v2_operation(conflicting.operation_id) == conflicting


@pytest.mark.parametrize("journal_kind", ["memory", "sqlite"])
def test_prune_preserves_active_tombstone_ambiguity_without_mutation(
    journal_kind: str, tmp_path: Path
) -> None:
    journal = (
        MemoryJournal()
        if journal_kind == "memory"
        else SQLiteJournal(tmp_path / "prune.sqlite3")
    )
    original = _with_manifest(
        replace(
            _operation(
                "op_original",
                request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
            ),
            account_id="Owner@Example.com",
        )
    )
    assert journal.create_v2(original, claim_fence=True)[0] == "created"
    assert journal.settle_v2(
        original.operation_id,
        expected="pending",
        state="applied",
        response={"state": "applied"},
        rows=[{"sequence": 1, "result": "applied"}],
    )
    assert journal.prune_v2(now="2030-01-01T00:00:00+00:00") == 1
    conflicting = _with_manifest(
        replace(
            _operation(
                "op_conflicting",
                request_id=original.request_id,
                state="applied",
            ),
            request_hash="sha256:conflicting-request",
            response={"state": "applied"},
        )
    )
    _inject_v2_operation(journal, conflicting)
    before_bytes = journal.path.read_bytes() if isinstance(journal, SQLiteJournal) else None
    before_active = journal.get_v2_operation(conflicting.operation_id)

    with pytest.raises(RuntimeError, match="ambiguous stored v2 request"):
        journal.prune_v2(now="2031-01-01T00:00:00+00:00")

    assert journal.get_v2_operation(conflicting.operation_id) == before_active
    if isinstance(journal, SQLiteJournal):
        assert journal.path.read_bytes() == before_bytes
        with sqlite3.connect(journal.path) as connection:
            assert connection.execute(
                "SELECT count(*) FROM owner_operations_v2"
            ).fetchone() == (1,)
            assert connection.execute(
                "SELECT count(*) FROM owner_tombstones_v2"
            ).fetchone() == (1,)
    else:
        assert len(journal._v2_operations) == 1  # noqa: SLF001
        assert len(journal._v2_tombstones) == 1  # noqa: SLF001


def test_host_approval_rejects_conflict_inserted_after_authorization(
    tmp_path: Path,
) -> None:
    class RacingJournal(MemoryJournal):
        def authorize_v2(
            self,
            operation_id: str,
            authorization: object,
            *,
            now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        ) -> tuple[bool, list[str]]:
            result = super().authorize_v2(
                operation_id, authorization, now=now
            )
            if result[0]:
                original = self._v2_operations[operation_id]  # noqa: SLF001
                conflicting = _with_manifest(
                    replace(
                        original,
                        account_id=original.account_id.lower(),
                        operation_id="op_conflicting",
                        request_hash="sha256:conflicting-request",
                    )
                )
                _inject_v2_operation(self, conflicting)
            return result

    class CountingLibrary(MemoryLibrary):
        apply_calls = 0

        def apply(self, writes: list[Write]) -> ApplyResult:
            self.apply_calls += 1
            return super().apply(writes)

    factor = tmp_path / "owner-factor.json"
    enroll_owner_factor("correct horse battery staple", path=factor)
    journal = RacingJournal(
        owner_public_key=factor.with_name("owner-public-key.ed25519").read_bytes()
    )
    operation = _with_manifest(
        _operation(
            "op_original",
            request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
            state="awaiting_owner",
        )
    )
    assert journal.create_v2(operation, claim_fence=False)[0] == "created"
    authorization = verified_authorization(
        operation,
        action="approve",
        passphrase="correct horse battery staple",
        path=factor,
    )
    assert authorization is not None
    library = CountingLibrary()

    result = ThingsWorkspace(
        library, journal=journal, account_id=operation.account_id
    ).host_approve_v2(operation.operation_id, authorization)

    assert result == {
        "state": "rejected",
        "code": "request_conflict",
        "next_action": "correct_request",
        "instruction": (
            "Conflicting stored operations share this request_id. "
            "No Cloud write was attempted."
        ),
    }
    assert library.apply_calls == 0


@pytest.mark.parametrize("journal_kind", ["memory", "sqlite"])
@pytest.mark.parametrize("entrypoint", ["execute", "approve"])
def test_apply_session_serializes_cloud_write_and_concurrent_retry(
    journal_kind: str, entrypoint: str, tmp_path: Path
) -> None:
    class BlockingLibrary(MemoryLibrary):
        apply_calls = 0

        def __init__(self) -> None:
            super().__init__([Record(uuid="a", kind="task", title="A")])
            self.entered = Event()
            self.release = Event()

        def apply(self, writes: list[Write]) -> ApplyResult:
            self.apply_calls += 1
            self.entered.set()
            assert self.release.wait(5)
            return super().apply(writes)

    journal = (
        MemoryJournal()
        if journal_kind == "memory"
        else SQLiteJournal(tmp_path / f"{entrypoint}.sqlite3")
    )
    library = BlockingLibrary()
    operation_id: str | None = None
    authorization: object = None
    if entrypoint == "approve":
        factor_dir = tmp_path / journal_kind
        factor_dir.mkdir()
        factor = factor_dir / "owner-factor.json"
        enroll_owner_factor("correct horse battery staple", path=factor)
        public_key = factor.with_name("owner-public-key.ed25519").read_bytes()
        journal = (
            MemoryJournal(owner_public_key=public_key)
            if journal_kind == "memory"
            else SQLiteJournal(
                tmp_path / f"{entrypoint}.sqlite3",
                owner_public_key=public_key,
            )
        )
        operation = _with_manifest(
            _operation(
                "op_approved",
                request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
                state="awaiting_owner",
            ),
            tool="things_update",
            writes=[
                {
                    "action": "update",
                    "uuid": "a",
                    "kind": "task",
                    "title": "B",
                }
            ],
            touched=[["title"]],
            before=[{"id": "task:a", "title": "A"}],
            display_titles=["A"],
        )
        assert journal.create_v2(operation, claim_fence=False)[0] == "created"
        operation_id = operation.operation_id
        authorization = verified_authorization(
            operation,
            action="approve",
            passphrase="correct horse battery staple",
            path=factor,
        )
        assert authorization is not None
    workspace_one = ThingsWorkspace(
        library, journal=journal, account_id="owner@example.com"
    )
    workspace_two = ThingsWorkspace(
        library, journal=journal, account_id="owner@example.com"
    )
    results: list[dict[str, object]] = []
    finished = Event()
    draft = OperationDraft.build(
        "things_update",
        "0198f0ee-98d4-7bd5-91ba-8e76019b2735",
        {"items": [{"id": "task:a", "set": {"title": "B"}}]},
    )

    def invoke(workspace: ThingsWorkspace) -> None:
        result = (
            workspace.execute_v2(draft)
            if entrypoint == "execute"
            else workspace.host_approve_v2(operation_id or "", authorization)
        )
        results.append(result)

    first = Thread(target=invoke, args=(workspace_one,))
    first.start()
    assert library.entered.wait(5)
    second = Thread(target=invoke, args=(workspace_two,))
    second.start()
    finished.wait(0.1)
    retry_waited = second.is_alive()
    library.release.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert retry_waited
    assert library.apply_calls == 1
    assert [result["state"] for result in results] == ["applied", "applied"]
    assert results[0]["operation_id"] == results[1]["operation_id"]


def test_pending_retry_reads_only_after_outcome_unknown_writer_exits(
    tmp_path: Path,
) -> None:
    class LateCommitLibrary(MemoryLibrary):
        def __init__(self) -> None:
            super().__init__([Record(uuid="a", kind="task", title="A")])
            self.apply_entered = Event()
            self.allow_apply = Event()
            self.retry_read_old_state = Event()
            self.remote_applied = False
            self.creator_refresh_failed = False

        def refresh(self, *, force: bool = False) -> None:
            if self.remote_applied:
                if not self.creator_refresh_failed:
                    self.creator_refresh_failed = True
                    raise CloudError("outcome unavailable after remote commit")
                return
            if self.apply_entered.is_set():
                self.retry_read_old_state.set()

        def apply(self, writes: list[Write]) -> ApplyResult:
            self.apply_entered.set()
            assert self.allow_apply.wait(5)
            super().apply(writes)
            self.remote_applied = True
            raise CloudError("connection lost after remote commit")

    journal = SQLiteJournal(tmp_path / "outcome-unknown.sqlite3")
    library = LateCommitLibrary()
    draft = OperationDraft.build(
        "things_update",
        "0198f0ee-98d4-7bd5-91ba-8e76019b2735",
        {"items": [{"id": "task:a", "set": {"title": "B"}}]},
    )
    results: list[dict[str, object]] = []

    def execute() -> None:
        results.append(
            ThingsWorkspace(
                library, journal=journal, account_id="owner@example.com"
            ).execute_v2(draft)
        )

    creator = Thread(target=execute)
    creator.start()
    assert library.apply_entered.wait(5)
    retry = Thread(target=execute)
    retry.start()
    read_before_writer_exit = library.retry_read_old_state.wait(0.2)
    library.allow_apply.set()
    creator.join(5)
    retry.join(5)

    assert not creator.is_alive() and not retry.is_alive()
    assert read_before_writer_exit is False
    assert [result["state"] for result in results] == ["pending", "applied"]
    assert library.records["a"].title == "B"


@pytest.mark.parametrize("journal_kind", ["memory", "sqlite"])
@pytest.mark.parametrize("entrypoint", ["execute", "approve"])
def test_pending_owner_is_acquired_before_create_or_authorize_becomes_visible(
    journal_kind: str, entrypoint: str, tmp_path: Path
) -> None:
    class PauseMixin:
        def _initialize_pause(self) -> None:
            self.pending_visible = Event()
            self.release_creator = Event()

        def _pause_creator(self) -> None:
            self.pending_visible.set()
            assert self.release_creator.wait(5)

        def create_v2(self, *args: object, **kwargs: object) -> object:
            result = super().create_v2(*args, **kwargs)  # type: ignore[misc]
            if entrypoint == "execute" and result[0] == "created":
                self._pause_creator()
            return result

        @contextmanager
        def create_apply_session_v2(
            self, *args: object, **kwargs: object
        ) -> object:
            with super().create_apply_session_v2(  # type: ignore[misc]
                *args, **kwargs
            ) as start:
                if entrypoint == "execute" and start.outcome == "created":
                    self._pause_creator()
                yield start

        def authorize_v2(self, *args: object, **kwargs: object) -> object:
            result = super().authorize_v2(*args, **kwargs)  # type: ignore[misc]
            if entrypoint == "approve" and result[0]:
                self._pause_creator()
            return result

        @contextmanager
        def authorize_apply_session_v2(
            self, *args: object, **kwargs: object
        ) -> object:
            with super().authorize_apply_session_v2(  # type: ignore[misc]
                *args, **kwargs
            ) as start:
                if entrypoint == "approve" and start.authorized:
                    self._pause_creator()
                yield start

    class GapMemoryJournal(PauseMixin, MemoryJournal):
        def __init__(self, *, owner_public_key: bytes | None = None) -> None:
            super().__init__(owner_public_key=owner_public_key)
            self._initialize_pause()

    class GapSQLiteJournal(PauseMixin, SQLiteJournal):
        def __init__(
            self, path: Path, *, owner_public_key: bytes | None = None
        ) -> None:
            super().__init__(path, owner_public_key=owner_public_key)
            self._initialize_pause()

    class CountingLibrary(MemoryLibrary):
        apply_calls = 0

        def apply(self, writes: list[Write]) -> ApplyResult:
            self.apply_calls += 1
            return super().apply(writes)

    public_key: bytes | None = None
    factor: Path | None = None
    if entrypoint == "approve":
        factor = tmp_path / "owner-factor.json"
        enroll_owner_factor("correct horse battery staple", path=factor)
        public_key = factor.with_name("owner-public-key.ed25519").read_bytes()
    journal = (
        GapMemoryJournal(owner_public_key=public_key)
        if journal_kind == "memory"
        else GapSQLiteJournal(
            tmp_path / f"{entrypoint}.sqlite3", owner_public_key=public_key
        )
    )
    library = CountingLibrary([Record(uuid="a", kind="task", title="A")])
    operation_id: str | None = None
    authorization: object = None
    if entrypoint == "approve":
        operation = _with_manifest(
            _operation(
                "op_approved",
                request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
                state="awaiting_owner",
            ),
            tool="things_update",
            writes=[
                {
                    "action": "update",
                    "uuid": "a",
                    "kind": "task",
                    "title": "B",
                }
            ],
            touched=[["title"]],
            before=[{"id": "task:a", "title": "A"}],
            display_titles=["A"],
        )
        assert journal.create_v2(operation, claim_fence=False)[0] == "created"
        operation_id = operation.operation_id
        assert factor is not None
        authorization = verified_authorization(
            operation,
            action="approve",
            passphrase="correct horse battery staple",
            path=factor,
        )
        assert authorization is not None
    draft = OperationDraft.build(
        "things_update",
        "0198f0ee-98d4-7bd5-91ba-8e76019b2735",
        {"items": [{"id": "task:a", "set": {"title": "B"}}]},
    )
    results: list[dict[str, object]] = []

    def invoke() -> None:
        workspace = ThingsWorkspace(
            library, journal=journal, account_id="owner@example.com"
        )
        results.append(
            workspace.execute_v2(draft)
            if entrypoint == "execute"
            else workspace.host_approve_v2(operation_id or "", authorization)
        )

    creator = Thread(target=invoke)
    creator.start()
    assert journal.pending_visible.wait(5)
    retry = Thread(target=invoke)
    retry.start()
    retry.join(0.2)
    retry_finished_in_gap = not retry.is_alive()
    journal.release_creator.set()
    creator.join(5)
    retry.join(5)

    assert not creator.is_alive() and not retry.is_alive()
    assert retry_finished_in_gap is False
    assert library.apply_calls == 1
    assert [result["state"] for result in results] == ["applied", "applied"]
    assert library.records["a"].title == "B"


def test_sqlite_apply_owner_outwaits_short_database_busy_timeout(
    tmp_path: Path,
) -> None:
    class ShortTimeoutJournal(SQLiteJournal):
        def _connect(self) -> sqlite3.Connection:
            connection = super()._connect()
            connection.execute("PRAGMA busy_timeout=25")
            return connection

    class BlockingLibrary(MemoryLibrary):
        def __init__(self) -> None:
            super().__init__([Record(uuid="a", kind="task", title="A")])
            self.entered = Event()
            self.release = Event()
            self.apply_calls = 0

        def apply(self, writes: list[Write]) -> ApplyResult:
            self.apply_calls += 1
            self.entered.set()
            assert self.release.wait(5)
            return super().apply(writes)

    journal = ShortTimeoutJournal(tmp_path / "short-timeout.sqlite3")
    library = BlockingLibrary()
    draft = OperationDraft.build(
        "things_update",
        "0198f0ee-98d4-7bd5-91ba-8e76019b2735",
        {"items": [{"id": "task:a", "set": {"title": "B"}}]},
    )
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def execute() -> None:
        try:
            results.append(
                ThingsWorkspace(
                    library, journal=journal, account_id="owner@example.com"
                ).execute_v2(draft)
            )
        except BaseException as error:
            errors.append(error)

    creator = Thread(target=execute)
    creator.start()
    assert library.entered.wait(5)
    retry = Thread(target=execute)
    retry.start()
    retry.join(0.15)
    retry_waited = retry.is_alive()
    library.release.set()
    creator.join(5)
    retry.join(5)

    assert retry_waited
    assert errors == []
    assert [result["state"] for result in results] == ["applied", "applied"]
    assert library.apply_calls == 1


def test_sqlite_process_death_keeps_pending_for_reconcile_without_replay(
    tmp_path: Path,
) -> None:
    draft = OperationDraft.build(
        "things_update",
        "0198f0ee-98d4-7bd5-91ba-8e76019b2735",
        {"items": [{"id": "task:a", "set": {"title": "B"}}]},
    )
    operation = _with_manifest(
        replace(
            _operation("op_crash", request_id=draft.request_id),
            request_hash=draft.request_hash,
        ),
        tool="things_update",
        writes=[
            {
                "action": "update",
                "uuid": "a",
                "kind": "task",
                "title": "B",
            }
        ],
        touched=[["title"]],
        before=[{"id": "task:a", "title": "A"}],
        display_titles=["A"],
    )
    path = tmp_path / "crash.sqlite3"
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json
import os
import sys
from pathlib import Path
from things_orchestrator.journal import SQLiteJournal, V2Operation

operation = V2Operation(**json.loads(sys.argv[2]))
journal = SQLiteJournal(Path(sys.argv[1]))
with journal.create_apply_session_v2(operation, claim_fence=True) as start:
    assert start.outcome == "created"
    assert start.session is not None
    os._exit(0)
""",
            str(path),
            json.dumps(asdict(operation)),
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
    )
    assert child.returncode == 0
    journal = SQLiteJournal(path)
    pending = journal.get_v2_operation(operation.operation_id)
    assert pending is not None and pending.state == "pending"

    class NoReplayLibrary(MemoryLibrary):
        apply_calls = 0

        def apply(self, writes: list[Write]) -> ApplyResult:
            self.apply_calls += 1
            return super().apply(writes)

    library = NoReplayLibrary([Record(uuid="a", kind="task", title="A")])
    result = ThingsWorkspace(
        library, journal=journal, account_id="owner@example.com"
    ).execute_v2(draft)

    assert result["state"] == "not_applied"
    assert library.apply_calls == 0


def test_sqlite_apply_owner_blocks_exact_retry_across_processes(
    tmp_path: Path,
) -> None:
    draft = OperationDraft.build(
        "things_update",
        "0198f0ee-98d4-7bd5-91ba-8e76019b2735",
        {"items": [{"id": "task:a", "set": {"title": "B"}}]},
    )
    operation = _with_manifest(
        replace(
            _operation("op_cross_process", request_id=draft.request_id),
            request_hash=draft.request_hash,
        ),
        tool="things_update",
        writes=[
            {
                "action": "update",
                "uuid": "a",
                "kind": "task",
                "title": "B",
            }
        ],
        touched=[["title"]],
        before=[{"id": "task:a", "title": "A"}],
        display_titles=["A"],
    )
    path = tmp_path / "cross-process.sqlite3"
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            """
import json
import sys
import time
from pathlib import Path
from things_orchestrator.journal import SQLiteJournal, V2Operation

operation = V2Operation(**json.loads(sys.argv[2]))
ready = Path(sys.argv[3])
release = Path(sys.argv[4])
with SQLiteJournal(Path(sys.argv[1])).create_apply_session_v2(
    operation, claim_fence=True
) as start:
    assert start.outcome == "created"
    ready.write_text("ready")
    while not release.exists():
        time.sleep(0.01)
""",
            str(path),
            json.dumps(asdict(operation)),
            str(ready),
            str(release),
        ],
        cwd=Path(__file__).resolve().parents[1],
    )
    for _attempt in range(500):
        if ready.exists():
            break
        Event().wait(0.01)
    assert ready.exists()
    journal = SQLiteJournal(path)

    class NoReplayLibrary(MemoryLibrary):
        apply_calls = 0

        def apply(self, writes: list[Write]) -> ApplyResult:
            self.apply_calls += 1
            return super().apply(writes)

    library = NoReplayLibrary([Record(uuid="a", kind="task", title="A")])
    results: list[dict[str, object]] = []
    retry = Thread(
        target=lambda: results.append(
            ThingsWorkspace(
                library, journal=journal, account_id="owner@example.com"
            ).execute_v2(draft)
        )
    )
    retry.start()
    retry.join(0.2)
    retry_waited = retry.is_alive()
    release.write_text("release")
    assert child.wait(timeout=5) == 0
    retry.join(5)

    assert retry_waited
    assert not retry.is_alive()
    assert results[0]["state"] == "not_applied"
    assert library.apply_calls == 0


def test_sqlite_apply_owner_is_shared_by_real_and_symlink_database_paths(
    tmp_path: Path,
) -> None:
    draft = OperationDraft.build(
        "things_update",
        "0198f0ee-98d4-7bd5-91ba-8e76019b2735",
        {"items": [{"id": "task:a", "set": {"title": "B"}}]},
    )
    operation = _with_manifest(
        replace(
            _operation("op_symlink", request_id=draft.request_id),
            request_hash=draft.request_hash,
        ),
        tool="things_update",
        writes=[
            {
                "action": "update",
                "uuid": "a",
                "kind": "task",
                "title": "B",
            }
        ],
        touched=[["title"]],
        before=[{"id": "task:a", "title": "A"}],
        display_titles=["A"],
    )
    real_path = tmp_path / "real.sqlite3"
    SQLiteJournal(real_path)
    alias_path = tmp_path / "alias.sqlite3"
    alias_path.symlink_to(real_path)
    ready = tmp_path / "symlink-ready"
    release = tmp_path / "symlink-release"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            """
import json
import sys
import time
from pathlib import Path
from things_orchestrator.journal import SQLiteJournal, V2Operation

operation = V2Operation(**json.loads(sys.argv[2]))
ready = Path(sys.argv[3])
release = Path(sys.argv[4])
with SQLiteJournal(Path(sys.argv[1])).create_apply_session_v2(
    operation, claim_fence=True
) as start:
    assert start.outcome == "created"
    ready.write_text("ready")
    while not release.exists():
        time.sleep(0.01)
""",
            str(real_path),
            json.dumps(asdict(operation)),
            str(ready),
            str(release),
        ],
        cwd=Path(__file__).resolve().parents[1],
    )
    for _attempt in range(500):
        if ready.exists():
            break
        Event().wait(0.01)
    assert ready.exists()
    journal = SQLiteJournal(alias_path)
    library = MemoryLibrary([Record(uuid="a", kind="task", title="A")])
    results: list[dict[str, object]] = []
    retry = Thread(
        target=lambda: results.append(
            ThingsWorkspace(
                library, journal=journal, account_id="owner@example.com"
            ).execute_v2(draft)
        )
    )
    retry.start()
    retry.join(0.2)
    retry_waited = retry.is_alive()
    release.write_text("release")
    assert child.wait(timeout=5) == 0
    retry.join(5)

    assert retry_waited
    assert not retry.is_alive()
    assert results[0]["state"] == "not_applied"


def test_sqlite_journal_rejects_existing_hard_link(tmp_path: Path) -> None:
    real_path = tmp_path / "real.sqlite3"
    SQLiteJournal(real_path)
    alias_path = tmp_path / "alias.sqlite3"
    os.link(real_path, alias_path)

    with pytest.raises(
        RuntimeError, match="hard-linked SQLite journals are not supported"
    ):
        SQLiteJournal(alias_path)


def test_sqlite_apply_owner_rejects_hard_link_added_after_init(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.sqlite3"
    journal = SQLiteJournal(path)
    os.link(path, tmp_path / "alias.sqlite3")

    with pytest.raises(
        RuntimeError, match="hard-linked SQLite journals are not supported"
    ):
        with journal.apply_session_v2("missing"):
            pass


@pytest.mark.parametrize("journal_kind", ["memory", "sqlite"])
def test_apply_session_cannot_settle_after_context_exit(
    journal_kind: str, tmp_path: Path,
) -> None:
    journal = (
        MemoryJournal()
        if journal_kind == "memory"
        else SQLiteJournal(tmp_path / "journal.sqlite3")
    )
    operation = _operation(
        f"op_late_settle_{journal_kind}",
        request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
    )
    assert journal.create_v2(operation, claim_fence=True)[0] == "created"

    with journal.apply_session_v2(operation.operation_id) as session:
        assert session is not None
    assert not session.settle(
        state="applied",
        response={"state": "applied"},
        rows=[
            {
                "sequence": 1,
                "action": "create",
                "target_id": "task:a",
                "desired": {},
                "observed": {},
                "result": "applied",
            }
        ],
    )

    stored = journal.get_v2_operation(operation.operation_id)
    assert stored is not None
    assert stored.state == "pending"


@pytest.mark.parametrize("journal_kind", ["memory", "sqlite"])
def test_apply_session_rejects_foreign_thread_while_context_is_active(
    journal_kind: str, tmp_path: Path,
) -> None:
    journal = (
        MemoryJournal()
        if journal_kind == "memory"
        else SQLiteJournal(tmp_path / "journal.sqlite3")
    )
    operation = _operation(
        f"op_foreign_thread_{journal_kind}",
        request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
    )
    assert journal.create_v2(operation, claim_fence=True)[0] == "created"
    rows = [
        {
            "sequence": 1,
            "action": "create",
            "target_id": "task:a",
            "desired": {},
            "observed": {},
            "result": "applied",
        }
    ]
    results: list[bool] = []

    with journal.apply_session_v2(operation.operation_id) as session:
        assert session is not None
        caller = Thread(
            target=lambda: results.append(
                session.settle(
                    state="applied",
                    response={"state": "applied"},
                    rows=rows,
                )
            )
        )
        caller.start()
        Event().wait(0.05)
    caller.join(5)

    assert not caller.is_alive()
    assert results == [False]
    stored = journal.get_v2_operation(operation.operation_id)
    assert stored is not None
    assert stored.state == "pending"


@pytest.mark.parametrize("journal_kind", ["memory", "sqlite"])
def test_apply_session_rejects_forked_process_while_context_is_active(
    journal_kind: str, tmp_path: Path,
) -> None:
    journal = (
        MemoryJournal()
        if journal_kind == "memory"
        else SQLiteJournal(tmp_path / "journal.sqlite3")
    )
    operation = _operation(
        f"op_foreign_process_{journal_kind}",
        request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
    )
    assert journal.create_v2(operation, claim_fence=True)[0] == "created"
    rows = [
        {
            "sequence": 1,
            "action": "create",
            "target_id": "task:a",
            "desired": {},
            "observed": {},
            "result": "applied",
        }
    ]

    with journal.apply_session_v2(operation.operation_id) as session:
        assert session is not None
        child = os.fork()
        if child == 0:
            changed = session.settle(
                state="applied",
                response={"state": "applied"},
                rows=rows,
            )
            os._exit(1 if changed else 0)
        _pid, status = os.waitpid(child, 0)

    assert os.waitstatus_to_exitcode(status) == 0
    stored = journal.get_v2_operation(operation.operation_id)
    assert stored is not None
    assert stored.state == "pending"


@pytest.mark.parametrize("journal_kind", ["memory", "sqlite"])
def test_apply_session_close_waits_for_started_owner_settlement(
    journal_kind: str, tmp_path: Path,
) -> None:
    settle_entered = Event()
    allow_settle = Event()

    class BlockingMemoryJournal(MemoryJournal):
        def _settle_v2_locked(self, *args: object, **kwargs: object) -> bool:
            settle_entered.set()
            allow_settle.wait(5)
            return super()._settle_v2_locked(  # type: ignore[misc]
                *args, **kwargs
            )

    class BlockingSQLiteJournal(SQLiteJournal):
        def _settle_v2_owned(self, *args: object, **kwargs: object) -> bool:
            settle_entered.set()
            allow_settle.wait(5)
            return super()._settle_v2_owned(  # type: ignore[misc]
                *args, **kwargs
            )

    journal: MemoryJournal | SQLiteJournal = (
        BlockingMemoryJournal()
        if journal_kind == "memory"
        else BlockingSQLiteJournal(tmp_path / "journal.sqlite3")
    )
    operation = _operation(
        f"op_close_race_{journal_kind}",
        request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
    )
    assert journal.create_v2(operation, claim_fence=True)[0] == "created"
    rows = [
        {
            "sequence": 1,
            "action": "create",
            "target_id": "task:a",
            "desired": {},
            "observed": {},
            "result": "applied",
        }
    ]
    session_ready = Event()
    start_settle = Event()
    sessions: list[V2ApplySession] = []
    results: list[bool] = []

    def own_and_settle() -> None:
        with journal.apply_session_v2(operation.operation_id) as session:
            assert session is not None
            sessions.append(session)
            session_ready.set()
            start_settle.wait(5)
            results.append(
                session.settle(
                    state="applied",
                    response={"state": "applied"},
                    rows=rows,
                )
            )

    owner = Thread(target=own_and_settle)
    owner.start()
    assert session_ready.wait(5)
    start_settle.set()
    assert settle_entered.wait(5)
    close_finished = Event()

    def close_session() -> None:
        close = getattr(sessions[0], "close")
        close()
        close_finished.set()

    closer = Thread(target=close_session)
    closer.start()
    close_overtook_settlement = close_finished.wait(0.1)
    allow_settle.set()
    owner.join(5)
    closer.join(5)

    assert not close_overtook_settlement
    assert not owner.is_alive() and not closer.is_alive()
    assert results == [True]
    stored = journal.get_v2_operation(operation.operation_id)
    assert stored is not None
    assert stored.state == "applied"


@pytest.mark.parametrize("journal_kind", ["memory", "sqlite"])
def test_authorize_apply_session_rejects_expired_operation(
    journal_kind: str, tmp_path: Path,
) -> None:
    factor = tmp_path / "owner-factor.json"
    enroll_owner_factor("correct horse battery staple", path=factor)
    public_key = factor.with_name("owner-public-key.ed25519").read_bytes()
    journal = (
        MemoryJournal(owner_public_key=public_key)
        if journal_kind == "memory"
        else SQLiteJournal(
            tmp_path / "journal.sqlite3", owner_public_key=public_key
        )
    )
    expires_at = "2030-01-01T00:00:01+00:00"
    operation = _with_manifest(
        replace(
            _operation(
                f"op_expired_{journal_kind}",
                request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
                state="awaiting_owner",
            ),
            expires_at=expires_at,
        ),
        expires_at=expires_at,
    )
    assert journal.create_v2(operation, claim_fence=False)[0] == "created"
    authorization = verified_authorization(
        operation,
        action="approve",
        passphrase="correct horse battery staple",
        path=factor,
    )
    assert authorization is not None

    with journal.authorize_apply_session_v2(
        operation.operation_id,
        authorization,
        now=lambda: datetime(2030, 1, 1, 0, 0, 2, tzinfo=timezone.utc),
    ) as start:
        assert not start.authorized
        assert start.session is None

    stored = journal.get_v2_operation(operation.operation_id)
    assert stored is not None
    assert stored.state == "stale"
    assert stored.response == {
        "state": "stale",
        "instruction": "The owner approval window expired.",
        "operation_id": operation.operation_id,
    }


def test_host_approval_rechecks_expiry_after_waiting_for_apply_owner(
    tmp_path: Path,
) -> None:
    attempted = Event()

    class WaitingSQLiteJournal(SQLiteJournal):
        @contextmanager
        def authorize_apply_session_v2(
            self, *args: object, **kwargs: object
        ) -> object:
            attempted.set()
            with super().authorize_apply_session_v2(  # type: ignore[misc]
                *args, **kwargs
            ) as start:
                yield start

    factor = tmp_path / "owner-factor.json"
    enroll_owner_factor("correct horse battery staple", path=factor)
    public_key = factor.with_name("owner-public-key.ed25519").read_bytes()
    journal = WaitingSQLiteJournal(
        tmp_path / "journal.sqlite3", owner_public_key=public_key
    )
    expires_at = "2030-01-01T00:00:01+00:00"
    operation = _with_manifest(
        replace(
            _operation(
                "op_expiry_sqlite",
                request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
                state="awaiting_owner",
            ),
            expires_at=expires_at,
        ),
        expires_at=expires_at,
        tool="things_update",
        writes=[
            {
                "action": "update",
                "uuid": "a",
                "kind": "task",
                "title": "B",
            }
        ],
        touched=[["title"]],
        before=[{"id": "task:a", "title": "A"}],
        display_titles=["A"],
    )
    assert journal.create_v2(operation, claim_fence=False)[0] == "created"
    blocker = _with_manifest(
        replace(
            _operation(
                "op_blocker_sqlite",
                request_id="0198f0ef-3923-79b6-96a8-2bf28eac0d67",
            ),
            account_id="other@example.com",
        )
    )
    _inject_v2_operation(journal, blocker)
    authorization = verified_authorization(
        operation,
        action="approve",
        passphrase="correct horse battery staple",
        path=factor,
    )
    assert authorization is not None

    entered = Event()
    release = Event()

    def hold_owner() -> None:
        with journal.apply_session_v2(blocker.operation_id) as session:
            assert session is not None
            entered.set()
            release.wait(5)

    holder = Thread(target=hold_owner)
    holder.start()
    assert entered.wait(5)
    now = [datetime(2030, 1, 1, tzinfo=timezone.utc)]

    class CountingLibrary(MemoryLibrary):
        apply_calls = 0

        def apply(self, writes: list[Write]) -> ApplyResult:
            self.apply_calls += 1
            return super().apply(writes)

    library = CountingLibrary([Record(uuid="a", kind="task", title="A")])
    workspace = ThingsWorkspace(
        library,
        journal=journal,
        account_id=operation.account_id,
        clock=lambda: now[0],
    )
    results: list[dict[str, object]] = []
    approver = Thread(
        target=lambda: results.append(
            workspace.host_approve_v2(operation.operation_id, authorization)
        )
    )
    approver.start()
    assert attempted.wait(5)
    now[0] = datetime(2030, 1, 1, 0, 0, 2, tzinfo=timezone.utc)
    release.set()
    holder.join(5)
    approver.join(5)

    assert not holder.is_alive() and not approver.is_alive()
    assert results == [
        {
            "state": "stale",
            "instruction": "The owner approval window expired.",
            "operation_id": operation.operation_id,
        }
    ]
    assert library.apply_calls == 0


def test_receipt_cursor_is_bound_to_account_operation_hash_and_version() -> None:
    journal = MemoryJournal()
    operation = _operation(
        "op_receipt",
        request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
    )
    operation = _with_manifest(
        operation,
        writes=[
            {"action": "create", "uuid": str(index), "kind": "task", "title": str(index)}
            for index in range(1, 4)
        ],
        touched=[["title"] for _index in range(3)],
        before=[None, None, None],
        display_titles=[str(index) for index in range(1, 4)],
    )
    journal.create_v2(operation, claim_fence=True)
    rows = [
        {"sequence": index, "action": "create", "target_id": f"task:{index}", "desired": {"title": str(index)}, "observed": {"title": str(index)}, "result": "applied"}
        for index in range(1, 4)
    ]
    assert journal.settle_v2(
        operation.operation_id,
        expected="pending",
        state="applied",
        response={"state": "applied"},
        rows=rows,
    )
    first = journal.v2_receipt_page(operation.account_id, operation.operation_id, limit=2)
    assert [row["sequence"] for row in first.rows] == [1, 2]
    assert first.cursor is not None
    second = journal.v2_receipt_page(operation.account_id, operation.operation_id, limit=2, cursor=first.cursor)
    assert [row["sequence"] for row in second.rows] == [3]
    forged = first.cursor[:-1] + ("0" if first.cursor[-1] != "0" else "1")
    try:
        journal.v2_receipt_page(
            operation.account_id,
            operation.operation_id,
            limit=2,
            cursor=forged,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("forged cursor was accepted")


def test_owner_factor_stores_only_scrypt_verifier(tmp_path: Path) -> None:
    path = tmp_path / "owner-factor.json"
    enroll_owner_factor("correct horse battery staple", path=path)
    text = path.read_text()
    assert "correct horse" not in text
    assert verify_owner_factor("correct horse battery staple", path=path)
    assert not verify_owner_factor("wrong passphrase", path=path)
    assert path.stat().st_mode & 0o777 == 0o600
    public_key = path.with_name("owner-public-key.ed25519")
    assert len(public_key.read_bytes()) == 32
    assert public_key.stat().st_mode & 0o777 == 0o600


def test_authorization_binding_covers_action_and_operation_contract() -> None:
    operation = _operation(
        "op_approval",
        request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
        state="awaiting_owner",
    )
    assert authorization_binding(operation, action="approve") != authorization_binding(
        operation, action="decline"
    )
    assert authorization_binding(operation, action="approve") != authorization_binding(
        replace(operation, manifest_hash="sha256:v1:other"), action="approve"
    )
    assert authorization_binding(operation, action="approve") != authorization_binding(
        replace(operation, api_version="legacy-v1"), action="approve"
    )
    assert authorization_binding(operation, action="approve") != authorization_binding(
        replace(operation, tool="things_complete"), action="approve"
    )


def test_host_rendering_escapes_control_ansi_newline_and_delimiter() -> None:
    assert host_escape("\x1b]0;owned\x07\nA|B") == "\\u000aA\\u007cB"
    assert (
        host_escape("safe\u202evil\u2066x\u2069\u200b")
        == "safe\\u202evil\\u2066x\\u2069\\u200b"
    )
    operation = _with_manifest(
        _operation(
            "op_render",
            request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
            state="awaiting_owner",
        ),
        writes=[
            {
                "action": "trash",
                "kind": "task",
                "uuid": "abc",
                "title": "\x1b[31mApprove\n| now",
            }
        ],
    )
    rendered = render_operation(operation)
    assert "\x1b" not in rendered
    assert "Approve\\u000a\\u007c now" in rendered


def test_legacy_resolution_render_contains_complete_escaped_signed_plan() -> None:
    hostile = "\x1b]0;owned\x07\nA|B\u202e"
    journal = MemoryJournal()
    record = IntentRecord(
        "legacy-render", "sha256:fingerprint", "pending",
        plan={
            "writes": [{"action": "update", "uuid": "a", "kind": "task", "title": hostile}],
            "summary": [hostile],
            "preconditions": {"task:a": "r_1"},
        },
    )
    journal.save(record)
    workspace = ThingsWorkspace(MemoryLibrary(), journal=journal, account_id="owner@example.com")
    operation = workspace.host_get_legacy_resolution_v1(record.intent_id)
    assert operation is not None
    rendered = render_operation(operation)
    assert "legacy_plan |" in rendered
    assert "preconditions" in rendered and "summary" in rendered and "writes" in rendered
    assert "\x1b" not in rendered and "\nA" not in rendered and "\u202e" not in rendered
    assert "\\u000a" in rendered and "\\u007c" in rendered and "\\u202e" in rendered


def test_legacy_resolution_rejects_a_substituted_owner_envelope() -> None:
    journal = MemoryJournal()
    record = IntentRecord(
        "legacy-envelope",
        "sha256:fingerprint",
        "pending",
        plan={
            "writes": [
                {"action": "update", "uuid": "a", "kind": "task", "title": "B"}
            ]
        },
    )
    journal.save(record)
    workspace = ThingsWorkspace(
        MemoryLibrary(), journal=journal, account_id="owner@example.com"
    )
    operation = workspace.host_get_legacy_resolution_v1(record.intent_id)
    assert operation is not None

    for tampered in (
        replace(operation, api_version="2"),
        replace(operation, tool="things_trash"),
        replace(operation, safety_policy_digest="sha256:v1:other"),
        replace(operation, manifest={**operation.manifest, "display_titles": ["Other"]}),
    ):
        with pytest.raises(ValueError, match="integrity"):
            render_operation(tampered)


def test_host_operation_lookup_is_scoped_to_workspace_account() -> None:
    journal = MemoryJournal()
    owned = _operation(
        "op_owned",
        request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
    )
    foreign = _with_manifest(replace(
        _operation(
            "op_foreign",
            request_id="0198f0ef-3923-79b6-96a8-2bf28eac0d67",
        ),
        account_id="other@example.com",
    ))
    journal.create_v2(owned, claim_fence=True)
    journal.create_v2(foreign, claim_fence=True)
    workspace = ThingsWorkspace(
        MemoryLibrary(), journal=journal, account_id=owned.account_id
    )

    assert workspace.host_get_operation_v2(owned.operation_id) == owned
    assert workspace.host_get_operation_v2(foreign.operation_id) is None
    assert workspace.host_get_operation_v2("missing") is None


def test_server_runtime_does_not_import_owner_authority() -> None:
    source = (Path(__file__).parents[1] / "src/things_orchestrator/server.py").read_text()
    assert "owner_authority" not in source


def test_sqlite_cutover_quarantines_old_approvals_and_reports_every_fence(tmp_path: Path) -> None:
    journal = SQLiteJournal(tmp_path / "journal.sqlite3")
    for intent_id, state in (
        ("prepared-old", "prepared"),
        ("approval-old", "needs_approval"),
        ("pending-a", "pending"),
        ("pending-b", "pending"),
        ("applied-old", "applied"),
    ):
        journal.save(
            IntentRecord(
                intent_id=intent_id,
                fingerprint=intent_id,
                state=state,  # type: ignore[arg-type]
                result={"status": "partial"} if intent_id == "pending-b" else None,
            )
        )

    report = journal.cutover_v1()

    assert report["quarantined"] == ["approval-old", "prepared-old"]
    assert report["unresolved"] == ["pending-a", "pending-b"]
    assert report["partial_like"] == ["pending-b"]
    assert report["writes_blocked"] is True
    assert journal.get("prepared-old").state == "stale"  # type: ignore[union-attr]
    assert journal.get("approval-old").result["status"] == "quarantined"  # type: ignore[index,union-attr]
    assert journal.blocking_v2_operations("owner@example.com") == ["pending-a", "pending-b"]


def test_retained_v1_none_matched_stays_fenced_until_signed_resolution(tmp_path: Path) -> None:
    factor = tmp_path / "owner-factor.json"
    enroll_owner_factor("correct horse battery staple", path=factor)
    journal = SQLiteJournal(tmp_path / "journal.sqlite3")
    journal.save(IntentRecord(
        intent_id="legacy-pending",
        fingerprint="sha256:legacy",
        state="pending",
        plan={"writes": [{"action": "update", "uuid": "a", "kind": "task", "title": "New"}]},
    ))
    workspace = ThingsWorkspace(
        MemoryLibrary([Record(uuid="a", kind="task", title="Old")]),
        journal=journal,
        account_id="owner@example.com",
    )

    result = workspace.host_reconcile_v1_pending("legacy-pending")

    assert result["classification"] == "unknown"
    assert journal.get("legacy-pending").state == "pending"  # type: ignore[union-attr]
    assert journal.blocking_v2_operations("owner@example.com") == ["legacy-pending"]

    journal_with_key = SQLiteJournal(
        tmp_path / "journal.sqlite3",
        owner_public_key=factor.with_name("owner-public-key.ed25519").read_bytes(),
    )
    workspace = ThingsWorkspace(MemoryLibrary([Record(uuid="a", kind="task", title="Old")]), journal=journal_with_key, account_id="owner@example.com")
    operation = workspace.host_get_legacy_resolution_v1("legacy-pending")
    assert operation is not None
    authorization = verified_authorization(operation, action="legacy_accepted_as_is", passphrase="correct horse battery staple", path=factor)
    assert authorization is not None
    assert workspace.host_resolve_legacy_v1("legacy-pending", "accepted_as_is", authorization)
    stored = journal_with_key.get("legacy-pending")
    assert stored is not None and stored.state == "stale"
    assert stored.result is not None and stored.result["resolution"] == "accepted_as_is"
    assert stored.plan == {}
    assert journal_with_key.blocking_v2_operations("owner@example.com") == []


def test_malformed_all_none_v1_update_remains_fenced(tmp_path: Path) -> None:
    journal = SQLiteJournal(tmp_path / "journal.sqlite3")
    journal.save(IntentRecord(
        intent_id="legacy-malformed", fingerprint="sha256:legacy", state="pending",
        plan={"writes": [{"action": "update", "uuid": "a", "kind": "task"}]},
    ))
    workspace = ThingsWorkspace(MemoryLibrary([Record(uuid="a", kind="task", title="A")]), journal=journal, account_id="owner@example.com")
    result = workspace.host_reconcile_v1_pending("legacy-malformed")
    assert result["classification"] == "malformed"
    assert journal.get("legacy-malformed").state == "pending"  # type: ignore[union-attr]


def test_unknown_action_and_invalid_or_missing_kind_are_malformed_in_both_journals(tmp_path: Path) -> None:
    for journal in (MemoryJournal(), SQLiteJournal(tmp_path / "journal.sqlite3")):
        for index, write in enumerate((
            {"action": "teleport", "uuid": "a", "kind": "task"},
            {"action": "update", "uuid": "a", "kind": "widget", "title": "B"},
            {"action": "update", "uuid": "a", "title": "B"},
        )):
            intent_id = f"legacy-invalid-{type(journal).__name__}-{index}"
            journal.save(IntentRecord(intent_id=intent_id, fingerprint="sha256:legacy", state="pending", plan={"writes": [write]}))
            workspace = ThingsWorkspace(MemoryLibrary([Record(uuid="a", kind="task", title="A")]), journal=journal, account_id="owner@example.com")
            result = workspace.host_reconcile_v1_pending(intent_id)
            assert result["classification"] == "malformed"
            assert journal.get(intent_id).state == "pending"  # type: ignore[union-attr]


def test_retained_v1_partial_evidence_remains_fenced(tmp_path: Path) -> None:
    journal = SQLiteJournal(tmp_path / "journal.sqlite3")
    journal.save(IntentRecord(
        intent_id="legacy-partial", fingerprint="sha256:legacy", state="pending",
        plan={"writes": [
            {"action": "update", "uuid": "a", "kind": "task", "title": "Applied"},
            {"action": "update", "uuid": "b", "kind": "task", "title": "Desired"},
        ]},
    ))
    workspace = ThingsWorkspace(
        MemoryLibrary([
            Record(uuid="a", kind="task", title="Applied"),
            Record(uuid="b", kind="task", title="Before"),
        ]),
        journal=journal,
        account_id="owner@example.com",
    )
    result = workspace.host_reconcile_v1_pending("legacy-partial")
    assert result["classification"] == "partial"
    assert journal.get("legacy-partial").state == "pending"  # type: ignore[union-attr]


def test_signed_legacy_resolution_cannot_release_replaced_plan(tmp_path: Path) -> None:
    factor = tmp_path / "owner-factor.json"
    enroll_owner_factor("correct horse battery staple", path=factor)
    public_key = factor.with_name("owner-public-key.ed25519").read_bytes()
    for journal in (MemoryJournal(owner_public_key=public_key), SQLiteJournal(tmp_path / "journal.sqlite3", owner_public_key=public_key)):
        record = IntentRecord(intent_id=f"legacy-race-{type(journal).__name__}", fingerprint="sha256:original", state="pending", plan={"writes": [{"action": "update", "uuid": "a", "kind": "task", "title": "B"}]})
        journal.save(record)
        workspace = ThingsWorkspace(MemoryLibrary([Record(uuid="a", kind="task", title="A")]), journal=journal, account_id="owner@example.com")
        operation = workspace.host_get_legacy_resolution_v1(record.intent_id)
        assert operation is not None
        authorization = verified_authorization(operation, action="legacy_accepted_as_is", passphrase="correct horse battery staple", path=factor)
        assert authorization is not None
        journal.save(replace(record, plan={"writes": [{"action": "update", "uuid": "a", "kind": "task", "title": "C"}]}))
        assert not workspace.host_resolve_legacy_v1(record.intent_id, "accepted_as_is", authorization)
        assert not journal.resolve_v1_pending(
            record.intent_id,
            expected_fingerprint=operation.request_id,
            expected_plan_digest=operation.manifest_hash,
            state="stale",
            result={"status": "must_not_land"},
        )
        assert journal.get(record.intent_id).state == "pending"  # type: ignore[union-attr]


def test_v1_cutover_scrubs_terminal_owner_content_but_keeps_pending_evidence(tmp_path: Path) -> None:
    for journal in (MemoryJournal(), SQLiteJournal(tmp_path / "journal.sqlite3")):
        prefix = type(journal).__name__
        journal.save(IntentRecord(f"{prefix}-approval", "fp-a", "needs_approval", plan={"summary": "owner secret"}))
        journal.save(IntentRecord(f"{prefix}-applied", "fp-b", "applied", plan={"summary": "owner terminal"}, result={"owner": "text"}))
        journal.save(IntentRecord(f"{prefix}-pending", "fp-c", "pending", plan={"summary": "needed evidence"}))
        journal.cutover_v1()
        assert journal.get(f"{prefix}-approval").plan == {}  # type: ignore[union-attr]
        assert journal.get(f"{prefix}-applied").plan == {}  # type: ignore[union-attr]
        assert "owner" not in str(journal.get(f"{prefix}-applied").result)  # type: ignore[union-attr]
        assert journal.get(f"{prefix}-pending").plan == {"summary": "needed evidence"}  # type: ignore[union-attr]


def test_repeated_v1_cutover_preserves_only_safe_resolution_evidence(tmp_path: Path) -> None:
    for journal in (MemoryJournal(), SQLiteJournal(tmp_path / "journal.sqlite3")):
        prefix = type(journal).__name__
        journal.save(IntentRecord(
            f"{prefix}-signed", "fp-signed", "stale", plan={"summary": "owner secret"},
            result={
                "status": "owner_resolved_no_replay", "classification": "partial",
                "resolution": "accepted_as_is", "authorization": "ed25519:v1:safe",
                "owner_text": "must disappear",
            },
        ))
        journal.save(IntentRecord(
            f"{prefix}-auto", "fp-auto", "applied", plan={"summary": "owner secret"},
            result={
                "status": "reconciled_no_replay", "classification": "applied",
                "owner_text": "must disappear",
            },
        ))
        journal.cutover_v1()
        journal.cutover_v1()
        signed = journal.get(f"{prefix}-signed")
        automatic = journal.get(f"{prefix}-auto")
        assert signed is not None and signed.plan == {}
        assert signed.result == {
            "status": "owner_resolved_no_replay", "classification": "partial",
            "resolution": "accepted_as_is", "authorization": "ed25519:v1:safe",
        }
        assert automatic is not None and automatic.plan == {}
        assert automatic.result == {"status": "reconciled_no_replay", "classification": "applied"}


@pytest.mark.parametrize("write", [
    {"action": "create"}, {"action": "move"},
    {"action": "move", "into_uuid": "project-a"}, {"action": "tags"},
    {"action": "move", "into_uuid": "ghost", "into_kind": "task"},
    {"action": "move", "kind": "project", "into_uuid": "p", "into_kind": "project"},
    {"action": "rename_area", "kind": "task", "title": "Wrong kind"},
    {"action": "create_heading", "kind": "project", "title": "Wrong kind"},
    {"action": "checklist"}, {"action": "repeat"}, {"action": "repeat_link"},
    {"action": "repeat_progress"},
])
def test_action_incomplete_legacy_plan_remains_fenced(write: dict[str, object]) -> None:
    journal = MemoryJournal()
    journal.save(IntentRecord(
        intent_id="legacy-incomplete", fingerprint="sha256:legacy", state="pending",
        plan={"writes": [{"uuid": "a", "kind": "task", **write}]},
    ))
    workspace = ThingsWorkspace(
        MemoryLibrary([Record(uuid="a", kind="task", title="A")]),
        journal=journal, account_id="owner@example.com",
    )
    result = workspace.host_reconcile_v1_pending("legacy-incomplete")
    assert result["classification"] == "malformed"
    assert journal.get("legacy-incomplete").state == "pending"  # type: ignore[union-attr]


def test_pending_v2_can_settle_not_applied_only_with_signed_readback_evidence(tmp_path: Path) -> None:
    factor = tmp_path / "owner-factor.json"
    enroll_owner_factor("correct horse battery staple", path=factor)
    journal = MemoryJournal(owner_public_key=factor.with_name("owner-public-key.ed25519").read_bytes())
    operation = _with_manifest(
        _operation("op_recover", request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735"),
        writes=[{"action": "update", "uuid": "a", "kind": "task", "title": "New"}],
        before=[{"id": "task:a", "title": "Old"}],
        touched=[["title"]],
        preconditions={"task:a": "frozen"},
        display_titles=["Old"],
    )
    journal.create_v2(operation, claim_fence=True)
    workspace = ThingsWorkspace(MemoryLibrary([Record(uuid="a", kind="task", title="Old")]), journal=journal, account_id=operation.account_id)
    forged = workspace.host_settle_not_applied_v2(operation.operation_id, "forged")
    authorization = verified_authorization(operation, action="settle_not_applied", passphrase="correct horse battery staple", path=factor)
    assert authorization is not None
    settled = workspace.host_settle_not_applied_v2(operation.operation_id, authorization)

    assert forged["state"] == "rejected"
    assert settled["state"] == "not_applied"
    stored = journal.get_v2_operation(operation.operation_id)
    assert stored is not None and stored.state == "not_applied"
    assert stored.authorization == authorization.record


def test_pending_v2_diverged_touched_evidence_stays_fenced(tmp_path: Path) -> None:
    factor = tmp_path / "owner-factor.json"
    enroll_owner_factor("correct horse battery staple", path=factor)
    journal = MemoryJournal(owner_public_key=factor.with_name("owner-public-key.ed25519").read_bytes())
    operation = _with_manifest(
        _operation("op_diverged", request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735"),
        writes=[{"action": "update", "uuid": "a", "kind": "task", "title": "Desired"}],
        before=[{"id": "task:a", "title": "Before"}],
        touched=[["title"]], preconditions={"task:a": "frozen"}, display_titles=["Before"],
    )
    journal.create_v2(operation, claim_fence=True)
    workspace = ThingsWorkspace(MemoryLibrary([Record(uuid="a", kind="task", title="Applied then overwritten")]), journal=journal, account_id=operation.account_id)
    authorization = verified_authorization(operation, action="settle_not_applied", passphrase="correct horse battery staple", path=factor)
    assert authorization is not None
    result = workspace.host_settle_not_applied_v2(operation.operation_id, authorization)
    assert result["state"] == "pending"
    assert journal.get_v2_operation(operation.operation_id).state == "pending"  # type: ignore[union-attr]


def test_workspace_returns_persisted_winner_when_settlement_cas_loses() -> None:
    class LosingJournal(MemoryJournal):
        def _settle_v2_locked(self, operation_id: str, **kwargs: object) -> bool:
            current = self._v2_operations[operation_id]  # noqa: SLF001
            self._v2_operations[operation_id] = replace(  # noqa: SLF001
                current,
                state="not_applied",
                response={"state": "not_applied", "code": "not_applied_precondition", "next_action": "read_receipt", "instruction": "persisted winner", "operation_id": operation_id},
            )
            return False

    journal = LosingJournal()
    workspace = ThingsWorkspace(MemoryLibrary([Record(uuid="a", kind="task", title="A")]), journal=journal, account_id="owner@example.com")
    from things_orchestrator.v2 import OperationDraft

    result = workspace.execute_v2(OperationDraft.build("things_update", "0198f0ee-98d4-7bd5-91ba-8e76019b2735", {"items": [{"id": "task:a", "set": {"title": "B"}}]}))
    assert result["instruction"] == "persisted winner"


def test_sqlite_retention_replaces_terminal_content_with_permanent_tombstone(tmp_path: Path) -> None:
    journal = SQLiteJournal(tmp_path / "journal.sqlite3")
    operation = _operation(
        "op_retained",
        request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
    )
    journal.create_v2(operation, claim_fence=True)
    journal.settle_v2(
        "op_retained", expected="pending", state="applied",
        response={"state": "applied", "owner_text": "private"},
        rows=[{"sequence": 1, "result": "applied"}],
    )

    assert journal.prune_v2(now="2030-01-01T00:00:00+00:00") == 1
    tombstone = journal.get_v2_request(
        operation.account_id, operation.api_version, operation.request_id
    )
    assert tombstone is not None
    assert tombstone.state == "applied"
    assert tombstone.manifest == {}
    retry = replace(operation, state="pending", response=None)
    assert journal.create_v2(retry, claim_fence=True)[0] == "existing"
    with journal._connect() as connection:  # noqa: SLF001
        stored = connection.execute("SELECT * FROM owner_tombstones_v2").fetchone()
        assert stored is not None
        assert operation.request_id not in str(tuple(stored))
        assert "private" not in str(tuple(stored))
