from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from things_orchestrator.journal import (
    IntentRecord,
    MemoryJournal,
    SQLiteJournal,
    V2Operation,
)
from things_orchestrator.library import MemoryLibrary
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


def test_only_legal_v2_transitions_are_accepted() -> None:
    journal = MemoryJournal()
    operation = _operation(
        "op_pending",
        request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
    )
    journal.create_v2(operation, claim_fence=True)
    assert journal.transition_v2("op_pending", expected="pending", state="applied")
    assert not journal.transition_v2("op_pending", expected="applied", state="pending")


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


def test_sqlite_retention_replaces_terminal_content_with_permanent_tombstone(tmp_path: Path) -> None:
    journal = SQLiteJournal(tmp_path / "journal.sqlite3")
    operation = _operation(
        "op_retained",
        request_id="0198f0ee-98d4-7bd5-91ba-8e76019b2735",
    )
    journal.create_v2(operation, claim_fence=True)
    journal.transition_v2("op_retained", expected="pending", state="applied", response={"state": "applied", "owner_text": "private"})

    assert journal.prune_v2(now="2030-01-01T00:00:00+00:00") == 1
    tombstone = journal.get_v2_request(
        operation.account_id, operation.api_version, operation.request_id
    )
    assert tombstone is not None
    assert tombstone.state == "applied"
    assert tombstone.manifest == {}
    assert journal.create_v2(operation, claim_fence=True)[0] == "existing"
    with journal._connect() as connection:  # noqa: SLF001
        stored = connection.execute("SELECT * FROM owner_tombstones_v2").fetchone()
        assert stored is not None
        assert operation.request_id not in str(tuple(stored))
        assert "private" not in str(tuple(stored))
