from __future__ import annotations

import sqlite3
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from things_orchestrator.journal import (
    IntentRecord,
    MemoryJournal,
    SQLiteJournal,
    V2Operation,
)
from things_orchestrator.library import MemoryLibrary, Record
from things_orchestrator.owner_authority import (
    authorization_binding,
    enroll_owner_factor,
    host_escape,
    render_operation,
    verified_authorization,
    verify_owner_factor,
)
from things_orchestrator.workspace import ThingsWorkspace


def _operation(
    operation_id: str,
    *,
    request_id: str,
    state: str = "pending",
) -> V2Operation:
    return V2Operation(
        account_id="owner@example.com",
        api_version="2",
        request_id=request_id,
        request_hash="sha256:v1:" + sha256(request_id.encode()).hexdigest(),
        operation_id=operation_id,
        tool="things_capture",
        state=state,  # type: ignore[arg-type]
        manifest={"writes": []},
        manifest_hash="sha256:v1:manifest",
        safety_policy_digest="sha256:v1:policy",
        expires_at="2026-08-29T13:00:00+00:00" if state == "awaiting_owner" else None,
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


def test_sqlite_unchanged_creation_rolls_back_operation_with_receipts(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    journal = SQLiteJournal(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TRIGGER crash_unchanged_receipt BEFORE INSERT ON owner_receipts_v2
               BEGIN SELECT RAISE(ABORT, 'injected crash'); END"""
        )
    operation = replace(
        _operation("op_unchanged", request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735"),
        state="unchanged",
        response={"state": "unchanged"},
    )
    rows = [{"sequence": 1, "action": "update", "target_id": "task:a", "desired": {}, "observed": {}, "result": "unchanged"}]
    try:
        journal.create_v2(operation, claim_fence=False, receipt_rows=rows)
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("receipt crash was not injected")
    assert journal.get_v2_operation("op_unchanged") is None
    assert journal.get_v2_request(operation.account_id, operation.api_version, operation.request_id) is None


def test_unchanged_creation_is_immediately_terminal_for_retention(tmp_path: Path) -> None:
    journal = SQLiteJournal(tmp_path / "journal.sqlite3")
    operation = replace(
        _operation("op_unchanged_retained", request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735"),
        state="unchanged",
        response={"state": "unchanged"},
    )
    rows = [{"sequence": 1, "action": "update", "target_id": "task:a", "desired": {}, "observed": {}, "result": "unchanged"}]
    assert journal.create_v2(operation, claim_fence=False, receipt_rows=rows)[0] == "created"

    assert journal.prune_v2(now="2030-01-01T00:00:00+00:00") == 1
    tombstone = journal.get_v2_request(operation.account_id, operation.api_version, operation.request_id)
    assert tombstone is not None and tombstone.state == "unchanged"


def test_only_legal_v2_transitions_are_accepted() -> None:
    journal = MemoryJournal()
    operation = _operation(
        "op_pending",
        request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
    )
    journal.create_v2(operation, claim_fence=True)
    assert journal.settle_v2("op_pending", expected="pending", state="applied", response={"state": "applied"}, rows=[])
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
        journal.create_v2(pending, claim_fence=True)
        assert not journal.transition_v2(pending.operation_id, expected="pending", state="applied")
        assert journal.get_v2_operation(pending.operation_id).state == "pending"  # type: ignore[union-attr]


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


def test_receipt_cursor_is_bound_to_account_operation_hash_and_version() -> None:
    journal = MemoryJournal()
    operation = _operation(
        "op_receipt",
        request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
    )
    journal.create_v2(operation, claim_fence=True)
    journal.append_v2_receipts(
        operation.operation_id,
        [
            {"sequence": index, "action": "update", "target_id": f"task:{index}", "desired": {"title": str(index)}, "observed": {"title": str(index)}, "result": "applied"}
            for index in range(1, 4)
        ],
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


def test_host_rendering_escapes_control_ansi_newline_and_delimiter() -> None:
    assert host_escape("\x1b]0;owned\x07\nA|B") == "\\u000aA\\u007cB"
    assert (
        host_escape("safe\u202evil\u2066x\u2069\u200b")
        == "safe\\u202evil\\u2066x\\u2069\\u200b"
    )
    operation = replace(
        _operation(
            "op_render",
            request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
            state="awaiting_owner",
        ),
        manifest={
            "writes": [
                {
                    "action": "trash",
                    "kind": "task",
                    "uuid": "abc",
                    "title": "\x1b[31mApprove\n| now",
                }
            ]
        },
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


def test_host_operation_lookup_is_scoped_to_workspace_account() -> None:
    journal = MemoryJournal()
    owned = _operation(
        "op_owned",
        request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
    )
    foreign = replace(
        _operation(
            "op_foreign",
            request_id="0198f0ef-3923-79b6-96a8-2bf28eac0d67",
        ),
        account_id="other@example.com",
    )
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


def test_pending_v2_can_settle_not_applied_only_with_signed_readback_evidence(tmp_path: Path) -> None:
    factor = tmp_path / "owner-factor.json"
    enroll_owner_factor("correct horse battery staple", path=factor)
    journal = MemoryJournal(owner_public_key=factor.with_name("owner-public-key.ed25519").read_bytes())
    operation = replace(
        _operation("op_recover", request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735"),
        manifest={
            "writes": [{"action": "update", "uuid": "a", "kind": "task", "title": "New"}],
            "before": [{"id": "task:a", "title": "Old"}],
            "touched": [["title"]],
            "preconditions": {"task:a": "frozen"},
            "display_titles": ["Old"],
        },
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
    operation = replace(
        _operation("op_diverged", request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735"),
        manifest={
            "writes": [{"action": "update", "uuid": "a", "kind": "task", "title": "Desired"}],
            "before": [{"id": "task:a", "title": "Before"}],
            "touched": [["title"]], "preconditions": {"task:a": "frozen"}, "display_titles": ["Before"],
        },
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
        def settle_v2(self, operation_id: str, **kwargs: object) -> bool:
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
    journal.settle_v2("op_retained", expected="pending", state="applied", response={"state": "applied", "owner_text": "private"}, rows=[])

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
